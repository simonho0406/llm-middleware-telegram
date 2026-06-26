

import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

# Add project root to path to allow module imports
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from services import web_search_service
from services.web_search_service import execute_parallel_google_searches

@pytest.mark.asyncio
async def test_execute_parallel_google_searches_runs_concurrently():
    """
    Tests that execute_parallel_google_searches runs searches in parallel
    using asyncio.gather and correctly aggregates the results.
    """
    # Arrange
    test_queries = ["query 1", "query 2", "failed query"]
    
    # Mock the internal _google_search function
    async def mock_google_search(query):
        if query == "failed query":
            return {'status': 'error', 'message': 'API limit reached'}
        return {'status': 'success', 'content': f"Results for {query}"}

    # We patch only the internal _google_search function, letting asyncio.gather run natively
    with patch('services.web_search_service._google_search', side_effect=mock_google_search) as mock_search_func:
        
        # Act
        results = await execute_parallel_google_searches(test_queries)
        
        # Assert
        # 1. Check that the mock function was called for each query
        assert mock_search_func.call_count == 3
        
        # 2. Check that the results are correctly aggregated
        assert isinstance(results, dict)
        assert len(results) == 2  # Only successful queries should be in the output
        assert results["query 1"] == "Results for query 1"
        assert results["query 2"] == "Results for query 2"
        assert "failed query" not in results


@pytest.mark.asyncio
async def test_execute_parallel_google_searches_handles_exceptions(caplog):
    """Merged from the former test_web_search.py: a raised exception for one query is
    swallowed and logged; only successful results are returned."""
    queries = ["success query 1", "failing query", "success query 2"]
    mock_google_search = AsyncMock(side_effect=[
        {'status': 'success', 'content': "Results for success query 1"},
        Exception("Mocked API Error"),
        {'status': 'success', 'content': "Results for success query 2"},
    ])

    with patch('services.web_search_service._google_search', new=mock_google_search):
        results = await execute_parallel_google_searches(queries)

    assert mock_google_search.call_count == 3
    assert len(results) == 2
    assert "failing query" not in results
    assert "Error during parallel Google search for 'failing query'" in caplog.text
    # The underlying exception detail must survive into the log, not just the prefix.
    assert "Mocked API Error" in caplog.text


# ── perform_search dispatch ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_perform_search_manual_routes_to_manual_provider():
    with patch.object(config, 'get_manual_search_provider', return_value='tavily'), \
         patch.object(config, 'get_automated_search_provider', return_value='google'), \
         patch('services.web_search_service._tavily_search', new=AsyncMock(return_value={'status': 'success', 'content': 'T'})) as mt, \
         patch('services.web_search_service._google_search', new=AsyncMock()) as mg:
        result = await web_search_service.perform_search("q", manual=True)

    assert result == {'status': 'success', 'content': 'T'}
    mt.assert_awaited_once()
    mg.assert_not_awaited()


@pytest.mark.asyncio
async def test_perform_search_automated_routes_to_automated_provider():
    with patch.object(config, 'get_manual_search_provider', return_value='tavily'), \
         patch.object(config, 'get_automated_search_provider', return_value='google'), \
         patch('services.web_search_service._tavily_search', new=AsyncMock()) as mt, \
         patch('services.web_search_service._google_search', new=AsyncMock(return_value={'status': 'success', 'content': 'G'})) as mg:
        result = await web_search_service.perform_search("q", manual=False)

    assert result == {'status': 'success', 'content': 'G'}
    mg.assert_awaited_once()
    mt.assert_not_awaited()


@pytest.mark.asyncio
async def test_perform_search_unsupported_provider_returns_error():
    with patch.object(config, 'get_automated_search_provider', return_value='bing'):
        result = await web_search_service.perform_search("q", manual=False)
    assert result['status'] == 'error'
    assert "Unsupported" in result['message']


# ── _tavily_search MCP-vs-HTTP routing ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_tavily_prefers_mcp_when_session_present():
    mcp = MagicMock()
    mcp.sessions = {"tavily-search": object()}
    mcp.execute_tool = AsyncMock(return_value="MCP search results")

    result = await web_search_service._tavily_search("q", mcp_service=mcp)

    assert result == {'status': 'success', 'content': "MCP search results"}
    mcp.execute_tool.assert_awaited_once_with("tavily-search", "tavily_search", {"query": "q"})


@pytest.mark.asyncio
async def test_tavily_missing_key_without_mcp_returns_error():
    with patch.object(config, 'TAVILY_API_KEY', ''):
        result = await web_search_service._tavily_search("q", mcp_service=None)
    assert result['status'] == 'error'
    assert "not configured" in result['message']


@pytest.mark.asyncio
async def test_tavily_mcp_error_falls_back_past_mcp():
    """An MCP error must NOT be returned as a success — it falls through to HTTP. With
    no API key the fallback ends in the 'not configured' error, proving MCP didn't
    short-circuit."""
    mcp = MagicMock()
    mcp.sessions = {"tavily-search": object()}
    mcp.execute_tool = AsyncMock(return_value="[Error: tavily upstream failed]")

    with patch.object(config, 'TAVILY_API_KEY', ''):
        result = await web_search_service._tavily_search("q", mcp_service=mcp)

    assert result['status'] == 'error'
    assert "not configured" in result['message']


# ── perform_multi_search dedup + all-fail ────────────────────────────────────────

@pytest.mark.asyncio
async def test_perform_multi_search_dedups_preserving_order():
    calls = []

    async def fake_tavily(q, mcp_service=None):
        calls.append(q)
        return {'status': 'success', 'content': f"R:{q}"}

    with patch.object(config, 'get_automated_search_provider', return_value='tavily'), \
         patch('services.web_search_service._tavily_search', new=fake_tavily):
        result = await web_search_service.perform_multi_search(["a", "a", "b"], manual=False)

    assert calls == ["a", "b"], "duplicate queries de-duplicated, order preserved"
    assert result['status'] == 'success'
    assert "R:a" in result['content'] and "R:b" in result['content']


@pytest.mark.asyncio
async def test_perform_multi_search_all_fail_returns_error():
    with patch.object(config, 'get_automated_search_provider', return_value='tavily'), \
         patch('services.web_search_service._tavily_search',
               new=AsyncMock(return_value={'status': 'error', 'message': 'boom'})):
        result = await web_search_service.perform_multi_search(["a", "b"], manual=False)

    assert result['status'] == 'error'
    assert "failed" in result['message'].lower()
