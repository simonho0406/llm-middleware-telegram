
# TICKET-045: Add Graceful Network Error Handling to In-Flight Messages

**Status:** OPEN

**Epic:** Robustness

**Problem:**
A transient `httpx.ConnectError` when editing a placeholder message during a long-running panel discussion can crash the entire workflow. The same robust `try...except` logic used for initial messages needs to be applied to all in-flight `edit_text` calls.

**Definition of Done:**

1.  **Identify all `edit_text` calls** within the `_run_refinement_cycle` and `_run_panel_workflow` functions in `bot/handlers/discuss_panel_handler.py`.
2.  **Wrap each of these calls** in a `try...except (telegram.error.NetworkError, telegram.error.TimedOut)` block.
3.  Inside the `except` block, log a clear warning (e.g., `logger.warning("Failed to update placeholder message: %s", e)`) but **do not** re-raise the exception. The operation should continue silently. The user can tolerate a missed status update, but not a crashed workflow.
4.  No new tests are required, as this is a non-functional change that is difficult to reliably simulate in a unit test. This is a pure hardening task based on live logs.
