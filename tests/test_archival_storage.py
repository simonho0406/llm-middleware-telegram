import pytest
import pytest_asyncio
import asyncio
import time
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import aiosqlite
from storage import database_storage, storage_manager
from bot import response_generator
import config

# Setup temporary DB for testing
TEST_DB_PATH = "./test_archival.db"

@pytest_asyncio.fixture
async def setup_db():
    config.DB_PATH = TEST_DB_PATH
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    
    await database_storage.init_database()
    
    # Create a test thread
    chat_id = 12345
    thread_id = "thread_test"
    await database_storage.create_thread(chat_id, thread_id)
    await database_storage.set_current_thread_id(chat_id, thread_id)
    
    yield
    
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)

@pytest.mark.asyncio
async def test_append_message(setup_db):
    chat_id = 12345
    
    # Append User Message
    await database_storage.save_message(chat_id, "user", "Hello 1")
    history = await database_storage.get_thread_history(chat_id)
    assert len(history) == 1
    assert history[0]['content'] == "Hello 1"
    
    # Append Assistant Message
    await database_storage.save_message(chat_id, "assistant", "Hi there")
    history = await database_storage.get_thread_history(chat_id)
    assert len(history) == 2
    assert history[1]['content'] == "Hi there"

@pytest.mark.asyncio
async def test_timestamp_stability(setup_db):
    chat_id = 12345
    
    # Insert first message
    await database_storage.save_message(chat_id, "user", "Msg 1")
    
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        async with db.execute("SELECT timestamp FROM messages WHERE content = 'Msg 1'") as cursor:
            ts1 = (await cursor.fetchone())[0]
            
    # Wait a bit
    await asyncio.sleep(1.1)
    
    # Insert second message
    await database_storage.save_message(chat_id, "assistant", "Msg 2")
    
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        async with db.execute("SELECT timestamp FROM messages WHERE content = 'Msg 1'") as cursor:
            ts1_new = (await cursor.fetchone())[0]
        async with db.execute("SELECT timestamp FROM messages WHERE content = 'Msg 2'") as cursor:
            ts2 = (await cursor.fetchone())[0]

    # Verify TS1 did not change, and TS2 > TS1
    assert ts1 == ts1_new
    assert ts2 > ts1

@pytest.mark.asyncio
async def test_reroll_cleanup(setup_db):
    chat_id = 12345
    
    await database_storage.save_message(chat_id, "user", "Prompt")
    await database_storage.save_message(chat_id, "assistant", "Bad Answer")
    
    history_before = await database_storage.get_thread_history(chat_id)
    assert len(history_before) == 2
    assert history_before[1]['content'] == "Bad Answer"
    
    # Perform Reroll Cleanup (Remove last assistant msg)
    success = await database_storage.remove_last_assistant_message(chat_id)
    assert success is True
    
    history_after = await database_storage.get_thread_history(chat_id)
    assert len(history_after) == 1
    assert history_after[0]['content'] == "Prompt"
    
    # Insert New Answer
    await database_storage.save_message(chat_id, "assistant", "Good Answer")
    history_final = await database_storage.get_thread_history(chat_id)
    assert len(history_final) == 2
    assert history_final[1]['content'] == "Good Answer"

@pytest.mark.asyncio
async def test_context_limit_fetching(setup_db):
    chat_id = 12345
    
    # Insert 600 messages
    for i in range(600):
        await database_storage.save_message(chat_id, "user" if i % 2 == 0 else "assistant", f"Msg {i}")
        
    # Default limit is 500
    history = await database_storage.get_thread_history(chat_id, limit=500)
    assert len(history) == 500
    # Should get the LAST messages (Msg 100 to 599)
    assert history[-1]['content'] == "Msg 599"
    assert history[0]['content'] == "Msg 100" 
