import logging
import asyncio
import zlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest # Import BadRequest
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from typing import List, Dict, Any # Import missing types
from hashlib import sha256 # Import sha256
from telegram.helpers import escape_markdown # Import escape_markdown
import re # Import regex for parsing grades

import config
from services import ollama_service, gemini_service, openai_compatible_service
from storage import storage_manager
from bot.messaging import send_safe_message, send_plain_message
from bot import providers # Ensure this import exists

logger = logging.getLogger(__name__)

# Conversation states
SELECT_PROVIDER, SELECT_MODELS, CONFIRM_MODELS, WAIT_FOR_PROMPT = range(4)
# Callback data prefixes
CALLBACK_PROVIDER_PREFIX = "ask_sel_prov_"
CALLBACK_MODEL_PREFIX = "ask_sel_mod_"
CALLBACK_ACTION_PREFIX = "ask_sel_act_"


# --- Helper Functions ---

async def get_models_for_provider(provider: str) -> List[Dict[str, Any]]:
    """Fetches models based on provider using standard service lookup."""
    models = []
    
    # 1. Try to get models from the service instance (Dynamic)
    service = providers.get_service_for_provider(provider)
    try:
        if service and hasattr(service, 'list_models'):
            # Some services return list of strings, others list of dicts
            raw_models = await service.list_models()
            if raw_models:
                # Normalize to List[Dict]
                if isinstance(raw_models[0], str):
                    models = [{"id": m, "name": m} for m in raw_models]
                elif isinstance(raw_models[0], dict):
                    models = [{"id": m.get('id'), "name": m.get('name', m.get('id'))} for m in raw_models]
    except Exception as e:
        logger.exception(f"Failed to list models dynamically for {provider}: {e}")

    # 2. Fallback to Config (Legacy/Static) if dynamic failed or returned empty
    if not models:
        provider_config = providers.get_config_for_provider(provider)
        if provider_config and provider_config.get('allowed_models'):
            models = [{"id": model_id, "name": model_id} for model_id in provider_config['allowed_models']]
            
    return models


import json
def build_provider_keyboard(last_models: list = None) -> InlineKeyboardMarkup:
    """Builds a dynamic provider selection keyboard."""
    provider_names = providers.get_available_provider_names()
    buttons = [InlineKeyboardButton(p, callback_data=f"{CALLBACK_PROVIDER_PREFIX}{p}") for p in provider_names]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)] # 2 buttons per row
    
    if last_models and len(last_models) > 0:
        model_count = len(last_models)
        keyboard.append([InlineKeyboardButton(f"🚀 Quick Run ({model_count} cached models)", callback_data=f"{CALLBACK_ACTION_PREFIX}run_last")])
        
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"{CALLBACK_ACTION_PREFIX}cancel")])
    return InlineKeyboardMarkup(keyboard)

async def build_model_keyboard(provider: str, selected_models: set, context: ContextTypes.DEFAULT_TYPE, page: int = 1) -> InlineKeyboardMarkup:
    """Builds the model selection keyboard for a given provider with pagination."""
    models = await get_models_for_provider(provider)
    models.sort(key=lambda x: x.get('name', x.get('id')).lower())

    ITEMS_PER_PAGE = 8
    total_models = len(models)
    
    # Calculate total pages and clamp/modulo the page parameter to ensure it is always within bounds
    total_pages = max(1, (total_models - 1) // ITEMS_PER_PAGE + 1)
    page = ((page - 1) % total_pages) + 1
    
    # Calculate slice
    start_idx = (page - 1) * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, total_models)
    
    # Store current page in context for toggling
    context.user_data['ask_selected_page'] = page
    
    current_page_models = models[start_idx:end_idx]

    keyboard = []
    row = []
    # Ensure model_metadata exists
    context.user_data.setdefault('model_metadata', {})

    for model in current_page_models:
        model_id = model.get('id')
        model_name = model.get('name', model_id)
        display_name = model_name if len(model_name) < 25 else model_name[:22] + "..."

        # Create hash-based key for callback data
        unique_key = f"{provider}_{model_id}".encode()
        model_key_hash = sha256(unique_key).hexdigest()[:12] # Use 12-char hash
        callback_data = f"{CALLBACK_MODEL_PREFIX}{provider}:{model_key_hash}"

        # Store metadata using the hash as the key
        context.user_data['model_metadata'][model_key_hash] = {
            'display': model_name,
            'actual_id': model_id, # Store the original, unhashed ID
            'provider': provider
        }

        # Check selection status using the original model_id
        selection_key = f"{provider}:{model_id}" # Key used for storing selection state
        prefix = "✅ " if selection_key in selected_models else ""

        row.append(InlineKeyboardButton(
            f"{prefix}{display_name}",
            callback_data=callback_data
        ))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Navigation Buttons (circular)
    if total_pages > 1:
        prev_page = ((page - 2) % total_pages) + 1
        next_page = (page % total_pages) + 1
        keyboard.append([
            InlineKeyboardButton("⬅️ Prev", callback_data=f"{CALLBACK_ACTION_PREFIX}page:{prev_page}"),
            InlineKeyboardButton("Next ➡️", callback_data=f"{CALLBACK_ACTION_PREFIX}page:{next_page}")
        ])

    # Add action buttons
    keyboard.append([
        InlineKeyboardButton("⬅️ Back to Providers", callback_data=f"{CALLBACK_ACTION_PREFIX}back_providers"),
        InlineKeyboardButton("➡️ Done Selecting", callback_data=f"{CALLBACK_ACTION_PREFIX}done"),
    ])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"{CALLBACK_ACTION_PREFIX}cancel")])
    return InlineKeyboardMarkup(keyboard)

