"""
Thread / history isolation — unit-speed coverage of an invariant that is otherwise
only exercised by the live e2e_qa.py run.

History is scoped by (chat_id, thread_id). A read for one chat/thread must never
return another chat's or another thread's messages. We use a real temp SQLite DB
(same fixture pattern as tests/test_storage_exceptions.py) rather than mocks, so the
actual SQL scoping is what's under test.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import pytest_asyncio

from storage import database_storage


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(isolated_db_path):
    # isolated_db_path (conftest) sets config.DB_PATH to a temp file and restores it
    # in a finally block, so a failure here never leaks the path to other tests.
    await database_storage.init_database()
    yield


def _contents(history):
    return [m.get("content") for m in history]


@pytest.mark.asyncio
async def test_no_cross_chat_or_cross_thread_bleed():
    chat_a, chat_b = 100, 200

    # Chat A: default thread + a second 'work' thread.
    await database_storage.save_message(chat_a, "user", "A-default-msg")
    await database_storage.create_thread(chat_a, "work")
    await database_storage.save_message(chat_a, "user", "A-work-msg", thread_id="work")

    # Chat B: default thread only.
    await database_storage.save_message(chat_b, "user", "B-default-msg")

    a_default = _contents(await database_storage.get_thread_history(chat_a))
    a_work = _contents(await database_storage.get_thread_history(chat_a, thread_id="work"))
    b_default = _contents(await database_storage.get_thread_history(chat_b))

    # Chat A default thread sees only its own default message.
    assert a_default == ["A-default-msg"]
    # Chat A 'work' thread is isolated from its own default thread.
    assert a_work == ["A-work-msg"]
    # Chat B cannot see any of chat A's messages.
    assert b_default == ["B-default-msg"]


@pytest.mark.asyncio
async def test_unknown_chat_returns_empty_history():
    assert await database_storage.get_thread_history(999999) == []
