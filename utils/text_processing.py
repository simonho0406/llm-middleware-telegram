import re
import logging
from typing import List, Dict, Any
from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

md = MarkdownIt("commonmark", {"breaks": True, "html": False}).enable('table')

from telegram.helpers import escape_markdown

class TelegramV2Renderer:
    def __init__(self):
        self.text = ""
        self.list_level = 0
        self.link_href = None
        self.ordered_list_stack = [] # To keep track of item numbers for nested ordered lists
        self.is_ordered_list = False # Flag to indicate if current list is ordered

    def render(self, tokens: List[Dict[str, Any]]) -> str:
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
                handler(child, token.children, -1)

    def render_text(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += escape_markdown(token.content, version=2)

    def render_paragraph_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass
    def render_paragraph_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        if self.list_level == 0 and index < len(tokens) - 1:
            self.text += '\n\n'

    def render_heading_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '*'
    def render_heading_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '*\n'

    def render_bullet_list_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.list_level += 1
        self.is_ordered_list = False # Set flag for unordered list
    def render_bullet_list_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.list_level -= 1
        if self.list_level == 0: self.text += '\n'

    def render_ordered_list_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.list_level += 1
        start = int(token.attrs.get('start', 1)) if token.attrs else 1
        self.ordered_list_stack.append(start)
        self.is_ordered_list = True # Set flag for ordered list
    def render_ordered_list_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.list_level -= 1
        self.ordered_list_stack.pop()
        if self.list_level == 0: self.text += '\n'

    def render_list_item_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        if self.is_ordered_list and self.ordered_list_stack: # Check flag and stack
            current_number = self.ordered_list_stack[-1]
            self.text += f'{current_number}\\. ' # Number and escaped dot
            self.ordered_list_stack[-1] += 1 # Increment for next item in this ordered list
        else:
            self.text += '\\- ' # Escaped hyphen for unordered list
    def render_list_item_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '\n'

    def render_inline(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.render_default(token, tokens, index)

    def render_strong_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '*'
    def render_strong_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '*'

    def render_em_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '_'
    def render_em_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '_'

    def render_code_inline(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += f'`{escape_markdown(token.content, version=2)}`'

    def render_fence(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        lang = token.info.split()[0] if token.info else ''
        content = token.content.rstrip()
        self.text += f'```{lang}\n{content}\n```\n'

    def render_link_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '['
        self.link_href = token.attrs.get('href', '')
    def render_link_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        href = self.link_href or ''
        self.text += f']({escape_markdown(href, version=2)})'
        self.link_href = None

def format_for_telegram_v2(markdown_text: str) -> str:
    tokens = md.parse(markdown_text)
    return TelegramV2Renderer().render(tokens)

def parse_markdown_to_ast(markdown_text: str) -> List[Dict[str, Any]]:
    return md.parse(markdown_text)

def render_ast_to_telegram_v2(tokens: List[Dict[str, Any]]) -> str:
    return TelegramV2Renderer().render(tokens)

def split_document_ast_aware(tokens: List, max_len: int = 4096) -> List[List]:
    """
    Splits a token stream into chunks of a maximum length, respecting block boundaries.
    This implementation is simplified to only split when necessary.
    """
    chunks = []
    current_chunk = []
    current_length = 0

    for token in tokens:
        # This is a very rough estimation of rendered length.
        # A more accurate method would be to render the token, but that's expensive.
        token_len = len(token.content) if token.content else 20

        if current_length + token_len > max_len and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_length = 0
        
        current_chunk.append(token)
        current_length += token_len

    if current_chunk:
        chunks.append(current_chunk)

    return [c for c in chunks if render_ast_to_telegram_v2(c)]
