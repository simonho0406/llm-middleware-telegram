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
    # New Archival Logic: We fetch a limited window (e.g., last 500) to avoid memory overload
    # But for context construction, we rely on ensure_context_fits to trim it further.
    context_history = await storage_manager.get_thread_history(chat_id)

    # De-duplicate prompt: If the prompt was already saved to DB (Archival mode), remove it from history
    # because the service.generate_response method typically appends the prompt again.
    if context_history and context_history[-1].get('role') == 'user' and context_history[-1].get('content') == prompt:
        context_history.pop()

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
            processed_history.append({'role': 'assistant', 'content': f"**[Previous Expert Panel Discussion Result]**\n\n{content}"})
        else:
            processed_history.append(message)

    if is_reroll and processed_history and processed_history[-1].get('role') == 'assistant':
        # Fallback for transient history that might not have been cleaned up yet? 
        # In the new logic, we clean DB before calling this, but if we fetched transient state:
        logger.info(f"{log_prefix}Reroll detected in history processing. Removing last assistant message.")
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

    # If force_truncate is active (e.g. from retry), apply a safety margin to reduce context size
    # This helps when the model is returning empty responses near the limit
    safety_margin = 0.75 if force_truncate else 1.0

    final_history, context_info = await ensure_context_fits(
        prompt=prompt,
        history=processed_history,
        model=model_to_use,
        provider=session_provider,
        safety_margin=safety_margin
    )

    if context_info:
        logger.info(f"{log_prefix}{context_info}")

    truncated_history = final_history

    # Check if auto-search is enabled
    from bot.settings import USER_SETTINGS
    autosearch_enabled = await storage_manager.get_user_setting(
        chat_id,
        'autosearch_chat',
        USER_SETTINGS['autosearch_chat']['default']
    )

    # Generate LLM response
    raw_full_llm_response = ""
    llm_error_reported_by_model = False

    try:
        logger.info(f"{log_prefix}Starting LLM generation...")

        if autosearch_enabled:
             search_instruction = "If you need to perform a web search for current information, include the search query inside <search> tags like <search>latest news on the Artemis mission</search>, but ALWAYS also provide your best answer based on your existing knowledge after the search tags."
             augmented_prompt = f"{search_instruction}\n\n{prompt}"
        else:
             augmented_prompt = prompt

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
    final_content = raw_full_llm_response.strip()
    if not final_content:
        if not force_truncate and not llm_error_reported_by_model:
             logger.warning(f"{log_prefix}Empty response received from model. Retrying with forced context truncation...")
             return await _generate_llm_response(chat_id, prompt, is_reroll, force_truncate=True, operation_id=operation_id)
        
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

    # --- Archival Step 1: Secure the Input ---
    try:
        if is_reroll:
            # For reroll, we remove the faulty previous answer so the prompt is now the last message
            await storage_manager.remove_last_assistant_message(chat_id)
        else:
            # For normal messages, we APPEND the user prompt immediately
            await storage_manager.save_message(chat_id, 'user', prompt)
    except Exception as e:
        logger.error(f"{log_prefix}Failed to save/update initial state: {e}")
        # Proceeding might be risky if we can't save, but we try to answer anyway?
        # Ideally we should warn, but let's proceed.

    # --- Generate ---
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
    try:
        message_sent_successfully = await send_safe_message(context, update, final_content)
    except Exception as e:
        logger.error(f"{log_prefix}Failed to send message: {e}")
        message_sent_successfully = False


    # Check if the task was cancelled before saving history
    if asyncio.current_task().cancelled():
        logger.info(f"{log_prefix}Task was cancelled, skipping history update.")
        return

    # --- Archival Step 2: Secure the Output ---
    if response_data.get('error') is None and message_sent_successfully:
        try:
            await storage_manager.save_message(chat_id, 'assistant', final_content)
            logger.info(f"{log_prefix}Assistant response saved to archive.")
        except Exception as e_hist:
            logger.error(f"{log_prefix}Failed to save assistant response: {e_hist}")
