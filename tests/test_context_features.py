import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from bot.handlers.context_sidebar_handler import show_context_page, CTX_PREFIX
from storage import storage_manager
import config

@pytest.mark.asyncio
async def test_grouping_logic():
    """Verifies flat history is grouped into Interaction Blocks (user + following
    assistant turns), newest block first, by driving the REAL show_context_page."""

    # Mock History (oldest -> newest, as get_thread_history_with_pk returns):
    #   Block A (oldest): User "Hi" + Asst "Hello" + Asst "How are you?"
    #   Block B (newest): User "Good" + Asst "Great!"
    mock_history = [
        {'id': 1, 'role': 'user', 'content': 'Hi'},
        {'id': 2, 'role': 'assistant', 'content': 'Hello'},
        {'id': 3, 'role': 'assistant', 'content': 'How are you?'},
        {'id': 4, 'role': 'user', 'content': 'Good'},
        {'id': 5, 'role': 'assistant', 'content': 'Great!'},
    ]

    mock_update = MagicMock()
    mock_update.effective_chat.id = 12345
    mock_update.callback_query = None
    mock_update.message.reply_text = AsyncMock()

    with patch('bot.handlers.context_sidebar_handler.storage_manager') as mock_storage:
        mock_storage.get_thread_history_with_pk = AsyncMock(return_value=mock_history)

        # Page 0 = newest interaction block.
        await show_context_page(mock_update, MagicMock(), page=0)
        text_newest = mock_update.message.reply_text.call_args[0][0]

        # Page 1 = older interaction block.
        await show_context_page(mock_update, MagicMock(), page=1)
        text_oldest = mock_update.message.reply_text.call_args[0][0]

    # Two interaction blocks were detected.
    assert "Interaction 1/2" in text_newest
    assert "Interaction 2/2" in text_oldest

    # Newest-first ordering: page 0 holds the "Good"/"Great!" turn, page 1 the "Hi" turn.
    assert "Good" in text_newest
    assert "Hi" in text_oldest
    assert "Good" not in text_oldest

    # The oldest block grouped BOTH assistant replies under the single user turn.
    assert "(2 messages" in text_oldest      # Hello + How are you?
    assert "(1 messages" in text_newest      # Great!


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
            (1, 'user', 'hi', 100, None, None),
            (2, 'assistant', 'hello', 101, None, None)
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

