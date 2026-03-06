import pytest
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from unittest.mock import AsyncMock, MagicMock, patch
from bot.handlers import misc_commands

@pytest.mark.asyncio
async def test_search_deduplication():
    # Setup mocks
    update = MagicMock()
    context = MagicMock()
    context.args = ["test", "query"]
    chat_id = 123
    update.effective_chat.id = chat_id
    update.effective_user.id = 456
    
    # Needs to be AsyncMock context.bot.send_message
    context.bot.send_message = AsyncMock()
    
    # Mock storage_manager
    with patch('bot.handlers.misc_commands.storage_manager') as mock_storage:
        # Mock web_search_service
        with patch('bot.handlers.misc_commands.web_search_service') as mock_search:
            # Mock providers
            with patch('bot.handlers.misc_commands.providers') as mock_providers:
                # Mock messaging
                with patch('bot.handlers.misc_commands.send_safe_message') as mock_send_msg:
                    
                    # Mock config default provider
                    with patch('bot.handlers.misc_commands.config') as mock_config:
                        mock_config.get_default_provider.return_value = 'ollama'
                    
                        # Make storage methods async
                    mock_storage.get_thread_key = AsyncMock()
                    mock_storage.get_thread_key.side_effect = lambda chat_id, key, default=None: default
                    mock_storage.get_thread_history = AsyncMock()
                    mock_storage.save_message = AsyncMock()
                    
                    # 1. Setup Scenario: Last message IS identical -> Should NOT save
                    mock_storage.get_thread_history.return_value = [
                        {'role': 'user', 'content': 'test query'}
                    ]
                    # Make perform_search async
                    mock_search.perform_search = AsyncMock()
                    mock_search.perform_search.return_value = {'status': 'success', 'content': 'results'}
                    
                    # Mock service.generate_response as async generator
                    mock_service = MagicMock()
                    async def async_gen(*args, **kwargs):
                        yield "response"
                    mock_service.generate_response = async_gen
                    
                    # Setup provider config to return this service
                    mock_providers.get_provider_details.return_value = {
                        'ollama': {'service': mock_service, 'default_model': 'model'},
                        'openrouter': {'service': mock_service, 'default_model': 'model'},
                        'nvidia': {'service': mock_service, 'default_model': 'model'},
                        'default': {'service': mock_service, 'default_model': 'model'}
                    }
                    mock_providers.get_config_for_provider.return_value = {'default_model': 'model'}

                    # Call function
                    await misc_commands.search_command(update, context)
                    
                    # Verify save_message NOT called for user query
                    # Note: save_message IS called for assistant response later, so we check call count or args
                    # calls: [call(chat_id, 'assistant', ...)]
                    assert mock_storage.save_message.call_count == 1
                    assert mock_storage.save_message.call_args[0][1] == 'assistant'

                    # 2. Setup Scenario: Last message is DIFFERENT -> Should SAVE
                    mock_storage.save_message.reset_mock()
                    mock_storage.get_thread_history.return_value = [
                        {'role': 'user', 'content': 'previous topic'}
                    ]
                    
                    await misc_commands.search_command(update, context)
                    
                    # Verify save_message called TWICE (user query + assistant response)
                    assert mock_storage.save_message.call_count == 2
                    assert mock_storage.save_message.call_args_list[0][0][1] == 'user'
                    assert mock_storage.save_message.call_args_list[0][0][2] == 'test query'

@pytest.mark.asyncio
async def test_search_reply_handling():
    # Setup mocks
    update = MagicMock()
    context = MagicMock()
    context.args = [] # No args
    update.effective_chat.id = 123
    
    # Mock reply message
    update.message = MagicMock()
    update.message.reply_to_message = MagicMock()
    update.message.reply_to_message.text = "reply query"
    
    context.bot.send_message = AsyncMock()
    
    with patch('bot.handlers.misc_commands.storage_manager') as mock_storage:
        with patch('bot.handlers.misc_commands.web_search_service') as mock_search:
            with patch('bot.handlers.misc_commands.providers') as mock_providers:
                with patch('bot.handlers.misc_commands.send_safe_message') as mock_send_msg:
                    # Mock config default provider
                    with patch('bot.handlers.misc_commands.config') as mock_config:
                        mock_config.get_default_provider.return_value = 'ollama'
                    
                        mock_storage.get_thread_key = AsyncMock()
                    mock_storage.get_thread_key.side_effect = lambda chat_id, key, default=None: default
                    mock_storage.get_thread_history = AsyncMock()
                    mock_storage.save_message = AsyncMock()

                    mock_storage.get_thread_history.return_value = []
                    mock_search.perform_search = AsyncMock()
                    mock_search.perform_search.return_value = {'status': 'success', 'content': 'results'}
                    
                    # Mock service
                    mock_service = MagicMock()
                    async def async_gen(*args, **kwargs):
                        yield "response"
                    mock_service.generate_response = async_gen
                    
                    mock_providers.get_provider_details.return_value = {
                        'ollama': {'service': mock_service, 'default_model': 'model'},
                        'openrouter': {'service': mock_service, 'default_model': 'model'},
                        'nvidia': {'service': mock_service, 'default_model': 'model'},
                        'default': {'service': mock_service, 'default_model': 'model'}
                    }
                    mock_providers.get_config_for_provider.return_value = {'default_model': 'model'}

                    # Call function
                    await misc_commands.search_command(update, context)
                    
                    # Verify search performed with reply text
                    mock_search.perform_search.assert_called_with("reply query")
                    
                    # Verify user query saved (since history empty)
                    mock_storage.save_message.assert_any_call(123, 'user', 'reply query')
