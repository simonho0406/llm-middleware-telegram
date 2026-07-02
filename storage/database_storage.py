import aiosqlite
import asyncio
import os
import logging
from typing import List, Dict, Any, Optional
import time
import config

logger = logging.getLogger(__name__)
_DEFAULT_THREAD_ID = "default"

# --- Write-durability tuning (message persistence is integrity-critical) ---
# Under WAL, concurrent writers are serialized; a burst of turns can otherwise fail with
# "database is locked". busy_timeout makes a writer WAIT for the lock; the retry covers the
# rare case where it still times out. See save_message().
_DB_TIMEOUT_SECONDS = 15          # busy-wait: a writer WAITS up to this for the WAL lock
_SAVE_RETRY_ATTEMPTS = 4
_SAVE_RETRY_BACKOFF_SECONDS = 0.2


def _connect():
    """Open a DB connection with a generous busy-timeout so a writer WAITS for the WAL
    write-lock instead of failing immediately with 'database is locked' under concurrency.

    Use this EVERYWHERE instead of a bare aiosqlite.connect(config.DB_PATH): the default
    5s busy-wait is easy to exceed during a burst of concurrent turns, and a failed write
    that a caller doesn't surface is a SILENT data loss the user can't observe. (aiosqlite's
    `timeout` maps to SQLite's busy_timeout.)
    """
    return aiosqlite.connect(config.DB_PATH, timeout=_DB_TIMEOUT_SECONDS)

# --- Internal Helper Functions (still require a passed connection) ---

async def _get_or_create_chat(conn: aiosqlite.Connection, chat_id: int) -> None:
    """Ensures a chat and its default thread exist, using a provided connection."""
    async with conn.cursor() as cursor:
        await cursor.execute("SELECT chat_id FROM chats WHERE chat_id = ?", (chat_id,))
        if await cursor.fetchone() is None:
            await conn.execute("BEGIN")
            try:
                await cursor.execute("INSERT INTO chats (chat_id, current_thread_id) VALUES (?, ?)", (chat_id, _DEFAULT_THREAD_ID))
                await cursor.execute("INSERT INTO threads (chat_id, thread_id) VALUES (?, ?)", (chat_id, _DEFAULT_THREAD_ID))
                await conn.commit()
                logger.info(f"Created new chat record and default thread for chat_id: {chat_id}")
            except Exception as e:
                await conn.rollback()
                raise e

