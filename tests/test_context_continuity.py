import pytest
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from telegram import Update, User, Chat, CallbackQuery, Message
from telegram.ext import ContextTypes

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from bot.handlers import ask_selected_handler, discuss_handler

@pytest.fixture
def mock_update_context():
    mock_update = MagicMock(spec=Update)
    mock_context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    
    chat = MagicMock(spec=Chat)
    chat.id = 12345
    user = MagicMock(spec=User)
    user.id = 67890
    
    mock_update.effective_chat = chat
    mock_update.effective_user = user
    
    # Mock Callback Query
    query = MagicMock(spec=CallbackQuery)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.data = "ask_sel_act_done"
    query.message = MagicMock(spec=Message)
    query.message.delete = AsyncMock()
    mock_update.callback_query = query
    
    mock_context.user_data = {}
    mock_context.chat_data = {}
    mock_context.bot.send_message = AsyncMock()
    
    return mock_update, mock_context

@pytest.mark.asyncio
async def test_ask_selected_context_and_archival(mock_update_context):
    """
    Verifies that /ask_selected:
    1. Fetches history.
    2. Passes history to LLM.
    3. Saves results to DB.
    """
    mock_update, mock_context = mock_update_context
    
    # Setup User Data
    mock_context.user_data['ask_selected_models'] = ['mock_provider:model1']
    mock_context.user_data['ask_selected_models_set'] = {'mock_provider:model1'}
    mock_context.user_data['ask_selected_prompt'] = 'Test Prompt'
    mock_context.user_data['model_metadata'] = {
        'mock_provider:model1': {'provider': 'mock_provider', 'actual_id': 'model1', 'display': 'Model 1'}
    }
    
    # Needs to match hash logic? No, the handler splits "provider:actual_id" from selected_list
    # The handler expects selected_list to contain "provider:actual_id" strings.
    
    # Updates to handler logic:
    # It splits item (from selected_list) by ":".
    
    mock_history = [{'role': 'user', 'content': 'Old Context'}]
    
    with patch('bot.handlers.ask_selected_handler.storage_manager.get_thread_history', new_callable=AsyncMock) as mock_get_history, \
         patch('bot.handlers.ask_selected_handler.storage_manager.save_message', new_callable=AsyncMock) as mock_save_message, \
         patch('bot.handlers.ask_selected_handler.openai_compatible_service') as mock_service_module, \
         patch('bot.handlers.ask_selected_handler.send_safe_message', new_callable=AsyncMock):
             
        mock_get_history.return_value = mock_history
        
        # Mock Service
        mock_service_func = AsyncMock(return_value="LLM Result")
        mock_service_module._generate_single_model_non_streaming = mock_service_func
        
        # Execute
        await ask_selected_handler.done_selecting_callback(mock_update, mock_context)
        
        # Assertions
        # 1. Check History Fetch
        mock_get_history.assert_called_with(12345, limit=500)
        
        # 2. Check Service Call receives history
        # Call args: (actual_id, prompt, context_history)
        # 2. Check Service called at least once (concurrent + synthesis calls)
        assert mock_service_func.call_count >= 1
        # Check that at least one call used the prompt
        args_list = [call.args for call in mock_service_func.call_args_list]
        prompts = [args[1] for args in args_list]
        assert 'Test Prompt' in prompts
        
        # 3. Check Archival
        assert mock_save_message.call_count == 2
        # Verify content (User then Assistant)
        calls = mock_save_message.call_args_list
        assert calls[0].args[1] == 'user'
        assert calls[0].args[2] == 'Test Prompt'
        assert calls[1].args[1] == 'assistant'
        assert "LLM Result" in calls[1].args[2]

@pytest.mark.asyncio
async def test_discuss_context_and_archival(mock_update_context):
    """
    Verifies that /discuss:
    1. Fetches history.
    2. Passes history to LLM (prepended).
    3. Saves transcript to DB.
    """
    mock_update, mock_context = mock_update_context
    
    # Setup User Data
    mock_context.user_data['discussion_data'] = {
        'user_prompt': 'Discuss Prompt',
        'selected_models': [
            {'id': 'm1', 'provider': 'p1', 'name': 'Model 1'},
            {'id': 'm2', 'provider': 'p2', 'name': 'Model 2'}
        ]
    }
    
    mock_history = [{'role': 'user', 'content': 'Existing Context'}]
    
    # Mock Generator
    async def mock_generator(context_history, prompt, model):
        yield f"Response from {model}"
    
    mock_service = MagicMock()
    mock_service.generate_response = mock_generator
    
    # Patch dependencies
    with patch('bot.handlers.discuss_handler.storage_manager.get_thread_history', new_callable=AsyncMock) as mock_get_history, \
         patch('bot.handlers.discuss_handler.storage_manager.save_message', new_callable=AsyncMock) as mock_save_message, \
         patch('bot.handlers.discuss_handler.get_service_for_provider', return_value=mock_service), \
         patch('bot.handlers.discuss_handler.send_safe_message', new_callable=AsyncMock), \
         patch('bot.handlers.discuss_handler.parse_markdown_to_ast'), \
         patch('bot.handlers.discuss_handler.split_document_ast_aware', return_value=[]), \
         patch('bot.handlers.discuss_handler.render_ast_to_telegram_v2'):

        mock_get_history.return_value = mock_history
        
        # Execute
        await discuss_handler.run_discussion(mock_update, mock_context)
        
        # Assertions
        # 1. Fetch History
        mock_get_history.assert_called_with(12345, limit=500)
        
        # 2. Check Archival
        assert mock_save_message.call_count == 2
        calls = mock_save_message.call_args_list
        assert calls[0].args[1] == 'user'
        assert calls[0].args[2] == 'Discuss Prompt'
        assert calls[1].args[1] == 'assistant'
        assert "Response from m1" in calls[1].args[2]
        assert "Response from m2" in calls[1].args[2]
