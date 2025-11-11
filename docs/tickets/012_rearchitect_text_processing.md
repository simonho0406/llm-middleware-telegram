# TICKET-012: Re-architect `utils/text_processing.py` for `markdown-it-py`

**Status:** CLOSED

**Problem:** With the new `markdown-it-py` dependency, the entire rendering pipeline in `utils/text_processing.py` must be replaced.

**Definition of Done:**
1.  **Completely overwrite** the file `utils/text_processing.py` with the new, correct implementation provided in this ticket. This new code uses the `markdown-it-py` token stream pattern.

```python
import re
import logging
from typing import List
from markdown_it import MarkdownIt
from mdit_py_plugins.table import table_plugin

logger = logging.getLogger(__name__)

# Configure the markdown-it parser
md = MarkdownIt("commonmark", {"breaks": True}).use(table_plugin)

def escape_markdown_v2(text: str) -> str:
    if not text:
        return ""
    escape_chars = r'_[]()~`>#+-=|{}.!'
    # In MarkdownV2, asterisks for bold/italic are only escaped if they are not part of a valid sequence.
    # The AST-based approach handles this contextually. For a simple escaper, we avoid escaping them.
    return re.sub(f'([\{re.escape(escape_chars)}])', r'\\\1', text)

class TelegramV2Renderer:
    def render(self, tokens: List) -> str:
        self.text = ""
        for token in tokens:
            handler_name = f"render_{token.type}"
            handler = getattr(self, handler_name, self.render_default)
            handler(token)
        return self.text

    def render_default(self, token):
        if token.children:
            for child in token.children:
                handler_name = f"render_{child.type}"
                handler = getattr(self, handler_name, self.render_default)
                handler(child)

    def render_text(self, token):
        self.text += escape_markdown_v2(token.content)

    def render_paragraph_open(self, token): pass
    def render_paragraph_close(self, token): self.text += '\n\n'

    def render_heading_open(self, token):
        self.text += '*'
    def render_heading_close(self, token):
        self.text += '*\n'

    def render_bullet_list_open(self, token): pass
    def render_bullet_list_close(self, token): pass

    def render_ordered_list_open(self, token):
        self.list_item_number = int(token.info) if token.info else 1
    def render_ordered_list_close(self, token): pass

    def render_list_item_open(self, token):
        if hasattr(self, 'list_item_number'):
            self.text += f'{self.list_item_number}\. '
            self.list_item_number += 1
        else:
            self.text += '\- '
    def render_list_item_close(self, token): self.text += '\n'

    def render_inline(self, token):
        self.render_default(token)

    def render_strong_open(self, token): self.text += '*'
    def render_strong_close(self, token): self.text += '*'

    def render_em_open(self, token): self.text += '_'
    def render_em_close(self, token): self.text += '_'

    def render_code_inline(self, token):
        self.text += f'`{escape_markdown_v2(token.content)}`'

    def render_fence(self, token):
        lang = token.info.split()[0] if token.info else ''
        self.text += f'```{lang}\n{token.content}```'

    def render_link_open(self, token):
        self.text += '['
    def render_link_close(self, token):
        href = token.meta.get('href', '')
        self.text += f']({escape_markdown_v2(href)})'

def format_for_telegram_v2(markdown_text: str) -> str:
    tokens = md.parse(markdown_text)
    return TelegramV2Renderer().render(tokens)

def parse_markdown_to_ast(markdown_text: str):
    return md.parse(markdown_text)

def render_ast_to_telegram_v2(tokens) -> str:
    return TelegramV2Renderer().render(tokens)

def split_document_ast_aware(tokens: List, max_len: int = 4096) -> List[List]:
    # This is a placeholder implementation. A real implementation would be more sophisticated.
    # For now, we will not split to keep the fix focused.
    return [tokens]
```