# --- Conversation Handler Functions ---

async def ask_selected_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the /ask_selected conversation."""
    chat_id = update.effective_chat.id
    logger.info(f"Starting /ask_selected conversation for chat_id: {chat_id}")

    prompt = None
    if context.args:
        prompt = " ".join(context.args)
    elif update.message.text and update.message.text.startswith('/ask_selected'):
         # Manual argument parsing for regex fallback
         text = update.message.text
         # Split by first space to separate command from args
         parts = text.split(" ", 1)
         if len(parts) > 1:
             prompt = parts[1].strip()
    
    # Check reply fallback if prompt is still None
    if not prompt and update.message.reply_to_message and update.message.reply_to_message.text:
        prompt = update.message.reply_to_message.text

    context.user_data['ask_selected_prompt'] = prompt
    context.user_data['ask_selected_models'] = [] # Store as list to preserve order [provider:actual_model_id]
    context.user_data['ask_selected_models_set'] = set() # Store as set for fast lookup
    context.user_data['model_metadata'] = {} # Initialize metadata mapping

    # Retrieve cached models for Quick Run
    try:
        last_models_json = await storage_manager.get_user_setting(chat_id, "last_ask_selected_models")
        if last_models_json:
            last_models = json.loads(last_models_json)
            context.user_data['last_ask_selected_models'] = last_models
        else:
            context.user_data['last_ask_selected_models'] = []
    except Exception as e:
        logger.error(f"Failed to load cached models: {e}")
        context.user_data['last_ask_selected_models'] = []

    reply_markup = build_provider_keyboard(context.user_data['last_ask_selected_models'])
    await update.message.reply_text("Please select a provider to choose models from:", reply_markup=reply_markup)
    return SELECT_PROVIDER

async def select_provider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles provider selection."""
    query = update.callback_query
    await query.answer()
    provider = query.data[len(CALLBACK_PROVIDER_PREFIX):]
    context.user_data['current_provider_selection'] = provider

    selected_models_set = context.user_data.get('ask_selected_models_set', set())
    # Initialize page 1
    reply_markup = await build_model_keyboard(provider, selected_models_set, context, page=1) # Pass context and page

    message_text = f"Select models from {provider} (Tap to toggle)"
    parse_mode = None

    await query.edit_message_text(
        message_text,
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )
    return SELECT_MODELS

