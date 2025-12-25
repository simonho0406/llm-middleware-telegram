import pytest
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from telegram import Update, Message, Chat, User, constants
from telegram.ext import ContextTypes, ConversationHandler

# Import the handlers we are testing
from bot.handlers import misc_commands
from bot.handlers import discuss_panel_handler
from bot import response_generator

@pytest.fixture
def mock_update_context():
    mock_update = MagicMock(spec=Update)
    mock_context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    mock_user = MagicMock(spec=User)
    mock_chat = MagicMock(spec=Chat)
    mock_message = MagicMock(spec=Message)

    chat_id = 12345
    user_id = 67890

    mock_user.id = user_id
    mock_user.username = "testuser"
    mock_chat.id = chat_id
    mock_chat.type = "private"

    mock_message.chat = mock_chat
    mock_message.from_user = mock_user
    mock_message.message_id = 100
    mock_message.reply_text = AsyncMock()
    mock_message.edit_text = AsyncMock()

    mock_update.effective_user = mock_user
    mock_update.effective_chat = mock_chat
    mock_update.message = mock_message
    mock_update.effective_message = mock_message
    mock_update.callback_query = None

    mock_context.chat_data = {}
    mock_context.user_data = {}
    mock_context.bot.send_message = AsyncMock()
    
    return mock_update, mock_context

# Helper for async iteration
async def async_iter(items):
    for item in items:
        yield item

@pytest.mark.asyncio
async def test_normal_chat_reroll_dislike(mock_update_context):
    """
    Story 1: User does not like reply in normal chat.
    Expectation: Last assistant message is removed, new one is generated and appended.
    """
    mock_update, mock_context = mock_update_context
    chat_id = mock_update.effective_chat.id
    
    # Setup initial history: [User: A, Assistant: B]
    initial_history = [
        {'role': 'user', 'content': 'Tell me a joke'},
        {'role': 'assistant', 'content': 'Why did the chicken cross the road?'}
    ]
    
    with patch('bot.handlers.misc_commands.storage_manager.get_current_thread_id', new_callable=AsyncMock) as mock_get_thread_id, \
         patch('bot.handlers.misc_commands.storage_manager.get_thread_key', new_callable=AsyncMock) as mock_get_key, \
         patch('bot.response_generator.storage_manager.get_thread_history', new_callable=AsyncMock) as mock_get_history, \
         patch('bot.response_generator.storage_manager.save_message', new_callable=AsyncMock) as mock_save_msg, \
         patch('bot.response_generator.storage_manager.remove_last_assistant_message', new_callable=AsyncMock) as mock_remove_last, \
         patch('bot.response_generator.send_safe_message', new_callable=AsyncMock) as mock_send_msg, \
         patch('bot.response_generator.providers.get_provider_details') as mock_get_providers:

        mock_get_thread_id.return_value = "thread_1"
        mock_get_key.side_effect = lambda cid, key, default=None: "Tell me a joke" if key == 'last_user_prompt' else "mock_provider"
        mock_get_history.return_value = list(initial_history) # Return a copy
        
        # Mock Provider Service
        mock_service = MagicMock()
        mock_service.generate_response.return_value = async_iter(["To get to the other side!"])
        mock_get_providers.return_value = {
            "mock_provider": {"service": mock_service, "default_model": "mock_model"}
        }
        
        # Execute /reroll
        await misc_commands.reroll_command(mock_update, mock_context)
        
        # Verify:
        # 1. remove_last_assistant_message called (to delete old answer)
        assert mock_remove_last.called, "Should verify removal of old assistant message"
        
        # 2. save_message called (to append new answer)
        assert mock_save_msg.called
        assert mock_save_msg.call_args[0][2] == 'To get to the other side!'

@pytest.mark.asyncio
async def test_normal_chat_reroll_error(mock_update_context):
    """
    Story 2: Some error happens in normal chat.
    Expectation: Error message is removed (if it was saved), new response generated.
    """
    mock_update, mock_context = mock_update_context
    
    # Setup history with error: [User: A, Assistant: [Error...]]
    initial_history = [
        {'role': 'user', 'content': 'Calc 1/0'},
        {'role': 'assistant', 'content': '[Error: Division by zero]'}
    ]
    
    with patch('bot.handlers.misc_commands.storage_manager.get_current_thread_id', new_callable=AsyncMock) as mock_get_thread_id, \
         patch('bot.handlers.misc_commands.storage_manager.get_thread_key', new_callable=AsyncMock) as mock_get_key, \
         patch('bot.response_generator.storage_manager.get_thread_history', new_callable=AsyncMock) as mock_get_history, \
         patch('bot.response_generator.storage_manager.save_message', new_callable=AsyncMock) as mock_save_msg, \
         patch('bot.response_generator.storage_manager.remove_last_assistant_message', new_callable=AsyncMock) as mock_remove_last, \
         patch('bot.response_generator.send_safe_message', new_callable=AsyncMock) as mock_send_msg, \
         patch('bot.response_generator.providers.get_provider_details') as mock_get_providers:

        mock_get_thread_id.return_value = "thread_1"
        mock_get_key.side_effect = lambda cid, key, default=None: "Calc 1/0" if key == 'last_user_prompt' else "mock_provider"
        mock_get_history.return_value = list(initial_history)
        
        mock_service = MagicMock()
        mock_service.generate_response.return_value = async_iter(["Infinity!"])
        mock_get_providers.return_value = {
            "mock_provider": {"service": mock_service, "default_model": "mock_model"}
        }
        
        await misc_commands.reroll_command(mock_update, mock_context)
        
        assert mock_remove_last.called
        assert mock_save_msg.called
        assert mock_save_msg.call_args[0][2] == 'Infinity!'

