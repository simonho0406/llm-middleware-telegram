import re
import logging

logger = logging.getLogger(__name__)

def escape_markdown_v2(text: str) -> str:
    """Escapes text for Telegram's MarkdownV2 parse mode."""
    if not text:
        return ""
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

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