async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles model selection/deselection."""
    query = update.callback_query
    await query.answer()
    callback_data = query.data[len(CALLBACK_MODEL_PREFIX):]

    try:
        if ':' not in callback_data:
            raise ValueError("Malformed callback data")
        provider, model_key_hash = callback_data.split(":", 1)
    except ValueError as e:
        logger.error(f"Invalid callback data format: {callback_data} - {e}")
        await query.answer("⚠️ Invalid selection data, please try again")
        return SELECT_MODELS

    # Retrieve actual model ID from metadata using the hash
    model_meta = context.user_data.get('model_metadata', {}).get(model_key_hash)
    if not model_meta:
        logger.error(f"Could not find metadata for model hash: {model_key_hash}")
        await query.answer("⚠️ Error retrieving model info, please try again")
        return SELECT_MODELS

    actual_model_id = model_meta['actual_id']
    selection_key = f"{provider}:{actual_model_id}" # Use actual ID for selection state

    selected_models_list: list = context.user_data.get('ask_selected_models', [])
    selected_models_set: set = context.user_data.get('ask_selected_models_set', set())

    if selection_key in selected_models_set:
        selected_models_set.remove(selection_key)
        if selection_key in selected_models_list:
             selected_models_list.remove(selection_key)
    else:
        selected_models_set.add(selection_key)
        selected_models_list.append(selection_key)
        
    context.user_data['ask_selected_models'] = selected_models_list
    context.user_data['ask_selected_models_set'] = selected_models_set

    # Rebuild keyboard with updated selection state and context
    current_provider = context.user_data.get('current_provider_selection', provider)
    current_page = context.user_data.get('ask_selected_page', 1)
    reply_markup = await build_model_keyboard(current_provider, selected_models_set, context, page=current_page) # Pass context and current_page
    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.debug("Keyboard not modified, skipping edit.")
        else:
            logger.error(f"Failed to edit keyboard markup: {e}")
            await query.answer("⚠️ Error updating selection")
    except Exception as e:
         logger.exception(f"Unexpected error editing keyboard markup: {e}")
         await query.answer("⚠️ Error updating selection")


    return SELECT_MODELS

async def page_models_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles model list pagination."""
    query = update.callback_query
    await query.answer()
    
    page = int(query.data.split(':')[-1])
    provider = context.user_data.get('current_provider_selection')
    selected_models_set = context.user_data.get('ask_selected_models_set', set())
    
    if not provider:
        await query.edit_message_text("Error: Provider context lost. Please start over.")
        return ConversationHandler.END
    
    reply_markup = await build_model_keyboard(provider, selected_models_set, context, page=page)
    await query.edit_message_reply_markup(reply_markup=reply_markup)
    
    return SELECT_MODELS

async def back_to_providers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles going back to provider selection."""
    query = update.callback_query
    await query.answer()
    last_models = context.user_data.get('last_ask_selected_models', [])
    reply_markup = build_provider_keyboard(last_models)
    await query.edit_message_text("Please select a provider to choose models from:", reply_markup=reply_markup)
    return SELECT_PROVIDER

async def run_last_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Bypasses provider/model selection using previously cached models."""
    query = update.callback_query
    await query.answer()
    
    last_models = context.user_data.get('last_ask_selected_models')
    if not last_models:
        reply_markup = build_provider_keyboard()
        await query.edit_message_text("No cached models found. Please select a provider:", reply_markup=reply_markup)
        return SELECT_PROVIDER
        
    context.user_data['ask_selected_models'] = last_models
    context.user_data['ask_selected_models_set'] = set(last_models)
    
    context.user_data.setdefault('model_metadata', {})
    for item in last_models:
        if ':' in item:
            provider, actual_id = item.split(":", 1)
            context.user_data['model_metadata'][f"direct_{item}"] = {
                'display': actual_id,
                'actual_id': actual_id,
                'provider': provider
            }

    prompt = context.user_data.get('ask_selected_prompt', '')
    if not prompt:
         await query.edit_message_text("Please enter your prompt now:")
         return WAIT_FOR_PROMPT

    return await _execute_council_flow(update, context, prompt, last_models, context.user_data['model_metadata'])

async def done_selecting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirms selection and executes the concurrent query."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    selected_models_list = context.user_data.get('ask_selected_models', [])
    selected_models_set = context.user_data.get('ask_selected_models_set', set())
    prompt = context.user_data.get('ask_selected_prompt', '')
    model_metadata = context.user_data.get('model_metadata', {}) # Get metadata

    if not selected_models_set:
        await query.edit_message_text("No models selected. Cancelling.")
        return ConversationHandler.END
    
    if not prompt:
         await query.edit_message_text("Please enter your prompt now:")
         return WAIT_FOR_PROMPT

    return await _execute_council_flow(update, context, prompt, selected_models_list, model_metadata)

