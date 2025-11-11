import logging
import time
from telegram.error import BadRequest
import asyncio
import re
import tiktoken
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter, TimedOut, BadRequest
import config
from bot import providers
from services import ollama_service, gemini_service, openrouter_service
from services.openai_compatible_service import OpenAICompatibleService

from storage import storage_manager
from bot.messaging import send_safe_message
from utils.context_manager import ensure_context_fits
from bot.settings import USER_SETTINGS

logger = logging.getLogger(__name__)

async def _generate_and_send_response(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, prompt: str, current_thread_id: str, is_reroll: bool = False, force_truncate: bool = False, placeholder_message = None) -> None:
    """Wraps the response generation in a cancellable task."""
    task = asyncio.create_task(
        _generate_and_send_response_task(update, context, chat_id, user_id, prompt, current_thread_id, is_reroll, force_truncate, placeholder_message)
    )
    context.chat_data['llm_task'] = task
    try:
        await task
    except asyncio.CancelledError:
        logger.info(f"(Chat {chat_id}) LLM task was cancelled.")

async def _generate_llm_response(chat_id: int, prompt: str, is_reroll: bool = False, force_truncate: bool = False, operation_id: str = "chat_response") -> dict:
    """
    Core LLM response generation logic, decoupled from message formatting and sending.
    Returns a response dict with 'content', 'error', 'truncated_history', and 'provider_info'.
    """
    log_prefix = f"(Chat {chat_id}) "

    # Load and process conversation history
    context_history = await storage_manager.get_thread_history(chat_id)
    import json
    processed_history = []
    for message in context_history:
        role = message.get('role')
        content = message.get('content')

        if role == 'panel_discussion': # Old format (summary)
            try:
                panel_data = json.loads(content)
                summary = (
                    f"A previous expert panel discussion was held on the topic: '{panel_data.get('original_prompt')}'..\n"
                    f"The final synthesized answer was: '{panel_data.get('final_answer')}'"
                )
                processed_history.append({'role': 'assistant', 'content': f"[Summary of Prior Panel Discussion]:\n{summary}"})
            except (json.JSONDecodeError, TypeError):
                processed_history.append({'role': 'assistant', 'content': "[A complex panel discussion occurred previously.]"})
        elif role == 'assistant:panel': # New format (full answer)
            processed_history.append({'role': 'assistant', 'content': content})
        else:
            processed_history.append(message)

    if is_reroll and processed_history and processed_history[-1].get('role') == 'assistant':
        logger.info(f"{log_prefix}Reroll detected. Removing last assistant message from history.")
        processed_history.pop()

    # Get provider configuration
    session_provider = await storage_manager.get_thread_key(chat_id, 'provider', config.get_default_provider())
    provider_details = providers.get_provider_details()

    if session_provider not in provider_details:
        logger.error(f"{log_prefix}Invalid provider '{session_provider}', falling back to default.")
        session_provider = config.get_default_provider()
        await storage_manager.set_thread_key(chat_id, 'provider', session_provider)

    provider_config = provider_details[session_provider]
    service = provider_config['service']
    model_key = 'model'
    default_model = provider_config['default_model']

    model_to_use = await storage_manager.get_thread_key(chat_id, model_key, default_model)
    provider_name_display = session_provider.capitalize()
    logger.info(f"{log_prefix}Using service: {service.__class__.__name__ if hasattr(service, '__class__') else service.__name__}, Model: {model_to_use}")

    provider_info = {
        'provider': session_provider,
        'provider_display': provider_name_display,
        'model': model_to_use,
        'service': service
    }

    # Automatically ensure context fits within model limits
    from utils.context_manager import ensure_context_fits

    final_history, context_info = await ensure_context_fits(
        prompt=prompt,
        history=processed_history,
        model=model_to_use,
        provider=session_provider
    )

    if context_info:
        logger.info(f"{log_prefix}{context_info}")

    truncated_history = final_history

    # Generate LLM response
    raw_full_llm_response = ""
    llm_error_reported_by_model = False

    try:
        logger.info(f"{log_prefix}Starting LLM generation...")

        search_instruction = "If you need to perform a web search for current information, include the search query inside <search> tags like <search>latest news on the Artemis mission</search>, but ALWAYS also provide your best answer based on your existing knowledge after the search tags."
        augmented_prompt = f"{search_instruction}\n\n{prompt}"

        async for chunk in service.generate_response(model=model_to_use, prompt=augmented_prompt, context_history=truncated_history):
            if chunk.startswith("[Error:") or chunk.startswith("Error:"):
                raw_full_llm_response = chunk
                llm_error_reported_by_model = True
                break
            raw_full_llm_response += chunk

        if not llm_error_reported_by_model:
            logger.info(f"{log_prefix}LLM generation complete. Length: {len(raw_full_llm_response)}")

    except Exception as e:
        logger.exception(f"{log_prefix}Critical error during LLM stream: {e}")
        raw_full_llm_response = "[Error: An unexpected error occurred while communicating with the AI.]"
        llm_error_reported_by_model = True

    # Handle search queries
    search_query = None
    search_tag_match = re.search(r"<search>(.*?)</search>", raw_full_llm_response, re.DOTALL)
    if search_tag_match:
        search_query = search_tag_match.group(1).strip()

        # Check if auto-search is enabled
        from bot.settings import USER_SETTINGS
        autosearch_enabled = await storage_manager.get_user_setting(
            chat_id,
            'autosearch_normal_chat',
            USER_SETTINGS['autosearch_normal_chat']['default']
        )

        if not autosearch_enabled:
            logger.info(f"{log_prefix}Auto-search disabled. Removing search tag and providing fallback answer.")
            # Remove the search tag entirely, but keep any additional content the LLM provided
            raw_full_llm_response = raw_full_llm_response.replace(search_tag_match.group(0), "").strip()

            # If there's still content after removing the search tag, keep it
            # If not, provide a helpful message
            if not raw_full_llm_response:
                raw_full_llm_response = f"I'd need to search for current information about '{search_query}' to give you an accurate answer. Auto-search is disabled - you can enable it in /config or try the /search command directly."
            search_query = None  # Clear search query since we're not using it

    final_content = raw_full_llm_response.strip()
    if not final_content:
        final_content = "[Error: The AI returned an empty response. This might be due to a content filter or an issue with the selected model. Please try rerolling or using a different model.]"
        llm_error_reported_by_model = True

    return {
        'content': final_content,
        'error': 'llm_error' if llm_error_reported_by_model else None,
        'truncated_history': truncated_history,
        'provider_info': provider_info,
        'search_query': search_query,
        'processed_history': processed_history
    }

