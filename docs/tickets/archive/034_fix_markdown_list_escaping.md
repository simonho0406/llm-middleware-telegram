# TICKET-034: Fix MarkdownV2 Escaping for Lists

**Status:** OPEN

**Epic:** Bugfixes

**Problem:**
The `TelegramV2Renderer` does not properly escape hyphen characters for unordered lists, causing the `send_safe_message` function to fail and fall back to plain text, losing all formatting.

**Definition of Done (TDD):**

1.  **Write a Failing Test:**
    *   In `tests/test_text_processing.py`, add a new test to the `TestTelegramV2Renderer` class called `test_unordered_list_escaping`.
    *   The test should take a simple markdown unordered list (e.g., `- item 1\n- item 2`) and assert that the output from `format_for_telegram_v2` contains escaped hyphens (e.g., `\- item 1\n\- item 2`).
    *   The initial run of `pytest` must show this new test failing.

2.  **Implement the Fix:**
    *   In `utils/text_processing.py`, modify the `render_list_item_open` method in the `TelegramV2Renderer` class.
    *   Change the line `self.text += '- '` to `self.text += '\- '`, ensuring the hyphen is properly escaped for Telegram's MarkdownV2 parser.

3.  **Verify the Fix:**
    *   Run `pytest` again.
    *   Confirm that the `test_unordered_list_escaping` test now passes.

