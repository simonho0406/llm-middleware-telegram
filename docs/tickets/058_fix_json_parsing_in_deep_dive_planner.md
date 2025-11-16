# TICKET-058: Fix JSON Parsing in Deep Dive Planner

**Status:** Open
**Priority:** High
**Blocked By:** TICKET-057 (Completed)

## Goal

To fix the failing unit test `test_plan_deep_dive_searches_extracts_queries_from_messy_json` by implementing robust JSON extraction logic in the `_plan_deep_dive_searches` function.

## Root Cause

The test is failing because the `_plan_deep_dive_searches` function in `bot/handlers/discuss_panel_handler.py` attempts to parse the entire LLM response as JSON, which fails when the response includes conversational text. The function must first extract the JSON substring before parsing.

## TDD Plan

This ticket must be implemented by following a strict Test-Driven Development workflow.

1.  **Verify the Failure:**
    *   Run the failing test to confirm its status:
        ```bash
        docker compose run --rm llm-middleware-telegram pytest tests/test_panel_orchestrator.py
        ```
    *   The test must fail with the `assert 0 == 4` error.

2.  **Implement the Fix:**
    *   Modify the `_plan_deep_dive_searches` function in `bot/handlers/discuss_panel_handler.py`.
    *   Inside the `try` block, before the `json.loads()` call, add logic to extract the JSON string from the `response_text`.
    *   **Use the following robust extraction strategy:**
        1.  First, search for a JSON array inside a markdown code block (e.g., ` ```json [...] ``` `). Use a regular expression for this.
        2.  If that fails, fall back to finding the substring between the very first `[` and the very last `]`.
        3.  If no JSON string can be extracted, the function should log the failure and return an empty list `[]`.

3.  **Verify the Fix:**
    *   Run the test again:
        ```bash
        docker compose run --rm llm-middleware-telegram pytest tests/test_panel_orchestrator.py
        ```
    *   The test must now pass.

4.  **Final Regression Check:**
    *   Run the entire test suite to ensure the fix has not introduced any regressions:
        ```bash
        docker compose run --rm llm-middleware-telegram pytest tests/
        ```
    *   All 20 tests must pass.
