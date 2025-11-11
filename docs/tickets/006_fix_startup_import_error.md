# TICKET-006: Fix Startup `ImportError` for `split_document_ast_aware`

**Status:** CLOSED (Cancelled)

**Problem:** The application is crashing on startup with an `ImportError` because the `split_document_ast_aware` function was accidentally deleted from `utils/text_processing.py`.

**Evidence:**
```
ImportError: cannot import name 'split_document_ast_aware' from 'utils.text_processing' (/app/utils/text_processing.py)
```

**Definition of Done:**
1.  Add the following function definition back into the `utils/text_processing.py` file.
2.  Place it after the `format_for_telegram_v2` function but before the `parse_markdown_to_ast` stub.
3.  This implementation is a simplified, non-AST version for now. Its purpose is to make the application start.

```python
def split_document_ast_aware(doc, max_len=4096):
    # This is a simplified, non-AST fallback implementation to fix the startup crash.
    # It splits by lines, which is not ideal but will work for now.
    text = render_ast_to_telegram_v2(doc)
    lines = text.split('\n')
    chunks = []
    current_chunk = ''
    for line in lines:
        if len(current_chunk) + len(line) + 1 > max_len:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            if current_chunk:
                current_chunk += '\n' + line
            else:
                current_chunk = line
    if current_chunk:
        chunks.append(current_chunk)
    
    # The function is expected to return a list of Document objects
    return [Document(chunk) for chunk in chunks]
```

```