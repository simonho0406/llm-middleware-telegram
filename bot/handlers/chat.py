import logging
import asyncio
import re
import tiktoken
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter, TimedOut, BadRequest
import config
from bot import providers  # Corrected import
from services import ollama_service, gemini_service, openrouter_service
from services.openai_compatible_service import OpenAICompatibleService
from config import CUSTOM_PROVIDERS_CONFIG
from storage import file_storage
from utils.text_processing import split_message_markdown_aware

logger = logging.getLogger(__name__)

def escape_markdown_v2(text: str) -> str:
    """Escapes text for Telegram's MarkdownV2 parse mode."""
    if not text:
        return ""
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

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

async def _truncate_history_by_tokens(history: list, prompt: str, max_tokens: int, output_buffer: int) -> list:
    """
    Truncates conversation history to not exceed a specified token limit,
    reserving space for the prompt and an output buffer.
    """
    prompt_tokens = count_tokens(prompt)
    max_history_tokens = max_tokens - prompt_tokens - output_buffer
    
    truncated_history = []
    current_token_count = 0

    for message in reversed(history):
        message_content = message.get("content", "")
        message_tokens = count_tokens(message_content)

        if current_token_count + message_tokens <= max_history_tokens:
            truncated_history.insert(0, message)
            current_token_count += message_tokens
        else:
            break
    
    if len(truncated_history) < len(history):
        logger.info(f"History truncated from {len(history)} to {len(truncated_history)} messages to fit token limit.")
        
    return truncated_history

