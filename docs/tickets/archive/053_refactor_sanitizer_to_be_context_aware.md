# TICKET-053: Refactor Sanitizer to be Context-Aware within the AST Renderer

**User Story:**
As a user, I want the bot to correctly render complex Markdown *without* corrupting the content of code blocks (` ``` `) or inline code (` `).

**The Problem:**
The current implementation of the sanitizer (TICKET-052) operates on the entire raw text before Markdown parsing. This is a critical flaw because it will incorrectly replace characters (e.g., special hyphens, quotes) *inside* code blocks, silently corrupting code snippets for the user. This is a major regression and a violation of user trust.

**Acceptance Criteria:**
1.  **Remove Global Sanitizer:**
    - The call to `sanitize_llm_output()` at the beginning of `send_safe_message` in `bot/messaging.py` **must be removed**. The raw, original text should be passed to the `parse_markdown_to_ast` function.

2.  **Integrate Sanitizer into Renderer:**
    - The `sanitize_llm_output` function (or its logic) in `utils/text_processing.py` will be used by the `TelegramV2Renderer`.
    - The `render_text` method within `TelegramV2Renderer` must be modified. It should first sanitize the content of the text token, and *then* escape the result for MarkdownV2.
    - **Crucially**, the `render_code_inline` and `render_fence` methods must **NOT** call the sanitizer. They should process their content literally to preserve the integrity of code blocks.

3.  **Handle HTML Tags:**
    - The `<br>` tag replacement can remain as a global pre-processing step, as these tags are unlikely to appear meaningfully inside a code block. The `sanitize_llm_output` function can be split into two: one for global HTML replacement and one for context-aware character sanitization within the renderer.
    - Alternatively, and more robustly, the `markdown-it-py` parser can be configured to handle `<br>` tags by converting them to `softbreak` or `hardbreak` tokens, which the renderer can then convert to a newline.

4.  **Verification:**
    - A test case must be considered where the bot is asked to produce a code block containing characters that the sanitizer would otherwise change (e.g., `my_dict = {'key‑name': 1}`).
    - The final rendered output must show the code block with its content completely unaltered.
    - The user's original example text from TICKET-052 must still render correctly.
