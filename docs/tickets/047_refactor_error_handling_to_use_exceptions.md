# TICKET-047: Refactor Error Handling to Use Custom Exceptions

**User Story:**
As a developer, I want the application to use proper Python exceptions for flow control and error handling instead of relying on string matching, so that the codebase is more robust, maintainable, and less prone to brittle errors.

**The Problem:**
Currently, functions like `get_robust_llm_response` signal failure by returning a string prefixed with `"[Error:..."`. The calling code in handlers must then check for this specific string to detect if an error occurred.

This is a "stringly-typed" anti-pattern that is:
- **Brittle:** A typo in the error string could cause failures to go undetected.
- **Not Expressive:** The string doesn't convey the *type* of error (e.g., Timeout, Rate Limit, Unavailable), forcing more string parsing to get context.
- **Un-Pythonic:** It ignores Python's powerful built-in exception handling mechanisms.

**Acceptance Criteria:**
1.  **Create `bot/errors.py`:**
    - A new file, `bot/errors.py`, will be created.
    - It will contain a base `ProviderError` exception.
    - It will contain specific exceptions that inherit from `ProviderError`, such as `ProviderTimeoutError`, `ProviderRateLimitError`, and `ProviderUnavailableError`.

2.  **Refactor `get_robust_llm_response`:**
    - This function in `utils/llm_utilities.py` must be modified.
    - Instead of returning an error string (e.g., `"[Error: ...]"`) upon failure, it must `raise` the appropriate custom exception (e.g., `raise ProviderTimeoutError(...)`).
    - On success, it should still return the dictionary `{'response': ..., 'retries': ..., 'fallback_used': ...}`.

3.  **Refactor Panel Handlers:**
    - The `_run_panel_workflow` and `_run_refinement_cycle` functions in `bot/handlers/discuss_panel_handler.py` must be updated.
    - All calls to `get_robust_llm_response` must be wrapped in `try...except` blocks that catch the new custom exceptions.
    - The `except` blocks should contain the logic for handling the failure (e.g., logging the error, updating `panel_results` with a 'Failure' status, providing a fallback response string for the next agent).

4.  **No Regressions:**
    - All existing 34 tests must continue to pass.
    - The functionality of the `/discuss_panel` command must remain unchanged from the user's perspective, other than potentially more descriptive internal error logging.
