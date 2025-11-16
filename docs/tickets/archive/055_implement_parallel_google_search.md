# TICKET-055: Implement Parallel Google Search Execution (Revised)

**Status:** Open
**Priority:** High

## Goal

To create a robust, efficient utility function that can execute multiple Google searches concurrently. This function is critical for the "deep dive" phase of the Conversational Research workflow.

## Key Insights & Context

- Sequential Google searches are too slow for a good user experience. We must use `asyncio.gather` to run them in parallel.
- The function **must be resilient to individual search failures**, returning results for the queries that succeed while logging the queries that fail.

## Acceptance Criteria (TDD Plan)

This ticket must be implemented following a strict Test-Driven Development (TDD) approach.

### 1. Create the Test First

Create the file `tests/test_web_search.py` with the following content. This test defines the required behavior.

```python
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
            "Results for success query 1",
            Exception("Mocked API Error"),
            "Results for success query 2"
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
```

### 2. Refactor the Implementation

Modify `services/web_search_service.py` to make the test pass.

1.  **Refactor `_google_search`:**
    *   This function must be modified to **let exceptions propagate upwards**. Do NOT catch `httpx.HTTPStatusError` or `Exception` inside it.
    *   On success, it must return the raw results string directly, **not** a dictionary like `{'status': 'success', ...}`.

2.  **Refactor `execute_parallel_google_searches`:**
    *   Create a new internal helper coroutine, e.g., `_search_and_handle_errors(query: str) -> tuple[str, str | None]`.
    *   This helper will contain the `try...except` block. It will `await _google_search(query)`.
    *   On success, it will return `(query, result)`.
    *   On failure (in the `except` block), it will log the error (e.g., `logger.error(f"Error during parallel Google search for '{query}': {e}")`) and return `(query, None)`.
    *   The main `execute_parallel_google_searches` function will use `asyncio.gather` to run `_search_and_handle_errors` for all queries.
    *   It will then build the final dictionary by iterating through the list of tuples returned by `gather`, adding only the ones where the result is not `None`.

### 3. Final Verification

Run `pytest tests/test_web_search.py`. The test must pass.