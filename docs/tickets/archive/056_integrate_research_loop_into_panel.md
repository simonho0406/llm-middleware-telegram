# TICKET-056: Integrate Conversational Research Loop into Panel Workflow (Recovery Plan)

**Status:** Blocked
**Priority:** CRITICAL

## Diagnosis

The developer is blocked due to a critical failure in the test's mock setup. An `AsyncMock` for `storage.storage_manager.get_user_setting` has an insufficiently configured `side_effect`, causing a `StopAsyncIteration` exception that silently aborts the test before any application logic or logging is executed.

This has rendered all debugging attempts futile. The following is a methodical recovery plan to restore a working test environment and complete the ticket.

## Part 1: Restore the Test Environment

First, we must fix the test so we can get visibility into the application logic.

1.  **Simplify the Failing Test:**
    *   In `tests/test_handler_integration.py`, completely **delete** the existing `test_panel_workflow_with_advanced_search` function.
    *   Replace it with the following **simplified** test. This version removes the problematic mock and focuses only on the initial part of the workflow, allowing us to verify that logging works.

    ```python
    import logging

    @pytest.mark.asyncio
    async def test_panel_workflow_logging_and_initial_plan(caplog):
        """
        A simplified test to verify that logging is working and the initial
        orchestrator plan is correctly parsed.
        """
        # 1. Arrange: Set up mocks
        caplog.set_level(logging.INFO)
        update = MagicMock()
        context = MagicMock()
        placeholder_msg = AsyncMock()
        chat_id = 12345
        user_prompt = "Test prompt"

        # Mock for get_robust_llm_response
        mock_llm_response = {
            'response': '{"requires_search": true, "search_query": "test query", "tasks": [{"role": "Proposer", "prompt": "p"}, {"role": "Critic", "prompt": "c"}]}',
            'retries': 0,
            'fallback_used': False
        }
        
        # Mocks for config and storage
        # This time, we use return_value which works for all calls.
        mock_storage_get = AsyncMock(return_value=None) # No custom config
        mock_config_get = MagicMock(return_value={
            'orchestrator': {'provider': 'test', 'model': 'test'},
            'roles': {
                'Proposer': {'provider': 'test', 'model': 'test'},
                'Critic': {'provider': 'test', 'model': 'test'}
            }
        })

        # 2. Act: Run the workflow within the patch context
        from bot.handlers.discuss_panel_handler import _run_panel_workflow

        with patch('bot.handlers.discuss_panel_handler.get_robust_llm_response', AsyncMock(return_value=mock_llm_response)), \
             patch('bot.handlers.configure_panel_handler.storage_manager.get_user_setting', mock_storage_get), \
             patch('config.get_expert_panel_config', mock_config_get):
            
            await _run_panel_workflow(update, context, user_prompt, [], placeholder_msg, chat_id)

        # 3. Assert: Check that our logs are now visible
        assert "Successfully parsed orchestrator's plan." in caplog.text, \
            "The log message confirming plan parsing was not found. The workflow did not execute as expected."
    ```

2.  **Run the Simplified Test:**
    *   Execute `pytest tests/test_handler_integration.py`.
    *   This test **must pass**. If it passes, it proves that the logging system is now working and the initial part of the workflow is reachable.

## Part 2: Implement the Feature Correctly

Now that we have a working, visible test environment, implement the full feature.

3.  **Replace the Test Again:**
    *   Delete the simplified test from the previous step.
    *   Replace it with the **correct, final version** of the integration test below. This version has the correct mocks.

    ```python
    @pytest.mark.asyncio
    async def test_panel_workflow_with_advanced_search(caplog):
        """
        Integration test for the full Conversational Research workflow.
        It mocks all external calls to verify the internal logic of the research loop.
        """
        # 1. Arrange: Mock all necessary components
        caplog.set_level(logging.INFO)
        update = MagicMock()
        context = MagicMock()
        placeholder_msg = AsyncMock()
        chat_id = 12345
        user_prompt = "What is the 'halting problem' in computer science?"

        # CORRECTED MOCK: Use return_value to handle all calls to get_user_setting
        mock_get_setting = AsyncMock(return_value=True)

        # Mock the LLM calls
        mock_llm_call = AsyncMock(side_effect=[
            {'response': '{"requires_search": true, "search_query": "halting problem", "tasks": [{"role": "Proposer", "prompt": "p"}, {"role": "Critic", "prompt": "c"}]}', 'retries': 0, 'fallback_used': False},
            {'response': '["implications of halting problem", "Turing machines"]', 'retries': 0, 'fallback_used': False},
            {'response': 'Proposer response.', 'retries': 0, 'fallback_used': False},
            {'response': 'Critic response.', 'retries': 0, 'fallback_used': False},
            {'response': '{"quality_score": 95}', 'retries': 0, 'fallback_used': False},
        ])

        # Mock search services
        mock_tavily_search = AsyncMock(return_value={'status': 'success', 'content': 'Tavily summary.'})
        mock_google_search = AsyncMock(return_value={"implications": "Implication results.", "Turing": "Turing results."})))

        # Mock config
        mock_config_get = MagicMock(return_value={
            'orchestrator': {'provider': 'test', 'model': 'test'},
            'roles': { 'Proposer': {'provider': 'test', 'model': 'test'}, 'Critic': {'provider': 'test', 'model': 'test'} }
        })

        # 2. Act: Run the workflow
        from bot.handlers.discuss_panel_handler import _run_panel_workflow
        with patch('storage.storage_manager.get_user_setting', mock_get_setting), \
             patch('bot.handlers.discuss_panel_handler.get_robust_llm_response', mock_llm_call), \
             patch('services.web_search_service.perform_search', mock_tavily_search), \
             patch('services.web_search_service.execute_parallel_google_searches', mock_google_search), \
             patch('config.get_expert_panel_config', mock_config_get):

            await _run_panel_workflow(update, context, user_prompt, [], placeholder_msg, chat_id)

        # 3. Assert: Check that the Proposer received the full Research Dossier
        proposer_call = None
        for call in mock_llm_call.call_args_list:
            if call.kwargs.get('role_name') == 'Proposer':
                proposer_call = call
                break
        
        assert proposer_call is not None, "Proposer was never called."
        proposer_prompt = proposer_call.kwargs.get('prompt')
        
        assert "--- RESEARCH DOSSIER ---" in proposer_prompt
        assert "Tavily summary." in proposer_prompt
        assert "Implication results." in proposer_prompt
        assert "Turing results." in proposer_prompt
        assert "Successfully created research dossier." in caplog.text
    ```

4.  **Fix the Application Logic:**
    *   Run the final test. It will fail.
    *   The failure is now visible and debuggable. The error is in `_run_panel_workflow` in `discuss_panel_handler.py`. The logic for checking `advanced_search_enabled` and augmenting the Proposer's prompt is flawed.
    *   Debug the `_run_panel_workflow` function until the test from step 3 passes.

5.  **Final Verification:**
    *   Run the full test suite (`pytest`) to ensure no regressions.
