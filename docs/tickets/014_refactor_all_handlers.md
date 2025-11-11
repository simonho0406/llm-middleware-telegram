
# TICKET-014: Refactor All Handlers to Use New `markdown-it-py` Pipeline

**Status:** CLOSED

**Problem:** The migration to `markdown-it-py` breaks the rendering logic in multiple message handlers. We must refactor all of them to use the new, correct `parse -> split -> render` workflow.

**Definition of Done:**
1.  For each file below, locate the code that sends messages.
2.  Replace the old rendering logic with the new, correct AST pipeline pattern:
    ```python
    # 1. Parse to token stream
    tokens = parse_markdown_to_ast(raw_text)
    # 2. Split token stream into chunks
    token_chunks = split_document_ast_aware(tokens)
    # 3. Render and send each chunk
    for chunk in token_chunks:
        chunk_text = render_ast_to_telegram_v2(chunk)
        # ... send message with chunk_text ...
    ```
3.  **Affected Files:**
    - `bot/messaging.py`
    - `bot/handlers/discuss_panel_handler.py`
    - `bot/handlers/chat.py`
    - `bot/handlers/discuss_handler.py`
    - `bot/handlers/ask_selected_handler.py`
