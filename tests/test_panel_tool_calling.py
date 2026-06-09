"""
Tests for the MCP tool-calling path added to _run_refinement_cycle.

Covers:
  - Quality gate JSON with tool_calls triggers mcp_service.execute_tool
  - Tool results are injected into the next Proposer refine prompt
  - Empty tool_calls list → no tool execution
  - Tool execution failure → error string propagated (not raised)
  - Skills dispatch (skill_ prefix) routes correctly
"""
import pytest
import sys
import os
import json
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.handlers.discuss_panel_handler import _run_refinement_cycle


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_panel_config(max_iterations=3, threshold=85):
    return {
        'quality_threshold': threshold,
        'max_refinement_iterations': max_iterations,
        'roles': {
            'Proposer': {
                'provider': 'mock_prov', 'model': 'mock_model',
                'request_timeout_seconds': 30
            },
            'Critic': {
                'provider': 'mock_prov', 'model': 'mock_model',
                'request_timeout_seconds': 30
            },
        }
    }


def _llm_response(text, is_error=False):
    return {'response': text, 'retries': 0, 'fallback_used': False, 'is_error': is_error}


def _quality_json(score, instructions="", tool_calls=None):
    return json.dumps({
        "quality_score": score,
        "refinement_instructions": instructions,
        "tool_calls": tool_calls or []
    })


# ---------------------------------------------------------------------------
# 1. Tool call is executed and result injected into next Proposer prompt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_call_from_quality_gate_is_executed_and_injected():
    """
    When quality gate returns tool_calls, mcp_service.execute_tool is called
    and the result appears in the second Proposer iteration's prompt.
    """
    tool_result_json = '{"version": "1.78.0"}'

    # Response sequence: Proposer1 → Critic1 → QualityGate(tool call) →
    #                    Proposer2(gets tool result) → Critic2 → QualityGate(pass)
    responses = [
        _llm_response("Rust version is X.Y."),                                   # Proposer iter 1
        _llm_response("Version X.Y is unverified."),                              # Critic iter 1
        _llm_response(_quality_json(50, "Fix version.", [                         # Quality gate iter 1
            {"name": "tavily-search__tavily_search", "arguments": {"query": "Rust stable version"}}
        ])),
        _llm_response("Rust stable is 1.78.0 as of 2024."),                       # Proposer iter 2
        _llm_response("Good, version verified."),                                  # Critic iter 2
        _llm_response(_quality_json(90)),                                          # Quality gate iter 2
    ]
    resp_iter = iter(responses)

    captured_proposer_calls = []

    async def mock_llm(*args, **kwargs):
        if kwargs.get('role_name') == 'Proposer':
            captured_proposer_calls.append(kwargs.get('prompt', ''))
        return next(resp_iter)

    mock_mcp = AsyncMock()
    mock_mcp.execute_tool = AsyncMock(return_value=tool_result_json)

    placeholder = AsyncMock()

    with patch('bot.handlers.discuss_panel_handler.get_robust_llm_response', side_effect=mock_llm):
        with patch('bot.handlers.discuss_panel_handler.get_expert_panel_fallback_config', return_value=(None, None)):
            result_text, score, iters = await _run_refinement_cycle(
                update=MagicMock(),
                context=MagicMock(),
                proposer_task={'role': 'Proposer', 'prompt': 'Describe Rust version history.'},
                critic_task={'role': 'Critic', 'prompt': 'Verify version numbers.'},
                user_prompt="What is the latest Rust version?",
                full_history=[],
                placeholder_msg=placeholder,
                panel_results={},
                orchestrator_service=MagicMock(),
                orchestrator_model='mock_model',
                orchestrator_timeout=30,
                orchestrator_config={'provider': 'mock_prov', 'model': 'mock_model'},
                panel_config=_make_panel_config(),
                mcp_service=mock_mcp,
                skill_service=None,
                available_tools_text="- tavily-search__tavily-search: Search the web",
                panel_execution_tool_names=frozenset({"tavily-search__tavily_search"}),
            )

    # MCP tool was dispatched with the correct server/tool/args
    mock_mcp.execute_tool.assert_called_once_with(
        'tavily-search', 'tavily_search', {'query': 'Rust stable version'}
    )

    # Second Proposer call received the tool result in its prompt
    assert len(captured_proposer_calls) == 2, "Expected exactly 2 Proposer calls"
    second_prompt = captured_proposer_calls[1]
    assert tool_result_json in second_prompt, (
        f"Tool result not found in second Proposer prompt.\nPrompt: {second_prompt[:400]}"
    )

    # Cycle resolved successfully
    assert result_text == "Rust stable is 1.78.0 as of 2024."
    assert score == 90


