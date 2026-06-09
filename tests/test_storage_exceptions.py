import pytest
import pytest_asyncio
import aiosqlite
import os
from storage import database_storage

@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(tmp_path):
    """Fixture to set up a clean, isolated test database for each test."""
    db_path = tmp_path / "test_storage.db"
    
    import config
    original_db_path = config.DB_PATH
    config.DB_PATH = str(db_path)
    
    await database_storage.init_database()
    
    yield
    
    config.DB_PATH = original_db_path
    if os.path.exists(db_path):
        os.remove(db_path)

@pytest.mark.asyncio
async def test_set_thread_history_redirect():
    """Test that setting 'history' via set_thread_key correctly redirects to replace_thread_history_dangerous."""
    chat_id = 9999
    thread_id = "test_thread"
    
    # Setup thread
    await database_storage.create_thread(chat_id, thread_id)
    
    # Set history using the special "history" key which should redirect
    test_history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"}
    ]
    
    # This previously raised NameError due to undefined set_thread_history
    await database_storage.set_thread_key(chat_id, 'history', test_history, thread_id)
    
    # Verify the history was saved
    saved_history = await database_storage.get_thread_history(chat_id, thread_id)
    assert len(saved_history) == 2
    assert saved_history[0]["content"] == "Hello"
    assert saved_history[1]["content"] == "Hi there"

@pytest.mark.asyncio
async def test_get_thread_history_redirect():
    """Test that getting 'history' via get_thread_key correctly redirects to get_thread_history."""
    chat_id = 9999
    thread_id = "test_thread"
    
    # Setup thread and insert history natively
    await database_storage.create_thread(chat_id, thread_id)
    await database_storage.save_message(chat_id, "user", "Test message", thread_id)
    
    # Fetch using the generic get_thread_key with 'history' string
    fetched_history = await database_storage.get_thread_key(chat_id, 'history', thread_id=thread_id)
    
    assert len(fetched_history) == 1
    assert fetched_history[0]["content"] == "Test message"
    
@pytest.mark.asyncio
async def test_invalid_thread_key():
    """Verify that invalid keys still raise ValueError."""
    with pytest.raises(ValueError, match="Invalid key 'invalid_key'"):
        await database_storage.get_thread_key(123, 'invalid_key')
        
    with pytest.raises(ValueError, match="Invalid key 'invalid_key'"):
        await database_storage.set_thread_key(123, 'invalid_key', 'value')

@pytest.mark.asyncio
async def test_sqlite_wal_mode():
    """Verify that SQLite WAL mode is enabled in the database."""
    import config
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("PRAGMA journal_mode;") as cursor:
            row = await cursor.fetchone()
            assert row[0].lower() == "wal"
