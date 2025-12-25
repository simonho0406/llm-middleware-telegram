import pytest
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, User, Chat
from telegram.ext import ContextTypes

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from bot.handlers import misc_commands

@pytest.fixture
def mock_update_context():
    mock_update = MagicMock(spec=Update)
    mock_context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 67890
    mock_context.args = ["test", "query"]
    mock_context.bot.send_message = AsyncMock()
    return mock_update, mock_context

@pytest.mark.asyncio
async def test_search_uses_full_context_and_save_message(mock_update_context):
    """
    Verifies the FIXED behavior of /search:
    1. It fetches ALL history via get_thread_history.
    2. It passes that history to the LLM (for context-aware search).
    3. It uses save_message (Append-Only) for both User query and Assistant response.
    """
    mock_update, mock_context = mock_update_context
    
    # Mock History Content
    mock_history = [{'role': 'user', 'content': 'Previous Context'}, {'role': 'assistant', 'content': 'Previous Answer'}]
    
    # Mock Dependencies
    with patch('services.web_search_service.perform_search', new_callable=AsyncMock) as mock_search, \
         patch('bot.handlers.misc_commands.storage_manager.get_thread_history', new_callable=AsyncMock) as mock_get_history, \
         patch('bot.handlers.misc_commands.storage_manager.save_message', new_callable=AsyncMock) as mock_save_message, \
         patch('bot.handlers.misc_commands.storage_manager.set_thread_history', new_callable=AsyncMock) as mock_set_history, \
         patch('bot.handlers.misc_commands.send_safe_message', new_callable=AsyncMock) as mock_send, \
         patch('bot.providers.get_provider_details') as mock_get_providers, \
         patch('config.get_default_provider', return_value='mock_provider'), \
         patch('bot.handlers.misc_commands.storage_manager.get_thread_key', new_callable=AsyncMock) as mock_get_thread_key:
             
        # Setup
        mock_search.return_value = {'status': 'success', 'content': 'Search Results'}
        mock_get_history.return_value = mock_history
        mock_get_thread_key.side_effect = lambda cid, key, default=None: 'mock_provider' if key == 'provider' else 'mock_model'
        
        # Mock LLM Service
        mock_service = MagicMock()
        async def async_gen(*args, **kwargs):
            yield "LLM Answer"
        mock_service.generate_response = MagicMock(side_effect=async_gen)
        
        mock_get_providers.return_value = {
            'mock_provider': {'service': mock_service, 'default_model': 'mock_model'}
        }
        
        # Execute
        await misc_commands.search_command(mock_update, mock_context)
        
        # Verify 1: Context History passed to LLM is NOT empty
        call_kwargs = mock_service.generate_response.call_args.kwargs
        assert call_kwargs['context_history'] == mock_history, "Expected full context history to be passed"
        
        # Verify 2: Uses save_message twice (User Query + Assistant Response)
        assert mock_save_message.call_count == 2
        # Check calls
        calls = mock_save_message.call_args_list
        assert calls[0].args[1] == 'user'
        assert calls[0].args[2] == 'test query'
        assert calls[1].args[1] == 'assistant'
        assert calls[1].args[2] == 'LLM Answer'
        
        # Verify 3: set_thread_history (Legacy/Destructive) is NOT called
        assert not mock_set_history.called, "Deprecated set_thread_history should NOT be called"
