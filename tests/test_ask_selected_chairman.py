import pytest
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from telegram import Update, User, Chat, CallbackQuery, Message
from telegram.ext import ContextTypes, ConversationHandler
import sys
import os

# Adjust path to import bot modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from bot.handlers import ask_selected_handler

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
    query.message = MagicMock(spec=Message)
    query.message.delete = AsyncMock()
    mock_update.callback_query = query
    
    mock_context.user_data = {}
    mock_context.bot.send_message = AsyncMock()
    
    return mock_update, mock_context

@pytest.mark.asyncio
async def test_chairman_identification_and_flow(mock_update_context):
    """
    Test that the first selected model is identified as Chairman and the flow proceeds correctly.
    """
    mock_update, mock_context = mock_update_context
    
    # Setup User Data with ORDERED list
    # Chairman should be 'provider1:modelA' (First selected)
    mock_context.user_data['ask_selected_models'] = ['provider1:modelA', 'provider2:modelB']
    mock_context.user_data['ask_selected_models_set'] = {'provider1:modelA', 'provider2:modelB'}
    mock_context.user_data['ask_selected_prompt'] = 'Test Prompt'
    mock_context.user_data['model_metadata'] = {
        'provider1:modelA': {'provider': 'provider1', 'actual_id': 'modelA', 'display': 'Chairman Model'},
        'provider2:modelB': {'provider': 'provider2', 'actual_id': 'modelB', 'display': 'Member Model'}
    }

    # Patch dependencies
    # We must patch providers.get_service_for_provider to return our mock service
    mock_service = AsyncMock()
    
    # Mock Service Generation logic
    async def mock_generate(model_id, prompt, history):
        if "Synthesis" in prompt:
            return f"Synthesis by {model_id}"
        return f"Response from {model_id}"
    
    mock_service._generate_single_model_non_streaming = AsyncMock(side_effect=mock_generate)

    with patch('bot.handlers.ask_selected_handler.storage_manager.get_thread_history', new_callable=AsyncMock) as mock_get_history, \
         patch('bot.handlers.ask_selected_handler.storage_manager.save_message', new_callable=AsyncMock), \
         patch('bot.handlers.ask_selected_handler.send_safe_message', new_callable=AsyncMock) as mock_send_safe, \
         patch('bot.handlers.ask_selected_handler.providers.get_service_for_provider') as mock_get_provider:
        
        mock_get_history.return_value = []
        mock_get_provider.return_value = mock_service
        
        # ACT
        await ask_selected_handler.done_selecting_callback(mock_update, mock_context)
        
        # ASSERT
        
        # 1. Verify Chairman Synthesis was requested
        # The service should be called with the Chairman's ID ('modelA') and a prompt containing "Synthesis"
        calls = mock_service._generate_single_model_non_streaming.call_args_list
        
        # We expect 3 calls: Member A, Member B, and Chairman Synthesis
        assert len(calls) == 3
        
        # Verify Synthesis Call
        synthesis_call = [c for c in calls if "Synthesis" in c.args[1]]
        assert len(synthesis_call) == 1
        assert synthesis_call[0].args[0] == 'modelA' # Chairman ID
        
        # 2. Verify Output contains Synthesis
        args, _ = mock_send_safe.call_args
        final_message = args[2]
        assert "🏛️ **Chairman Synthesis** (`Chairman Model`)" in final_message
        assert "Synthesis by modelA" in final_message
        assert "Response from modelB" in final_message


@pytest.mark.asyncio
async def test_wait_for_prompt_transition(mock_update_context):
    """
    Test that if no prompt is provided, we transition to WAIT_FOR_PROMPT.
    """
    mock_update, mock_context = mock_update_context
    
    # Setup User Data with NO PROMPT
    mock_context.user_data['ask_selected_models'] = ['p:m']
    mock_context.user_data['ask_selected_models_set'] = {'p:m'}
    mock_context.user_data['ask_selected_prompt'] = None 
    
    # ACT
    result = await ask_selected_handler.done_selecting_callback(mock_update, mock_context)
    
    # ASSERT
    assert result == ask_selected_handler.WAIT_FOR_PROMPT
    mock_update.callback_query.edit_message_text.assert_awaited_with("Please enter your prompt now:")

@pytest.mark.asyncio
async def test_wait_for_prompt_callback_execution(mock_update_context):
    """
    Test that entering a prompt triggers the execution flow.
    """
    mock_update, mock_context = mock_update_context
    
    # Mock a text message update (not callback query)
    mock_update.callback_query = None
    mock_update.message = MagicMock(spec=Message)
    mock_update.message.text = "Delayed Prompt"
    mock_update.message.reply_text = AsyncMock() # Used for status message
    
    # Setup User Data
    mock_context.user_data['ask_selected_models'] = ['p:m']
    mock_context.user_data['ask_selected_models_set'] = {'p:m'}
    mock_context.user_data['model_metadata'] = {'p:m': {'provider':'p', 'actual_id':'m', 'display':'M'}}
    
    # Patch internals to avoid full execution logic if we just want to verify flow, 
    # but here let's patch the dependencies to verify it runs through.
    # Patch internals
    # We need to ensure storage methods are awaitable
    with patch('bot.handlers.ask_selected_handler.storage_manager') as mock_storage, \
         patch('bot.handlers.ask_selected_handler.providers.get_service_for_provider') as mock_get_provider, \
         patch('bot.handlers.ask_selected_handler.send_safe_message', new_callable=AsyncMock):

        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.save_message = AsyncMock()
             
        mock_service = AsyncMock()
        mock_service._generate_single_model_non_streaming = AsyncMock(return_value="Response")
        mock_get_provider.return_value = mock_service
        
        # ACT
        result = await ask_selected_handler.wait_for_prompt_callback(mock_update, mock_context)
        
        # ASSERT
        assert result == ConversationHandler.END
        # Prompt is cleaned up, so we can't check user_data['ask_selected_prompt'] directly.
        # Instead, Verify service was called with the prompt "Delayed Prompt"
        # We look for the call that is NOT the synthesis call.
        # Actually any call with "Delayed Prompt" proves it was passed through.
        service_input_args = mock_service._generate_single_model_non_streaming.call_args_list[0].args
        assert service_input_args[1] == "Delayed Prompt"

        # Verify status message was sent (since it's a message handler, it replies)
        mock_update.message.reply_text.assert_awaited_with("Council is deliberating... 🏛️")
