
# TICKET-010: Refactor Presentation Layer from `mistletoe-ebp` to `markdown-it-py`

**Status:** CLOSED (Superseded)

**Phase:** B

**Problem:** The current rendering library, `mistletoe-ebp`, is unmaintained and represents a significant architectural risk. We are migrating to the modern, industry-standard `markdown-it-py` library.

**Definition of Done:**
1.  **Update Dependencies:** In `requirements.txt`, **replace** the line `mistletoe-ebp` with the following two lines:
    ```
    markdown-it-py
    mdit-py-plugins
    ```
2.  **Re-architect `utils/text_processing.py`:** Replace the entire file's content with a new implementation based on `markdown-it-py`. The new implementation must provide the same core functions (`format_for_telegram_v2`, `parse_markdown_to_ast`, etc.) but use `markdown-it-py`'s token-based rendering pattern.
3.  **Update `test_text_processing.py`:** The existing tests will fail. Update them to correctly test the output of the new `markdown-it-py` based renderer. The goal is to have all tests for this file passing again.
