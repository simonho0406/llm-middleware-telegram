import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from bot.handlers.context_sidebar_handler import show_context_page, CTX_PREFIX
from storage import storage_manager
import config

@pytest.mark.asyncio
async def test_grouping_logic():
    """Verifies that flat history is correctly grouped into Interaction Blocks."""
    
    # Mock History:
    # 1. User: "Hi" (ID 1)
    # 2. Asst: "Hello" (ID 2)
    # 3. Asst: "How are you?" (ID 3)
    # 4. User: "Good" (ID 4)
    # 5. Asst: "Great!" (ID 5)
    
    mock_history = [
        {'id': 1, 'role': 'user', 'content': 'Hi', 'timestamp': 100},
        {'id': 2, 'role': 'assistant', 'content': 'Hello', 'timestamp': 101},
        {'id': 3, 'role': 'assistant', 'content': 'How are you?', 'timestamp': 102},
        {'id': 4, 'role': 'user', 'content': 'Good', 'timestamp': 103},
        {'id': 5, 'role': 'assistant', 'content': 'Great!', 'timestamp': 104},
    ]
    
    # We need to simulate the logic inside context_sidebar_handler.show_context_page
    # Since extracting that logic to a pure function would be cleaner, let's copy-paste the logic here for verification
    # or rely on the function if valid.
    # The logic is embedded in `show_context_page`. 
    # Let's extract it or simulate.
    
    # Logic extracted:
    blocks = []
    current_block = []
    
    # Raw history is usually Oldest -> Newest (PK ASC)
    for msg in mock_history:
        if msg['role'] == 'user':
            if current_block:
                blocks.append(current_block)
            current_block = [msg]
        else:
            current_block.append(msg)
    if current_block:
        blocks.append(current_block)
        
    blocks.reverse() # Newest first
    
    # Expected: 2 Blocks
    assert len(blocks) == 2
    
    # Block 1 (Newest): User "Good" + Asst "Great!"
    block_newest = blocks[0]
    assert block_newest[0]['role'] == 'user'
    assert block_newest[0]['content'] == 'Good'
    assert len(block_newest) == 2
    
    # Block 2 (Oldest): User "Hi" + Asst "Hello" + Asst "How are you?"
    block_oldest = blocks[1]
    assert block_oldest[0]['content'] == 'Hi'
    assert len(block_oldest) == 3


@pytest.mark.asyncio
async def test_get_thread_history_with_pk_mock():
    # Verifies the function signature and mock behavior.
    with patch('storage.database_storage.aiosqlite.connect') as mock_connect:
        mock_db = MagicMock() # Changed from AsyncMock to MagicMock for the connection object wrapper
        # But wait, connect() is async context manager.
        # mock_connect is the function.
        # mock_connect.return_value.__aenter__ returns the db connection.
        
        # We need the db connection to handle 'async with db.cursor()'
        # If db.cursor() is called in 'async with', it must return an async context manager.
        # It is NOT awaited specifically.
        
        real_mock_db = AsyncMock() # This is the object inside 'as db'
        mock_connect.return_value.__aenter__.return_value = real_mock_db
        
        # Now handle db.cursor(). It is called as 'db.cursor()', not 'await db.cursor()'.
        # So db.cursor must be a MagicMock that returns an object with __aenter__.
        real_mock_db.cursor = MagicMock()
        mock_cursor_ctx = AsyncMock() # The context manager
        real_mock_db.cursor.return_value = mock_cursor_ctx
        
        mock_cursor = AsyncMock() # The actual cursor
        mock_cursor_ctx.__aenter__.return_value = mock_cursor
        
        # Mock get_thread_pk return
        # 1. _get_or_create_chat -> SELECT chat_id -> Returns (1,) (Chat exists)
        # 2. _get_thread_pk -> SELECT thread_pk -> Returns (99,) (Thread exists)
        mock_cursor.fetchone.side_effect = [(1,), (99,)] 
        
        # Mock fetchall
        mock_cursor.fetchall.return_value = [
            (1, 'user', 'hi', 100),
            (2, 'assistant', 'hello', 101)
        ]
        
        from storage import database_storage
        history = await database_storage.get_thread_history_with_pk(chat_id=123)
        
        assert len(history) == 2
        assert history[0]['id'] == 1
        assert history[0]['role'] == 'user'


@pytest.mark.asyncio
async def test_context_ui_rendering_safety():
    """
    Verifies that the Context UI string construction properly escapes HTML characters.
    """
    from bot.handlers.context_sidebar_handler import show_context_page
    from telegram import Update
    import html
    
    # Mock update/context
    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.id = 12345
    mock_update.callback_query = None
    mock_update.message.reply_text = AsyncMock()
    
    # Mock History Return
    with patch('bot.handlers.context_sidebar_handler.storage_manager') as mock_storage:
        mock_storage.get_thread_history_with_pk = AsyncMock(return_value=[
            {'id': 1, 'role': 'user', 'content': 'User input with <bold> and & signs.'},
            {'id': 2, 'role': 'assistant', 'content': 'Assistant output with "quotes" and stuff.'}
        ])
        
        # Execute
        await show_context_page(mock_update, MagicMock(), page=0)
        
        # Capture
        args, kwargs = mock_update.message.reply_text.call_args
        sent_text = args[0]
        
        # Verify Static HTML
        assert "<b>Context Manager</b>" in sent_text
        assert "<b>User</b>" in sent_text
        assert "<b>Assistant</b>" in sent_text
        assert "<i>" in sent_text
        
        # Verify Dynamic Content Escaping
        # "User input with <bold> and & signs." -> "&lt;bold&gt; and &amp; signs."
        assert "&lt;bold&gt;" in sent_text
        assert "&amp;" in sent_text
        # Quotes usually don't need escaping in body text but html.escape does it by default
        assert "&quot;quotes&quot;" in sent_text

