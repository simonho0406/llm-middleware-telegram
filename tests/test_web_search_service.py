

import pytest
import asyncio
from unittest.mock import patch, AsyncMock

# Add project root to path to allow module imports
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# The function we are testing does not exist yet
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
