import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from bot.handlers import misc_commands

@pytest.mark.asyncio
async def test_search_retry_flow():
    """
    Verifies that:
    1. A failed search sends a message with a Retry button.
    2. Clicking the Retry button triggers the search again.
    """
    # Setup mocks
    mock_update = MagicMock(spec=Update)
    mock_context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 67890
    mock_context.args = ["test", "query"]
    
    # Mock placeholder message
    mock_placeholder = AsyncMock()
    # We patch send_plain_message now
    
    # 1. Simulate Search Failure
    with patch('services.web_search_service.perform_search', new_callable=AsyncMock) as mock_search, \
         patch('bot.messaging.send_plain_message', new_callable=AsyncMock, return_value=mock_placeholder):
        mock_search.return_value = {'status': 'error', 'message': 'Simulated failure'}
        
        # Call search command
        await misc_commands.search_command(mock_update, mock_context)
        
        # Verify error message and Retry button
        args, kwargs = mock_placeholder.edit_text.call_args
        assert "Simulated failure" in args[0]
        assert "reply_markup" in kwargs
        keyboard = kwargs['reply_markup'].inline_keyboard
        assert len(keyboard) > 0
        assert keyboard[0][0].text == "🔄 Retry Search"
        assert keyboard[0][0].callback_data == "retry_search"

    # 2. Simulate Retry Click
    # Setup callback query
    mock_query = MagicMock()
    mock_query.answer = AsyncMock() # Ensure answer is awaitable
    mock_query.data = "retry_search"
    mock_query.message = mock_placeholder # The message with the button
    mock_update.callback_query = mock_query
    
    # We need to store the query somewhere because retry_search_callback needs it.
    mock_context.user_data = {'last_search_query': 'test query'}
    
    with patch('services.web_search_service.perform_search', new_callable=AsyncMock) as mock_search:
        # Second attempt succeeds
        mock_search.return_value = {'status': 'success', 'content': 'Search results'}
        
        # We also need to mock the LLM generation part since search_command calls it
        mock_service = MagicMock()
        mock_service.generate_response = MagicMock()
        async def async_gen(*args, **kwargs):
            yield "LLM Answer"
        mock_service.generate_response.side_effect = async_gen
        
        with patch('bot.providers.get_provider_details') as mock_get_providers, \
             patch('config.get_default_provider', return_value='mock_provider'), \
             patch('bot.handlers.misc_commands.storage_manager.get_thread_key', new_callable=AsyncMock) as mock_get_key, \
             patch('bot.handlers.misc_commands.storage_manager.save_message', new_callable=AsyncMock) as mock_save, \
             patch('bot.handlers.misc_commands.storage_manager.get_thread_history', new_callable=AsyncMock) as mock_history, \
             patch('bot.messaging.send_plain_message', new_callable=AsyncMock, return_value=mock_placeholder), \
             patch('bot.response_generator._generate_llm_response', new_callable=AsyncMock) as mock_gen_llm:
            
            mock_get_providers.return_value = {
                'mock_provider': {'service': mock_service, 'default_model': 'mock_model'}
            }
            mock_get_key.return_value = 'mock_provider'
            
            # Mock _generate_llm_response to return success so we don't hit DB/Providers
            mock_gen_llm.return_value = {
                'content': "LLM Answer",
                'error': None,
                'truncated_history': [],
                'provider_info': {},
                'processed_history': []
            }
            
            # Call the retry callback
            if hasattr(misc_commands, 'retry_search_callback'):
                await misc_commands.retry_search_callback(mock_update, mock_context)
                
                # Verify search was called again
                mock_search.assert_called_with('test query', manual=True)
            else:
                pytest.fail("retry_search_callback not implemented yet")
