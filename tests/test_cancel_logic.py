import pytest
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, User, Chat
from telegram.ext import ContextTypes, ConversationHandler

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.handlers import misc_commands, discuss_panel_handler

@pytest.fixture
def mock_update_context():
    mock_update = MagicMock(spec=Update)
    mock_context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    
    mock_user = MagicMock(spec=User)
    mock_user.id = 67890
    mock_user.username = "testuser"
    
    mock_chat = MagicMock(spec=Chat)
    mock_chat.id = 12345
    mock_chat.type = "private"
    
    mock_update.effective_user = mock_user
    mock_update.effective_chat = mock_chat
    
    mock_context.chat_data = {}
    mock_context.user_data = {}
    mock_context.bot.send_message = AsyncMock()
    
    return mock_update, mock_context

@pytest.mark.asyncio
async def test_normal_chat_cancel_success(mock_update_context):
    """
    Scenario: User cancels an ongoing LLM task in normal chat.
    Condition: chat_data has 'llm_task'.
    Expectation: task.cancel() is called, message sent, task removed.
    """
    mock_update, mock_context = mock_update_context
    
    # Setup Active Task. _pending_user_message_pk is attached by
    # _generate_and_send_response_task; for a task with no pending PK it stays
    # None. Set explicitly because MagicMock auto-generates attribute reads.
    mock_task = MagicMock()
    mock_task.done.return_value = False
    mock_task._pending_user_message_pk = None
    mock_context.chat_data['llm_task'] = mock_task
    
    # Execute
    with patch('bot.handlers.misc_commands.send_safe_message', new_callable=AsyncMock) as mock_send:
        await misc_commands.cancel_command(mock_update, mock_context)
        
        # Verify
        assert mock_task.cancel.called
        mock_send.assert_called_with(mock_context, mock_update, "The current AI response generation has been cancelled.")
        assert 'llm_task' not in mock_context.chat_data

@pytest.mark.asyncio
async def test_normal_chat_cancel_no_task(mock_update_context):
    """
    Scenario: User cancels but no task is running.
    Expectation: Info message sent.
    """
    mock_update, mock_context = mock_update_context
    # No task in chat_data
    
    with patch('bot.handlers.misc_commands.send_safe_message', new_callable=AsyncMock) as mock_send:
        await misc_commands.cancel_command(mock_update, mock_context)
        
        # Verify
        assert "no active response generation" in mock_send.call_args[0][2]

@pytest.mark.asyncio
async def test_panel_cancel_command_success(mock_update_context):
    """
    Scenario: User cancels inside a Panel Discussion.
    Condition: panel_state exists.
    Expectation: Panel task cancelled, state cleaned up, Conversation END returned.
    """
    mock_update, mock_context = mock_update_context
    
    # Setup Panel Task
    mock_panel_task = MagicMock()
    mock_panel_task.done.return_value = False
    
    # Implementation checks context.user_data.get('panel_task') directly
    mock_context.user_data['panel_task'] = mock_panel_task
    mock_context.user_data['panel_state'] = {
        'some_data': 'exists'
    }
    
    with patch('bot.handlers.discuss_panel_handler.send_safe_message', new_callable=AsyncMock) as mock_send, \
         patch('bot.handlers.discuss_panel_handler._cleanup_discussion_state', new_callable=AsyncMock) as mock_cleanup:
             
        # Execute
        result = await discuss_panel_handler.panel_cancel_command(mock_update, mock_context)
        
        # Verify
        assert mock_panel_task.cancel.called
        assert mock_cleanup.called
        assert result == ConversationHandler.END
        mock_send.assert_called_with(mock_context, mock_update, "Panel discussion cancelled.")
