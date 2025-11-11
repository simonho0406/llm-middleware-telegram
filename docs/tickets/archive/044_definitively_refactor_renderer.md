# TICKET-044: Definitively Refactor Renderer Using a Validated Escaping Strategy

**Status:** OPEN

**Epic:** Refactoring

**Problem:**
Our markdown rendering pipeline is architecturally flawed, causing numerous formatting bugs. Past attempts at fixing this have been tactical workarounds. We need to implement a robust, two-stage rendering architecture, but first, we must definitively validate the core `python-telegram-bot` escaping helper to ensure we are not building on an unstable foundation.

**Definition of Done (TDD):**

1.  **Phase 1: Validate the Core Tool.**
    *   In `tests/test_text_processing.py`, add a new, isolated test named `test_telegram_helpers_escape_markdown_is_correct`.
    *   This test must NOT use any custom renderer. It will call `telegram.helpers.escape_markdown(text, version=2)` directly.
    *   The input `text` will be a complex string containing all known special characters that need escaping for MarkdownV2: `_ * [ ] ( ) ~ ` > # + - = | {{ }} . !`
    *   The test will assert that the function's output is an exactly correct, fully-escaped string.
    *   **This test must be written and pass before any other changes are made.** This proves the reliability of our core dependency.

2.  **Phase 2: Implement the Correct Architecture.**
    *   In `utils/text_processing.py`, refactor the `TelegramV2Renderer` class with a clean separation of concerns:
        *   **Structural Rendering:** Methods like `render_strong_open` and `render_list_item_open` will only output Telegram's structural syntax (e.g., `*`, `\- `).
        *   **Content Rendering:** The `render_text` method will delegate all character escaping to the now-validated `escape_markdown(token.content, version=2)` function.
    *   The `format_for_telegram_v2` function will orchestrate this process: parse the markdown, then render it with the new, robust `TelegramV2Renderer`.

3.  **Phase 3: Final Verification.**
    *   Run the entire `pytest` suite.
    *   All tests in `tests/test_text_processing.py` must pass, including the new validation test from Phase 1 and all the existing tests for lists, links, etc., which are now being stress-tested against the new architecture.
