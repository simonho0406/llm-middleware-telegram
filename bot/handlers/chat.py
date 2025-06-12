import logging
import asyncio
import re
import tiktoken
from telegram import Update, constants
from telegram.ext import MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter, TimedOut, BadRequest
import config
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
    # Escape all characters that are special in MarkdownV2
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
    # This function seems to be a no-op, but we keep it for structural integrity
    # It was intended to remove <reflect> tags, but the regex is empty.
    text = re.sub(r"<reflect>.*?</reflect>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()

TELEGRAM_MAX_LEN = 4096

async def _generate_and_send_response(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, prompt: str, current_thread_id: str, is_reroll: bool = False) -> None:
    """
    The single, correct function to generate and send an AI response.
    Handles service selection, history management, and robust message sending with Markdown escaping.
    """
    log_prefix = f"(Chat {chat_id}) "
    
    # 1. Determine Service and Model
    session_provider = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    
    # This logic for initializing custom providers seems inefficient to run on every message.
    # However, we will not refactor it now to maintain stability.
    custom_service_instances = {}
    for provider_conf in CUSTOM_PROVIDERS_CONFIG:
        try:
            service_instance = OpenAICompatibleService(provider_conf)
            if service_instance.client:
                custom_service_instances[provider_conf['name']] = service_instance
        except Exception as e:
            logger.error(f"{log_prefix}Failed to initialize custom provider {provider_conf.get('name', 'UNKNOWN')}: {e}")

    provider_details = {
        'ollama': (ollama_service, 'ollama_model', config.DEFAULT_OLLAMA_MODEL),
        'gemini': (gemini_service, 'gemini_model', config.DEFAULT_GEMINI_MODEL),
        'openrouter': (openrouter_service, 'openrouter_model', config.DEFAULT_OPENROUTER_MODEL),
        **{name: (instance, f'{name}_model', instance.get_default_model()) for name, instance in custom_service_instances.items()}
    }

    if session_provider not in provider_details:
        logger.error(f"{log_prefix}Invalid provider '{session_provider}', falling back to default.")
        session_provider = config.DEFAULT_PROVIDER
        await file_storage.set_thread_key(chat_id, 'provider', session_provider)
    
    service, model_key, default_model = provider_details[session_provider]
    model_to_use = await file_storage.get_thread_key(chat_id, model_key, default_model)
    provider_name_display = session_provider.capitalize()
    logger.info(f"{log_prefix}Using service: {service.__class__.__name__ if hasattr(service, '__class__') else service.__name__}, Model: {model_to_use}")

    # 2. Send Placeholder Message
    placeholder_message = None
    try:
        placeholder_text = f"Thinking with {provider_name_display} ({model_to_use})..."
        if is_reroll:
            placeholder_text = f"Rerolling with {provider_name_display} ({model_to_use})..."
        
        escaped_placeholder_text = escape_markdown_v2(placeholder_text)
        
        reply_to_msg_id = update.message.message_id if update.message else None
        if not reply_to_msg_id and update.callback_query:
            reply_to_msg_id = update.callback_query.message.message_id

        placeholder_message = await context.bot.send_message(
            chat_id=chat_id,
            text=escaped_placeholder_text,
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            reply_to_message_id=reply_to_msg_id
        )
    except Exception as e:
        logger.error(f"{log_prefix}Failed to send placeholder message: {e}")

    # 3. Generate Full LLM Response
    raw_full_llm_response = ""
    llm_error_reported_by_model = False
    try:
        logger.info(f"{log_prefix}Starting LLM generation for thread {current_thread_id}...")
        context_history = await file_storage.get_thread_key(chat_id, 'history', [])

        if is_reroll and context_history and context_history[-1].get('role') == 'assistant':
            logger.info(f"{log_prefix}Reroll detected. Removing last assistant message from history.")
            context_history.pop()
        
        async for chunk in service.generate_response(model=model_to_use, prompt=prompt, context_history=context_history):
            if chunk.startswith("[Error:") or chunk.startswith("Error:"):
                logger.error(f"{log_prefix}LLM service reported an error: {chunk}")
                raw_full_llm_response = chunk
                llm_error_reported_by_model = True
                break
            raw_full_llm_response += chunk
        
        if not llm_error_reported_by_model:
            logger.info(f"{log_prefix}LLM generation complete. Length: {len(raw_full_llm_response)}")
        else:
            logger.warning(f"{log_prefix}LLM generation interrupted by model-reported error.")

    except Exception as e:
        logger.exception(f"{log_prefix}Critical error during LLM stream: {e}")
        raw_full_llm_response = "[Error: An unexpected error occurred while communicating with the AI.]"
        llm_error_reported_by_model = True

    # 4. Final Message Sending Logic
    final_content_to_send = raw_full_llm_response.strip()
    message_sent_or_edited_successfully = False
    
    reply_to_msg_id = update.message.message_id if update.message else None
    if not reply_to_msg_id and update.callback_query:
        reply_to_msg_id = update.callback_query.message.message_id
        
    try:
        if not final_content_to_send:
            final_content_to_send = "[Error: Received empty response from AI]"
        
        final_content_to_send = escape_meta_tags_for_markdown_attempt(final_content_to_send)
        
        if len(final_content_to_send) <= TELEGRAM_MAX_LEN and placeholder_message:
            await placeholder_message.edit_text(
                text=escape_markdown_v2(final_content_to_send),
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
            message_sent_or_edited_successfully = True
        else:
            if placeholder_message:
                await placeholder_message.delete()
            
            message_parts = split_message_markdown_aware(final_content_to_send, TELEGRAM_MAX_LEN)
            for idx, part in enumerate(message_parts):
                # Only reply to the original message on the first part
                current_reply_id = reply_to_msg_id if idx == 0 else None
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=escape_markdown_v2(part),
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    reply_to_message_id=current_reply_id
                )
            message_sent_or_edited_successfully = True
            
    except BadRequest as e:
        logger.error(f"{log_prefix}BadRequest sending/editing message, will try sending as plain text. Error: {e}")
        try:
            if placeholder_message:
                await placeholder_message.delete()
            # Fallback to sending as plain text
            await context.bot.send_message(
                chat_id=chat_id,
                text=final_content_to_send, # Send raw content
                parse_mode=None, # Explicitly disable parsing
                reply_to_message_id=reply_to_msg_id
            )
            message_sent_or_edited_successfully = True
        except Exception as fallback_e:
            logger.error(f"{log_prefix}Plain text fallback sending failed: {fallback_e}")
    except Exception as e:
        logger.exception(f"{log_prefix}Unexpected error during message sending: {e}")

    # 5. Update History
    if not llm_error_reported_by_model and final_content_to_send and message_sent_or_edited_successfully:
        logger.debug(f"{log_prefix}Updating conversation history.")
        try:
            current_history = await file_storage.get_thread_key(chat_id, 'history', [])
            if is_reroll and current_history and current_history[-1].get('role') == 'assistant':
                current_history.pop()

            current_history.extend([
                {'role': 'user', 'content': prompt},
                {'role': 'assistant', 'content': final_content_to_send}
            ])
            await file_storage.set_thread_key(chat_id, 'history', current_history)
            logger.info(f"{log_prefix}History updated to {len(current_history)} entries.")
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
            is_reroll=False
        )
    except Exception as e:
        logger.error(f"{log_prefix}Error in handle_message: {e}", exc_info=True)
        await update.message.reply_text("Sorry, a critical error occurred while handling your message.", parse_mode=None)

# Handler export
chat_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
