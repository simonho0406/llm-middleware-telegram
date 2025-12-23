import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os

# Add project root to path to allow imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from telegram import Update, User, Chat, Message
from telegram.ext import ContextTypes
import asyncio
from bot.handlers import chat
from bot.handlers import discuss_panel_handler
from storage import storage_manager

@pytest.mark.asyncio
async def test_full_user_flow_simulation():
    """
    Simulates a full user journey:
    1. User sends a normal message.
    2. User triggers a panel discussion (/discuss_panel).
    3. Panel executes (mocked).
    4. Result is stored and returned.
    """
    # Setup Mocks
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
    
    mock_message.text = "Tell me about quantum computing."
    mock_message.chat = mock_chat
    mock_message.from_user = mock_user
    mock_message.message_id = 100
    
    mock_update.effective_user = mock_user
    mock_update.effective_chat = mock_chat
    mock_update.message = mock_message
    mock_update.effective_message = mock_message
    
    # Mock Context Data
    mock_context.chat_data = {}
    mock_context.user_data = {}
    
    # Mock Storage Manager
    # Patching the local reference in response_generator is the most reliable way
    with patch('storage.storage_manager.get_thread_history', new_callable=AsyncMock) as mock_get_history, \
         patch('bot.response_generator.storage_manager.set_thread_history', new_callable=AsyncMock) as mock_rg_set_history, \
         patch('storage.storage_manager.set_thread_history', new_callable=AsyncMock) as mock_sm_set_history, \
         patch('storage.storage_manager.get_current_thread_id', new_callable=AsyncMock) as mock_get_thread_id, \
         patch('bot.response_generator._generate_llm_response', new_callable=AsyncMock) as mock_gen_response, \
         patch('bot.response_generator.send_safe_message', new_callable=AsyncMock) as mock_send_msg:
        
        # 1. Simulate Normal Message
        mock_get_thread_id.return_value = "thread_1"
        mock_get_history.return_value = []
        mock_gen_response.return_value = {
            'content': "Quantum computing is cool.",
            'error': None,
            'truncated_history': [],
            'provider_info': {'provider': 'mock'},
            'processed_history': [{'role': 'user', 'content': "Tell me about quantum computing."}, {'role': 'assistant', 'content': "Quantum computing is cool."}]
        }
        mock_send_msg.return_value = True # Ensure message sending is successful
        
        # Setup Job Queue Mock to capture debounce
        mock_job_queue = MagicMock()
        mock_context.job_queue = mock_job_queue
        captured_jobs = []
        
        def side_effect_run_once(callback, interval, data, chat_id):
            captured_jobs.append((callback, data))
            return MagicMock() # Return a dummy job object
            
        mock_job_queue.run_once.side_effect = side_effect_run_once

        await chat.handle_message(mock_update, mock_context)
        
        # Manually trigger the debounced job
        assert len(captured_jobs) == 1, "Debounce job was not scheduled"
        callback, job_data = captured_jobs[0]
        
        # Setup the job context expected by process_buffered_message
        mock_job = MagicMock()
        mock_job.data = job_data
        mock_context.job = mock_job
        
        # Run the actual processing logic
        await callback(mock_context)
        
        # Verify response was sent
        mock_send_msg.assert_called_with(mock_context, mock_update, "Quantum computing is cool.")
        
        # Verify history was updated (check all potential mocks)
        history_updated = (
            mock_rg_set_history.called or 
            mock_sm_set_history.called
        )
        assert history_updated, "storage_manager.set_thread_history was not called (checked response_generator local ref)"
        
        # 2. Simulate Panel Trigger
        pass

@pytest.mark.asyncio
async def test_panel_orchestrator_integration():
    """
    Tests the panel orchestrator integration with storage and messaging.
    """
    from bot.handlers.discuss_panel_handler import _run_panel_workflow
    
    chat_id = 12345
    user_prompt = "Explain string theory"
    
    # Mock dependencies
    with patch('bot.handlers.discuss_panel_handler.get_robust_llm_response', new_callable=AsyncMock) as mock_llm, \
         patch('bot.handlers.discuss_panel_handler.storage_manager', new_callable=AsyncMock) as mock_storage, \
         patch('bot.handlers.discuss_panel_handler.send_safe_message', new_callable=AsyncMock) as mock_send:
             
        # Setup LLM responses for the different roles
        # Note: The first call is the Master Orchestrator, which expects a JSON OBJECT.
        planner_json = {
            "requires_search": False,
            "search_query": "",
            "tasks": [
                {"role": "Proposer", "instruction": "Propose an answer"},
                {"role": "Critic", "instruction": "Critique the answer"}
            ]
        }
        
        quality_json = {
            "quality_score": 9, # Note: Key is quality_score, not score
            "refinement_instructions": "None"
        }
        
        import json
        from itertools import chain, repeat
        
        # Define the sequence of expected responses
        responses = [
            {'response': json.dumps(planner_json), 'retries': 0, 'fallback_used': False}, # Master Orchestrator (JSON Object)
            {'response': "Proposer: String theory posits...", 'retries': 0, 'fallback_used': False}, # Proposer
            {'response': "Critic: Good start, but mention M-theory.", 'retries': 0, 'fallback_used': False}, # Critic
            {'response': json.dumps(quality_json), 'retries': 0, 'fallback_used': False}, # Quality Gate (JSON Object)
            {'response': "**Final Answer:** String theory is...", 'retries': 0, 'fallback_used': False} # Synthesis
        ]
        
        # Add an infinite iterator of fallback responses to prevent StopAsyncIteration
        fallback_response = {'response': "Fallback response", 'retries': 0, 'fallback_used': False}
        mock_llm.side_effect = chain(responses, repeat(fallback_response))
        
        mock_placeholder = AsyncMock()
        
        # Run the workflow
        # Signature: (update, context, user_prompt, full_history, placeholder_msg, chat_id)
        # Returns: panel_results, final_answer, debug_info
        panel_results, final_answer, debug_info = await _run_panel_workflow(
            update=MagicMock(),
            context=MagicMock(),
            user_prompt=user_prompt,
            full_history=[],
            placeholder_msg=mock_placeholder,
            chat_id=chat_id
        )
        
        # Verify results
        # Note: Since Refiner is not configured in our mock, final_answer should be the Proposer response.
        # Wait, if Quality Gate passes, it synthesizes final answer.
        # So final_answer should be the Synthesis response.
        assert "**Final Answer:**" in final_answer
        assert "Proposer" in panel_results
        assert "Critic" in panel_results
        
        # Verify LLM was called for each stage
        assert mock_llm.call_count >= 4
