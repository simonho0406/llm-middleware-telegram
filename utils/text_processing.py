import re
import logging

logger = logging.getLogger(__name__)

def escape_markdown_v2(text: str) -> str:
    """Escapes text for Telegram's MarkdownV2 parse mode."""
    if not text:
        return ""
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


def pure_markdown_to_telegram_v2(text: str) -> str:
    """
    Line-aware parser that converts pure Markdown to Telegram MarkdownV2 format.
    
    This function processes text line by line, preserving list markers, blockquotes,
    and headers while intelligently escaping special characters only in plain text.
    It handles nested formatting and malformed input gracefully.
    
    Args:
        text: Pure Markdown text from the Refiner agent
        
    Returns:
        Telegram MarkdownV2-safe text with proper escaping
    """
    if not text:
        return ""
    
    # Characters that need escaping in MarkdownV2 when not part of formatting
    escape_chars = r'()#+-=|{}.!'
    
    lines = text.split('\n')
    processed_lines = []
    in_code_block = False
    
    for line in lines:
        if in_code_block:
            # Inside code block - preserve everything as-is until we find closing ```
            if line.strip() == '```' or line.strip().startswith('```'):
                in_code_block = False
            processed_lines.append(line)
            continue
        
        # Check for code block start
        if line.strip().startswith('```'):
            in_code_block = True
            processed_lines.append(line)
            continue
        
        # Process this line with line-aware formatting preservation
        processed_line = _process_line_with_formatting_preservation(line, escape_chars)
        processed_lines.append(processed_line)
    
    return '\n'.join(processed_lines)


def _process_line_with_formatting_preservation(line: str, escape_chars: str) -> str:
    """
    Process a single line, preserving markdown list markers, blockquotes, and headers
    while escaping special characters in plain text portions.
    """
    if not line:
        return line
    
    # Detect line-level markdown structures that should be preserved
    stripped = line.lstrip()
    
    # Preserve list markers (-, *, +, 1., 2., etc.)
    if re.match(r'^[-*+]\s', stripped) or re.match(r'^\d+\.\s', stripped):
        # Find where the list content starts after the marker
        marker_match = re.match(r'^([-*+]|\d+\.)\s*', stripped)
        if marker_match:
            indent = line[:len(line) - len(stripped)]  # Preserve indentation
            marker = marker_match.group(0)
            content = stripped[len(marker):]
            processed_content = _process_text_with_inline_formatting(content, escape_chars)
            return indent + marker + processed_content
    
    # Preserve blockquotes
    if stripped.startswith('> '):
        indent = line[:len(line) - len(stripped)]
        content = stripped[2:]  # Remove "> "
        processed_content = _process_text_with_inline_formatting(content, escape_chars)
        return indent + '> ' + processed_content
    
    # Preserve headers
    if stripped.startswith('#'):
        header_match = re.match(r'^(#+)\s*', stripped)
        if header_match:
            indent = line[:len(line) - len(stripped)]
            header_prefix = header_match.group(0)
            content = stripped[len(header_prefix):]
            processed_content = _process_text_with_inline_formatting(content, escape_chars)
            return indent + header_prefix + processed_content
    
    # Regular line - process with full formatting
    return _process_text_with_inline_formatting(line, escape_chars)


def _process_text_with_inline_formatting(text: str, escape_chars: str) -> str:
    """
    Process text for inline formatting like *bold*, _italic_, `code`, handling nesting gracefully.
    Uses a more robust approach than simple find() to handle complex cases.
    """
    if not text:
        return ""
    
    result = []
    i = 0
    length = len(text)
    
    while i < length:
        char = text[i]
        
        # Handle single backtick code spans first (highest precedence)
        if char == '`':
            code_span, new_i = _extract_code_span(text, i)
            if code_span:
                result.append(code_span)
                i = new_i
                continue
            else:
                # Unclosed backtick - pass through (backticks don't need escaping)
                result.append(char)
                i += 1
                continue
        
        # Handle bold formatting (**text** or __text__)
        elif char == '*' and i < length - 1 and text[i + 1] == '*':
            bold_text, new_i = _extract_formatting_span(text, i, '**')
            if bold_text:
                result.append(bold_text)
                i = new_i
                continue
            else:
                # Unclosed ** - escape the first *
                result.append('\\*')
                i += 1
                continue
                
        elif char == '_' and i < length - 1 and text[i + 1] == '_':
            bold_text, new_i = _extract_formatting_span(text, i, '__')
            if bold_text:
                result.append(bold_text)
                i = new_i
                continue
            else:
                # Unclosed __ - escape the first _
                result.append('\\_')
                i += 1
                continue
        
        # Handle italic formatting (*text* or _text_)
        elif char == '*':
            italic_text, new_i = _extract_formatting_span(text, i, '*')
            if italic_text:
                result.append(italic_text)
                i = new_i
                continue
            else:
                # Unclosed * - escape it
                result.append('\\*')
                i += 1
                continue
                
        elif char == '_':
            italic_text, new_i = _extract_formatting_span(text, i, '_')
            if italic_text:
                result.append(italic_text)
                i = new_i
                continue
            else:
                # Unclosed _ - escape it
                result.append('\\_')
                i += 1
                continue
        
        # Handle characters that need escaping in plain text
        elif char in escape_chars:
            result.append('\\' + char)
            i += 1
        
        # Regular characters - pass through unchanged
        else:
            result.append(char)
            i += 1
    
    return ''.join(result)


