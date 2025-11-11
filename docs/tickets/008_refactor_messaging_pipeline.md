
# TICKET-008: Refactor `messaging.py` to Use Correct AST Pipeline

**Status:** CLOSED (Superseded)

**Problem:** `bot/messaging.py` uses the text processing pipeline incorrectly, passing strings to functions that expect AST objects. This needs to be refactored to follow the proper `parse -> split -> render` workflow.

**Definition of Done:**
1. In `bot/messaging.py`, modify the `send_safe_message` function.
2. Replace the existing `try...except` block with the following logic, which correctly orchestrates the AST pipeline:

```python
    try:
        # 1. Parse the entire text to an AST Document once.
        doc = parse_markdown_to_ast(escape_meta_tags(text))
        
        # 2. Split the AST Document into a list of smaller AST Documents.
        doc_chunks = split_document_ast_aware(doc)
        
        # 3. Iterate and render each AST chunk to a string for sending.
        for i, chunk_doc in enumerate(doc_chunks):
            chunk_text = render_ast_to_telegram_v2(chunk_doc)
            if not chunk_text.strip():
                continue

            # The rest of the sending logic (is_edit, etc.) remains the same.
            if is_edit and i == 0:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=update.callback_query.message.message_id,
                    text=chunk_text,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup if i == len(doc_chunks) - 1 else None
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=chunk_text,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup if i == len(doc_chunks) - 1 else None
                )

    except Exception as e:
        logger.warning(f"{log_prefix}AST pipeline failed: {e}. Falling back to simple text.")
        # The existing fallback logic remains the same.
        try:
            plain_text = escape_meta_tags(text)
            chunks = [plain_text[i:i+TELEGRAM_MAX_LEN] for i in range(0, len(plain_text), TELEGRAM_MAX_LEN)]
            for i, chunk in enumerate(chunks):
                if is_edit and i == 0:
                     await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=update.callback_query.message.message_id,
                        text=chunk,
                        parse_mode=None,
                        reply_markup=reply_markup if i == len(chunks) - 1 else None
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        parse_mode=None,
                        reply_markup=reply_markup if i == len(chunks) - 1 else None
                    )
        except Exception as final_e:
            logger.error(f"{log_prefix}Final fallback to plain text also failed: {final_e}")
```
