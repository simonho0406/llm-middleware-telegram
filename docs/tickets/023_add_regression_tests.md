
# TICKET-023: Retroactively Add Tests for Robustness Fixes

**Status:** CLOSED (Superseded by T-026)

**Epic:** TDD Hardening

**Problem:** The fixes for the `TypeError` in `/discuss_panel` (T-022) and the external API failure reporting (T-21) were not driven by tests. We must add specific tests to prevent these regressions from ever happening again.

**Definition of Done:**

1.  **Test for `/discuss_panel` TypeError:**
    *   In `tests/test_handler_integration.py`, create a new test that specifically calls the `_run_panel_workflow` function.
    *   This test should use `pytest.raises` to assert that the *original* broken code would have raised a `TypeError`.
    *   The test should then be updated to assert that the *current, fixed* code runs without raising a `TypeError`.
    *   This will likely require mocking several objects, such as `update`, `context`, and the LLM services.

2.  **Test for External API Failure Reporting:**
    *   In `tests/test_handler_integration.py`, create a new test for the `search_command`.
    *   Use `unittest.mock.patch` to mock the `web_search_service.perform_search` function so that it returns a predictable error dictionary (e.g., `{'status': 'error', 'message': 'Mock API Error'}`).
    *   The test must assert that the `placeholder_message.edit_text` method is called with the correct error message.
