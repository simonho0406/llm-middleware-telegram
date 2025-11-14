# TICKET-052: Implement Pre-rendering Sanitizer for LLM Output

**User Story:**
As a user, I want the bot to correctly render complex Markdown that contains non-standard characters (like special hyphens or bullets) and simple HTML tags (like `<br>`), so that the final output is clean and well-formatted.

**The Problem:**
LLMs often produce output that is not strictly compliant with standard Markdown. They include:
1.  Unicode characters that look like but are not standard syntax (e.g., non-breaking hyphens `‑` instead of `-`, bullets `•` instead of `*` or `-`).
2.  Simple HTML tags for formatting (e.g., `<br>` for line breaks).

The current rendering pipeline expects clean Markdown and fails to correctly format these inputs, resulting in broken italics, un-rendered lists, and visible HTML tags in the final message.

**Acceptance Criteria:**
1.  **Create `sanitize_llm_output` function:**
    - A new function, `sanitize_llm_output(text: str) -> str`, will be created in `utils/text_processing.py`.
    - This function must perform the following string replacements on the input text:
        - Replace all occurrences of `<br>` and `<br/>` (case-insensitive) with a newline character (`\n`).
        - Replace the Unicode non-breaking hyphen (`‑`, U+2011) with a standard ASCII hyphen-minus (`-`, U+002D).
        - Replace the Unicode bullet (`•`, U+2022) with a hyphen and a space (`- `) to ensure it's treated as a list item.
        - Replace the Unicode narrow no-break space (` `, U+202F) with a standard space (` `).
        - Replace "smart quotes" (`“`, `”`, `‘`, `’`) with their standard ASCII equivalents (`"` and `'`).

2.  **Integrate Sanitizer into Messaging Pipeline:**
    - The `send_safe_message` function in `bot/messaging.py` must be updated.
    - It must call `sanitize_llm_output()` on the incoming text *before* the text is passed to the `parse_markdown_to_ast` function.
    - This ensures that all messages sent via this central function are sanitized, fixing the issue for regular chats, panel discussions, and all other features.

3.  **Verification:**
    - The specific text provided by the user in the bug report must now render correctly in Telegram, with all italics, lists, and line breaks appearing as intended.