async def _generate_and_send_response(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, prompt: str, current_thread_id: str, is_reroll: bool = False, force_truncate: bool = False) -> None:
    """
    Generates and sends an AI response, now with interactive context management.
    """
    log_prefix = f"(Chat {chat_id}) "
    
    context_history = await file_storage.get_thread_key(chat_id, 'history', [])
    if is_reroll and context_history and context_history[-1].get('role') == 'assistant':
        logger.info(f"{log_prefix}Reroll detected. Removing last assistant message from history.")
        context_history.pop()

    if not force_truncate:
        total_tokens = count_tokens(prompt) + sum(count_tokens(msg.get("content", "")) for msg in context_history)
        if total_tokens > config.DEFAULT_MAX_CONTEXT_TOKENS:
            logger.warning(f"{log_prefix}Context limit exceeded ({total_tokens} > {config.DEFAULT_MAX_CONTEXT_TOKENS}). Prompting user to shrink.")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Shrink and Retry", callback_data="shrink_and_retry")]])
            await context.bot.send_message(
                chat_id=chat_id,
                text="This conversation is getting long and might exceed the AI's memory. Shall I proceed with a shortened history?",
                reply_markup=keyboard,
                parse_mode=None
            )
            return
        
    session_provider = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    provider_details = providers.get_provider_details()  # Corrected function call
    
    if session_provider not in provider_details:
        logger.error(f"{log_prefix}Invalid provider '{session_provider}', falling back to default.")
        session_provider = config.DEFAULT_PROVIDER
        await file_storage.set_thread_key(chat_id, 'provider', session_provider)
    
    provider_config = provider_details[session_provider]
    service = provider_config['service']
    model_key = provider_config['model_session_key']
    default_model = provider_config['default_model']
    
    model_to_use = await file_storage.get_thread_key(chat_id, model_key, default_model)
    provider_name_display = session_provider.capitalize()
    logger.info(f"{log_prefix}Using service: {service.__class__.__name__ if hasattr(service, '__class__') else service.__name__}, Model: {model_to_use}")

    placeholder_message = None
    try:
        placeholder_text = f"Thinking with {provider_name_display} ({model_to_use})..."
        if is_reroll: placeholder_text = f"Rerolling with {provider_name_display} ({model_to_use})..."
        
        escaped_placeholder_text = escape_markdown_v2(placeholder_text)
        
        reply_to_msg_id = update.message.message_id if update.message else (update.callback_query.message.message_id if update.callback_query else None)

        placeholder_message = await context.bot.send_message(chat_id=chat_id, text=escaped_placeholder_text, parse_mode=constants.ParseMode.MARKDOWN_V2, reply_to_message_id=reply_to_msg_id)
    except Exception as e:
        logger.error(f"{log_prefix}Failed to send placeholder message: {e}")

    raw_full_llm_response = ""
    llm_error_reported_by_model = False
    truncated_history = []
    try:
        logger.info(f"{log_prefix}Starting LLM generation for thread {current_thread_id}...")
        
        truncated_history = await _truncate_history_by_tokens(
            history=context_history,
            prompt=prompt,
            max_tokens=config.DEFAULT_MAX_CONTEXT_TOKENS,
            output_buffer=config.CONTEXT_TOKEN_OUTPUT_BUFFER
        )
        
        async for chunk in service.generate_response(model=model_to_use, prompt=prompt, context_history=truncated_history):
            if chunk.startswith("[Error:") or chunk.startswith("Error:"):
                raw_full_llm_response = chunk
                llm_error_reported_by_model = True
                break
            raw_full_llm_response += chunk
        
        if not llm_error_reported_by_model: logger.info(f"{log_prefix}LLM generation complete. Length: {len(raw_full_llm_response)}")

    except Exception as e:
        logger.exception(f"{log_prefix}Critical error during LLM stream: {e}")
        raw_full_llm_response = "[Error: An unexpected error occurred while communicating with the AI.]"
        llm_error_reported_by_model = True

    final_content_to_send = raw_full_llm_response.strip()
    if not final_content_to_send:
        final_content_to_send = "[Error: The AI returned an empty response. This might be due to a content filter or an issue with the selected model. Please try rerolling or using a different model.]"
    message_sent_or_edited_successfully = False
    
    reply_to_msg_id = update.message.message_id if update.message else (update.callback_query.message.message_id if update.callback_query else None)

    try:
        final_content_to_send = escape_meta_tags_for_markdown_attempt(final_content_to_send)
        
        if len(final_content_to_send) <= TELEGRAM_MAX_LEN and placeholder_message:
            await placeholder_message.edit_text(text=escape_markdown_v2(final_content_to_send), parse_mode=constants.ParseMode.MARKDOWN_V2)
            message_sent_or_edited_successfully = True
        else:
            if placeholder_message:
                await placeholder_message.delete()
            
            message_parts = split_message_markdown_aware(final_content_to_send, TELEGRAM_MAX_LEN)
            for idx, part in enumerate(message_parts):
                current_reply_id = reply_to_msg_id if idx == 0 else None
                await context.bot.send_message(chat_id=chat_id, text=escape_markdown_v2(part), parse_mode=constants.ParseMode.MARKDOWN_V2, reply_to_message_id=current_reply_id)
            message_sent_or_edited_successfully = True
            
    except BadRequest as e:
        logger.error(f"{log_prefix}BadRequest sending/editing message, will try sending as plain text. Error: {e}")
        try:
            if placeholder_message: await placeholder_message.delete()
            await context.bot.send_message(chat_id=chat_id, text=final_content_to_send, parse_mode=None, reply_to_message_id=reply_to_msg_id)
            message_sent_or_edited_successfully = True
        except Exception as fallback_e:
            logger.error(f"{log_prefix}Plain text fallback sending failed: {fallback_e}")
    except Exception as e:
        logger.exception(f"{log_prefix}Unexpected error during message sending: {e}")

    if not llm_error_reported_by_model and final_content_to_send and message_sent_or_edited_successfully:
        logger.debug(f"{log_prefix}Updating conversation history.")
        try:
            history_to_save = truncated_history
            history_to_save.extend([
                {'role': 'user', 'content': prompt},
                {'role': 'assistant', 'content': final_content_to_send}
            ])
            
            final_truncated_history = await _truncate_history_by_tokens(
                history=history_to_save,
                prompt="",
                max_tokens=config.DEFAULT_MAX_CONTEXT_TOKENS,
                output_buffer=0
            )

            await file_storage.set_thread_key(chat_id, 'history', final_truncated_history)
            logger.info(f"{log_prefix}History updated to {len(final_truncated_history)} entries.")
        except Exception as e_hist:
            logger.error(f"{log_prefix}Failed to update history: {e_hist}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages, saves the prompt, and calls the response generator."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message_text = update.message.text
    log_prefix = f"(Chat {chat_id}) "
    
    if not message_text:
        return

    logger.info(f"{log_prefix}Message from User {user_id}: '{message_text[:100]}...'")

    if config.ALLOWED_CHAT_IDS and chat_id not in config.ALLOWED_CHAT_IDS:
        logger.warning(f"{log_prefix}Unauthorized chat ID. User: {user_id}.")
        return

    try:
        current_thread_id = await file_storage.get_current_thread_id(chat_id)
        await file_storage.set_thread_key(chat_id, 'last_user_prompt', message_text)
        logger.debug(f"{log_prefix}Saved last_user_prompt for thread {current_thread_id}.")

        await _generate_and_send_response(
            update=update,
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            prompt=message_text,
            current_thread_id=current_thread_id,
            is_reroll=False,
            force_truncate=False
        )
    except Exception as e:
        logger.error(f"{log_prefix}Error in handle_message: {e}", exc_info=True)
        await update.message.reply_text("Sorry, a critical error occurred while handling your message.", parse_mode=None)

chat_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