async def wait_for_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user input prompt after model selection."""
    prompt = update.message.text
    context.user_data['ask_selected_prompt'] = prompt
    
    selected_models_list = context.user_data.get('ask_selected_models', [])
    model_metadata = context.user_data.get('model_metadata', {})
    
    return await _execute_council_flow(update, context, prompt, selected_models_list, model_metadata)

async def _execute_council_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, selected_list: list, model_metadata: dict) -> int:
    """Executes the Council flow (Chairman Synthesis)."""
    
    status_message = None
    chat_id = update.effective_chat.id
    
    if update.callback_query:
        # Case 1: Callback Query (Done button)
        query = update.callback_query
        try:
            # Delete the selection keyboard/message explicitly
            await query.message.delete()
        except BadRequest:
            pass 
            
        # Send a FRESH status message
        status_message = await send_plain_message(
            context,
            chat_id,
            "Council is deliberating... 🏛️"
        )
    else:
        # Case 2: User sent a text prompt (Wait for prompt)
        status_message = await update.message.reply_text("Council is deliberating... 🏛️")

    # Use display names for logs/status
    display_names = []
    
    # Identify Chairman (First selected model)
    chairman_key = selected_list[0]
    chairman_provider, chairman_id = chairman_key.split(":", 1)
    
    # Get Chairman Display Name
    chairman_meta = None
    for meta in model_metadata.values():
         if meta['actual_id'] == chairman_id and meta['provider'] == chairman_provider:
              chairman_meta = meta
              break
    chairman_display_name = chairman_meta['display'] if chairman_meta else chairman_id
    
    for item in selected_list:
         provider, actual_id = item.split(":", 1)
         # Find display name
         display_name = actual_id # Fallback
         found_meta = None
         for meta in model_metadata.values():
              if meta['actual_id'] == actual_id and meta['provider'] == provider:
                   found_meta = meta
                   break
         if found_meta:
             display_name = found_meta['display']
         display_names.append(f"{provider}:{display_name}")


    logger.info(f"Executing /ask_selected for chat {chat_id} with models: {selected_list} and prompt: '{prompt}'")
    
    # Incremental Archival: Save USER prompt IMMEDIATELY
    try:
        pk = await storage_manager.save_message(chat_id, 'user', prompt)
        context.user_data['pending_council_message_pk'] = pk
    except Exception as e:
        logger.exception(f"Failed to save user prompt: {e}")
        if status_message:
            try:
                await status_message.edit_text("⚠️ An error occurred while saving your message. Please try again.")
            except BadRequest:
                pass
        return ConversationHandler.END

    try:
        await storage_manager.set_user_setting(chat_id, "last_ask_selected_models", json.dumps(selected_list))
    except Exception as e:
        logger.error(f"Failed to cache models layout: {e}")

    if status_message:
        try:
            # Edit the FRESH status message
            await status_message.edit_text(
                f"Asking selected models w/ Chairman *{escape_markdown(chairman_display_name, version=2)}*: {escape_markdown(', '.join(display_names), version=2)}\.\.\.",
                parse_mode='MarkdownV2'
            )
        except BadRequest as e:
             logger.error(f"Failed to edit status message: {e}")

    # --- Execute Concurrent Queries ---
    tasks = []
    model_map = {}
    results = {} 

    # Register task for cancellation. Defensively cancel any previous task on
    # this slot first (concurrent_updates=True can let two /ask_selected
    # invocations race for the same chat).
    _old_task = context.chat_data.get('llm_task')
    if _old_task and not _old_task.done():
        logger.warning(f"(Chat {update.effective_chat.id}) Cancelling zombie llm_task before ask_selected override.")
        _old_task.cancel()
    context.chat_data['llm_task'] = asyncio.current_task()

    # Fetch context history (limit to 500 lines)
    chat_id = update.effective_chat.id
    try:
        context_history = await storage_manager.get_thread_history(chat_id, limit=500)
    except Exception as e:
        logger.exception(f"Failed to fetch history for ask_selected: {e}")
        context_history = []

    for item in selected_list:
        provider, actual_id = item.split(":", 1)
        service = None

        # Find display name for logging/error messages
        display_name = actual_id 
        for meta in model_metadata.values():
             if meta['actual_id'] == actual_id and meta['provider'] == provider:
                  display_name = meta['display']
                  break

        task_model_key = f"{provider}:{display_name}"
        actual_id_for_api = actual_id 

        if provider == "ollama":
            # Pass strict=False or similar if needed, or just rely on service
            pass
            
        service = providers.get_service_for_provider(provider)

        if service:
            service_func = getattr(service, "_generate_single_model_non_streaming", None)
            if service_func:
                logger.debug(f"Creating task for {task_model_key} using API ID: {actual_id_for_api}")
                task = asyncio.create_task(service_func(actual_id_for_api, prompt, context_history))
                tasks.append(task)
                model_map[task] = task_model_key 
            else:
                logger.error(f"Service {provider} missing _generate_single_model_non_streaming method.")
                results[task_model_key] = f"[{provider} service error]"
        else:
             logger.error(f"Could not find service for provider {provider}")
             results[task_model_key] = f"[{provider} service not found]"


    # Gather results
    results_raw = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results
    for i, task in enumerate(tasks):
        model_key_display = model_map.get(task, f"unknown_{i}")
        result_data = results_raw[i]
        if isinstance(result_data, Exception):
            logger.error(f"Exception during concurrent generation for model {model_key_display}: {result_data}")
            results[model_key_display] = f"[Unhandled Exception: {result_data}]"
        elif result_data is None:
             logger.error(f"Received None result for model {model_key_display}")
             results[model_key_display] = "[Error: Service returned no response]"
        else:
            results[model_key_display] = str(result_data)

    # --- Format and Send Final Response Using AST Pipeline ---
    response_parts = [f"**Responses for prompt:** {prompt}"]
    sorted_results = sorted(results.items())

    for model_key_display, response_text in sorted_results:
        response_parts.append(f"\n\n---\n**Model: `{model_key_display}`**\n---\n{response_text}")

    # --- Chairman Synthesis ---
    synthesis_prompt = (
        f"You are the Chairman of an expert LLM Council. The user asked: '{prompt}'\n\n"
        "Here are the responses from the council members:\n"
    )
    
    for model_key_display, response_text in sorted_results:
        synthesis_prompt += f"\n--- Member: {model_key_display} ---\n{response_text}\n"
        
    synthesis_prompt += (
        "\n\nBased on the above, provide a comprehensive Synthesis Answer.\n"
        "1. Start with an 'Executive Summary' that integrates the best insights.\n"
        "2. **Grade** each model's response on a scale of 0-10 (where 10 is perfect) based on accuracy, helpfulness, and adherence to the prompt. Format as 'Grade for [Model Name]: [Score]/10'.\n" 
        "3. Note any significant consensus or conflicts between members.\n"
        "4. Provide the final, most accurate answer."
    )
    
    synthesis_service = providers.get_service_for_provider(chairman_provider)

    chairman_response = "*(Chairman synthesis failed)*"
    if synthesis_service:
        service_func = getattr(synthesis_service, "_generate_single_model_non_streaming", None)
        if service_func:
            logger.info(f"Generating Chairman Synthesis with {chairman_key}")
            try:
                chairman_response = await service_func(chairman_id, synthesis_prompt, []) 
            except Exception as e:
                logger.exception(f"Chairman Synthesis failed: {e}")
                chairman_response = f"[Error: Chairman Synthesis Failed - {e}]"
       
            # Extract and log grades
            grade_matches = re.findall(r"Grade for (.+?): (\d+(?:\.\d+)?)/10", chairman_response, re.IGNORECASE)
            if grade_matches:
                for model_name, score in grade_matches:
                    logger.info(f"🏆 Model Grade - {model_name.strip()}: {score}")
            else:
                logger.warning("No grades found in Chairman Synthesis.")
    
    
    # Format Final Output
    final_response_markdown = f"🏛️ **Chairman Synthesis** (`{chairman_display_name}`)\n\n{chairman_response}\n\n"
    final_response_markdown += "═" * 20 + "\n\n"
    
    for model_key_display, response_text in sorted_results:
        final_response_markdown += f"**Member: `{model_key_display}`**\n---\n{response_text}\n\n"


    # Delete the placeholder/status message
    if status_message:
        try:
            await status_message.delete()
        except BadRequest:
            pass
            
    # --- Format and Send Final Response ---
    # We use force_new=True because we deleted the status message and want a fresh response,
    # avoiding "Message to edit not found" errors if a callback was involved.
    # send_safe_message handles the AST rendering pipeline internally.
    await send_safe_message(context, update, final_response_markdown, force_new=True)

    # --- Archival: Save to DB ---
    try:
        # Save Assistant Response (Summary of all models)
        await storage_manager.save_message(chat_id, 'assistant', final_response_markdown)
        logger.info(f"Archived /ask_selected interaction for chat {chat_id}")
    except Exception as e:
        logger.exception(f"Failed to archive /ask_selected interaction: {e}")


    # Clean up user_data
    context.user_data.pop('ask_selected_prompt', None)
    context.user_data.pop('ask_selected_models', None)
    context.user_data.pop('ask_selected_models_set', None)
    context.user_data.pop('current_provider_selection', None)
    context.user_data.pop('model_metadata', None)
    context.chat_data.pop('llm_task', None)  # Clean up task reference
    
    # Clear pending PK since the interaction block is now complete and stable
    context.user_data.pop('pending_council_message_pk', None)

    return ConversationHandler.END

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the conversation."""
    query = update.callback_query
    await query.answer()
    
    # Cancel any running LLM task associated with this flow
    llm_task = context.chat_data.get('llm_task')
    if llm_task and not llm_task.done():
        llm_task.cancel()
        logger.info(f"Cancelled in-flight /ask_selected task.")
        
    await query.edit_message_text("Selection cancelled\.")
    # Clean up user_data
    context.user_data.pop('ask_selected_prompt', None)
    context.user_data.pop('ask_selected_models', None)
    context.user_data.pop('ask_selected_models_set', None)
    context.user_data.pop('current_provider_selection', None)
    context.user_data.pop('model_metadata', None)
    context.chat_data.pop('llm_task', None)
    
    # Surgical cleanup of orphaned user prompt preventing data loss history wipes
    pending_pk = context.user_data.pop('pending_council_message_pk', None)
    if pending_pk is not None:
        chat_id = update.effective_chat.id
        await storage_manager.delete_messages(chat_id, [pending_pk])
        logger.info(f"Cleaned up orphaned council prompt PK {pending_pk} due to cancellation.")
        
    return ConversationHandler.END

