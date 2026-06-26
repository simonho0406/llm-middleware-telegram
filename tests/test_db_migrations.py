"""
Migration tests for storage.database_storage.

init_database() uses CREATE TABLE IF NOT EXISTS, so an existing OLD-schema table
survives and is then upgraded in place by _migrate_messages_table /
_migrate_user_settings_table. We seed the old shapes, run init_database(), and assert:
  * messages.content becomes nullable; tool_calls / tool_call_id columns are added;
    pre-existing rows are preserved.
  * user_settings.value is converted TEXT -> INTEGER, with 'true'/'1'/'yes'/'on' -> 1.
  * a second init_database() run is idempotent.
"""
import os
import sys
import sqlite3

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest

from storage import database_storage


def _seed_old_schema(db_path):
    """Create a pre-migration DB: messages with NOT NULL content and no tool columns,
    user_settings with TEXT values."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, current_thread_id TEXT NOT NULL)")
    cur.execute("""
        CREATE TABLE threads (
            thread_pk INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, thread_id TEXT NOT NULL,
            name TEXT, provider TEXT, model TEXT, last_user_prompt TEXT,
            UNIQUE (chat_id, thread_id)
        )""")
    # OLD messages schema: content NOT NULL, no tool_calls / tool_call_id.
    cur.execute("""
        CREATE TABLE messages (
            message_pk INTEGER PRIMARY KEY AUTOINCREMENT, thread_fk INTEGER NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, timestamp INTEGER NOT NULL
        )""")
    # OLD user_settings schema: value is TEXT.
    cur.execute("""
        CREATE TABLE user_settings (
            chat_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY (chat_id, key)
        )""")

    cur.execute("INSERT INTO chats VALUES (1, 'default')")
    cur.execute("INSERT INTO threads (chat_id, thread_id, name) VALUES (1, 'default', 'Default')")
    cur.execute("INSERT INTO messages (thread_fk, role, content, timestamp) VALUES (1, 'user', 'hello world', 100)")
    cur.execute("INSERT INTO user_settings (chat_id, key, value) VALUES (1, 'enable_mcp', 'true')")
    cur.execute("INSERT INTO user_settings (chat_id, key, value) VALUES (1, 'enable_streaming', 'false')")
    conn.commit()
    conn.close()


def _table_info(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    # name -> (type, notnull)
    return {r[1]: (r[2], r[3]) for r in rows}


@pytest.mark.asyncio
async def test_messages_table_migrated_and_data_preserved(isolated_db_path):
    db_path = isolated_db_path
    _seed_old_schema(db_path)

    await database_storage.init_database()  # config.DB_PATH set by the fixture

    cols = _table_info(db_path, "messages")
    # content is now nullable (notnull flag cleared) and tool columns exist.
    assert cols["content"][1] == 0, "content NOT NULL constraint should be dropped"
    assert "tool_calls" in cols and "tool_call_id" in cols

    # The seeded row survived the rebuild.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT role, content FROM messages WHERE message_pk = 1").fetchone()
        # A NULL-content row is now insertable (proves the constraint is gone).
        conn.execute("INSERT INTO messages (thread_fk, role, content, timestamp) VALUES (1, 'assistant', NULL, 101)")
        conn.commit()
    finally:
        conn.close()
    assert row == ("user", "hello world")


@pytest.mark.asyncio
async def test_user_settings_text_to_integer_conversion(isolated_db_path):
    db_path = isolated_db_path
    _seed_old_schema(db_path)

    await database_storage.init_database()  # config.DB_PATH set by the fixture

    cols = _table_info(db_path, "user_settings")
    assert cols["value"][0].upper() == "INTEGER", "value column should be migrated to INTEGER"

    conn = sqlite3.connect(db_path)
    try:
        values = dict(conn.execute("SELECT key, value FROM user_settings WHERE chat_id = 1").fetchall())
    finally:
        conn.close()
    assert values["enable_mcp"] == 1     # 'true' -> 1
    assert values["enable_streaming"] == 0  # 'false' -> 0


@pytest.mark.asyncio
async def test_init_database_is_idempotent(isolated_db_path):
    db_path = isolated_db_path
    _seed_old_schema(db_path)

    await database_storage.init_database()  # config.DB_PATH set by the fixture
    # Second run must not raise and must leave data intact.
    await database_storage.init_database()

    conn = sqlite3.connect(db_path)
    try:
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        setting = conn.execute(
            "SELECT value FROM user_settings WHERE chat_id = 1 AND key = 'enable_mcp'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert msg_count == 1
    assert setting == 1
