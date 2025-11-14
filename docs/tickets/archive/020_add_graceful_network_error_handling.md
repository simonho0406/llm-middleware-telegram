
# TICKET-020: Add Graceful Handling for Network Errors

**Status:** CLOSED

**Epic:** Robustness

**Problem:** A `telegram.error.NetworkError` in any handler currently crashes the operation and fills the logs with a long, unhelpful traceback. The application should be resilient to transient network issues.

**Definition of Done:**
1.  Wrap the initial `context.bot.send_message` calls in the entry points of major handlers (`start_panel_discussion`, `handle_message`, `search_command`, etc.) in a `try...except telegram.error.NetworkError` block.
2.  In the `except` block, log a clean, single-line error message (e.g., `logger.error("Network error while sending initial message in X handler: %s", e)`).
3.  Optionally, attempt to send a single, simple error message to the user (e.g., "A network error occurred, please try again."), but this action itself should be wrapped in a `try...except` to prevent cascading failures.