def _extract_code_span(text: str, start_pos: int) -> tuple[str, int]:
    """Extract a code span (`...`) if properly closed, handling nesting gracefully."""
    if text[start_pos] != '`':
        return None, start_pos
    
    # Find the matching closing backtick
    end_pos = text.find('`', start_pos + 1)
    if end_pos != -1:
        return text[start_pos:end_pos + 1], end_pos + 1
    else:
        return None, start_pos


def _extract_formatting_span(text: str, start_pos: int, delimiter: str) -> tuple[str, int]:
    """Extract a formatting span (like **bold** or *italic*) if properly closed."""
    if not text[start_pos:].startswith(delimiter):
        return None, start_pos
    
    # Look for matching closing delimiter, but be smarter about nesting
    search_start = start_pos + len(delimiter)
    end_pos = text.find(delimiter, search_start)
    
    if end_pos != -1:
        # Basic validation: ensure the content between delimiters isn't empty
        content = text[search_start:end_pos]
        if content.strip():  # Non-empty content
            return text[start_pos:end_pos + len(delimiter)], end_pos + len(delimiter)
    
    return None, start_pos

TELEGRAM_MAX_LEN = 4096 # Default, can be overridden

def split_message_markdown_aware(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """
    Splits a long message into chunks suitable for Telegram, respecting MarkdownV2 code blocks.

    Args:
        text: The full text message to split.
        max_len: The maximum length allowed per chunk (Telegram limit).

    Returns:
        A list of text chunks, each under max_len.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    current_chunk = ""
    in_code_block = False
    code_block_delimiter = ""
    lines = text.splitlines(keepends=True) # Keep newlines for accurate length

    for line in lines:
        # Detect start/end of code blocks
        if line.strip().startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_block_delimiter = line.strip()
            # Check if this is the matching closing delimiter
            elif line.strip() == code_block_delimiter:
                 in_code_block = False
            # Handle nested or mismatched blocks simply by toggling state
            # This isn't perfect parsing but handles common cases.

        # --- Check if adding the next line exceeds max_len ---
        if len(current_chunk) + len(line) > max_len:
            # If we are inside a code block, we cannot split here.
            # We need to backtrack and split *before* the code block started,
            # or if the block itself is too long, split forcefully (though this is less ideal).
            if in_code_block:
                 # Option 1: If the current chunk (before this line) is valid, push it.
                 if current_chunk:
                     logger.debug(f"Splitting before code block due to length limit.")
                     chunks.append(current_chunk)
                     current_chunk = ""
                 # Option 2: If the code block line *itself* is too long (rare)
                 elif len(line) > max_len:
                     logger.warning("Code block line exceeds max_len, splitting forcefully.")
                     # Force split the long line (might break rendering)
                     chunks.append(line[:max_len])
                     current_chunk = line[max_len:] # Start next chunk with the remainder
                     continue # Skip normal append below

            # If not in a code block, try to find a good split point
            else:
                # Prefer splitting at double newline (paragraph break)
                split_pos = current_chunk.rfind('\n\n')
                if split_pos != -1:
                    chunks.append(current_chunk[:split_pos + 2]) # Include the double newline
                    current_chunk = current_chunk[split_pos + 2:]
                # Otherwise, try splitting at the last single newline
                elif '\n' in current_chunk:
                     split_pos = current_chunk.rfind('\n')
                     chunks.append(current_chunk[:split_pos + 1]) # Include the newline
                     current_chunk = current_chunk[split_pos + 1:]
                # If no newline, split at the last space
                elif ' ' in current_chunk:
                    split_pos = current_chunk.rfind(' ')
                    chunks.append(current_chunk[:split_pos + 1]) # Include the space
                    current_chunk = current_chunk[split_pos + 1:]
                # Force split if no good point found (long word/line)
                else:
                    logger.warning("Force splitting text as no paragraph/space break found.")
                    chunks.append(current_chunk[:max_len])
                    current_chunk = current_chunk[max_len:]

        # Add the line to the potentially modified current_chunk
        current_chunk += line

    # Add the last remaining chunk
    if current_chunk:
        # If the last chunk is still too long (e.g., a huge code block at the end)
        while len(current_chunk) > max_len:
             logger.warning("Last chunk exceeds max_len, force splitting.")
             # Try to split gracefully first if possible within the remainder
             split_point = max_len
             if in_code_block: # Less ideal to split code blocks, but necessary
                 pass # Force split at max_len
             else:
                 # Try splitting at newline/space if possible
                 nl_pos = current_chunk.rfind('\n', 0, max_len)
                 sp_pos = current_chunk.rfind(' ', 0, max_len)
                 best_pos = max(nl_pos, sp_pos)
                 if best_pos > 0:
                     split_point = best_pos + 1 # Include the newline/space

             chunks.append(current_chunk[:split_point])
             current_chunk = current_chunk[split_point:]
        chunks.append(current_chunk) # Add the final part

    # Filter out empty chunks that might result from splitting logic
    return [chunk for chunk in chunks if chunk.strip()]
