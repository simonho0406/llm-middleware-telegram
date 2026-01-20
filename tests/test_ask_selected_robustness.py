import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, User, Chat, Message, CallbackQuery
from telegram.ext import ContextTypes

# Adjust path to import bot modules
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from bot.handlers import ask_selected_handler

@pytest.fixture
def mock_update_context():
    mock_update = MagicMock(spec=Update)
    mock_context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    
    chat = MagicMock(spec=Chat)
    chat.id = 12345
    mock_update.effective_chat = chat
    
    # Mock Callback Query
    query = MagicMock(spec=CallbackQuery)
    query.answer = AsyncMock()
    query.message = MagicMock(spec=Message)
    query.message.delete = AsyncMock()
    mock_update.callback_query = query
    
    mock_context.user_data = {}
    mock_context.bot.send_message = AsyncMock()
    
    return mock_update, mock_context

@pytest.mark.asyncio
async def test_ask_selected_partial_failure(mock_update_context):
    """
    Test that if one model fails (raises Exception), the others succeed 
    and the flow completes successfully.
    """
    mock_update, mock_context = mock_update_context
    
    # Setup: 2 models selected. Model A (Chairman), Model B (Member)
    mock_context.user_data['ask_selected_models'] = ['provider1:modelA', 'provider2:modelB']
    mock_context.user_data['ask_selected_models_set'] = {'provider1:modelA', 'provider2:modelB'}
    mock_context.user_data['ask_selected_prompt'] = 'Test Prompt'
    mock_context.user_data['model_metadata'] = {
        'provider1:modelA': {'provider': 'provider1', 'actual_id': 'modelA', 'display': 'Chairman (Good)'},
        'provider2:modelB': {'provider': 'provider2', 'actual_id': 'modelB', 'display': 'Member (Bad)'}
    }

    # Custom Service Mock that fails for modelB
    mock_service = AsyncMock()
    
    async def mock_generate(model_id, prompt, history):
        if model_id == 'modelB':
            raise RuntimeError("Simulated API Crash")
        if "Synthesis" in prompt:
            return "Synthesis Result"
        return f"Response from {model_id}"
        
    mock_service._generate_single_model_non_streaming = AsyncMock(side_effect=mock_generate)

    # Patch dependencies
    with patch('bot.handlers.ask_selected_handler.storage_manager') as mock_storage, \
         patch('bot.handlers.ask_selected_handler.providers.get_service_for_provider') as mock_get_provider, \
         patch('bot.handlers.ask_selected_handler.send_safe_message', new_callable=AsyncMock) as mock_send_safe:
        
        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.save_message = AsyncMock()
        mock_get_provider.return_value = mock_service
        
        # ACT
        await ask_selected_handler.done_selecting_callback(mock_update, mock_context)
        
        # ASSERT
        
        # 1. Verify safe message was sent (Flow finished)
        assert mock_send_safe.called
        args, _ = mock_send_safe.call_args
        final_message = args[2]
        
        # 2. Verify Chairman survived
        assert "Response from modelA" in final_message
        
        # 3. Verify Failed Model reported error
        assert "Member (Bad)" in final_message
        assert "Unhandled Exception" in final_message
        assert "Simulated API Crash" in final_message
        
        # 4. Verify Synthesis still ran
        assert "Chairman Synthesis" in final_message
