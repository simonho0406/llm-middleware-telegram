import logging
import asyncio
import re
from telegram import Update, constants, error
from telegram.ext import MessageHandler, filters, ContextTypes
import config
from storage import storage_manager
from bot.messaging import send_safe_message
from bot.response_generator import _generate_and_send_response


logger = logging.getLogger(__name__)

STREAMING_THROTTLE_SECONDS = 3.0

def count_tokens(text: str) -> int:
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text, disallowed_special=()))
    except Exception as e:
        logger.warning(f"Token counting with tiktoken failed: {e}. Falling back to char count.")
        return len(text) // 4

def escape_meta_tags_for_markdown_attempt(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<reflect>.*?</reflect>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()

TELEGRAM_MAX_LEN = 4096


# --- Message Handlers ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages and calls the response generator."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message_text = update.message.text
    log_prefix = f"(Chat {chat_id}) "

    if not message_text:
        return

    logger.info(f"{log_prefix}Message from User {user_id}: '{message_text[:100]}...'")

    if config.get_allowed_chat_ids() and chat_id not in config.get_allowed_chat_ids():
        logger.warning(f"{log_prefix}Unauthorized chat ID. User: {user_id}.")
        return

    try:
        current_thread_id = await storage_manager.get_current_thread_id(chat_id)
        await storage_manager.set_thread_key(chat_id, 'last_user_prompt', message_text)
        logger.debug(f"{log_prefix}Saved last_user_prompt for thread {current_thread_id}.")

        await _generate_and_send_response(
            update=update,
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            prompt=message_text,
            current_thread_id=current_thread_id,
        )
    except error.NetworkError as e:
        logger.error(f"Network error in handle_message: {e}")
        try:
            await send_safe_message(context, update, "A network error occurred, please try again.")
        except Exception as e_inner:
            logger.error(f"Failed to send network error message to user: {e_inner}")
    except Exception as e:
        logger.error(f"{log_prefix}Error in handle_message: {e}", exc_info=True)
        await send_safe_message(context, update, "Sorry, a critical error occurred while handling your message.")

async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles edited messages, treating them as new prompts if they were the last user message."""
    if not update.edited_message:
        return

    # Check if a panel discussion is active. If so, ignore the edit.
    if context.user_data and 'panel_state' in context.user_data:
        chat_id = update.effective_chat.id
        log_prefix = f"(Chat {chat_id}) "
        logger.info(f"{log_prefix}Ignoring edit because a panel discussion is active.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    edited_text = update.edited_message.text
    log_prefix = f"(Chat {chat_id}) "

    logger.info(f"{log_prefix}User {user_id} edited a message.")

    history = await storage_manager.get_thread_history(chat_id)

    if not history:
        logger.info(f"{log_prefix}Ignoring edit in empty history.")
        return

    # Find the last user message in the history
    last_user_message_index = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i]['role'] == 'user':
            last_user_message_index = i
            break

    # If there's no user message, we can't do anything
    if last_user_message_index == -1:
        logger.info(f"{log_prefix}Ignoring edit, no previous user message found.")
        return

    # Cancel any in-flight task for the previous prompt
    if 'llm_task' in context.chat_data and not context.chat_data['llm_task'].done():
        context.chat_data['llm_task'].cancel()
        logger.info(f"{log_prefix}Cancelled in-flight LLM task due to message edit.")

    # Remove all messages after the last user message (i.e., any assistant responses)
    history = history[:last_user_message_index + 1]

    # Update the content of the last user message
    history[last_user_message_index]['content'] = edited_text

    # Save the corrected history
    await storage_manager.set_thread_history(chat_id, history)
    logger.info(f"{log_prefix}Corrected history after edit.")

    # Trigger a new response generation
    current_thread_id = await storage_manager.get_current_thread_id(chat_id)
    await _generate_and_send_response(
        update=update,
        context=context,
        chat_id=chat_id,
        user_id=user_id,
        prompt=edited_text,
        current_thread_id=current_thread_id,
        is_reroll=True # Treat an edit like a reroll
    )

chat_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
edited_message_handler = MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edited_message)
