
# TICKET-017: Implement True AST-Aware Document Splitting

**Status:** CLOSED

**Problem:** The `split_document_ast_aware` function in `utils/text_processing.py` is currently a placeholder and does not correctly split messages while respecting Markdown block boundaries.

**Definition of Done:**
1.  Overwrite the `utils/text_processing.py` file.
2.  In the new version, replace the placeholder implementation of `split_document_ast_aware` with a real implementation that correctly chunks the `markdown-it-py` token stream into a `List[List[Token]]`.
3.  The logic should iterate through the token stream and group tokens into chunks, starting a new chunk after a top-level block element (like a paragraph, list, or heading) closes.

```python
# This is the new implementation for split_document_ast_aware
def split_document_ast_aware(tokens: List, max_len: int = 4096) -> List[List]:
    chunks = []
    current_chunk = []
    current_length = 0
    # A simplistic approach to identify tokens that can end a block
    block_enders = {'paragraph_close', 'heading_close', 'fence', 'bullet_list_close', 'ordered_list_close'}

    for token in tokens:
        # Estimate token length (this is a rough guess, but better than nothing)
        token_len = len(token.content) if token.content else 20
        
        if current_length + token_len > max_len and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_length = 0
        
        current_chunk.append(token)
        current_length += token_len

        if token.type in block_enders:
            chunks.append(current_chunk)
            current_chunk = []
            current_length = 0

    if current_chunk:
        chunks.append(current_chunk)

    return [c for c in chunks if c] # Filter out empty lists
```
