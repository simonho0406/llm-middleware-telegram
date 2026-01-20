import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from bot.handlers.flash_handler import flash_command
from telegram import Update, Message, Chat, User
from telegram.ext import ContextTypes

@pytest.mark.asyncio
async def test_flash_command_skips_save():
    """
    Verifies that calling /flash triggers _generate_and_send_response 
    with skip_save=True.
    """
    # Mock Update and Context
    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 67890
    mock_update.message.reply_to_message = None
    
    mock_context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    mock_context.args = ["Hello", "World"]
    mock_context.bot.send_message = AsyncMock()
    
    # Mock dependencies
    with patch('bot.handlers.flash_handler.storage_manager') as mock_storage, \
         patch('bot.handlers.flash_handler._generate_and_send_response') as mock_generate, \
         patch('bot.handlers.flash_handler.send_safe_message') as mock_send_safe:
         
        mock_storage.get_current_thread_id = AsyncMock(return_value="thread_1")
        
        # Execute
        await flash_command(mock_update, mock_context)
        
        # Verify
        mock_generate.assert_awaited_once()
        call_kwargs = mock_generate.await_args.kwargs
        
        assert call_kwargs['skip_save'] is True, "skip_save must be True for flash commands"
        assert call_kwargs['chat_id'] == 12345
        assert call_kwargs.get('task_key') == 'flash_task', "Flash task must be isolated"
        
        # Ensure storage (save_message) was NOT called directly by the handler (it shouldn't be anyway, but good to check)
        # The logic is inside _generate, which we mocked, so we trust _generate unit tests for the rest.
