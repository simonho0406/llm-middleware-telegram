
# TICKET-007: Implement Correct AST-Aware Document Splitter

**Status:** OPEN

**Problem:** The `split_document_ast_aware` function is missing from `utils/text_processing.py`. We need a correct, efficient implementation that operates on AST `Document` objects, not raw strings.

**Definition of Done:**
1. Add the following complete and correct implementation of `split_document_ast_aware` to `utils/text_processing.py`. This function correctly takes a `Document` and returns a `List[Document]`.

```python
def split_document_ast_aware(document: Document, max_len: int = 4096) -> List[Document]:
    renderer = TelegramV2Renderer()
    chunks = []
    current_blocks = []
    current_length = 0
    
    for block in document.children:
        block_text = renderer.render(block)
        block_length = len(block_text)
        
        if block_length > max_len:
            if current_blocks:
                chunks.append(Document(current_blocks))
                current_blocks = []
                current_length = 0
            chunks.append(Document([block]))
            continue
        
        if current_length + block_length > max_len and current_blocks:
            chunks.append(Document(current_blocks))
            current_blocks = []
            current_length = 0
        
        current_blocks.append(block)
        current_length += block_length
    
    if current_blocks:
        chunks.append(Document(current_blocks))
    
    return chunks
```
