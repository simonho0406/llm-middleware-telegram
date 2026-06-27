"""
Tests for the LLM-friendly conversation_history view and the system-prompt
cheat-sheet that teaches the model how to query it.

The model used to guess a table named `conversation_history` and fail with
"no such table". We now (a) provide exactly that view, flattened and chat-scoped,
and (b) inject its shape + the chat's own id into the system prompt.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import pytest_asyncio
import aiosqlite

import config
from storage import database_storage


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(tmp_path):
    db_path = tmp_path / "test_history_view.db"
    original = config.DB_PATH
    config.DB_PATH = str(db_path)
    await database_storage.init_database()
    yield
    config.DB_PATH = original
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.mark.asyncio
async def test_conversation_history_view_is_chat_scoped_and_flat():
    # Two different chats so we can prove scoping works.
    await database_storage.save_message(111, 'user', 'hello from chat A')
    await database_storage.save_message(111, 'assistant', 'hi A')
    await database_storage.save_message(222, 'user', 'hello from chat B')

    async with aiosqlite.connect(config.DB_PATH) as db:
        # The view exists with the expected flat shape (what the model is told).
        async with db.execute("PRAGMA table_info(conversation_history)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        assert {'id', 'chat_id', 'thread_id', 'thread_name', 'role', 'content', 'timestamp'} <= cols

        # Scoping by chat_id returns only that chat's rows.
        async with db.execute(
            "SELECT role, content FROM conversation_history WHERE chat_id = ? ORDER BY timestamp, id",
            (111,),
        ) as cur:
            rows = await cur.fetchall()
        assert rows == [('user', 'hello from chat A'), ('assistant', 'hi A')]

        # The other chat's data is not leaked into chat 111's scope.
        async with db.execute(
            "SELECT COUNT(*) FROM conversation_history WHERE chat_id = ?", (222,)
        ) as cur:
            assert (await cur.fetchone())[0] == 1


def test_catalog_injects_history_cheatsheet_when_sqlite_present():
    from bot.response_generator import _build_tool_catalog_section

    mcp_tools = [
        {"function": {"name": "sqlite-tools__read_query", "description": "run a read query"}},
        {"function": {"name": "tavily-search__tavily_search", "description": "web search"}},
    ]
    sample_chat_id = 123456789  # arbitrary non-personal sample id
    section = _build_tool_catalog_section(mcp_tools, [], chat_id=sample_chat_id, thread_id="default")

    assert "conversation_history" in section
    assert str(sample_chat_id) in section    # the chat's own id is injected
    assert "default" in section              # the current thread id is injected
    assert "thread_id = 'default'" in section  # current-thread scope is the default example
    assert "read_query" in section


def test_catalog_omits_cheatsheet_without_sqlite():
    from bot.response_generator import _build_tool_catalog_section

    mcp_tools = [
        {"function": {"name": "tavily-search__tavily_search", "description": "web search"}},
    ]
    section = _build_tool_catalog_section(mcp_tools, [], chat_id=123)
    assert "conversation_history" not in section
