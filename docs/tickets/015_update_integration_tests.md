
# TICKET-015: Update Integration Tests for `markdown-it-py`

**Status:** CLOSED (Obsolete)

**Problem:** The integration tests in `tests/test_handler_integration.py` will fail after the `markdown-it-py` migration due to changes in output formatting.

**Definition of Done:**
1.  Open `tests/test_handler_integration.py`.
2.  Review any tests that make assertions about the content of messages.
3.  Update these assertions to match the expected output from the new `markdown-it-py` rendering pipeline.
