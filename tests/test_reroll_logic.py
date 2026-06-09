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
    
    # Shared Mocks
    mock_get_thread_id = AsyncMock(return_value="thread_1")
    mock_get_key = AsyncMock(side_effect=lambda cid, key, default=None: "Tell me a joke" if key == 'last_user_prompt' else "mock_provider")
    mock_get_history = AsyncMock(return_value=list(initial_history))
    mock_save_msg = AsyncMock()
    mock_remove_last = AsyncMock()
    
    with patch('storage.storage_manager.get_current_thread_id', mock_get_thread_id), \
         patch('storage.storage_manager.get_thread_key', mock_get_key), \
         patch('storage.storage_manager.get_thread_history', mock_get_history), \
         patch('storage.storage_manager.save_message', mock_save_msg), \
         patch('storage.storage_manager.remove_last_assistant_message', mock_remove_last), \
         patch('storage.storage_manager.remove_last_assistant_message', mock_remove_last), \
         patch('bot.handlers.misc_commands.storage_manager.get_current_thread_id', mock_get_thread_id), \
         patch('bot.handlers.misc_commands.storage_manager.get_thread_key', mock_get_key), \
         patch('bot.handlers.misc_commands.storage_manager.delete_messages', AsyncMock()), \
         patch('bot.response_generator.storage_manager.get_thread_history', mock_get_history), \
         patch('bot.response_generator.storage_manager.save_message', mock_save_msg), \
         patch('bot.response_generator.storage_manager.remove_last_assistant_message', mock_remove_last), \
         patch('bot.response_generator.send_safe_message', new_callable=AsyncMock) as mock_send_msg, \
         patch('bot.response_generator.providers.get_provider_details') as mock_get_providers, \
         patch('bot.response_generator.config.get_enable_streaming', return_value=False), \
         patch('bot.response_generator.storage_manager.get_user_setting', AsyncMock(return_value=False)):

        # Mock Provider Service
        mock_service = MagicMock()
        mock_service.generate_response.return_value = async_iter(["To get to the other side!"])
        mock_get_providers.return_value = {
            "mock_provider": {"service": mock_service, "default_model": "mock_model"},
            "nvidia": {"service": mock_service, "default_model": "mock_model"},
            "ollama": {"service": mock_service, "default_model": "mock_model"}
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
    
    # Shared Mocks
    mock_get_thread_id = AsyncMock(return_value="thread_1")
    mock_get_key = AsyncMock(side_effect=lambda cid, key, default=None: "Calc 1/0" if key == 'last_user_prompt' else "mock_provider")
    mock_get_history = AsyncMock(return_value=list(initial_history))
    mock_save_msg = AsyncMock()
    mock_remove_last = AsyncMock()
    
    with patch('storage.storage_manager.get_current_thread_id', mock_get_thread_id), \
         patch('storage.storage_manager.get_thread_key', mock_get_key), \
         patch('storage.storage_manager.get_thread_history', mock_get_history), \
         patch('storage.storage_manager.save_message', mock_save_msg), \
         patch('storage.storage_manager.remove_last_assistant_message', mock_remove_last), \
         patch('storage.storage_manager.remove_last_assistant_message', mock_remove_last), \
         patch('bot.handlers.misc_commands.storage_manager.get_current_thread_id', mock_get_thread_id), \
         patch('bot.handlers.misc_commands.storage_manager.get_thread_key', mock_get_key), \
         patch('bot.handlers.misc_commands.storage_manager.delete_messages', AsyncMock()), \
         patch('bot.response_generator.storage_manager.get_thread_history', mock_get_history), \
         patch('bot.response_generator.storage_manager.save_message', mock_save_msg), \
         patch('bot.response_generator.storage_manager.remove_last_assistant_message', mock_remove_last), \
         patch('bot.response_generator.send_safe_message', new_callable=AsyncMock) as mock_send_msg, \
         patch('bot.response_generator.providers.get_provider_details') as mock_get_providers, \
         patch('bot.response_generator.config.get_enable_streaming', return_value=False), \
         patch('bot.response_generator.storage_manager.get_user_setting', AsyncMock(return_value=False)):

        mock_service = MagicMock()
        mock_service.generate_response.return_value = async_iter(["Infinity!"])
        mock_get_providers.return_value = {
             "mock_provider": {"service": mock_service, "default_model": "mock_model"},
             "nvidia": {"service": mock_service, "default_model": "mock_model"},
             "ollama": {"service": mock_service, "default_model": "mock_model"}
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
         patch('bot.handlers.discuss_panel_handler.send_safe_message', new_callable=AsyncMock) as mock_send_msg, \
         patch('bot.handlers.discuss_panel_handler.storage_manager.save_message', new_callable=AsyncMock) as mock_save_msg:
        
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
         patch('bot.handlers.discuss_panel_handler.send_plain_message', new_callable=AsyncMock) as mock_send_plain, \
         patch('bot.handlers.discuss_panel_handler.storage_manager.save_message', new_callable=AsyncMock) as mock_save:
        
        await discuss_panel_handler.timeout_handler(mock_update, mock_context)
        
        # Verify: cleanup called
        assert mock_cleanup.called
        # Verify: User notified
        mock_send_plain.assert_called_with(mock_context, chat_id, "Panel discussion has timed out due to inactivity.")
        # Note: We do NOT expect save_message here anymore as it's handled upstream (incremental save)

@pytest.mark.asyncio
async def test_panel_reroll_db_cleanup(mock_update_context):
    """
    Story: User rerolls a panel outcome.
    Expectation: The OLD outcome must be removed from the database before the new one is generated.
    """
    mock_update, mock_context = mock_update_context
    chat_id = mock_update.effective_chat.id
    
    # Setup Panel State
    import asyncio
    mock_context.user_data['panel_state'] = {
        "full_transcript": [
            {"role": "user", "content": "Explain Quantum"},
            {"role": "assistant", "content": "Old Answer"}
        ],
        "original_prompt": "Explain Quantum",
        "lock": asyncio.Lock()
    }
    
    with patch('bot.handlers.discuss_panel_handler._run_panel_workflow', new_callable=AsyncMock) as mock_run_panel, \
         patch('bot.handlers.discuss_panel_handler.storage_manager.remove_last_assistant_message', new_callable=AsyncMock) as mock_remove_last, \
         patch('bot.handlers.discuss_panel_handler.storage_manager.save_message', new_callable=AsyncMock) as mock_save_msg, \
         patch('bot.handlers.discuss_panel_handler.send_safe_message', new_callable=AsyncMock):
        
        mock_run_panel.return_value = ({}, "New Answer", "Proposer Content")
        
        await discuss_panel_handler.reroll_discussion(mock_update, mock_context)
        
        # Verify db cleanup
        assert mock_remove_last.called, "Must remove old assistant message from DB before rerolling"
        mock_remove_last.assert_called_with(chat_id)

@pytest.mark.asyncio
async def test_panel_cancel_cleanup(mock_update_context):
    """
    Story: User cancels panel, or panel times out.
    Expectation: The placeholder message (spinner) should be edited to 'Cancelled' to avoid stuck UI.
    """
    mock_update, mock_context = mock_update_context
    chat_id = 12345
    
    # Setup context with a placeholder in user_data
    mock_placeholder = AsyncMock()
    mock_context.user_data['panel_placeholder'] = mock_placeholder
    
    mock_panel_task = MagicMock() # Use MagicMock for synchronous methods like done()
    mock_panel_task.done.return_value = False
    mock_panel_task.cancel = MagicMock()
    # To be awaitable (for `await panel_task`), it needs __await__ or be compatible
    # But since we use MagicMock, we might need to make it awaitable if the code awaits it.
    # The code does `await panel_task`.
    # Let's use AsyncMock but configure done() as a property or sync method?
    # Easier: Just make done() return False on a MagicMock that has __await__.
    
    # Better approach:
    mock_panel_task = AsyncMock()
    mock_panel_task.done = MagicMock(return_value=False) 
    mock_panel_task.cancel = MagicMock()
    
    mock_context.user_data['panel_task'] = mock_panel_task
    
    # Execute cleanup WITHOUT passing placeholder explicitly (simulating /cancel command)
    await discuss_panel_handler._cleanup_discussion_state(mock_context, chat_id)
    
    # Verify placeholder was edited
    assert mock_placeholder.edit_text.called
    assert "Discussion cancelled" in mock_placeholder.edit_text.call_args[0][0]
    
    # Verify cleanup
    assert 'panel_placeholder' not in mock_context.user_data
