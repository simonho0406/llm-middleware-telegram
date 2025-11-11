
# TICKET-046: Report Panel Agent Failures in Summary

**Status:** OPEN

**Epic:** UX / Observability

**Problem:**
Panel agents (like the Critic) are frequently failing and falling back, but this is completely invisible to the user, who only experiences a long, unexplained delay. We need to expose this information to the user so they can diagnose and reconfigure failing agents.

**Definition of Done:**

1.  **Enhance `get_robust_llm_response`:**
    *   In `utils/llm_utilities.py`, modify `get_robust_llm_response` to return not just the response string, but a dictionary containing `{'response': str, 'retries': int, 'fallback_used': bool}`.

2.  **Update `_run_refinement_cycle`:**
    *   In `bot/handlers/discuss_panel_handler.py`, update the calls to `get_robust_llm_response` to handle the new dictionary return type.
    *   Store the `retries` and `fallback_used` data in the `panel_results` dictionary for each agent (Proposer, Critic, Refiner).

3.  **Update `_format_panel_summary`:**
    *   Modify this function to read the new retry and fallback data from `panel_results`.
    *   Append this information to the summary string for each agent. For example:
        *   `"⚠️ Critic: nvidia/gpt-oss-120b (Failure) (3 retries, fallback used)"`
        *   `"✅ Proposer: gemini/gemini-pro (Success) (0 retries)"`

4.  **Write a Verification Test:**
    *   In `tests/test_handler_integration.py`, create a new test for `_format_panel_summary`.
    *   Provide it with a sample `panel_results` dictionary containing retry and fallback data.
    *   Assert that the output summary string correctly includes the "(3 retries, fallback used)" text.
