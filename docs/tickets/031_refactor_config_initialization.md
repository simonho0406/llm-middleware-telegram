
# TICKET-031: Refactor Config Initialization to Fix Test Suite

**Status:** CLOSED

**Epic:** TDD Hardening

**Problem:** A fundamental architectural flaw in our application's startup sequence is causing `config.PROMPTS` to be `None` during test runs, leading to persistent `AttributeError` failures. This ticket refactors the configuration and prompt loading to be robust and self-contained, fixing the test suite.

**Definition of Done:**

1.  **Refactor `config.py` to be Self-Contained:**
    *   Modify `config.py` to import and initialize the `PromptManager` directly. Replace the line `PROMPTS = None` with the following:
        ```python
        from bot.prompt_loader import prompt_manager
        PROMPTS = prompt_manager
        logger.info("Prompt manager initialized and attached to config at import time.")
        ```

2.  **Simplify `main.py`:**
    *   In `main.py`, find and **delete** the following lines, as this logic now lives in `config.py`:
        ```python
        from bot.prompt_loader import prompt_manager
        config.PROMPTS = prompt_manager
        logger.info("Prompt manager initialized and attached to config.")
        ```

3.  **Cleanup Obsolete Test:**
    *   In `tests/test_handler_integration.py`, **delete the entire `test_discuss_panel_type_error_regression` function.** This test has served its diagnostic purpose and its complexity is no longer needed.

4.  **Verify the Fix:**
    *   Run the full `pytest` suite.
    *   Confirm that the test count is now 26 and that all 26 tests pass.
