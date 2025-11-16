import pytest
import asyncio
from unittest.mock import patch, AsyncMock

from services import web_search_service

@pytest.mark.asyncio
async def test_execute_parallel_google_searches_handles_failures(caplog):
    """
    Tests that execute_parallel_google_searches:
    1. Runs searches in parallel.
    2. Gracefully handles exceptions for individual failed searches.
    3. Returns a dictionary containing only the successful results.
    4. Logs the failures.
    """
    # 1. Define test data
    queries = [
        "success query 1",
        "failing query",
        "success query 2"
    ]
    
    # 2. Mock the underlying single-search function
    # The side_effect will return a success, then an exception, then another success.
    mock_google_search = AsyncMock(
        side_effect=[
            {'status': 'success', 'content': "Results for success query 1"},
            Exception("Mocked API Error"),
            {'status': 'success', 'content': "Results for success query 2"}
        ]
    )

    # 3. Patch the function and execute the parallel search
    with patch('services.web_search_service._google_search', new=mock_google_search):
        results = await web_search_service.execute_parallel_google_searches(queries)

    # 4. Assert the results
    # It should have called the mock for each query
    assert mock_google_search.call_count == 3, "Should have attempted all 3 searches in parallel."

    # The final dictionary should only contain the successful results
    assert len(results) == 2
    assert results["success query 1"] == "Results for success query 1"
    assert results["success query 2"] == "Results for success query 2"
    assert "failing query" not in results

    # The error for the failed query should have been logged
    assert "Error during parallel Google search for 'failing query'" in caplog.text
    assert "Mocked API Error" in caplog.text