
# TICKET-036: Harden Prompt Manager to Error on Missing Prompts

**Status:** OPEN

**Epic:** Robustness

**Problem:**
The `prompt_manager.get_prompt()` method currently returns `None` when a prompt is not found. This leads to downstream `AttributeError` crashes when the calling code tries to use the `None` object. The prompt manager should fail fast and raise a specific error.

**Definition of Done (TDD):**

1.  **Write a Failing Test:**
    *   In a new test file, `tests/test_prompt_loader.py`, write a test that calls `prompt_manager.get_prompt()` with a non-existent prompt name.
    *   Assert that this call raises a `FileNotFoundError`.
    *   The initial run of `pytest` must show this test failing because no error is raised (or the wrong error is raised).

2.  **Implement the Fix:**
    *   In `bot/prompt_loader.py`, modify the `get_prompt` method.
    *   If the requested prompt key does not exist in the loaded prompts dictionary, raise a `FileNotFoundError` with a clear error message (e.g., `f"Prompt '{name}' not found."`).

3.  **Verify the Fix:**
    *   Run `pytest` again. Confirm that the new test passes.