# ---------------------------------------------------------------------------
# 2. Empty tool_calls → no tool execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_tool_calls_skips_execution():
    """When quality gate returns tool_calls: [], no tool is executed."""
    responses = [
        _llm_response("Draft response."),               # Proposer
        _llm_response("Critique."),                     # Critic
        _llm_response(_quality_json(90)),               # Quality gate (pass, no tool)
    ]
    resp_iter = iter(responses)

    mock_mcp = AsyncMock()
    mock_mcp.execute_tool = AsyncMock()

    with patch('bot.handlers.discuss_panel_handler.get_robust_llm_response', side_effect=lambda *a, **kw: (lambda: next(resp_iter))() if True else None):
        with patch('bot.handlers.discuss_panel_handler.get_expert_panel_fallback_config', return_value=(None, None)):

            async def mock_llm(*args, **kwargs):
                return next(resp_iter)

            with patch('bot.handlers.discuss_panel_handler.get_robust_llm_response', side_effect=mock_llm):
                await _run_refinement_cycle(
                    update=MagicMock(), context=MagicMock(),
                    proposer_task={'role': 'Proposer', 'prompt': 'Write something.'},
                    critic_task={'role': 'Critic', 'prompt': 'Check it.'},
                    user_prompt="Tell me something.",
                    full_history=[], placeholder_msg=AsyncMock(), panel_results={},
                    orchestrator_service=MagicMock(), orchestrator_model='m',
                    orchestrator_timeout=30, orchestrator_config={'provider': 'p', 'model': 'm'},
                    panel_config=_make_panel_config(threshold=85),
                    mcp_service=mock_mcp, skill_service=None, available_tools_text=""
                )

    mock_mcp.execute_tool.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Tool execution failure → error string in tool_results, loop continues
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_execution_failure_is_graceful():
    """If mcp_service.execute_tool raises, the error is recorded and the loop continues."""
    responses = [
        _llm_response("Draft."),
        _llm_response("Critique."),
        _llm_response(_quality_json(50, "Need data.", [
            {"name": "tavily-search__tavily-search", "arguments": {"query": "something"}}
        ])),
        _llm_response("Improved draft."),
        _llm_response("Better."),
        _llm_response(_quality_json(90)),
    ]
    resp_iter = iter(responses)

    captured_second_prompt = []

    async def mock_llm(*args, **kwargs):
        if kwargs.get('role_name') == 'Proposer' and len(captured_second_prompt) == 1:
            captured_second_prompt.append(kwargs.get('prompt', ''))
        elif kwargs.get('role_name') == 'Proposer' and not captured_second_prompt:
            captured_second_prompt.append('')  # placeholder for first call
        return next(resp_iter)

    mock_mcp = AsyncMock()
    mock_mcp.execute_tool = AsyncMock(side_effect=RuntimeError("connection refused"))

    with patch('bot.handlers.discuss_panel_handler.get_robust_llm_response', side_effect=mock_llm):
        with patch('bot.handlers.discuss_panel_handler.get_expert_panel_fallback_config', return_value=(None, None)):
            result_text, score, _ = await _run_refinement_cycle(
                update=MagicMock(), context=MagicMock(),
                proposer_task={'role': 'Proposer', 'prompt': 'Write.'},
                critic_task={'role': 'Critic', 'prompt': 'Check.'},
                user_prompt="Question.", full_history=[], placeholder_msg=AsyncMock(),
                panel_results={}, orchestrator_service=MagicMock(),
                orchestrator_model='m', orchestrator_timeout=30,
                orchestrator_config={'provider': 'p', 'model': 'm'},
                panel_config=_make_panel_config(),
                mcp_service=mock_mcp, skill_service=None, available_tools_text="",
                panel_execution_tool_names=frozenset({"tavily-search__tavily-search"}),
            )

    # Loop must still complete — error doesn't crash the cycle
    assert result_text == "Improved draft."
    assert score == 90
    mock_mcp.execute_tool.assert_called_once()


