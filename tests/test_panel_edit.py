import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, Message
from telegram.ext import ContextTypes
from bot.handlers import discuss_panel_handler, chat

@pytest.mark.asyncio
async def test_panel_edit_flow():
    """
    Verifies that editing a message during a panel discussion:
    1. Cancels the running task (if any).
    2. Updates the transcript.
    3. Restarts the workflow.
    """
    # Setup mocks
    mock_update = MagicMock(spec=Update)
    mock_context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 67890
    
    # Mock edited message
    mock_edited_message = MagicMock(spec=Message)
    mock_edited_message.text = "Corrected follow-up"
    mock_edited_message.message_id = 999
    mock_update.edited_message = mock_edited_message
    mock_update.message = None # It's an edit
    
    # Setup panel state
    mock_context.user_data = {
        'panel_state': {
            'full_transcript': [
                {'role': 'user', 'content': 'Original follow-up', 'message_id': 999},
                {'role': 'assistant', 'content': 'Old response'} 
            ],
            'status': 'AWAITING_FOLLOW_UP' # Or PANEL_IN_PROGRESS
        }
    }
    
    # Mock running task
    class AwaitableMock(MagicMock):
        def __await__(self):
            yield from iter([])
            
    mock_task = AwaitableMock()
    mock_task.done.return_value = False
    mock_context.user_data['panel_task'] = mock_task
    
    # Mock placeholder
    mock_placeholder = AsyncMock()
    mock_context.user_data['panel_placeholder'] = mock_placeholder

    # Mock _run_panel_workflow
    with patch('bot.handlers.discuss_panel_handler._run_panel_workflow', new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ({}, "Final Answer", [])
        
        # Call handle_edited_message (which should delegate to handle_panel_edit)
        # We need to patch chat.handle_edited_message to CALL discuss_panel_handler.handle_panel_edit
        # But we are testing the integration.
        # First, let's verify discuss_panel_handler.handle_panel_edit logic directly.
        # Mock send_safe_message to avoid AST errors
        with patch('bot.handlers.discuss_panel_handler.send_safe_message', new_callable=AsyncMock) as mock_send:
            if hasattr(discuss_panel_handler, 'handle_panel_edit'):
                await discuss_panel_handler.handle_panel_edit(mock_update, mock_context)
    
                # Await the new background task
                new_task = mock_context.user_data['panel_task']
                # The new task is a real asyncio.Task because handle_panel_edit calls asyncio.create_task
                # But wait, mock_context.user_data['panel_task'] was initially our mock.
                # handle_panel_edit overwrites it with the new task.
                if new_task != mock_task:
                    await new_task
    
                # 1. Verify task cancellation (of the OLD task)
                mock_task.cancel.assert_called_once()
    
                # 2. Verify transcript update
                transcript = mock_context.user_data['panel_state']['full_transcript']
                # Should have truncated the assistant response, updated user message, AND appended new response
                assert len(transcript) == 2
                assert transcript[0]['content'] == "Corrected follow-up"
                assert transcript[1]['content'] == "Final Answer"
    
                # 3. Verify workflow restart
                mock_run.assert_called_once()
                args, _ = mock_run.call_args
                assert args[2] == "Corrected follow-up" # user_prompt
            else:
                pytest.fail("handle_panel_edit not implemented yet")
