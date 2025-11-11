
# TICKET-040: Fix Hyperactive Message Splitting

**Status:** OPEN

**Epic:** Bugfixes

**Problem:**
The `split_document_ast_aware` function creates a new message chunk for every single paragraph, regardless of length. This shatters long responses into dozens of tiny, unreadable messages.

**Definition of Done (TDD):**

1.  **Write a Failing Test:**
    *   In `tests/test_text_processing.py`, create a new test, `test_splitter_does_not_split_unnecessarily`.
    *   Create a multi-paragraph markdown string that is well under the `max_len` of 4096.
    *   Parse this string into a token stream.
    *   Pass the stream to `split_document_ast_aware`.
    *   Assert that the function returns a list containing exactly **one** chunk.
    *   The initial run of `pytest` must show this test failing (i.e., it will return multiple chunks).

2.  **Implement the Fix:**
    *   In `utils/text_processing.py`, modify the `split_document_ast_aware` function.
    *   **Remove** the logic that splits on `block_enders`. The function should *only* split a chunk when `current_length + token_len > max_len`.

3.  **Verify the Fix:**
    *   Run `pytest`. The new test should now pass.
