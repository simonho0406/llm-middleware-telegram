# TICKET-038: Expose Refiner Failures to the User

**Status:** CLOSED

**Epic:** Robustness

**Problem:**
If the `Refiner` agent fails (e.g., due to a rate limit), the panel workflow silently uses the unrefined proposer response as the final answer. The user is not notified of this failure, leading to a degraded experience.

**Definition of Done (TDD):**

1.  **Write a Failing Test:**
    *   In `tests/test_handler_integration.py`, create a new test for `_run_panel_workflow`.
    *   Mock the `get_robust_llm_response` function.
    *   Configure the mock so that the call for the `Refiner` role returns an error string (e.g., `"[Error: Rate limit exceeded]"`).
    *   Assert that the final answer returned by `_run_panel_workflow` contains a warning message indicating that the final refinement step was skipped.

2.  **Implement the Fix:**
    *   In `bot/handlers/discuss_panel_handler.py`, modify the `_run_panel_workflow` function.
    *   After the `Refiner` call, check if the `refiner_response` starts with `"[Error:"`.
    *   If it does, log a warning and prepend a user-facing warning to the `final_answer` (e.g., `"⚠️ **Warning:** The final refinement step was skipped due to an error. The following is the unpolished response.\n\n---\n\n"`).

3.  **Verify the Fix:**
    *   Run `pytest`. The new test should now pass.
