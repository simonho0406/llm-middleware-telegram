# TICKET-018: Finalize and Verify Rendering Engine Migration

**Status:** OPEN

**Problem:** The rendering engine migration is in a corrupted, intermediate state. `utils/text_processing.py` contains a mix of old and new code, and `tests/test_text_processing.py` is obsolete. This ticket will perform a single, atomic operation to fix both files and bring the test suite to a passing state.

**Definition of Done:**

1.  **Overwrite `utils/text_processing.py`** with the correct, clean code below. This version removes all `mistletoe` remnants and correctly implements the `markdown-it-py` pipeline.

    ```python
    import re
    import logging
    from typing import List, Dict, Any
    from markdown_it import MarkdownIt
    from mdit_py_plugins.table import table_plugin

    logger = logging.getLogger(__name__)

    # Configure the markdown-it parser
    md = MarkdownIt("commonmark", {"breaks": True, "html": False}).use(table_plugin)

    def escape_markdown_v2(text: str) -> str:
        if not text:
            return ""
        # Escape all special characters for Telegram MarkdownV2
        escape_chars = r'_[]()~`>#+-=|{}.!'
        return re.sub(f'([\\{re.escape(escape_chars)}])', r'\\\\1', text)

    class TelegramV2Renderer:
        def render(self, tokens: List[Dict[str, Any]]) -> str:
            self.text = ""
            for i, token in enumerate(tokens):
                handler_name = f"render_{token.type}"
                handler = getattr(self, handler_name, self.render_default)
                handler(token, tokens, i)
            return self.text.strip()

        def render_default(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            if token.children:
                for child in token.children:
                    handler_name = f"render_{child.type}"
                    handler = getattr(self, handler_name, self.render_default)
                    # Pass along the context if needed, though most children don't need it
                    handler(child, token.children, -1)

        def render_text(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            self.text += escape_markdown_v2(token.content)

        def render_paragraph_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass
        def render_paragraph_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            # Add newlines only if it's not the absolute last token
            if index < len(tokens) - 1:
                self.text += '\n\n'

        def render_heading_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            self.text += '*'
        def render_heading_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            self.text += '*'

        def render_bullet_list_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass
        def render_bullet_list_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass

        def render_ordered_list_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            self.list_item_number = int(token.info) if token.info else 1
        def render_ordered_list_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass

        def render_list_item_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            if hasattr(self, 'list_item_number'):
                self.text += f'{self.list_item_number}. '
                self.list_item_number += 1
            else:
                self.text += '- '
        def render_list_item_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '\n'

        def render_inline(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            self.render_default(token, tokens, index)

        def render_strong_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '*'
        def render_strong_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '*'

        def render_em_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '_'
        def render_em_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '_'

        def render_code_inline(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            self.text += f'`{escape_markdown_v2(token.content)}`'

        def render_fence(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            lang = token.info.split()[0] if token.info else ''
            self.text += f'```{lang}\n{escape_markdown_v2(token.content)}\n```'

        def render_link_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            self.text += '['
        def render_link_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
            href = token.attrs.get('href', '')
            self.text += f']({escape_markdown_v2(href)})'

    def format_for_telegram_v2(markdown_text: str) -> str:
        tokens = md.parse(markdown_text)
        return TelegramV2Renderer().render(tokens)

    def parse_markdown_to_ast(markdown_text: str) -> List[Dict[str, Any]]:
        return md.parse(markdown_text)

    def render_ast_to_telegram_v2(tokens: List[Dict[str, Any]]) -> str:
        return TelegramV2Renderer().render(tokens)

    def split_document_ast_aware(tokens: List[Dict[str, Any]], max_len: int = 4096) -> List[List[Dict[str, Any]]]:
        return [tokens] # Placeholder
    ```

2.  **Overwrite `tests/test_text_processing.py`** with the new, correct test suite below. This suite properly tests the `TelegramV2Renderer`.

    ```python
    import pytest
    import sys
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from utils.text_processing import format_for_telegram_v2

    class TestTelegramV2Renderer:
        def test_headings(self):
            assert format_for_telegram_v2('# Hello') == '*Hello*'

        def test_bold(self):
            assert format_for_telegram_v2('**bold**') == '*bold*'

        def test_italic(self):
            assert format_for_telegram_v2('*italic*') == '_italic_'

        def test_inline_code(self):
            assert format_for_telegram_v2('`code`') == '`code`'

        def test_links(self):
            md = '[Google](https://google.com)'
            expected = '[Google](https://google\.com)'
            assert format_for_telegram_v2(md) == expected

        def test_unordered_list(self):
            md = '- one\n- two'
            expected = '- one\n- two'
            assert format_for_telegram_v2(md) == expected

        def test_ordered_list(self):
            md = '1. one\n2. two'
            expected = '1. one\n2. two'
            assert format_for_telegram_v2(md) == expected

        def test_escaping_simple(self):
            md = 'Hello. World! (test)'
            expected = 'Hello\. World\! \(test\)'
            assert format_for_telegram_v2(md) == expected

        def test_code_block(self):
            md = '```python\nprint("hello.world")\n```'
            expected = '```python\nprint("hello\.world")\n```'
            assert format_for_telegram_v2(md) == expected
    ```