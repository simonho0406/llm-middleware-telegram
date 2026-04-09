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

import httpx

async def send_draft_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    draft_id: int,
    text: str
):
    """
    Sends a message draft using the raw Telegram API temporarily (until PTB updates).
    """
    if not text:
        return
        
    log_prefix = f"(Chat {chat_id}) "
    
    # Send draft via direct HTTP request
    url = f"https://api.telegram.org/bot{context.bot.token}/sendMessageDraft"
    # Sanitize: strip stray HTML tags LLMs emit, then strip meta/reflect tags
    sanitized_text = escape_meta_tags(replace_html_tags(text))
    
    payload = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "text": sanitized_text
    }
    
    try:
        async with httpx.AsyncClient() as client:
            # Short timeout to prevent draft updates from blocking
            resp = await client.post(url, json=payload, timeout=2.0)
            if resp.status_code != 200:
                logger.debug(f"{log_prefix}Draft update failed: {resp.status_code} - {resp.text}")
    except Exception as e:
         logger.debug(f"{log_prefix}Exception during draft update: {e}")


async def finalize_draft(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    draft_id: int
):
    """
    Dismiss a rolling draft block by sending an empty/final update.
    This tells Telegram to stop showing the typing animation for this draft.
    """
    url = f"https://api.telegram.org/bot{context.bot.token}/sendMessageDraft"
    payload = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "text": ""
    }
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload, timeout=2.0)
    except Exception as e:
        logger.debug(f"(Chat {chat_id}) Exception finalizing draft {draft_id}: {e}")

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
            logger.debug(f"{log_prefix}Swallowed MessageNotModified error.")
            return True
        else:
            logger.warning(f"{log_prefix}AST pipeline BadRequest: {e}. Falling back to plain text.")

    except Exception as e:
        logger.exception(f"{log_prefix}AST pipeline failed: {e}. Falling back to simple text.")

    # Shared plaintext fallback for both BadRequest and generic Exception
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
        logger.exception(f"{log_prefix}Final fallback to plain text also failed: {final_e}")
        return False

async def send_plain_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    reply_markup=None,
    reply_to_message_id=None
): # Returns Optional[telegram.Message]
    """
    Sends a plain text message chunked to TELEGRAM_MAX_LEN.
    Used for safe delivery of raw internal errors, system notices, or raw strings 
    that should not be parsed as Markdown to avoid BadRequest crashes.
    Returns the last sent Message object.
    """
    if not text:
        return None

    try:
        last_msg = None
        chunks = [text[i:i+TELEGRAM_MAX_LEN] for i in range(0, len(text), TELEGRAM_MAX_LEN)]
        for i, chunk in enumerate(chunks):
            last_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=None, # Explicitly safe
                reply_markup=reply_markup if i == len(chunks) - 1 else None,
                reply_to_message_id=reply_to_message_id if i == 0 else None
            )
        return last_msg
    except Exception as e:
        logger.exception(f"(Chat {chat_id}) send_plain_message failed: {e}")
        return None
