import pytest
import logging
from unittest.mock import MagicMock, AsyncMock, patch

@pytest.mark.asyncio
@pytest.mark.parametrize("advanced_search_enabled, should_run_advanced_search", [
    (True, True),
    (False, False),
])
async def test_panel_workflow_with_advanced_search(caplog, advanced_search_enabled, should_run_advanced_search):
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

    # CORRECTED MOCK: Use a side_effect to handle different settings
    # Mock the LLM calls
    llm_side_effects = [
        {'response': '{"requires_search": true, "search_query": "halting problem", "tasks": [{"role": "Proposer", "prompt": "p"}, {"role": "Critic", "prompt": "c"}]}', 'retries': 0, 'fallback_used': False},
    ]
    if should_run_advanced_search:
        llm_side_effects.extend([
            {'response': '["implications of halting problem", "Turing machines"]', 'retries': 0, 'fallback_used': False},
        ])
    llm_side_effects.extend([
        {'response': 'Proposer response.', 'retries': 0, 'fallback_used': False},
        {'response': 'Critic response.', 'retries': 0, 'fallback_used': False},
        {'response': '{"quality_score": 95}', 'retries': 0, 'fallback_used': False},
        {'response': '{"quality_score": 95}', 'retries': 0, 'fallback_used': False},
        MagicMock(), # Add a mock for the final call
    ])
    mock_llm_call = AsyncMock(side_effect=llm_side_effects)
    # Mock search services
    mock_tavily_search = AsyncMock(return_value={'status': 'success', 'content': 'Tavily summary.'})
    mock_google_search = AsyncMock(return_value={"implications": "Implication results.", "Turing": "Turing results."} )

    # Mock config
    mock_config_get = MagicMock(return_value={
        'orchestrator': {'provider': 'test', 'model': 'test'},
        'roles': { 'Proposer': {'provider': 'test', 'model': 'test'}, 'Critic': {'provider': 'test', 'model': 'test'} }
    })

    # 2. Act: Run the workflow
    from bot.handlers.discuss_panel_handler import _run_panel_workflow
    with patch('bot.handlers.discuss_panel_handler.storage_manager.get_user_setting', side_effect=lambda chat_id, setting_name, default: advanced_search_enabled if setting_name == 'advanced_search_panel' else True), \
         patch('bot.handlers.discuss_panel_handler.get_robust_llm_response', mock_llm_call), \
         patch('services.web_search_service.perform_search', mock_tavily_search), \
         patch('services.web_search_service.execute_parallel_google_searches', mock_google_search), \
         patch('config.get_expert_panel_config', mock_config_get):
        await _run_panel_workflow(update, context, user_prompt, [], placeholder_msg, chat_id)

    # 3. Assert: Check that the Proposer received the correct prompt
    proposer_call = None
    for call in mock_llm_call.call_args_list:
        if call.kwargs.get('role_name') == 'Proposer':
            proposer_call = call
            break
    
    assert proposer_call is not None, "Proposer was never called."
    proposer_prompt = proposer_call.kwargs.get('prompt')

    if should_run_advanced_search:
        assert "--- RESEARCH DOSSIER ---" in proposer_prompt
        assert "Tavily summary." in proposer_prompt
        assert "Implication results." in proposer_prompt
        assert "Turing results." in proposer_prompt
        assert "Successfully created research dossier." in caplog.text
        mock_google_search.assert_called_once()
    else:
        assert "--- RESEARCH DOSSI-ER ---" not in proposer_prompt
        assert "--- WEB SEARCH RESULTS ---" in proposer_prompt
        assert "Tavily summary." in proposer_prompt
        mock_google_search.assert_not_called()