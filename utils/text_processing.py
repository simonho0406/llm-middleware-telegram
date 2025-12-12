import re
import logging
import telegram # Add this import
from typing import List, Dict, Any
from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

md = MarkdownIt("commonmark", {"breaks": True, "html": False}).enable('table')

def sanitize_text_characters(text: str) -> str:
    """
    Replaces various Unicode characters with their standard ASCII equivalents.
    This should only be applied to plain text, not code blocks.
    """
    if not isinstance(text, str):
        return text
    
    replacements = {
        '‑': '-',  # Non-breaking hyphen to standard hyphen
        '•': '- ',
        ' ': ' ',  # Narrow no-break space to standard space
        '“': '"',  # Smart quote to standard quote
        '”': '"',  # Smart quote to standard quote
        '‘': "'",  # Smart quote to standard quote
        '’': "'",  # Smart quote to standard quote
    }
    for unicode_char, ascii_char in replacements.items():
        text = text.replace(unicode_char, ascii_char)
        
    return text

class TelegramV2Renderer:
    def __init__(self):
        self.text = ""
        self.list_level = 0
        self.link_href = None
        self.ordered_list_stack = [] # To keep track of item numbers for nested ordered lists
        self.is_ordered_list = False # Flag to indicate if current list is ordered
        self.in_blockquote = False # Flag for blockquote context

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
        sanitized_content = sanitize_text_characters(token.content)
        escaped_text = telegram.helpers.escape_markdown(sanitized_content, version=2)
        if self.in_blockquote:
            # If inside blockquote, ensure newlines are prefixed with \>
            # But wait, render_text usually doesn't contain newlines unless it's a code block or we missed a split?
            # Actually, softbreaks are separate tokens.
            # If the text itself has newlines (unlikely for 'text' token in commonmark unless preserved?), we handle it.
            pass
        self.text += escaped_text

    def render_paragraph_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass
    def render_paragraph_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        # Only add double newline if it's not the last paragraph in the block
        if self.list_level == 0 and index < len(tokens) - 1:
            # Check if next token is blockquote_close
            next_token = tokens[index + 1]
            if next_token.type == 'blockquote_close':
                return
            
            self.text += '\n\n'
            if self.in_blockquote:
                self.text += '\\> '

    def render_heading_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '\n*'
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
        if self.ordered_list_stack:
            self.ordered_list_stack.pop()
        if self.list_level == 0: self.text += '\n'

    def render_list_item_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        # Check if this is a continuation item (hidden)
        if getattr(token, 'hidden', False):
            return

        if self.is_ordered_list and self.ordered_list_stack: # Check flag and stack
            current_number = self.ordered_list_stack[-1]
            self.text += f'{current_number}\\. ' # Number and escaped dot for ordered lists (preserves numbering across splits)
            self.ordered_list_stack[-1] += 1 # Increment for next item in this ordered list
        else:
            self.text += '\\- ' # Escaped hyphen for unordered list (safer than native bullet which can cause 'reserved character' errors)
    def render_list_item_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '\n'

    def render_softbreak(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '\n'
        if self.in_blockquote:
            self.text += '\\> '

    def render_hardbreak(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '\n'
        if self.in_blockquote:
            self.text += '\\> '

    def render_inline(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.render_default(token, tokens, index)

    def render_strong_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '*'
    def render_strong_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '*'

    def render_em_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '_'
    def render_em_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): self.text += '_'

    def render_code_inline(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += f'`{telegram.helpers.escape_markdown(token.content, version=2)}`'

    def render_fence(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        lang = token.info.split()[0] if token.info else ''
        content = token.content.rstrip()
        self.text += f'```{lang}\n{content}\n```\n'
        
    def render_code_block(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        # Indented code block -> Fenced code block
        content = token.content.rstrip()
        self.text += f'```\n{content}\n```\n'

    def render_blockquote_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '\\> '
        self.in_blockquote = True
    def render_blockquote_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '\n'
        self.in_blockquote = False
    
    def render_hr(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '\n\\-\\-\\-\n'

    def render_image(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        # Render image as a link: [Alt Text](URL)
        alt = token.content or "Image"
        src = token.attrs.get('src', '')
        self.text += f'[{telegram.helpers.escape_markdown(alt, version=2)}]({telegram.helpers.escape_markdown(src, version=2)})'

    def render_link_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '['
        self.link_href = token.attrs.get('href', '')
    def render_link_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        href = self.link_href or ''
        self.text += f']({telegram.helpers.escape_markdown(href, version=2)})'
        self.link_href = None

    # --- Table Handlers ---
    def render_table_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        # Telegram doesn't support tables. We'll try to render it as a list of lines.
        self.text += '\n'
    def render_table_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '\n'

    def render_thead_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass
    def render_thead_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass

    def render_tbody_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass
    def render_tbody_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass

    def render_tr_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass
    def render_tr_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '\n'

    def render_th_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '*' # Bold headers
    def render_th_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += '* | '

    def render_td_open(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int): pass
    def render_td_close(self, token: Dict[str, Any], tokens: List[Dict[str, Any]], index: int):
        self.text += ' | '

def format_for_telegram_v2(markdown_text: str) -> str:
    tokens = md.parse(markdown_text)
    return TelegramV2Renderer().render(tokens)

def parse_markdown_to_ast(markdown_text: str) -> List[Dict[str, Any]]:
    return md.parse(markdown_text)

def render_ast_to_telegram_v2(tokens: List[Dict[str, Any]]) -> str:
    return TelegramV2Renderer().render(tokens)

def replace_html_tags(text: str) -> str:
    """
    Replaces <br> tags with newlines.
    """
    if not isinstance(text, str):
        return text

    # Replace <br> tags with newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    return text

def split_document_ast_aware(tokens: List, max_len: int = 4096) -> List[List]:
    """
    Splits a token stream into chunks of a maximum length, respecting block boundaries
    and maintaining context (closing/re-opening blocks) across chunks.
    """
    chunks = []
    current_chunk = []
    current_len = 0
    
    # Stack of open tokens to track context
    block_stack = []
    
    # Stack of counters for ordered lists
    ordered_list_counters = [] 

    # Helper to clone a token
    def clone_token(t):
        new_t = type(t)(t.type, t.tag, t.nesting)
        new_t.attrs = t.attrs.copy() if t.attrs else {}
        new_t.map = t.map[:] if t.map else None
        new_t.level = t.level
        new_t.content = t.content
        new_t.info = t.info
        new_t.meta = t.meta.copy() if t.meta else {}
        new_t.block = t.block
        new_t.hidden = getattr(t, 'hidden', False)
        new_t.children = [clone_token(c) for c in t.children] if t.children else None
        return new_t

    # Helper to flush current chunk
    def flush_chunk():
        nonlocal current_chunk, current_len
        if not current_chunk: return

        # Close open blocks
        closing_tokens = []
        for open_token in reversed(block_stack):
            close_type = open_token.type.replace('_open', '_close')
            close_token = type(open_token)(close_type, open_token.tag, -1)
            close_token.level = open_token.level
            closing_tokens.append(close_token)
        
        current_chunk.extend(closing_tokens)
        chunks.append(current_chunk)
        
        # Start new chunk
        current_chunk = []
        current_len = 0
        
        # Re-open blocks
        for open_token in block_stack:
            new_open = clone_token(open_token)
            new_open.children = None # Block tokens don't have children usually
            
            if open_token.type == 'ordered_list_open':
                ol_index = 0
                for t in block_stack:
                    if t == open_token: break
                    if t.type == 'ordered_list_open': ol_index += 1
                if ol_index < len(ordered_list_counters):
                    new_open.attrs['start'] = ordered_list_counters[ol_index]
            
            if open_token.type == 'list_item_open':
                new_open.hidden = True
            
            current_chunk.append(new_open)
            current_len += 5

    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        
        # Calculate Length
        token_len = len(token.content) if token.content else 0
        token_len += 10 # Overhead
        
        # Check if token fits
        if current_len + token_len > max_len:
            # If current chunk is not empty, flush it first
            if current_chunk:
                flush_chunk()
            
            # Now current_chunk is empty (except re-opened blocks).
            # Check if token fits NOW.
            if current_len + token_len > max_len:
                # Token is HUGE. We must split it.
                remaining_space = max_len - current_len - 20
                
                if token.type == 'inline' and token.children:
                    # Split inline token by children
                    # We create a new inline token and fill it with children until full
                    
                    # We need to process children one by one
                    children_queue = token.children[:]
                    
                    while children_queue:
                        # Create a partial inline token
                        partial_inline = clone_token(token)
                        partial_inline.children = []
                        partial_content_len = 0
                        
                        while children_queue:
                            child = children_queue[0]
                            child_len = len(child.content) if child.content else 0
                            child_len += 5 # Overhead
                            
                            if current_len + partial_content_len + child_len < max_len:
                                # Child fits in current partial inline
                                partial_inline.children.append(child)
                                partial_content_len += child_len
                                children_queue.pop(0)
                            else:
                                # Child does NOT fit.
                                if not partial_inline.children:
                                    # Even a single child is too big!
                                    # We must split the child (if it's text).
                                    if child.type == 'text':
                                        space_for_text = max_len - current_len - 20
                                        if space_for_text > 0:
                                            part1_text = child.content[:space_for_text]
                                            part2_text = child.content[space_for_text:]
                                            
                                            child_part1 = clone_token(child)
                                            child_part1.content = part1_text
                                            
                                            child.content = part2_text # Update child for next pass
                                            
                                            partial_inline.children.append(child_part1)
                                            # Don't pop child from queue, we processed part of it
                                            break 
                                        else:
                                            # No space at all? Flush and retry.
                                            break
                                    else:
                                        # Can't split non-text child easily.
                                        # If partial_inline is empty, we are stuck.
                                        # Force add it and let it overflow? Or flush?
                                        break
                                else:
                                    # Partial inline is full.
                                    break
                        
                        if partial_inline.children:
                            current_chunk.append(partial_inline)
                            flush_chunk()
                        else:
                            # We couldn't add anything.
                            # If we just flushed, and still can't add, we are stuck.
                            # Force add one child to avoid infinite loop?
                            if children_queue:
                                force_child = children_queue.pop(0)
                                partial_inline.children.append(force_child)
                                current_chunk.append(partial_inline)
                                flush_chunk()
                    
                    continue # Done processing this huge inline token

                elif token.type in ['fence', 'code_block']:
                    # Split code block content
                    content = token.content
                    while content:
                        space = max_len - current_len - 20
                        if space <= 0:
                            flush_chunk()
                            space = max_len - current_len - 20
                        
                        part = content[:space]
                        content = content[space:]
                        
                        part_token = clone_token(token)
                        part_token.content = part
                        current_chunk.append(part_token)
                        
                        if content: # If there is more, we must flush
                            flush_chunk()
                    continue

        # Add Token
        current_chunk.append(token)
        current_len += token_len
        
        # Update State
        if token.type.endswith('_open'):
            block_stack.append(token)
            if token.type == 'ordered_list_open':
                start = 1
                if token.attrs and 'start' in token.attrs:
                    try:
                        start = int(token.attrs['start'])
                    except:
                        pass
                ordered_list_counters.append(start)
            elif token.type == 'list_item_open':
                for t in reversed(block_stack[:-1]):
                    if t.type == 'ordered_list_open':
                        if ordered_list_counters:
                            ordered_list_counters[-1] += 1
                        break
                    if t.type == 'bullet_list_open':
                        break

        elif token.type.endswith('_close'):
            if block_stack:
                popped = block_stack.pop()
                if popped.type == 'ordered_list_open':
                    if ordered_list_counters:
                        ordered_list_counters.pop()

    if current_chunk:
        chunks.append(current_chunk)

    return [c for c in chunks if render_ast_to_telegram_v2(c)]