async def _generate_and_send_response_task(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, prompt: str, current_thread_id: str, is_reroll: bool = False, force_truncate: bool = False, placeholder_message = None) -> None:
    log_prefix = f"(Chat {chat_id}) "

    response_data = await _generate_llm_response(chat_id, prompt, is_reroll, force_truncate)

    if response_data.get('error') == 'context_limit_exceeded':
        # This logic remains in the handler as it's specific to the chat workflow
        await send_safe_message(context, update, "Context window is full. Please use /config to manage conversation history.")
        return

    if response_data.get('search_query'):
        from .handlers import misc_commands
        logger.info(f"{log_prefix}Auto-search triggered. Delegating to search_command: '{response_data['search_query']}'")
        context.args = [response_data['search_query']]
        await misc_commands.search_command(update, context, placeholder_message)
        return

    final_content = response_data.get('content', "[Error: Empty response from AI]")

    # The handler is now responsible for placeholder deletion
    if placeholder_message:
        try:
            await placeholder_message.delete()
        except Exception as e:
            logger.warning(f"{log_prefix}Failed to delete placeholder message: {e}")

    # Centralized, safe sending
    message_sent_successfully = await send_safe_message(context, update, final_content)

    # Check if the task was cancelled before saving history
    if asyncio.current_task().cancelled():
        logger.info(f"{log_prefix}Task was cancelled, skipping history update.")
        return

    # Update history
    if response_data.get('error') is None and message_sent_successfully:
        try:
            history = response_data.get('processed_history', [])
            # For a reroll or edit, the user message is already in the history.
            if not is_reroll:
                history.append({'role': 'user', 'content': prompt})
            history.append({'role': 'assistant', 'content': final_content})
            await storage_manager.set_thread_history(chat_id, history)
        except Exception as e_hist:
            logger.error(f"{log_prefix}Failed to update history: {e_hist}")