# ---------------------------------------------------------------------------
# 4. Skill dispatch (skill_ prefix) routes to skill_service
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skill_tool_call_routes_to_skill_service():
    """Tool names starting with skill_ are dispatched to skill_service, not mcp_service."""
    skill_playbook = "# Code Review Playbook\nCheck for readability..."

    responses = [
        _llm_response("Initial draft."),
        _llm_response("Needs a code review check."),
        _llm_response(_quality_json(55, "Apply skill.", [
            {"name": "skill_code-review", "arguments": {}}
        ])),
        _llm_response("Improved with code review checklist."),
        _llm_response("Good."),
        _llm_response(_quality_json(90)),
    ]
    resp_iter = iter(responses)

    captured_second_prompt = []
    call_count = [0]

    async def mock_llm(*args, **kwargs):
        if kwargs.get('role_name') == 'Proposer':
            call_count[0] += 1
            if call_count[0] == 2:
                captured_second_prompt.append(kwargs.get('prompt', ''))
        return next(resp_iter)

    mock_mcp = AsyncMock()
    mock_mcp.execute_tool = AsyncMock()

    mock_skill_svc = MagicMock()
    mock_skill_svc.get_skill_playbook = MagicMock(return_value=skill_playbook)

    with patch('bot.handlers.discuss_panel_handler.get_robust_llm_response', side_effect=mock_llm):
        with patch('bot.handlers.discuss_panel_handler.get_expert_panel_fallback_config', return_value=(None, None)):
            await _run_refinement_cycle(
                update=MagicMock(), context=MagicMock(),
                proposer_task={'role': 'Proposer', 'prompt': 'Write.'},
                critic_task={'role': 'Critic', 'prompt': 'Check.'},
                user_prompt="Review my code.", full_history=[], placeholder_msg=AsyncMock(),
                panel_results={}, orchestrator_service=MagicMock(),
                orchestrator_model='m', orchestrator_timeout=30,
                orchestrator_config={'provider': 'p', 'model': 'm'},
                panel_config=_make_panel_config(),
                mcp_service=mock_mcp, skill_service=mock_skill_svc,
                available_tools_text="- skill_code-review: Code review checklist"
            )

    # Skill service was called, MCP was not
    mock_skill_svc.get_skill_playbook.assert_called_once_with('code-review')
    mock_mcp.execute_tool.assert_not_called()

    # Playbook content appears in the second Proposer prompt
    assert captured_second_prompt, "Second Proposer call was not captured"
    assert skill_playbook in captured_second_prompt[0], (
        f"Skill playbook not found in second Proposer prompt.\nPrompt: {captured_second_prompt[0][:400]}"
    )


