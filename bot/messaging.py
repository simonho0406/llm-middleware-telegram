import logging
import asyncio
import re
from telegram import Update, constants
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from utils.text_processing import (
    parse_markdown_to_ast,
    split_document_ast_aware,
    render_ast_to_telegram_v2,
    render_ast_to_plain_text
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
    is_edit: bool = False
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
    is_edit = is_edit or (update.callback_query is not None)
    
    reply_to_msg_id = update.effective_message.message_id if update.effective_message else None

    final_content = escape_meta_tags(text)

    try:
        # Primary Path: AST-based rendering
        document = parse_markdown_to_ast(final_content)
        ast_chunks = split_document_ast_aware(document)

        for i, chunk_doc in enumerate(ast_chunks):
            text_chunk = render_ast_to_telegram_v2(chunk_doc)
            if not text_chunk.strip():
                continue

            if is_edit and i == 0:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=update.callback_query.message.message_id,
                    text=text_chunk,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup if i == len(ast_chunks) - 1 else None
                )
            else:
                current_reply_id = reply_to_msg_id if i == 0 else None
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text_chunk,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    reply_to_message_id=current_reply_id,
                    reply_markup=reply_markup if i == len(ast_chunks) - 1 else None
                )

    except Exception as e:
        logger.warning(f"{log_prefix}AST MarkdownV2 rendering failed: {e}. Falling back to plain text.")
        try:
            chunks = [final_content[i:i+TELEGRAM_MAX_LEN] for i in range(0, len(final_content), TELEGRAM_MAX_LEN)]
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
                    current_reply_id = reply_to_msg_id if i == 0 else None
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        parse_mode=None,
                        reply_to_message_id=current_reply_id,
                        reply_markup=reply_markup if i == len(chunks) - 1 else None
                    )
        except Exception as final_e:
            logger.error(f"{log_prefix}Final fallback to plain text also failed: {final_e}")