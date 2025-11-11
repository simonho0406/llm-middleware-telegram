# TICKET-041: Harden Quality Gate JSON Parsing

**Status:** OPEN

**Epic:** Robustness

**Problem:**
The Quality Gate (Master Orchestrator) sometimes returns conversational text around its JSON output, causing our strict JSON parser to fail. This triggers an emergency fallback that prematurely ends the refinement loop.

**Definition of Done (TDD):**

1.  **Write a Failing Test:**
    *   In `tests/test_handler_integration.py`, create a new test, `test_quality_gate_json_extraction`.
    *   Create a sample LLM response string that includes conversational text before and after a valid JSON object (e.g., `"Of course! Here is the assessment: {"quality_score": 80}"`).
    *   This test should call the JSON extraction logic inside `_run_refinement_cycle` (it may be necessary to refactor the extraction logic into its own helper function to test it in isolation).
    *   Assert that the correct JSON object is extracted and parsed successfully.
    *   The initial run of `pytest` should show this test failing.

2.  **Implement the Fix:**
    *   In `bot/handlers/discuss_panel_handler.py`, refactor the JSON parsing logic in `_run_refinement_cycle`.
    *   The new logic should be more robust. A good strategy is to find the first `{` and the last `}` in the response string and attempt to parse the content between them.
    *   Replace the multi-strategy approach with this simpler, more resilient method.

3.  **Verify the Fix:**
    *   Run `pytest`. The new test should now pass.