@pytest.mark.asyncio
async def test_panel_reroll_success(mock_update_context):
    """
    Story 4: User rerolls a panel outcome.
    Expectation: Panel state is updated with new result.
    """
    mock_update, mock_context = mock_update_context
    chat_id = mock_update.effective_chat.id
    
    # Setup Panel State
    import asyncio
    mock_context.user_data['panel_state'] = {
        "full_transcript": [
            {"role": "user", "content": "Explain Quantum"},
            {"role": "assistant", "content": "It is complex."}
        ],
        "original_prompt": "Explain Quantum",
        "lock": asyncio.Lock()
    }
    
    with patch('bot.handlers.discuss_panel_handler._run_panel_workflow', new_callable=AsyncMock) as mock_run_panel, \
         patch('bot.handlers.discuss_panel_handler.send_safe_message', new_callable=AsyncMock) as mock_send_msg:
        
        # Mock successful panel run
        mock_run_panel.return_value = (
            {"Proposer": {"status": "Success", "content": "A"}}, 
            "It is very complex.", 
            "A"
        )
        
        await discuss_panel_handler.reroll_discussion(mock_update, mock_context)
        
        # Verify state update
        transcript = mock_context.user_data['panel_state']['full_transcript']
        assert len(transcript) == 2
        assert transcript[-1]['content'] == "It is very complex."
        assert transcript[-1]['content'] != "It is complex."

@pytest.mark.asyncio
async def test_panel_reroll_error_session_termination(mock_update_context):
    """
    Story 5: User rerolls after a panel error (or error during reroll).
    Current Behavior Check: Does it terminate the session?
    Fixed Behavior: Should NOT terminate session.
    """
    mock_update, mock_context = mock_update_context
    chat_id = mock_update.effective_chat.id
    
    # Setup Panel State
    import asyncio
    mock_context.user_data['panel_state'] = {
        "full_transcript": [
            {"role": "user", "content": "Explain Quantum"},
            {"role": "assistant", "content": "It is complex."}
        ],
        "original_prompt": "Explain Quantum",
        "lock": asyncio.Lock()
    }
    
    with patch('bot.handlers.discuss_panel_handler._run_panel_workflow', new_callable=AsyncMock) as mock_run_panel, \
         patch('bot.handlers.discuss_panel_handler._cleanup_discussion_state', new_callable=AsyncMock) as mock_cleanup:
        
        # Mock FAILED panel run
        mock_run_panel.side_effect = Exception("Simulated Panel Error")
        
        result = await discuss_panel_handler.reroll_discussion(mock_update, mock_context)
        
        # Verify behavior
        assert result == discuss_panel_handler.AWAITING_FOLLOW_UP # Fixed behavior: Keeps session alive
        assert not mock_cleanup.called # Fixed behavior: Does NOT clean up state

@pytest.mark.asyncio
async def test_panel_timeout_data_loss(mock_update_context):
    """
    Story: Panel timeout.
    Current Behavior Check: Is data saved?
    Fixed Behavior: Data SHOULD be saved.
    """
    mock_update, mock_context = mock_update_context
    chat_id = 12345
    mock_context.job.chat_id = chat_id
    
    # Setup Panel State with some valuable data
    mock_context.user_data['panel_state'] = {
        "final_answer": "Valuable Insight",
        "full_transcript": []
    }
    
    with patch('bot.handlers.discuss_panel_handler._cleanup_discussion_state', new_callable=AsyncMock) as mock_cleanup, \
         patch('bot.handlers.discuss_panel_handler.storage_manager.save_message', new_callable=AsyncMock) as mock_save:
        
        await discuss_panel_handler.timeout_handler(mock_update, mock_context)
        
        # Verify: Was save_message called?
        assert mock_save.called # Fixed behavior: Data is saved
        assert mock_cleanup.called
