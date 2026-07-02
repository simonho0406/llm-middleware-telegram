"""
Message-persistence integrity tests (silent-history-loss defense).

A lost save is a SILENT failure: the user sees the answer, but the turn drops out of
history, so the next turn is missing it. Under WAL, concurrent writers are serialized, so a
burst of turns can hit "database is locked". These tests pin that:
  1. Many CONCURRENT saves all persist (no silent loss) — the real-world burst scenario.
  2. A non-transient save error PROPAGATES (so the caller can surface it) rather than being
     swallowed.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
import pytest
import pytest_asyncio
from unittest.mock import patch

import config
from storage import database_storage as db


@pytest_asyncio.fixture(autouse=True)
async def _tmp_db(tmp_path):
    original = config.DB_PATH
    config.DB_PATH = str(tmp_path / "integrity.db")
    await db.init_database()
    yield
    config.DB_PATH = original


@pytest.mark.asyncio
async def test_concurrent_saves_all_persist():
    """40 concurrent message saves to the same chat must ALL land — none silently dropped
    to a 'database is locked' race. This is the burst scenario the busy_timeout + retry fix
    protects."""
    chat_id = 424242
    await db.create_thread(chat_id, db._DEFAULT_THREAD_ID)

    N = 40
    results = await asyncio.gather(
        *[db.save_message(chat_id, "user" if i % 2 == 0 else "assistant", f"msg-{i}") for i in range(N)],
        return_exceptions=True,
    )

    # No save raised, and every one returned a real message_pk.
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"{len(errors)} concurrent saves failed: {errors[:3]}"
    assert all(isinstance(r, int) for r in results)

    # And all N are actually in history (the durability check that matters).
    history = await db.get_thread_history(chat_id)
    saved_contents = {m["content"] for m in history}
    for i in range(N):
        assert f"msg-{i}" in saved_contents, f"msg-{i} was silently lost"
    assert len([m for m in history if m["content"].startswith("msg-")]) == N


@pytest.mark.asyncio
async def test_save_message_propagates_non_transient_error():
    """A non-lock DB error must PROPAGATE (not be swallowed), so the caller can tell the
    user the turn wasn't saved instead of silently losing it."""
    chat_id = 555
    await db.create_thread(chat_id, db._DEFAULT_THREAD_ID)

    import aiosqlite
    boom = aiosqlite.OperationalError("disk I/O error")  # not 'locked'/'busy' → non-transient
    with patch("storage.database_storage.aiosqlite.connect", side_effect=boom):
        with pytest.raises(aiosqlite.OperationalError):
            await db.save_message(chat_id, "assistant", "should not be swallowed")


def test_is_locked_error_classification():
    import aiosqlite
    assert db._is_locked_error(aiosqlite.OperationalError("database is locked")) is True
    assert db._is_locked_error(aiosqlite.OperationalError("database is busy")) is True
    assert db._is_locked_error(aiosqlite.OperationalError("disk I/O error")) is False
    assert db._is_locked_error(ValueError("locked")) is False  # only OperationalError counts
