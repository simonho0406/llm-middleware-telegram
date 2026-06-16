"""
Unit tests for query-aware tool-result distillation (utils/tool_distiller.py).

Covers the pass-through fast paths (small results, error sentinels), the happy
path (large result → utility-model extract), and the never-block guarantee
(distiller model error → token-truncation fallback). The utility model call is
mocked, so these run fast and offline.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import AsyncMock, patch

from utils.tool_distiller import distill_tool_result
from utils.context_manager import count_tokens


def _big(n_tokens: int) -> str:
    # ~1 token per word for the tiktoken cl100k encoder on simple words.
    return " ".join(["alpha"] * n_tokens)


@pytest.mark.asyncio
async def test_small_result_passes_through_without_llm():
    text = "short result"
    with patch("utils.llm_utilities.get_robust_llm_response", new_callable=AsyncMock) as llm:
        out = await distill_tool_result(text, query="anything")
    assert out == text
    llm.assert_not_awaited()  # below threshold → no distiller call


@pytest.mark.asyncio
async def test_error_sentinel_passes_through():
    for sentinel in ("[Error: boom]", "[Denied: nope]", "[Success: done]"):
        with patch("utils.llm_utilities.get_robust_llm_response", new_callable=AsyncMock) as llm:
            out = await distill_tool_result(sentinel, query="q")
        assert out == sentinel
        llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_string_passes_through():
    assert await distill_tool_result(None, query="q") is None
    assert await distill_tool_result({"a": 1}, query="q") == {"a": 1}


@pytest.mark.asyncio
async def test_large_result_is_distilled():
    big = _big(20000)  # over the default 16k keep-budget → must be distilled
    with patch.object(__import__("config"), "get_distiller_enabled", return_value=True), \
         patch("utils.llm_utilities.get_robust_llm_response", new_callable=AsyncMock) as llm:
        llm.return_value = {"response": "DISTILLED: alpha facts", "is_error": False}
        out = await distill_tool_result(big, query="find alpha", tool_name="notion__x")
    llm.assert_awaited_once()
    assert out == "DISTILLED: alpha facts"


@pytest.mark.asyncio
async def test_result_that_fits_budget_passes_through_raw():
    # A result under max_keep_tokens is kept verbatim — no LLM, no truncation.
    text = _big(2000)
    with patch("utils.llm_utilities.get_robust_llm_response", new_callable=AsyncMock) as llm:
        out = await distill_tool_result(text, query="q", max_keep_tokens=8000)
    assert out == text
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_distiller_error_falls_back_to_truncation():
    big = _big(8000)
    with patch.object(__import__("config"), "get_distiller_enabled", return_value=True), \
         patch.object(__import__("config"), "get_distiller_max_output_tokens", return_value=500), \
         patch("utils.llm_utilities.get_robust_llm_response", new_callable=AsyncMock) as llm:
        llm.return_value = {"response": "[Error: distiller model 404]", "is_error": True}
        out = await distill_tool_result(big, query="q")
    # Fell back to truncation: not raised, and capped near the output budget (not the full 8000).
    assert isinstance(out, str)
    assert count_tokens(out) <= 600  # ~500 budget + tokenizer slack
    assert count_tokens(out) < count_tokens(big)


@pytest.mark.asyncio
async def test_disabled_distiller_truncates_without_llm():
    big = _big(8000)
    with patch.object(__import__("config"), "get_distiller_enabled", return_value=False), \
         patch.object(__import__("config"), "get_distiller_max_output_tokens", return_value=500), \
         patch("utils.llm_utilities.get_robust_llm_response", new_callable=AsyncMock) as llm:
        out = await distill_tool_result(big, query="q")
    llm.assert_not_awaited()
    assert count_tokens(out) <= 600
