"""
Regression test for the panel quality-gate JSON-parse robustness fix (Fix C).

Root cause: a single malformed JSON response from the quality-gate model set
quality_score=-1 and BROKE the entire refinement loop, discarding all remaining rounds —
a production-surfaced panel-quality regression. The fix retrofits the same
retry-with-repair pattern already used by the orchestrator plan parser: a bad parse
re-prompts the model (with an explicit repair instruction) before giving up, and only
persistent failure across all configured attempts falls back to the old abort behavior.
"""
import pytest
import sys
import os
import json
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.handlers.panel_workflow import _run_refinement_cycle


def _make_panel_config(max_iterations=3, threshold=85):
    return {
        'quality_threshold': threshold,
        'max_refinement_iterations': max_iterations,
        'roles': {
            'Proposer': {'provider': 'mock_prov', 'model': 'mock_model', 'request_timeout_seconds': 30},
            'Critic': {'provider': 'mock_prov', 'model': 'mock_model', 'request_timeout_seconds': 30},
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


async def _run_cycle_with_responses(responses, max_iterations=3, threshold=85, quality_gate_parse_attempts=3):
    resp_iter = iter(responses)

    async def mock_llm(*args, **kwargs):
        return next(resp_iter)

    placeholder = AsyncMock()

    with patch('bot.handlers.panel_workflow.get_robust_llm_response', side_effect=mock_llm), \
         patch('bot.handlers.panel_workflow.get_expert_panel_fallback_config', return_value=(None, None)), \
         patch('config.get_panel_quality_gate_parse_attempts', return_value=quality_gate_parse_attempts):
        return await _run_refinement_cycle(
            update=MagicMock(),
            context=MagicMock(),
            proposer_task={'role': 'Proposer', 'prompt': 'Answer the question.'},
            critic_task={'role': 'Critic', 'prompt': 'Review the answer.'},
            user_prompt="What is the answer?",
            full_history=[],
            placeholder_msg=placeholder,
            panel_results={},
            orchestrator_service=MagicMock(),
            orchestrator_model='mock_model',
            orchestrator_timeout=30,
            orchestrator_config={'provider': 'mock_prov', 'model': 'mock_model'},
            panel_config=_make_panel_config(max_iterations, threshold),
            mcp_service=None,
            skill_service=None,
            available_tools_text="No tools available.",
            panel_execution_tool_names=frozenset(),
        ), resp_iter


@pytest.mark.asyncio
async def test_malformed_quality_gate_json_retries_and_recovers():
    """A malformed quality-gate response on the first attempt must NOT abort the
    refinement loop — it should retry with a repair prompt and recover, ending in a
    normal (non -1) score."""
    responses = [
        _llm_response("The answer is 42."),                                     # Proposer round 1
        _llm_response("Looks reasonable."),                                     # Critic round 1
        _llm_response('Sure! {"quality_score": 90, oops this is broken json'),  # Quality gate attempt 1 (malformed)
        _llm_response(_quality_json(90)),                                       # Quality gate attempt 2 (valid, meets threshold)
    ]

    (result_text, score, iters), resp_iter = await _run_cycle_with_responses(responses, quality_gate_parse_attempts=3)

    # The malformed attempt must NOT have set score to -1 / aborted the loop — the
    # retry recovered within the SAME round, so exactly one refinement round elapsed.
    assert score == 90
    assert iters == 1
    assert result_text == "The answer is 42."
    # All 4 scripted responses must have been consumed (proves the retry actually
    # happened — a tautological test would pass even if only 3 were consumed and the
    # loop had aborted early via a different path, e.g. an unrelated exception).
    assert next(resp_iter, "EXHAUSTED") == "EXHAUSTED"


@pytest.mark.asyncio
async def test_persistent_malformed_json_falls_back_to_best_so_far():
    """If EVERY quality-gate attempt in a round is malformed (a genuinely broken model,
    not a one-off hiccup), the old safety-net behavior must still apply: stop refining
    and return the best response seen so far, with score=-1 for that round."""
    # 3 malformed quality-gate attempts (quality_gate_parse_attempts=3 below).
    responses = [
        _llm_response("First answer."),                  # Proposer round 1
        _llm_response("Some review."),                    # Critic round 1
        _llm_response("not json at all, sorry"),           # Quality gate attempt 1 (malformed)
        _llm_response("still not json"),                   # Quality gate attempt 2 (malformed)
        _llm_response("nope, still broken {{{"),            # Quality gate attempt 3 (malformed)
    ]

    (result_text, score, iters), _ = await _run_cycle_with_responses(responses, quality_gate_parse_attempts=3)

    # Persistent failure across all attempts falls back to the pre-fix behavior: abort
    # with the best (only, in this case) response seen, score=-1 for the failed round.
    assert score == -1
    assert result_text == "First answer."