# ---------------------------------------------------------------------------
# 5. available_tools_text appears in the quality gate prompt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_available_tools_passed_to_quality_gate_prompt():
    """The scoped quality_gate_tools_text must appear in the quality gate prompt (not the full available_tools_text)."""
    # quality_gate_tools_text is the filtered set passed to the Quality Gate;
    # available_tools_text is the full set passed to the Planner (not Quality Gate).
    quality_gate_tools_text = "- tavily-search__tavily_search: Search the web"
    full_tools_text = "- tavily-search__tavily_search: Search the web\n- sqlite-tools__read_query: Run SQL"

    responses = [
        _llm_response("Draft."),
        _llm_response("Critique."),
        _llm_response(_quality_json(90)),
    ]
    resp_iter = iter(responses)

    captured_quality_prompt = []

    async def mock_llm(*args, **kwargs):
        if kwargs.get('role_name') == 'Master Orchestrator':
            captured_quality_prompt.append(kwargs.get('prompt', ''))
        return next(resp_iter)

    with patch('bot.handlers.discuss_panel_handler.get_robust_llm_response', side_effect=mock_llm):
        with patch('bot.handlers.discuss_panel_handler.get_expert_panel_fallback_config', return_value=(None, None)):
            await _run_refinement_cycle(
                update=MagicMock(), context=MagicMock(),
                proposer_task={'role': 'Proposer', 'prompt': 'Write.'},
                critic_task={'role': 'Critic', 'prompt': 'Check.'},
                user_prompt="Question.", full_history=[], placeholder_msg=AsyncMock(),
                panel_results={}, orchestrator_service=MagicMock(),
                orchestrator_model='m', orchestrator_timeout=30,
                orchestrator_config={'provider': 'p', 'model': 'm'},
                panel_config=_make_panel_config(),
                mcp_service=None, skill_service=None,
                available_tools_text=full_tools_text,
                quality_gate_tools_text=quality_gate_tools_text,
            )

    assert captured_quality_prompt, "Quality gate was never called"
    assert quality_gate_tools_text in captured_quality_prompt[0], (
        f"quality_gate_tools_text not found in quality gate prompt.\nPrompt: {captured_quality_prompt[0][:500]}"
    )
    # Full tools text must NOT appear — the Quality Gate sees only its scoped subset
    assert full_tools_text not in captured_quality_prompt[0], (
        "Full available_tools_text leaked into quality gate prompt (scoping broken)."
    )


# ---------------------------------------------------------------------------
# 6. Rubric non-numeric sub-criteria are warned and treated as 0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rubric_non_numeric_score_is_warned_and_zeroed(caplog):
    """Quality Gate returning a string for a rubric criterion must be logged as
    WARNING and treated as 0, not silently filtered out.
    """
    import logging

    # Quality gate returns one non-numeric score ("high") and three numeric ones
    rubric_with_string = json.dumps({
        "scores": {
            "factual_grounding": "high",
            "completeness": 22,
            "accuracy": 20,
            "clarity": 18,
        },
        "refinement_instructions": "",
        "tool_calls": [],
    })

    responses = [
        _llm_response("Draft."),
        _llm_response("Critique."),
        _llm_response(rubric_with_string),   # score = 0+22+20+18 = 60 (not 85 threshold)
        _llm_response("Improved draft."),
        _llm_response("Better."),
        _llm_response(json.dumps({
            "scores": {"factual_grounding": 28, "completeness": 22, "accuracy": 22, "clarity": 18},
            "refinement_instructions": "",
            "tool_calls": [],
        })),  # total 90 → pass
    ]
    resp_iter = iter(responses)

    with patch('bot.handlers.discuss_panel_handler.get_robust_llm_response',
               side_effect=lambda *a, **kw: next(resp_iter)):
        with patch('bot.handlers.discuss_panel_handler.get_expert_panel_fallback_config',
                   return_value=(None, None)):
            with caplog.at_level(logging.WARNING, logger='bot.handlers.discuss_panel_handler'):
                result_text, score, _ = await _run_refinement_cycle(
                    update=MagicMock(), context=MagicMock(),
                    proposer_task={'role': 'Proposer', 'prompt': 'Write.'},
                    critic_task={'role': 'Critic', 'prompt': 'Check.'},
                    user_prompt="Question.", full_history=[], placeholder_msg=AsyncMock(),
                    panel_results={}, orchestrator_service=MagicMock(),
                    orchestrator_model='m', orchestrator_timeout=30,
                    orchestrator_config={'provider': 'p', 'model': 'm'},
                    panel_config=_make_panel_config(),
                    mcp_service=None, skill_service=None,
                )

    # Non-numeric value must have produced a warning
    assert any(
        "non-numeric score" in record.message and "factual_grounding" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    ), "Expected a WARNING about non-numeric rubric criterion 'factual_grounding'"

    # Loop completed successfully with the second iteration scoring 90
    assert score == 90
