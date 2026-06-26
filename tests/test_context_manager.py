"""
Unit tests for utils.context_manager.

Covers two correctness-critical behaviors that were previously untested:
  1. ensure_context_fits — system messages are NEVER truncated; newest non-system
     turns are kept; chronological order is restored; over-budget input degrades to
     a system-only return rather than raising.
  2. _repair_tool_call_pairs — truncation can split a paired assistant tool-call /
     tool-result group; orphaned halves must be removed so providers never see a
     split pair (both Gemini and OpenAI reject those).
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import patch

from utils.context_manager import (
    ensure_context_fits,
    _repair_tool_call_pairs,
    ModelContextLimits,
)


def _wordcount(text):
    return len((text or "").split())


# ── ensure_context_fits ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_system_messages_protected_and_newest_kept_in_order():
    """System messages survive truncation; only the newest non-system turns are kept,
    and their chronological order is restored (not reversed)."""
    history = [
        {'role': 'system', 'content': 's'},                       # 1 word, protected
        {'role': 'user',      'content': 'OLD ' + 'w ' * 9},      # 10 words
        {'role': 'assistant', 'content': 'MID ' + 'w ' * 9},      # 10 words
        {'role': 'user',      'content': 'NEW ' + 'w ' * 9},      # 10 words
    ]
    # effective_input_limit = 22; available = 22 - prompt(1) - system(1) = 20.
    # Reverse-fill keeps NEW (10) + MID (10) = 20; OLD (10) would overflow → dropped.
    limits = ModelContextLimits(max_context_tokens=22, max_completion_tokens=0, buffer_tokens=0)

    with patch('utils.context_manager.get_model_context_limits', return_value=limits), \
         patch('utils.context_manager.count_tokens', side_effect=_wordcount):
        final, info = await ensure_context_fits("q", history, "m", "p")

    # System retained and first.
    assert final[0]['role'] == 'system' and final[0]['content'] == 's'
    # Oldest dropped; newest two kept.
    joined = " ".join(m['content'] for m in final)
    assert "OLD" not in joined
    assert "MID" in joined and "NEW" in joined
    # Chronological order restored: MID precedes NEW.
    assert joined.index("MID") < joined.index("NEW")
    # Something was dropped → an info message is returned.
    assert info is not None and "adjusted" in info


@pytest.mark.asyncio
async def test_prompt_plus_system_over_budget_returns_system_only():
    """If prompt + system context alone exceed the limit, return only the system
    messages plus an info string — never raise."""
    history = [
        {'role': 'system', 'content': 'big ' * 50},   # 50 words
        {'role': 'user', 'content': 'hello there'},
    ]
    limits = ModelContextLimits(max_context_tokens=10, max_completion_tokens=0, buffer_tokens=0)

    with patch('utils.context_manager.get_model_context_limits', return_value=limits), \
         patch('utils.context_manager.count_tokens', side_effect=_wordcount):
        final, info = await ensure_context_fits("q", history, "m", "p")

    assert [m['role'] for m in final] == ['system']
    assert info is not None and "too long" in info.lower()


@pytest.mark.asyncio
async def test_history_within_budget_unchanged():
    """When everything fits, history is returned intact with no info message."""
    history = [
        {'role': 'system', 'content': 's'},
        {'role': 'user', 'content': 'short one'},
        {'role': 'assistant', 'content': 'short two'},
    ]
    limits = ModelContextLimits(max_context_tokens=1000, max_completion_tokens=0, buffer_tokens=0)

    with patch('utils.context_manager.get_model_context_limits', return_value=limits), \
         patch('utils.context_manager.count_tokens', side_effect=_wordcount):
        final, info = await ensure_context_fits("q", history, "m", "p")

    assert final == history
    assert info is None


# ── _repair_tool_call_pairs ──────────────────────────────────────────────────────

def test_repair_removes_orphaned_tool_result():
    """A tool-result whose originating assistant tool-call was truncated is removed."""
    history = [
        {'role': 'user', 'content': 'hi'},
        {'role': 'tool', 'tool_call_id': 'missing', 'content': 'orphan result'},
    ]
    repaired = _repair_tool_call_pairs(history)
    assert repaired == [{'role': 'user', 'content': 'hi'}]


def test_repair_removes_incomplete_assistant_tool_call():
    """An assistant tool-call turn whose result was truncated away is removed."""
    history = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'a'}]},
        {'role': 'user', 'content': 'next'},
    ]
    repaired = _repair_tool_call_pairs(history)
    assert repaired == [{'role': 'user', 'content': 'next'}]


def test_repair_preserves_healthy_pair():
    """A fully-paired assistant tool-call + tool-result is left intact."""
    history = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'a'}]},
        {'role': 'tool', 'tool_call_id': 'a', 'content': 'ok'},
    ]
    repaired = _repair_tool_call_pairs(history)
    assert repaired == history


def test_repair_two_sweep_cascade():
    """Removing one orphan can expose another. An assistant calling {a,b} with only
    result 'a' present: the assistant is dropped (incomplete), which then orphans the
    'a' result, removed on the second sweep → nothing survives."""
    history = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'a'}, {'id': 'b'}]},
        {'role': 'tool', 'tool_call_id': 'a', 'content': 'partial'},
    ]
    repaired = _repair_tool_call_pairs(history)
    assert repaired == []
