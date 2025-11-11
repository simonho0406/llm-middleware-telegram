
# TICKET-033: Fix `KeyError` in `_run_panel_workflow` via TDD

**Status:** OPEN

**Epic:** Bugfixes

**Problem:**
A `KeyError: 'full_history_json'` crashes the `_run_panel_workflow` function because the code is passing a different key (`full_history`) to the prompt template. This needs to be fixed, and a regression test must be added to prevent a recurrence.

**Definition of Done (TDD):**

1.  **Write a Failing Test:**
    *   In `tests/test_handler_integration.py`, add a new test to the `TestRegressionPrevention` class that isolates and calls `_run_panel_workflow`.
    *   This test must mock all dependencies of `_run_panel_workflow` (e.g., `load_panel_config`, `get_robust_llm_response`, etc.) to ensure it can run in isolation.
    *   The test should specifically call the function with an empty `full_history` list.
    *   The initial run of `pytest` must show this new test failing with the `KeyError: 'full_history_json'`.

2.  **Implement the Fix:**
    *   In `bot/handlers/discuss_panel_handler.py`, locate the `.format()` call for the `plan_template` inside the `_run_panel_workflow` function.
    *   Change the keyword argument from `full_history=json.dumps(full_history, indent=2)` to `full_history_json=json.dumps(full_history, indent=2)`.

3.  **Verify the Fix:**
    *   Run `pytest` again.
    *   Confirm that the new test now passes and that no existing tests have broken.
