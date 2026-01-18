import logging
import asyncio
import re
from telegram import Update, constants
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from utils.text_processing import (
    split_document_ast_aware, 
    parse_markdown_to_ast, 
    render_ast_to_telegram_v2,
    replace_html_tags # Import the new function
)

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096

def escape_meta_tags(text: str) -> str:
    """Removes meta tags like <reflect> from the text."""
    if not text:
        return ""
    text = re.sub(r"<reflect>.*?</reflect>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()

async def send_safe_message(
    context: ContextTypes.DEFAULT_TYPE, 
    update: Update, 
    text: str, 
    reply_markup=None,
    is_edit: bool = False,
    force_new: bool = False
):
    """
    A centralized and safe message sending function.
    It takes raw text, processes it through the AST pipeline, and handles
    Telegram's MarkdownV2 parsing, splitting, and fallbacks.
    It can handle both new messages and edits from callback queries.
    """
    if not text:
        logger.warning("send_safe_message called with empty text.")
        return

    chat_id = update.effective_chat.id
    log_prefix = f"(Chat {chat_id}) "
    
    # Determine if it's an edit based on the explicit parameter or the presence of a callback query.
    # If force_new is True, explicitly disable edit mode.
    is_edit = (is_edit or (update.callback_query is not None)) and not force_new
    
    reply_to_msg_id = update.effective_message.message_id if update.effective_message else None

    try:
        # 0. Replace HTML tags globally.
        processed_text = replace_html_tags(text)
        
        # 1. Parse the entire text to an AST Document once.
        doc = parse_markdown_to_ast(escape_meta_tags(processed_text))
        
        # 2. Split the AST Document into a list of smaller AST Documents.
        doc_chunks = split_document_ast_aware(doc)
        
        # 3. Iterate and render each AST chunk to a string for sending.
        for i, chunk_doc in enumerate(doc_chunks):
            chunk_text = render_ast_to_telegram_v2(chunk_doc)
            if not chunk_text.strip():
                continue

            # The rest of the sending logic (is_edit, etc.) remains the same.
            if is_edit and i == 0:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=update.callback_query.message.message_id,
                    text=chunk_text,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup if i == len(doc_chunks) - 1 else None
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=chunk_text,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup if i == len(doc_chunks) - 1 else None
                )
        return True

    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            # This is benign; we tried to update with the same content.
            # Treat it as a success to avoid scary logs and retries.
            logger.debug(f"{log_prefix}Swallowed MessageNotModified error.")
            return True
        else:
            logger.warning(f"{log_prefix}AST pipeline failed: {e}. Falling back to simple text.")
            # Fall through to fallback logic

    except Exception as e:
        logger.warning(f"{log_prefix}AST pipeline failed: {e}. Falling back to simple text.")
        # The existing fallback logic remains the same.
        try:
            plain_text = escape_meta_tags(text)
            chunks = [plain_text[i:i+TELEGRAM_MAX_LEN] for i in range(0, len(plain_text), TELEGRAM_MAX_LEN)]
            for i, chunk in enumerate(chunks):
                if is_edit and i == 0:
                     await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=update.callback_query.message.message_id,
                        text=chunk,
                        parse_mode=None,
                        reply_markup=reply_markup if i == len(chunks) - 1 else None
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        parse_mode=None,
                        reply_markup=reply_markup if i == len(chunks) - 1 else None
                    )
            return True

        except Exception as final_e:
            logger.error(f"{log_prefix}Final fallback to plain text also failed: {final_e}")
            return False
