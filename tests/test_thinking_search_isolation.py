"""
Tests that <search> tags nested inside <thinking> blocks are NOT treated as real
search queries after Fix 2 (strip thinking before extracting search tags).
"""
import re
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.response_generator import _extract_and_process_search_tags

# Replicate the exact pre-processing step from _generate_llm_response
def _strip_thinking(raw: str) -> str:
    return re.sub(r'<thinking>.*?</thinking>\s*', '', raw, flags=re.DOTALL).strip()


def test_search_inside_thinking_not_extracted():
    """A <search> tag that lives only inside a <thinking> block must not be extracted."""
    raw = (
        "<thinking>I should look up more details. "
        "<search>internal reasoning query</search> "
        "But I already know enough.</thinking>"
        "The answer is 42."
    )
    pre_processed = _strip_thinking(raw)
    content, queries = _extract_and_process_search_tags(pre_processed, autosearch_enabled=True, log_prefix="test")

    assert queries is None, "Search query inside <thinking> must not be extracted"
    assert "internal reasoning query" not in content
    assert "The answer is 42." in content


def test_search_outside_thinking_is_extracted():
    """A <search> tag outside any <thinking> block is extracted normally."""
    raw = (
        "<thinking>I should search externally.</thinking>"
        "Let me find the latest news. "
        "<search>AI news 2024</search>"
    )
    pre_processed = _strip_thinking(raw)
    content, queries = _extract_and_process_search_tags(pre_processed, autosearch_enabled=True, log_prefix="test")

    assert queries == ["AI news 2024"]


def test_mixed_thinking_and_real_search():
    """Only the <search> outside <thinking> is extracted; the one inside is discarded."""
    raw = (
        "<thinking>"
        "I might need <search>fake internal query</search> for verification."
        "</thinking>"
        "Here is my answer. <search>actual external query</search>"
    )
    pre_processed = _strip_thinking(raw)
    content, queries = _extract_and_process_search_tags(pre_processed, autosearch_enabled=True, log_prefix="test")

    assert queries == ["actual external query"], "Only the outer query should be extracted"
    assert "fake internal query" not in (queries or [])
    assert "fake internal query" not in content


def test_no_search_tags_returns_none():
    """When no <search> tags exist at all, queries is None."""
    raw = "Just a plain response with no search tags."
    pre_processed = _strip_thinking(raw)
    content, queries = _extract_and_process_search_tags(pre_processed, autosearch_enabled=True, log_prefix="test")

    assert queries is None
    assert content == raw


def test_autosearch_disabled_strips_tags_from_content():
    """When autosearch is disabled, <search> tags are removed from content and queries is None."""
    raw = "Answer: <search>some query</search> here."
    pre_processed = _strip_thinking(raw)
    content, queries = _extract_and_process_search_tags(pre_processed, autosearch_enabled=False, log_prefix="test")

    assert queries is None
    assert "<search>" not in content
    assert "some query" not in content


def test_multiline_thinking_block_fully_stripped():
    """Multi-line <thinking> blocks (common in reasoning models) are fully stripped."""
    raw = (
        "<thinking>\n"
        "  Step 1: Analyze the question.\n"
        "  Step 2: I should search for <search>step inside think</search>.\n"
        "  Step 3: Actually I know the answer.\n"
        "</thinking>\n"
        "The final answer is Python."
    )
    pre_processed = _strip_thinking(raw)
    content, queries = _extract_and_process_search_tags(pre_processed, autosearch_enabled=True, log_prefix="test")

    assert queries is None
    assert "step inside think" not in content
    assert "The final answer is Python." in content