async def _get_thread_pk(conn: aiosqlite.Connection, chat_id: int, thread_id: Optional[str] = None) -> Optional[int]:
    """Gets the primary key (thread_pk) of a specific thread, or the current thread if thread_id is None."""
    await _get_or_create_chat(conn, chat_id)
    async with conn.cursor() as cursor:
        if thread_id:
            await cursor.execute("SELECT thread_pk FROM threads WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id))
        else: # Get current thread's pk
            await cursor.execute("SELECT T.thread_pk FROM threads T JOIN chats C ON T.chat_id = C.chat_id WHERE T.chat_id = ? AND T.thread_id = C.current_thread_id", (chat_id,))
        row = await cursor.fetchone()
        return row[0] if row else None

# --- Public Interface ---

def _assert_data_dir_writable(data_dir: str) -> None:
    """Fail loud and fast at startup if the data directory isn't writable.

    Without this, a permission problem (e.g. the container's non-root user not owning a
    host bind-mounted ./data) doesn't surface until the first DB access — which, because
    the DB runs in WAL mode, can be an innocuous-looking read like `/threads`, producing a
    confusing "attempt to write a readonly database" deep in a handler traceback instead of
    a clear, actionable error at boot.
    """
    probe_path = os.path.join(data_dir, ".write_probe")
    try:
        with open(probe_path, "w") as f:
            f.write("ok")
        os.remove(probe_path)
    except OSError as e:
        raise RuntimeError(
            f"Data directory '{data_dir}' is not writable ({e}). The SQLite database "
            "(WAL mode) requires write access even for reads. If running in Docker with "
            "the non-root 'appuser', fix the host directory ownership with: "
            "`sudo chown -R 10001:10001 ./data`"
        ) from e


async def init_database():
    """Initializes the database and creates tables, managing its own connection."""
    data_dir = os.path.dirname(config.DB_PATH)
    os.makedirs(data_dir, exist_ok=True)
    _assert_data_dir_writable(data_dir)
    async with _connect() as db:
        try:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA foreign_keys = ON;")
        except aiosqlite.OperationalError as e:
            if "readonly" in str(e).lower():
                raise RuntimeError(
                    f"Cannot initialize the database at '{config.DB_PATH}': {e}. "
                    "The data directory passed the writability probe but SQLite itself "
                    "reports readonly — check the database file's own permissions/ownership."
                ) from e
            raise
        await db.execute("CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, current_thread_id TEXT NOT NULL)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                thread_pk INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, thread_id TEXT NOT NULL,
                name TEXT, provider TEXT, model TEXT, last_user_prompt TEXT,
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE, UNIQUE (chat_id, thread_id)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                message_pk INTEGER PRIMARY KEY AUTOINCREMENT, thread_fk INTEGER NOT NULL, role TEXT NOT NULL,
                content TEXT, timestamp INTEGER NOT NULL,
                tool_calls TEXT, tool_call_id TEXT,
                FOREIGN KEY (thread_fk) REFERENCES threads(thread_pk) ON DELETE CASCADE
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS panel_tasks (
                task_pk INTEGER PRIMARY KEY AUTOINCREMENT, thread_fk INTEGER NOT NULL,
                role TEXT NOT NULL, plan_json TEXT NOT NULL, status TEXT NOT NULL, timestamp INTEGER NOT NULL,
                FOREIGN KEY (thread_fk) REFERENCES threads(thread_pk) ON DELETE CASCADE
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                chat_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value INTEGER NOT NULL,  -- Booleans stored as 0 or 1
                PRIMARY KEY (chat_id, key),
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
            )""")
        
        # Check and migrate existing user_settings table if it has wrong data type
        await _migrate_user_settings_table(db)

        # Check and migrate existing messages table for tool calling columns
        await _migrate_messages_table(db)

        # LLM-friendly history view. The model (via the read-only sqlite MCP) can't
        # easily join `messages` (keyed by internal thread_fk) to `threads` to scope
        # by chat, and it tends to guess a table named `conversation_history`. This
        # view meets that expectation: a flat, self-describing shape with chat_id,
        # thread name, role, content and timestamp — so a single
        #   SELECT ... FROM conversation_history WHERE chat_id = ? ORDER BY timestamp
        # answers "what's in my history". Created on the read-write connection so the
        # read-only MCP connection can SELECT from it. Use CREATE VIEW IF NOT EXISTS
        # so column/shape changes require a drop; keep it in sync with the schema.
        await db.execute("DROP VIEW IF EXISTS conversation_history")
        await db.execute("""
            CREATE VIEW conversation_history AS
            SELECT
                m.message_pk  AS id,
                t.chat_id     AS chat_id,
                t.thread_id   AS thread_id,
                t.name        AS thread_name,
                m.role        AS role,
                m.content     AS content,
                m.timestamp   AS timestamp
            FROM messages m
            JOIN threads  t ON m.thread_fk = t.thread_pk
        """)

        await db.commit()
        logger.info("Database initialized successfully.")

async def _migrate_messages_table(db: aiosqlite.Connection):
    """
    Ensures the messages table has the correct schema:
      - content column is nullable (tool-calling assistant turns have content=None per OpenAI spec)
      - tool_calls and tool_call_id columns exist
    SQLite does not support ALTER COLUMN, so a table-rebuild is used when needed.
    """
    try:
        async with db.cursor() as cursor:
            await cursor.execute("PRAGMA table_info(messages)")
            cols_info = await cursor.fetchall()

        existing_col_names = {col[1] for col in cols_info}
        content_col = next((c for c in cols_info if c[1] == 'content'), None)
        # PRAGMA table_info: col[3] is the notnull flag (1 = NOT NULL constraint active)
        content_is_not_null = bool(content_col and content_col[3] == 1)

        if content_is_not_null:
            # Must rebuild the table to drop the NOT NULL constraint on content.
            logger.info("Migrating messages table: removing NOT NULL constraint from 'content' column (table rebuild)...")

            # Only copy columns that actually exist in the source table
            src_cols_list = ["message_pk", "thread_fk", "role", "content", "timestamp"]
            if 'tool_calls' in existing_col_names:
                src_cols_list.append("tool_calls")
            if 'tool_call_id' in existing_col_names:
                src_cols_list.append("tool_call_id")
            src_cols = ", ".join(src_cols_list)

            # Drop any leftover table from a previous failed migration attempt so
            # CREATE TABLE messages_new doesn't immediately raise "table already exists".
            await db.execute("DROP TABLE IF EXISTS messages_new")

            # Disable FK enforcement for the duration of the copy.  Orphaned rows
            # (thread_fk pointing to a deleted thread) would otherwise block the INSERT.
            # PRAGMA foreign_keys must be changed outside an active transaction; all
            # preceding DDL statements auto-committed any pending tx, so this is safe.
            await db.execute("PRAGMA foreign_keys = OFF")
            try:
                # BEGIN EXCLUSIVE serializes against any concurrent writer
                # (e.g. an in-flight save_message during a polling-loop
                # restart). Without it, the INSERT...SELECT and the
                # DROP TABLE can interleave with a writer's INSERT, losing
                # the writer's row when the original table is dropped.
                await db.execute("BEGIN EXCLUSIVE")
                try:
                    await db.execute("""
                        CREATE TABLE messages_new (
                            message_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                            thread_fk INTEGER NOT NULL,
                            role TEXT NOT NULL,
                            content TEXT,
                            timestamp INTEGER NOT NULL,
                            tool_calls TEXT,
                            tool_call_id TEXT,
                            FOREIGN KEY (thread_fk) REFERENCES threads(thread_pk) ON DELETE CASCADE
                        )
                    """)
                    await db.execute(f"INSERT INTO messages_new ({src_cols}) SELECT {src_cols} FROM messages")
                    await db.execute("DROP TABLE messages")
                    await db.execute("ALTER TABLE messages_new RENAME TO messages")
                    await db.execute("COMMIT")
                except Exception:
                    await db.execute("ROLLBACK")
                    raise
                logger.info("Messages table rebuilt: 'content' is now nullable, tool calling columns present.")
            finally:
                await db.execute("PRAGMA foreign_keys = ON")
            # The rebuild already includes tool_calls/tool_call_id — nothing more to do.
            return

        # Table already has nullable content; just ensure the tool-call columns exist.
        if 'tool_calls' not in existing_col_names:
            logger.info("Migrating messages table: adding 'tool_calls' column.")
            await db.execute("ALTER TABLE messages ADD COLUMN tool_calls TEXT")

        if 'tool_call_id' not in existing_col_names:
            logger.info("Migrating messages table: adding 'tool_call_id' column.")
            await db.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")

    except Exception as e:
        logger.exception(f"Failed to migrate messages table: {e}")

async def _migrate_user_settings_table(db: aiosqlite.Connection):
    """Migrates user_settings table from TEXT to INTEGER values if needed."""
    try:
        # Check current schema
        async with db.cursor() as cursor:
            await cursor.execute("PRAGMA table_info(user_settings)")
            columns = await cursor.fetchall()
            
            # Find the value column and check its type
            value_column = None
            for col in columns:
                if col[1] == 'value':  # col[1] is column name
                    value_column = col[2]  # col[2] is data type
                    break
            
            if value_column and value_column.upper() == 'TEXT':
                logger.info("Migrating user_settings table from TEXT to INTEGER values...")

                # Read existing data first (outside the exclusive tx so we can
                # build the row list with Python-side conversion).
                await cursor.execute("SELECT chat_id, key, value FROM user_settings")
                existing_data = await cursor.fetchall()

                # Wrap the destructive rebuild in BEGIN EXCLUSIVE to serialize
                # against any concurrent writer (peer to the messages table
                # migration — see ticket 027).
                await db.execute("BEGIN EXCLUSIVE")
                try:
                    await db.execute("DROP TABLE user_settings")
                    await db.execute("""
                        CREATE TABLE user_settings (
                            chat_id INTEGER NOT NULL,
                            key TEXT NOT NULL,
                            value INTEGER NOT NULL,
                            PRIMARY KEY (chat_id, key),
                            FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                        )""")
                    for chat_id, key, old_value in existing_data:
                        if isinstance(old_value, str):
                            new_value = 1 if old_value.lower() in ('true', '1', 'yes', 'on') else 0
                        else:
                            new_value = int(bool(old_value))
                        await db.execute(
                            "INSERT INTO user_settings (chat_id, key, value) VALUES (?, ?, ?)",
                            (chat_id, key, new_value)
                        )
                    await db.execute("COMMIT")
                except Exception:
                    await db.execute("ROLLBACK")
                    raise

                logger.info(f"Successfully migrated {len(existing_data)} user settings records.")
                
    except Exception as e:
        logger.exception(f"Failed to migrate user_settings table: {e}")

async def get_current_thread_id(chat_id: int) -> str:
    async with _connect() as db:
        await _get_or_create_chat(db, chat_id)
        async with db.cursor() as cursor:
            await cursor.execute("SELECT current_thread_id FROM chats WHERE chat_id = ?", (chat_id,))
            row = await cursor.fetchone()
            return row[0] if row else _DEFAULT_THREAD_ID

async def save_panel_task(chat_id: int, role: str, plan_json: str, status: str = 'pending', thread_id: Optional[str] = None) -> int:
    """Saves a panel task to the state tracker."""
    async with _connect() as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return -1
        timestamp = int(time.time())
        async with db.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO panel_tasks (thread_fk, role, plan_json, status, timestamp) VALUES (?, ?, ?, ?, ?)",
                (thread_pk, role, plan_json, status, timestamp)
            )
            await db.commit()
            return cursor.lastrowid

async def update_panel_task_status(task_pk: int, status: str) -> None:
    async with _connect() as db:
        await db.execute("UPDATE panel_tasks SET status = ? WHERE task_pk = ?", (status, task_pk))
        await db.commit()

async def get_panel_tasks(chat_id: int, thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves all tasks for the current panel session."""
    async with _connect() as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return []
        async with db.cursor() as cursor:
            await cursor.execute(
                "SELECT task_pk, role, plan_json, status FROM panel_tasks WHERE thread_fk = ? ORDER BY timestamp ASC",
                (thread_pk,)
            )
            rows = await cursor.fetchall()
            return [{'task_pk': row[0], 'role': row[1], 'plan_json': row[2], 'status': row[3]} for row in rows]

async def clear_panel_tasks(chat_id: int, thread_id: Optional[str] = None) -> None:
    async with _connect() as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return
        await db.execute("DELETE FROM panel_tasks WHERE thread_fk = ?", (thread_pk,))
        await db.commit()

async def set_current_thread_id(chat_id: int, thread_id: str) -> None:
    async with _connect() as db:
        await _get_or_create_chat(db, chat_id)
        await db.execute("UPDATE chats SET current_thread_id = ? WHERE chat_id = ?", (thread_id, chat_id))
        await db.commit()

async def get_thread_key(chat_id: int, key: str, default: Any = None, thread_id: Optional[str] = None) -> Any:
    valid_keys = {"name", "provider", "model", "last_user_prompt"}
    if key not in valid_keys:
        # This is the new, critical check
        if key == 'history':
            # Redirect to the correct function instead of failing
            return await get_thread_history(chat_id, thread_id)
        raise ValueError(f"Invalid key '{key}' for get_thread_key. Must be one of {valid_keys}")
    
    async with _connect() as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return default
        async with db.cursor() as cursor:
            await cursor.execute(f"SELECT {key} FROM threads WHERE thread_pk = ?", (thread_pk,))
            row = await cursor.fetchone()
            return row[0] if row and row[0] is not None else default

async def set_thread_key(chat_id: int, key: str, value: Any, thread_id: Optional[str] = None) -> None:
    valid_keys = {"name", "provider", "model", "last_user_prompt"}
    if key not in valid_keys:
        raise ValueError(f"Invalid key '{key}' for set_thread_key. Must be one of {valid_keys}")

    async with _connect() as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return
        await db.execute(f"UPDATE threads SET {key} = ? WHERE thread_pk = ?", (value, thread_pk))
        await db.commit()

async def get_thread_history(chat_id: int, thread_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    import json
    if limit is None:
        limit = config.get_thread_history_fetch_limit()
    async with _connect() as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return []
        async with db.cursor() as cursor:
            # Fetch last N messages relative to timestamp
            # We use a subquery to get the latest N, then order them ASC for the LLM
            await cursor.execute(
                f"SELECT role, content, tool_calls, tool_call_id FROM (SELECT role, content, tool_calls, tool_call_id, message_pk FROM messages WHERE thread_fk = ? ORDER BY message_pk DESC LIMIT ?) ORDER BY message_pk ASC", 
                (thread_pk, limit)
            )
            rows = await cursor.fetchall()
            
            history = []
            for row in rows:
                role, content, tool_calls_str, tool_call_id = row
                msg = {"role": role, "content": content}
                if tool_calls_str:
                    try:
                        msg["tool_calls"] = json.loads(tool_calls_str)
                    except Exception:
                        msg["tool_calls"] = None
                if tool_call_id is not None:
                    msg["tool_call_id"] = tool_call_id
                history.append(msg)
            return history

async def get_thread_history_with_pk(chat_id: int, thread_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetches history including message_pk for granular management."""
    import json
    if limit is None:
        limit = config.get_thread_history_fetch_limit()
    async with _connect() as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return []
        async with db.cursor() as cursor:
            # Similar to get_thread_history but includes message_pk and timestamp
            await cursor.execute(
                f"SELECT message_pk, role, content, timestamp, tool_calls, tool_call_id FROM (SELECT message_pk, role, content, timestamp, tool_calls, tool_call_id FROM messages WHERE thread_fk = ? ORDER BY message_pk DESC LIMIT ?) ORDER BY message_pk ASC", 
                (thread_pk, limit)
            )
            rows = await cursor.fetchall()
            history = []
            for row in rows:
                msg_pk, role, content, ts, tool_calls_str, tool_call_id = row
                msg = {"id": msg_pk, "role": role, "content": content, "timestamp": ts}
                if tool_calls_str:
                    try:
                        msg["tool_calls"] = json.loads(tool_calls_str)
                    except Exception:
                        msg["tool_calls"] = None
                if tool_call_id is not None:
                    msg["tool_call_id"] = tool_call_id
                history.append(msg)
            return history

async def delete_messages(chat_id: int, message_ids: List[int]) -> bool:
    """Deletes specific messages by their PKs."""
    if not message_ids:
        return False
    async with _connect() as db:
        # Verify ownership (optional but good practice)? For now just delete by PK.
        # Actually, PK is global unique usually, but let's ensure they belong to the chat?
        # That's expensive. Simpler to just delete by PKs.
        placeholders = ','.join('?' for _ in message_ids)
        cursor = await db.execute(f"DELETE FROM messages WHERE message_pk IN ({placeholders})", tuple(message_ids))
        await db.commit()
        logger.info(f"Deleted {cursor.rowcount} messages for chat {chat_id}")
        return cursor.rowcount > 0

async def delete_messages_after(chat_id: int, target_pk: int, thread_id: Optional[str] = None) -> int:
    """Atomically deletes all messages in a thread that occur strictly after a specific message_pk."""
    async with _connect() as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return 0
        
        cursor = await db.execute(
            "DELETE FROM messages WHERE thread_fk = ? AND message_pk > ?", 
            (thread_pk, target_pk)
        )
        await db.commit()
        logger.info(f"Deleted {cursor.rowcount} messages after PK {target_pk} for chat {chat_id}")
        return cursor.rowcount

async def update_message_content(message_pk: int, new_content: str) -> bool:
    """Atomically updates the content of a specific message by its PK."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE messages SET content = ? WHERE message_pk = ?", 
            (new_content, message_pk)
        )
        await db.commit()
        logger.info(f"Updated content for message PK {message_pk}")
        return cursor.rowcount > 0


async def remove_last_assistant_message(chat_id: int, thread_id: Optional[str] = None) -> bool:
    """Removes the last assistant message and any tool-result rows it owns."""
    import json as _json
    async with _connect() as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return False

        async with db.cursor() as cursor:
            await cursor.execute(
                "SELECT message_pk, tool_calls FROM messages WHERE thread_fk = ? AND role = 'assistant' ORDER BY message_pk DESC LIMIT 1",
                (thread_pk,)
            )
            row = await cursor.fetchone()
            if not row:
                return False

            message_pk, tool_calls_json = row[0], row[1]

            # Delete any tool-result rows whose tool_call_id belongs to this assistant turn.
            # Without this, /reroll leaves orphaned role=tool rows that corrupt future history.
            if tool_calls_json:
                try:
                    tc_ids = [tc["id"] for tc in _json.loads(tool_calls_json) if tc.get("id")]
                    if tc_ids:
                        placeholders = ",".join("?" * len(tc_ids))
                        await db.execute(
                            f"DELETE FROM messages WHERE thread_fk = ? AND role = 'tool' AND tool_call_id IN ({placeholders})",
                            (thread_pk, *tc_ids)
                        )
                except Exception:
                    pass  # malformed JSON is non-fatal; proceed to delete the assistant row

            await db.execute("DELETE FROM messages WHERE message_pk = ?", (message_pk,))
            await db.commit()
            logger.info(f"Removed last assistant message (pk {message_pk}) for reroll in chat {chat_id}")
            return True

def _is_locked_error(e: Exception) -> bool:
    """True for a transient SQLite write-contention error (WAL serializes writers)."""
    return isinstance(e, aiosqlite.OperationalError) and (
        "locked" in str(e).lower() or "busy" in str(e).lower()
    )


async def save_message(chat_id: int, role: str, content: Optional[str], thread_id: Optional[str] = None, tool_calls: Optional[List[Dict[str, Any]]] = None, tool_call_id: Optional[str] = None) -> Optional[int]:
    """Saves (Appends) a single message to the history and returns its message_pk.

    Message persistence is integrity-critical: a lost save means the turn silently drops
    out of history (the user saw it, but it won't be in context next turn). Under WAL,
    concurrent writers are serialized, so a burst of turns can hit "database is locked".
    We therefore (a) raise the busy_timeout so a writer WAITS for the lock instead of
    failing fast, and (b) retry the whole transaction a few times on a transient lock.
    Non-transient errors (and exhausted retries) PROPAGATE so the caller surfaces them to
    the user rather than dropping the turn silently.
    """
    import json
    for attempt in range(1, _SAVE_RETRY_ATTEMPTS + 1):
        try:
            async with _connect() as db:
                thread_pk = await _get_thread_pk(db, chat_id, thread_id)
                if not thread_pk:
                    logger.error(f"Attempted to save message to non-existent thread for chat_id {chat_id}")
                    return None

                timestamp = int(time.time())
                tool_calls_str = json.dumps(tool_calls) if tool_calls is not None else None

                async with db.cursor() as cursor:
                    await cursor.execute(
                        "INSERT INTO messages (thread_fk, role, content, timestamp, tool_calls, tool_call_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (thread_pk, role, content, timestamp, tool_calls_str, tool_call_id)
                    )
                    message_pk = cursor.lastrowid

                await db.commit()
                logger.info(f"Saved single message with role '{role}' to thread_pk {thread_pk} for chat {chat_id} (PK: {message_pk})")
                return message_pk
        except Exception as e:
            if _is_locked_error(e) and attempt < _SAVE_RETRY_ATTEMPTS:
                logger.warning(f"save_message: DB locked (attempt {attempt}/{_SAVE_RETRY_ATTEMPTS}) for chat {chat_id}; retrying. {e}")
                await asyncio.sleep(_SAVE_RETRY_BACKOFF_SECONDS * attempt)
                continue
            # Non-transient, or retries exhausted: do NOT swallow — propagate so the caller
            # can tell the user their turn wasn't saved (integrity over silent success).
            logger.error(f"save_message FAILED for chat {chat_id} (role '{role}') after {attempt} attempt(s): {e}")
            raise

async def create_thread(chat_id: int, thread_id: str) -> bool:
    async with _connect() as db:
        try:
            await _get_or_create_chat(db, chat_id)
            await db.execute("INSERT INTO threads (chat_id, thread_id) VALUES (?, ?)", (chat_id, thread_id))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            logger.warning(f"Attempted to create existing thread '{thread_id}' for chat {chat_id}")
            return False

async def delete_thread(chat_id: int, thread_id: str) -> bool:
    async with _connect() as db:
        if thread_id == _DEFAULT_THREAD_ID:
            return False
        current_id = await get_current_thread_id(chat_id)
        if current_id == thread_id:
            await set_current_thread_id(chat_id, _DEFAULT_THREAD_ID)
        cursor = await db.execute("DELETE FROM threads WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id))
        await db.commit()
        return cursor.rowcount > 0

async def list_threads(chat_id: int) -> List[Dict[str, Any]]:
    async with _connect() as db:
        await _get_or_create_chat(db, chat_id)
        async with db.cursor() as cursor:
            await cursor.execute("SELECT thread_id, name FROM threads WHERE chat_id = ?", (chat_id,))
            rows = await cursor.fetchall()
            return [{"id": row[0], "name": row[1]} for row in rows]

async def rename_thread(chat_id: int, new_name: str) -> bool:
    """Rename the current thread. Returns True on success, False on failure.

    The previous implementation just `return await set_thread_key(...)`, but
    set_thread_key returns None on success — so the caller's `if success`
    check always took the failure branch and showed "An error occurred"
    even though the UPDATE went through. Mirror the file_storage pattern:
    verify the thread exists, do the update, return a real bool.
    """
    try:
        async with _connect() as db:
            thread_pk = await _get_thread_pk(db, chat_id, thread_id=None)
            if not thread_pk:
                logger.warning(f"rename_thread: no current thread for chat {chat_id}")
                return False
            await db.execute("UPDATE threads SET name = ? WHERE thread_pk = ?", (new_name, thread_pk))
            await db.commit()
            return True
    except Exception as e:
        logger.exception(f"Failed to rename thread for chat {chat_id}: {e}")
        return False

async def get_all_chat_ids() -> List[int]:
    """Returns a list of all chat IDs in the database."""
    async with _connect() as db:
        async with db.cursor() as cursor:
            await cursor.execute("SELECT chat_id FROM chats")
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def get_user_setting(chat_id: int, key: str, default: Any = None) -> Any:
    """Retrieves a setting value with proper boolean conversion"""
    from bot.settings import USER_SETTINGS
    async with _connect() as db:
        await _get_or_create_chat(db, chat_id)
        async with db.cursor() as cursor:
            await cursor.execute("SELECT value FROM user_settings WHERE chat_id = ? AND key = ?", (chat_id, key))
            row = await cursor.fetchone()
            if row:
                value = row[0]
                # Convert integer to boolean for bool settings
                if key in USER_SETTINGS and USER_SETTINGS[key]['type'] == bool:
                    return bool(value)
                return value
            return default

async def set_user_setting(chat_id: int, key: str, value: Any) -> None:
    """Stores setting value with proper boolean conversion. If value is None, deletes the setting."""
    from bot.settings import USER_SETTINGS
    
    async with _connect() as db:
        await _get_or_create_chat(db, chat_id)
        
        if value is None:
            # Delete the setting entirely
            await db.execute("DELETE FROM user_settings WHERE chat_id = ? AND key = ?", (chat_id, key))
        else:
            # Convert Python bool to SQLite integer
            if key in USER_SETTINGS and USER_SETTINGS[key]['type'] == bool:
                value = 1 if value else 0
            await db.execute("INSERT OR REPLACE INTO user_settings (chat_id, key, value) VALUES (?, ?, ?)", (chat_id, key, value))
        
        await db.commit()