async def conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles conversation timeout."""
    chat_id = update.effective_chat.id
    logger.warning(f"/ask_selected conversation timed out for chat_id: {chat_id}")
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text("Model selection timed out.")
        elif update.message:
             await send_plain_message(context, chat_id, "Model selection timed out.")
    except Exception as e:
        logger.exception(f"Error sending timeout message: {e}")
    # Clean up user_data
    context.user_data.pop('ask_selected_prompt', None)
    context.user_data.pop('ask_selected_models', None)
    context.user_data.pop('ask_selected_models_set', None)
    context.user_data.pop('current_provider_selection', None)
    context.user_data.pop('model_metadata', None)

    # Cancel any running LLM task
    llm_task = context.chat_data.get('llm_task')
    if llm_task and not llm_task.done():
        llm_task.cancel()
        logger.info(f"/ask_selected LLM task cancelled due to timeout for chat_id: {chat_id}")
    context.chat_data.pop('llm_task', None)

    # Surgical cleanup of orphaned user prompt
    pending_pk = context.user_data.pop('pending_council_message_pk', None)
    if pending_pk is not None:
        await storage_manager.delete_messages(chat_id, [pending_pk])
        logger.info(f"Cleaned up orphaned council prompt PK {pending_pk} due to timeout in chat {chat_id}.")


# --- Handler Export ---
ask_selected_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("ask_selected", ask_selected_start),
        MessageHandler(filters.Regex(r'^/ask_selected'), ask_selected_start)
    ],
    states={
        SELECT_PROVIDER: [
            CallbackQueryHandler(select_provider_callback, pattern=f"^{CALLBACK_PROVIDER_PREFIX}"),
            CallbackQueryHandler(run_last_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}run_last$"),
            CallbackQueryHandler(cancel_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}cancel$"),
        ],
    SELECT_MODELS: [
        CallbackQueryHandler(select_model_callback, pattern=f"^{CALLBACK_MODEL_PREFIX}"),
        CallbackQueryHandler(page_models_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}page:"),
        CallbackQueryHandler(back_to_providers_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}back_providers$"),
        CallbackQueryHandler(done_selecting_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}done$"),
        CallbackQueryHandler(cancel_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}cancel$"),
    ],
    WAIT_FOR_PROMPT: [
        MessageHandler(filters.TEXT & ~filters.COMMAND, wait_for_prompt_callback)
    ],
    },
    fallbacks=[CommandHandler("cancel", cancel_callback)],
    conversation_timeout=300,
    per_user=True,
    per_chat=True,
    per_message=False
)

# Export the single ConversationHandler
ask_selected_handlers = [ask_selected_conv_handler]