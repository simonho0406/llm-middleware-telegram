# TICKET-013: Update Test Suite for `markdown-it-py`

**Status:** CLOSED (Superseded)

**Problem:** After re-architecting `utils/text_processing.py` to use `markdown-it-py`, the entire test suite in `tests/test_text_processing.py` is obsolete and must be rewritten.

**Definition of Done:**
1.  **Completely overwrite** the file `tests/test_text_processing.py` with the new, correct test suite provided in this ticket. This new suite correctly tests the `markdown-it-py` pipeline.

```python
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils.text_processing import format_for_telegram_v2

class TestMarkdownItRenderer:
    def test_headings(self):
        assert format_for_telegram_v2('# Hello').strip() == '*Hello*'

    def test_bold(self):
        assert format_for_telegram_v2('**bold**').strip() == '*bold*'

    def test_italic(self):
        assert format_for_telegram_v2('*italic*').strip() == '_italic_'

    def test_inline_code(self):
        assert format_for_telegram_v2('`code`').strip() == '`code`'

    def test_links(self):
        md = '[Google](https://google.com)'
        expected = '[Google](https://google\.com)'
        assert format_for_telegram_v2(md).strip() == expected

    def test_unordered_list(self):
        md = '- one\n- two'
        expected = '\- one\n\- two'
        assert format_for_telegram_v2(md).strip() == expected

    def test_ordered_list(self):
        md = '1. one\n2. two'
        expected = '1\. one\n2\. two'
        assert format_for_telegram_v2(md).strip() == expected

    def test_escaping(self):
        md = 'Hello. World! (test)'
        expected = 'Hello\. World\! \(test\)'
        assert format_for_telegram_v2(md).strip() == expected
```
