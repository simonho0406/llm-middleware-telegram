"""
Regression tests for the post-launch context-budget fixes (Fix B).

Root cause: chat used a hardcoded max_input_tokens=28000 (not even present in
config.yaml), so a heavy user's long thread got truncated from ~500 messages down to
~33-67 — an unacceptable quality loss. The fix makes the chat budget capability-driven by
default (same mechanism panels already use), with the hardcoded cap only as an opt-in
override. These tests pin: (1) the new capability-driven defaults, and (2) that a long
seeded thread now retains dramatically more history than the old regression.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import patch

import config
from utils.context_manager import ensure_context_fits, ModelContextLimits


def _wordcount(text):
    return len((text or "").split())


# ── config defaults are capability-driven, not silent traps ─────────────────────

def test_chat_max_context_tokens_defaults_to_none():
    """No yaml override → chat gets no extra cap beyond the model-capability budget
    (matches panels). This is the core fix: previously defaulted to a hardcoded 28000."""
    with patch.dict(config._yaml_config, {}, clear=False):
        config._yaml_config.pop("chat_max_context_tokens", None)
        assert config.get_chat_max_context_tokens() is None


def test_chat_max_context_tokens_honors_explicit_override():
    """An operator who explicitly wants a tighter chat budget can still set one."""
    with patch.dict(config._yaml_config, {"chat_max_context_tokens": 60000}, clear=False):
        assert config.get_chat_max_context_tokens() == 60000


def test_default_max_context_tokens_fallback_is_not_a_silent_trap():
    """The code-level fallback (used only if the yaml key is absent) must match the
    shipped config.yaml value (128000), not a stale, much smaller value (previously 3800)
    that would silently collapse context ~34x if the yaml key were ever dropped."""
    with patch.dict(config._yaml_config, {}, clear=False):
        config._yaml_config.pop("default_max_context_tokens", None)
        assert config.get_default_max_context_tokens() >= 100000


def test_context_token_output_buffer_fallback_is_not_a_silent_trap():
    with patch.dict(config._yaml_config, {}, clear=False):
        config._yaml_config.pop("context_token_output_buffer", None)
        assert config.get_context_token_output_buffer() >= 10000


def test_thread_history_fetch_limit_raised_from_old_500():
    """The DB pre-filter limit must be well above the old hardcoded 500 — otherwise it
    becomes the new binding constraint even after the token budget is fixed."""
    with patch.dict(config._yaml_config, {}, clear=False):
        config._yaml_config.pop("thread_history_fetch_limit", None)
        assert config.get_thread_history_fetch_limit() > 500


# ── the actual regression: a long thread must NOT be gutted to ~30-60 messages ──

@pytest.mark.asyncio
async def test_long_thread_retains_most_history_without_chat_cap():
    """Reproduces the production scenario: ~500 messages in one thread (sized so the
    total comfortably exceeds a 28k-token budget but fits well within a capability-driven
    ~108k one — the same order of magnitude as the real Azure/Oracle logs), an 'unknown'
    model (the same fallback path minimax-m3 hit in the logs), and NO max_input_tokens cap
    (the new chat default). Must retain far more than the ~33-67 messages seen in the
    regression (confirmed by mutation check: the OLD 28000 cap on this exact fixture
    retains only 279/500 — this test fails against the old hardcoded default)."""
    history = [
        {'role': 'user' if i % 2 == 0 else 'assistant', 'content': f"message {i} " + ("w " * 98)}
        for i in range(500)
    ]  # ~100 words each => ~50,000 words total: > 28k (old cap) but < 108k (new budget)

    # Mirrors the "unknown model" fallback: effective_input_limit = 128000 - 20000 = 108000
    limits = ModelContextLimits(max_context_tokens=128000, max_completion_tokens=20000, buffer_tokens=20000)

    with patch('utils.context_manager.get_model_context_limits', return_value=limits), \
         patch('utils.context_manager.count_tokens', side_effect=_wordcount):
        # max_input_tokens=None — the new chat default (no extra clamp).
        final, info = await ensure_context_fits("q", history, "m", "p", max_input_tokens=None)

    # Under the capability-driven ~108k budget, this 50k-word thread fits entirely.
    assert len(final) == 500, f"expected all 500 messages retained under the capability budget, got {len(final)}"


@pytest.mark.asyncio
async def test_explicit_chat_cap_still_truncates_when_operator_sets_one():
    """An operator who opts into a tighter chat budget still gets real truncation —
    the mechanism isn't removed, just no longer the forced default."""
    history = [
        {'role': 'user', 'content': ('w ' * 50)} for _ in range(500)
    ]
    limits = ModelContextLimits(max_context_tokens=128000, max_completion_tokens=20000, buffer_tokens=20000)

    with patch('utils.context_manager.get_model_context_limits', return_value=limits), \
         patch('utils.context_manager.count_tokens', side_effect=_wordcount):
        final, info = await ensure_context_fits("q", history, "m", "p", max_input_tokens=1000)

    assert len(final) < 500
    assert info is not None
