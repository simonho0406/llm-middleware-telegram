import re
import logging
from typing import Any, List, Union
try:
    import mistletoe
    from mistletoe import Document
    from mistletoe.renderers.base import BaseRenderer
    from mistletoe.block_tokens import BlockToken
    from mistletoe.span_tokens import SpanToken
except ImportError:
    mistletoe = None
    Document = None
    BaseRenderer = None
    BlockToken = None
    SpanToken = None

logger = logging.getLogger(__name__)

def escape_markdown_v2(text: str) -> str:
    """Escapes text for Telegram's MarkdownV2 parse mode."""
    if not text:
        return ""
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


# Legacy markdown processing functions removed - replaced with AST-based pipeline


# ================================
# AST-BASED ARCHITECTURE COMPONENTS
# ================================

# Only define AST-based classes if mistletoe is available
if mistletoe is not None and BaseRenderer is not None:
    
    class TelegramV2Renderer(BaseRenderer):
        """
        Custom AST-based renderer for Telegram MarkdownV2 format.
        
        This renderer provides context-aware escaping by understanding the document structure.
        It ensures special characters are only escaped in plain text content, not in formatting syntax.
        """
        
        def __init__(self):
            super().__init__()
            # Characters that need escaping in MarkdownV2 plain text contexts
            # Complete list: _ * [ ] ( ) ~ ` > # + - = | { } . ! \
            self.escape_chars = r'_*[]()~`>#+-=|{}.!\\'  # Backslash must be last
            # Manually register all missing renderers that mistletoe doesn't auto-detect
            self.render_map['LineBreak'] = self.render_line_break
            self.render_map['ThematicBreak'] = self.render_thematic_break
            self.render_map['SoftBreak'] = self.render_line_break  # Handle both soft and hard breaks
            self.render_map['HardLineBreak'] = self.render_line_break  # Handle both soft and hard breaks
            self.render_map['Strong'] = self.render_strong_emphasis  # Handle **bold** text
        
        def render_raw_text(self, token):
            """Render raw text with proper MarkdownV2 escaping."""
            if hasattr(token, 'content'):
                text = token.content
            else:
                text = str(token)
            
            # Escape special characters for MarkdownV2
            return re.sub(f'([{re.escape(self.escape_chars)}])', r'\\\1', text)
        
        def render_emphasis(self, token):
            """Render italic text (_text_) - MarkdownV2 uses underscores for italic."""
            return f"_{self.render_inner(token)}_"
        
        def render_strong_emphasis(self, token):
            """Render bold text (**text**).""" 
            return f"**{self.render_inner(token)}**"
        
        def render_inline_code(self, token):
            """Render inline code (`code`)."""
            return f"`{token.children[0].content}`"
        
        def render_code_fence(self, token):
            """Render code blocks (```code```)."""
            language = getattr(token, 'language', '') or ''
            code_content = token.children[0].content if token.children else ''
            return f"```{language}\n{code_content}```"
        
        def render_heading(self, token):
            """Render headings with bold formatting instead of # - MarkdownV2 doesn't support # headers."""
            content = self.render_inner(token)
            # MarkdownV2 doesn't support # headers, use bold text instead
            return f"**{content}**"
        
        def render_list(self, token):
            """Render lists with proper formatting."""
            items = []
            for i, item in enumerate(token.children):
                if hasattr(token, 'start') and token.start is not None:
                    # Ordered list - safely handle start value
                    try:
                        # If start is callable, call it; otherwise use it directly
                        start_num = token.start() if callable(token.start) else token.start
                        number = int(start_num) + i
                        items.append(f"{number}\\. {self.render_inner(item)}")
                    except (TypeError, ValueError, AttributeError):
                        # Fallback to sequential numbering starting from 1
                        number = i + 1
                        items.append(f"{number}\\. {self.render_inner(item)}")
                else:
                    # Unordered list - use escaped dash instead of bullet
                    items.append(f"\\- {self.render_inner(item)}")
            return '\n'.join(items)
        
        def render_list_item(self, token):
            """Render individual list items."""
            return self.render_inner(token)
        
        def render_quote(self, token):
            """Render blockquotes with escaped > characters."""
            content = self.render_inner(token)
            # Split by lines and add escaped > to each line
            lines = content.split('\n')
            return '\n'.join(f"\\> {line}" for line in lines)
        
        def render_link(self, token):
            """Render links [text](url)."""
            text = self.render_inner(token)
            url = token.target
            return f"[{text}]({url})"
        
        def render_paragraph(self, token):
            """Render paragraphs with proper spacing."""
            return self.render_inner(token)
        
        def render_line_break(self, token):
            """Render line breaks (both hard and soft)."""
            if hasattr(token, 'soft') and token.soft:
                # Soft line break - treat as space in Telegram
                return ' '
            else:
                # Hard line break - treat as newline
                return '\n'
        
        def render_thematic_break(self, token):
            """Render thematic breaks (horizontal rules)."""
            return '\n---\n'
        
        def render_table(self, token):
            """Render tables as plain-text representation in code block."""
            rows = []
            
            # Process header row if it exists
            if hasattr(token, 'header') and token.header:
                header_cells = []
                for cell in token.header.children:
                    cell_content = self.render_inner(cell).strip()
                    header_cells.append(cell_content)
                
                # Create header row
                header_row = '| ' + ' | '.join(header_cells) + ' |'
                rows.append(header_row)
                
                # Create separator row
                separator = '|' + '|'.join(['----------' for _ in header_cells]) + '|'
                rows.append(separator)
            
            # Process data rows
            for child in token.children:
                if hasattr(child, 'children'):  # This is a table row
                    row_cells = []
                    for cell in child.children:
                        cell_content = self.render_inner(cell).strip()
                        row_cells.append(cell_content)
                    
                    row = '| ' + ' | '.join(row_cells) + ' |'
                    rows.append(row)
            
            # Join all rows and wrap in code block for alignment preservation
            table_content = '\n'.join(rows)
            return f'```\n{table_content}\n```'
        
        def render_inner(self, token):
            """Render the inner content of a token."""
            if hasattr(token, 'children') and token.children:
                return ''.join(self.render(child) for child in token.children)
            elif hasattr(token, 'content'):
                return self.render_raw_text(token)
            else:
                return ''
        
        def render_document(self, token):
            """Render a Document token (top-level container)."""
            return self.render_inner(token)

    
    class PlainTextRenderer(BaseRenderer):
        """
        Simple renderer that strips all formatting and returns clean plain text.
        Used as fallback when MarkdownV2 parsing fails.
        """

        def __init__(self):
            super().__init__()
            # Manually register all missing renderers that mistletoe doesn't auto-detect
            self.render_map['Strong'] = self.render_strong_emphasis  # Handle **bold** text

        def render_raw_text(self, token):
            """Render raw text without any escaping."""
            if hasattr(token, 'content'):
                return token.content
            else:
                return str(token)
        
        def render_emphasis(self, token):
            """Render italic as plain text."""
            return self.render_inner(token)
        
        def render_strong_emphasis(self, token):
            """Render bold as plain text."""
            return self.render_inner(token)
        
        def render_inline_code(self, token):
            """Render inline code as plain text."""
            return token.children[0].content if token.children else ''
        
        def render_code_fence(self, token):
            """Render code blocks as plain text."""
            return token.children[0].content if token.children else ''
        
        def render_heading(self, token):
            """Render headings as plain text."""
            return self.render_inner(token)
        
        def render_list(self, token):
            """Render lists as plain text with simple bullets."""
            items = []
            for i, item in enumerate(token.children):
                if hasattr(token, 'start') and token.start is not None:
                    # Ordered list - safely handle start value
                    try:
                        # If start is callable, call it; otherwise use it directly
                        start_num = token.start() if callable(token.start) else token.start
                        number = int(start_num) + i
                        items.append(f"{number}. {self.render_inner(item)}")
                    except (TypeError, ValueError, AttributeError):
                        # Fallback to sequential numbering starting from 1
                        number = i + 1
                        items.append(f"{number}. {self.render_inner(item)}")
                else:
                    # Unordered list
                    items.append(f"• {self.render_inner(item)}")
            return '\n'.join(items)
        
        def render_list_item(self, token):
            """Render list items as plain text."""
            return self.render_inner(token)
        
        def render_quote(self, token):
            """Render blockquotes as plain text."""
            return self.render_inner(token)
        
        def render_link(self, token):
            """Render links as plain text with URL."""
            text = self.render_inner(token)
            url = token.target
            return f"{text} ({url})"
        
        def render_paragraph(self, token):
            """Render paragraphs as plain text."""
            return self.render_inner(token)
        
        def render_table(self, token):
            """Render tables as simple plain text without code block wrapping."""
            rows = []
            
            # Process header row if it exists
            if hasattr(token, 'header') and token.header:
                header_cells = []
                for cell in token.header.children:
                    cell_content = self.render_inner(cell).strip()
                    header_cells.append(cell_content)
                
                # Create header row
                header_row = '| ' + ' | '.join(header_cells) + ' |'
                rows.append(header_row)
                
                # Create separator row
                separator = '|' + '|'.join(['----------' for _ in header_cells]) + '|'
                rows.append(separator)
            
            # Process data rows
            for child in token.children:
                if hasattr(child, 'children'):  # This is a table row
                    row_cells = []
                    for cell in child.children:
                        cell_content = self.render_inner(cell).strip()
                        row_cells.append(cell_content)
                    
                    row = '| ' + ' | '.join(row_cells) + ' |'
                    rows.append(row)
            
            # Return plain text table without code block wrapping
            return '\n'.join(rows)
        
        def render_inner(self, token):
            """Render the inner content of a token."""
            if hasattr(token, 'children') and token.children:
                return ''.join(self.render(child) for child in token.children)
            elif hasattr(token, 'content'):
                return self.render_raw_text(token)
            else:
                return ''
        
        def render_document(self, token):
            """Render a Document token (top-level container)."""
            return self.render_inner(token)

    
    def split_document_ast_aware(document: 'Document', max_len: int = 4096) -> List['Document']:
        """
        Splits a Markdown AST document into smaller documents suitable for Telegram.
        
        This function respects the logical structure of the document, ensuring that
        blocks like lists, tables, and code blocks are never split in the middle.
        
        Args:
            document: A mistletoe Document object (AST)
            max_len: Maximum length per chunk (Telegram limit)
            
        Returns:
            List of Document objects, each containing a subset of the original blocks
        """
        renderer = TelegramV2Renderer()
        chunks = []
        current_blocks = []
        current_length = 0
        
        for block in document.children:
            # Render this block to estimate its length
            block_text = renderer.render(block)
            block_length = len(block_text)
            
            # If this single block exceeds max_len, it goes in its own chunk
            if block_length > max_len:
                # Finalize current chunk if it has content
                if current_blocks:
                    chunk_doc = Document(children=current_blocks)
                    chunks.append(chunk_doc)
                    current_blocks = []
                    current_length = 0

                # Create a chunk with just this oversized block
                oversized_doc = Document(children=[block])
                chunks.append(oversized_doc)
                continue
            
            # If adding this block would exceed the limit, finalize current chunk
            if current_length + block_length > max_len and current_blocks:
                chunk_doc = Document(children=current_blocks)
                chunks.append(chunk_doc)
                current_blocks = []
                current_length = 0
            
            # Add this block to the current chunk
            current_blocks.append(block)
            current_length += block_length
        
        # Add the final chunk if it has content
        if current_blocks:
            chunk_doc = Document(children=current_blocks)
            chunks.append(chunk_doc)
        
        return chunks

    
    def parse_markdown_to_ast(markdown_text: str) -> 'Document':
        """
        Parse Markdown text into an AST using mistletoe.

        Args:
            markdown_text: Pure Markdown text

        Returns:
            mistletoe Document object (AST)
        """
        # Use the correct API: Document.read() expects string or list of lines
        return Document.read(markdown_text)

    
    def render_ast_to_telegram_v2(document: 'Document') -> str:
        """
        Render a Markdown AST to Telegram MarkdownV2 format.
        
        Args:
            document: mistletoe Document object
            
        Returns:
            Telegram MarkdownV2-formatted string
        """
        renderer = TelegramV2Renderer()
        return renderer.render(document)

    
    def render_ast_to_plain_text(document: 'Document') -> str:
        """
        Render a Markdown AST to clean plain text (fallback).

        Args:
            document: mistletoe Document object

        Returns:
            Clean plain text string
        """
        renderer = PlainTextRenderer()
        return renderer.render(document)


    def format_for_telegram_v2(markdown_text: str) -> str:
        """
        High-level function to format Markdown text for Telegram MarkdownV2.

        This is the single authoritative formatting function that uses AST-based processing.

        Args:
            markdown_text: Pure Markdown text

        Returns:
            Telegram MarkdownV2-formatted string

        Raises:
            Exception: If AST processing fails for any reason
        """
        try:
            # Parse the markdown into an AST
            document = parse_markdown_to_ast(markdown_text)

            # Render to Telegram MarkdownV2
            return render_ast_to_telegram_v2(document)

        except Exception as e:
            # Re-raise with context for the caller to handle fallback
            logger.error(f"AST-based formatting failed: {e}")
            raise Exception(f"AST formatting failed: {e}") from e

else:
    # Placeholder/stub versions when mistletoe is not available
    def split_document_ast_aware(document, max_len: int = 4096):
        """
        Fallback function when mistletoe is not available.
        """
        raise ImportError("mistletoe library not available. Install with: pip install mistletoe-ebp")
    
    def parse_markdown_to_ast(markdown_text: str):
        """
        Fallback function when mistletoe is not available.
        """
        raise ImportError("mistletoe library not available. Install with: pip install mistletoe-ebp")
    
    def render_ast_to_telegram_v2(document) -> str:
        """
        Fallback function when mistletoe is not available.
        """
        raise ImportError("mistletoe library not available. Install with: pip install mistletoe-ebp")
    
    def render_ast_to_plain_text(document) -> str:
        """
        Fallback function when mistletoe is not available.
        """
        raise ImportError("mistletoe library not available. Install with: pip install mistletoe-ebp")
