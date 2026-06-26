import pytest
import os
import sys
import asyncio

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
    
    # Flat grouped patch block so EVERY mock — including config — is active when
    # search_command runs. (A previously-nested `with patch(config)` closed early,
    # leaving config.get_default_provider unpatched at call time.)
    with patch('bot.handlers.misc_commands.storage_manager') as mock_storage, \
         patch('bot.handlers.misc_commands.web_search_service') as mock_search, \
         patch('bot.handlers.misc_commands.providers') as mock_providers, \
         patch('bot.handlers.misc_commands.send_safe_message'), \
         patch('bot.handlers.misc_commands.send_plain_message', new_callable=AsyncMock), \
         patch('bot.handlers.misc_commands.config') as mock_config:

        mock_config.get_default_provider.return_value = 'ollama'

        mock_storage.get_thread_key = AsyncMock(side_effect=lambda chat_id, key, default=None: default)
        mock_storage.get_thread_history = AsyncMock()
        mock_storage.save_message = AsyncMock()

        # 1. Setup Scenario: Last message IS identical -> Should NOT save the user query
        mock_storage.get_thread_history.return_value = [
            {'role': 'user', 'content': 'test query'}
        ]
        mock_search.perform_search = AsyncMock(return_value={'status': 'success', 'content': 'results'})

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

        await misc_commands.search_command(update, context)

        # Only the assistant response is saved (the duplicate user query is skipped).
        assert mock_storage.save_message.call_count == 1
        assert mock_storage.save_message.call_args[0][1] == 'assistant'

        # 2. Setup Scenario: Last message is DIFFERENT -> Should SAVE both
        mock_storage.save_message.reset_mock()
        mock_storage.get_thread_history.return_value = [
            {'role': 'user', 'content': 'previous topic'}
        ]

        await misc_commands.search_command(update, context)

        assert mock_storage.save_message.call_count == 2
        assert mock_storage.save_message.call_args_list[0][0][1] == 'user'
        assert mock_storage.save_message.call_args_list[0][0][2] == 'test query'

@pytest.mark.asyncio
async def test_autosearch_does_not_self_cancel():
    """Regression: the auto-search delegation runs INSIDE the existing 'llm_task',
    so chat_data['llm_task'] equals asyncio.current_task() when search_command
    executes. The defensive cancel-before-override guard must NOT cancel the task
    it is running in — otherwise the search aborts and no reply is ever sent.

    We simulate that by pre-storing the running task into the slot and asserting
    (a) it is not cancelled, and (b) the search completes (assistant message saved).
    """
    update = MagicMock()
    context = MagicMock()
    context.args = ["test", "query"]
    chat_id = 123
    update.effective_chat.id = chat_id
    update.effective_user.id = 456

    # Real dict so the identity guard reads/writes the actual stored task object.
    running_task = asyncio.current_task()
    context.chat_data = {'llm_task': running_task}

    with patch('bot.handlers.misc_commands.storage_manager') as mock_storage, \
         patch('bot.handlers.misc_commands.web_search_service') as mock_search, \
         patch('bot.handlers.misc_commands.providers') as mock_providers, \
         patch('bot.handlers.misc_commands.send_safe_message'), \
         patch('bot.handlers.misc_commands.send_plain_message', new_callable=AsyncMock), \
         patch('bot.handlers.misc_commands.config') as mock_config:

        mock_config.get_default_provider.return_value = 'ollama'
        mock_storage.get_thread_key = AsyncMock(side_effect=lambda chat_id, key, default=None: default)
        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.save_message = AsyncMock()

        mock_search.perform_search = AsyncMock(return_value={'status': 'success', 'content': 'results'})

        mock_service = MagicMock()
        async def async_gen(*args, **kwargs):
            yield "the answer"
        mock_service.generate_response = async_gen

        mock_providers.get_provider_details.return_value = {
            'ollama': {'service': mock_service, 'default_model': 'model'},
            'default': {'service': mock_service, 'default_model': 'model'},
        }
        mock_providers.get_config_for_provider.return_value = {'default_model': 'model'}

        # If the bug were present, .cancel() on the running task would arm a
        # CancelledError and this await would raise instead of completing.
        await misc_commands.search_command(update, context)

        # The running task must not have been cancelled by the guard.
        assert not running_task.cancelling(), "search_command self-cancelled the running task"

        # The search completed end-to-end: the assistant response was saved.
        assert any(
            call.args[1] == 'assistant'
            for call in mock_storage.save_message.call_args_list
        ), "assistant response was not saved — search aborted early"

        # The slot still points at the running task (reassignment was a no-op).
        assert context.chat_data['llm_task'] is running_task


@pytest.mark.asyncio
async def test_search_reply_handling():
    # Setup mocks
    update = MagicMock()
    context = MagicMock()
    context.args = [] # No args
    update.effective_chat.id = 123
    update.effective_user.id = 456
    
    # Mock reply message
    update.message = MagicMock()
    update.message.reply_to_message = MagicMock()
    update.message.reply_to_message.text = "reply query"
    
    # Flat grouped patch block so config is active when search_command runs.
    with patch('bot.handlers.misc_commands.storage_manager') as mock_storage, \
         patch('bot.handlers.misc_commands.web_search_service') as mock_search, \
         patch('bot.handlers.misc_commands.providers') as mock_providers, \
         patch('bot.handlers.misc_commands.send_safe_message'), \
         patch('bot.handlers.misc_commands.send_plain_message', new_callable=AsyncMock), \
         patch('bot.handlers.misc_commands.config') as mock_config:

        mock_config.get_default_provider.return_value = 'ollama'

        mock_storage.get_thread_key = AsyncMock(side_effect=lambda chat_id, key, default=None: default)
        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.save_message = AsyncMock()

        mock_search.perform_search = AsyncMock(return_value={'status': 'success', 'content': 'results'})

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

        await misc_commands.search_command(update, context)

        # Search performed with the replied-to text.
        # mcp_service is looked up from app.bot_data and may be a MagicMock here;
        # ANY accepts whatever the mock returns.
        from unittest.mock import ANY
        mock_search.perform_search.assert_called_with("reply query", manual=True, mcp_service=ANY)

        # User query saved (history was empty, so not a duplicate).
        mock_storage.save_message.assert_any_call(123, 'user', 'reply query')
