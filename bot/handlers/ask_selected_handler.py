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
from telegram.helpers import escape_markdown
from typing import List, Dict, Any # Import missing types
from hashlib import sha256 # Import sha256
from urllib.parse import quote # Import quote

import config
from services import ollama_service, gemini_service, openrouter_service, openai_compatible_service
from storage import storage_manager

logger = logging.getLogger(__name__)

# Conversation states
SELECT_PROVIDER, SELECT_MODELS, CONFIRM_MODELS = range(3)
# Callback data prefixes
CALLBACK_PROVIDER_PREFIX = "ask_sel_prov_"
CALLBACK_MODEL_PREFIX = "ask_sel_mod_"
CALLBACK_ACTION_PREFIX = "ask_sel_act_"

# --- Helper Functions ---

async def get_models_for_provider(provider: str) -> List[Dict[str, Any]]:
    """Fetches models based on provider."""
    models = []
    if provider == "ollama":
        ollama_models = await ollama_service.list_models()
        models = [{"id": m, "name": m} for m in ollama_models]
    elif provider == "gemini":
        # Fetch models dynamically from Gemini service
        models = await gemini_service.list_models()
    elif provider == "openrouter":
        models = await openrouter_service.list_models() # Fetch free models
    else:
        # Handle custom OpenAI-compatible providers
        provider_config = providers.get_config_for_provider(provider)
        if provider_config and provider_config.get('allowed_models'):
            models = [{"id": model_id, "name": model_id} for model_id in provider_config['allowed_models']]
    return models

from bot import providers # Ensure this import exists

def build_provider_keyboard() -> InlineKeyboardMarkup:
    """Builds a dynamic provider selection keyboard."""
    provider_names = providers.get_available_provider_names()
    buttons = [InlineKeyboardButton(p, callback_data=f"{CALLBACK_PROVIDER_PREFIX}{p}") for p in provider_names]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)] # 2 buttons per row
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"{CALLBACK_ACTION_PREFIX}cancel")])
    return InlineKeyboardMarkup(keyboard)

async def build_model_keyboard(provider: str, selected_models: set, context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    """Builds the model selection keyboard for a given provider."""
    models = await get_models_for_provider(provider)
    models.sort(key=lambda x: x.get('name', x.get('id')).lower())

    keyboard = []
    row = []
    # Ensure model_metadata exists
    context.user_data.setdefault('model_metadata', {})

    for model in models:
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

    if not context.args:
        await update.message.reply_text("Please provide a prompt\\. Usage: /ask_selected <your prompt>\\.")
        return ConversationHandler.END

    context.user_data['ask_selected_prompt'] = " ".join(context.args)
    context.user_data['ask_selected_models'] = set() # Store as "provider:actual_model_id"
    context.user_data['model_metadata'] = {} # Initialize metadata mapping

    reply_markup = build_provider_keyboard()
    await update.message.reply_text("Please select a provider to choose models from:", reply_markup=reply_markup)
    return SELECT_PROVIDER

async def select_provider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles provider selection."""
    query = update.callback_query
    await query.answer()
    provider = query.data[len(CALLBACK_PROVIDER_PREFIX):]
    context.user_data['current_provider_selection'] = provider

    selected_models = context.user_data.get('ask_selected_models', set())
    reply_markup = await build_model_keyboard(provider, selected_models, context) # Pass context

    message_text = f"Select models from {provider} (Tap to toggle)"
    parse_mode = None
    if provider == "openrouter": # Example: OpenRouter might need specific formatting
        message_text = escape_markdown(message_text, version=2)
        parse_mode = 'MarkdownV2'

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

    selected_models_set: set = context.user_data.get('ask_selected_models', set())

    if selection_key in selected_models_set:
        selected_models_set.remove(selection_key)
    else:
        selected_models_set.add(selection_key)
    context.user_data['ask_selected_models'] = selected_models_set

    # Rebuild keyboard with updated selection state and context
    current_provider = context.user_data.get('current_provider_selection', provider)
    reply_markup = await build_model_keyboard(current_provider, selected_models_set, context) # Pass context
    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.debug("Keyboard not modified, skipping edit.")
        else:
            logger.error(f"Failed to edit keyboard markup: {e}")
            await query.answer("⚠️ Error updating selection")
    except Exception as e:
         logger.error(f"Unexpected error editing keyboard markup: {e}")
         await query.answer("⚠️ Error updating selection")


    return SELECT_MODELS

async def page_models_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles model list pagination."""
    query = update.callback_query
    await query.answer()
    
    page = int(query.data.split(':')[-1])
    provider = context.user_data.get('current_provider_selection')
    selected_models = context.user_data.get('ask_selected_models', set())
    
    if not provider:
        await query.edit_message_text("Error: Provider context lost. Please start over.")
        return ConversationHandler.END
    
    reply_markup = await build_model_keyboard(provider, selected_models, context, page=page)
    await query.edit_message_reply_markup(reply_markup=reply_markup)
    
    return SELECT_MODELS

async def back_to_providers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles going back to provider selection."""
    query = update.callback_query
    await query.answer()
    reply_markup = build_provider_keyboard()
    await query.edit_message_text("Please select a provider to choose models from:", reply_markup=reply_markup)
    return SELECT_PROVIDER

async def done_selecting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirms selection and executes the concurrent query."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    selected_models_set = context.user_data.get('ask_selected_models', set())
    prompt = context.user_data.get('ask_selected_prompt', '')
    model_metadata = context.user_data.get('model_metadata', {}) # Get metadata

    if not selected_models_set:
        await query.edit_message_text("No models selected. Cancelling.")
        return ConversationHandler.END
    if not prompt:
         await query.edit_message_text("Error: Prompt not found. Cancelling.")
         return ConversationHandler.END

    # Use display names for the confirmation message
    display_names = []
    selected_list = sorted(list(selected_models_set)) # Keep sorted list of provider:actual_id
    for item in selected_list:
         provider, actual_id = item.split(":", 1)
         # Find the display name from metadata (might need reverse lookup if keys are hashes)
         display_name = actual_id # Fallback
         # Find the hash key corresponding to this actual_id and provider
         found_meta = None
         for meta in model_metadata.values():
              if meta['actual_id'] == actual_id and meta['provider'] == provider:
                   found_meta = meta
                   break
         if found_meta:
             display_name = found_meta['display']
         display_names.append(f"{provider}:{display_name}")


    logger.info(f"Executing /ask_selected for chat {chat_id} with models: {selected_list} and prompt: '{prompt}'")

    try:
        await query.edit_message_text(
            f"Asking selected models: {escape_markdown(', '.join(display_names), version=2)}\\.\\.\\.",
            parse_mode='MarkdownV2'
        )
    except BadRequest as e:
         logger.error(f"Failed to edit confirmation message: {e}")
         # Continue execution even if edit fails
    placeholder_message = query.message

    # --- Execute Concurrent Queries ---
    tasks = []
    model_map = {}
    results = {} # Initialize results dict here

    context_history = None

    for item in selected_list: # Use selected_list which contains provider:actual_id
        provider, actual_id = item.split(":", 1) # Now splitting the correct key
        service = None

        # Find display name for logging/error messages
        display_name = actual_id # Fallback
        for meta in model_metadata.values():
             if meta['actual_id'] == actual_id and meta['provider'] == provider:
                  display_name = meta['display']
                  break

        task_model_key = f"{provider}:{display_name}" # Key for results dict

        actual_id_for_api = actual_id # Default

        if provider == "ollama":
            service = ollama_service
            try:
                available_models = await ollama_service.list_models()
                if actual_id not in available_models:
                    logger.error(f"Model {display_name} ({actual_id}) not found locally in Ollama.")
                    results[task_model_key] = f"[Model Not Found: {display_name}]"
                    continue # Skip this model
            except Exception as e:
                 logger.error(f"Failed to list Ollama models: {e}")
                 results[task_model_key] = f"[Error checking Ollama models: {e}]"
                 continue
        elif provider == "gemini":
            service = gemini_service
            # Gemini API uses the base model name (e.g., 'gemini-1.5-pro-latest')
            actual_id_for_api = actual_id
        elif provider == "openrouter":
            service = openrouter_service
            # OpenRouter uses provider/model_name format (e.g., 'google/gemini-pro')
            # We stored the correct ID in 'actual_id' when fetching models
            actual_id_for_api = actual_id
        else:
            # Handle custom OpenAI-compatible providers
            service = openai_compatible_service
            actual_id_for_api = actual_id

        if service:
            service_func = getattr(service, "_generate_single_model_non_streaming", None)
            if service_func:
                logger.debug(f"Creating task for {task_model_key} using API ID: {actual_id_for_api}")
                task = asyncio.create_task(service_func(actual_id_for_api, prompt, context_history))
                tasks.append(task)
                model_map[task] = task_model_key # Map task back to display key
            else:
                logger.error(f"Service {provider} missing _generate_single_model_non_streaming method.")
                results[task_model_key] = f"[{provider} service error]"
        else:
             logger.error(f"Could not find service for provider {provider}")
             results[task_model_key] = f"[{provider} service not found]"


    # Gather results
    results_raw = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results (using the display key from model_map)
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

    # --- Format and Send Final Response ---
    from utils.text_processing import split_message_markdown_aware, escape_markdown_v2
    from telegram import constants

    escaped_prompt = escape_markdown_v2(prompt)
    response_parts = [f"*Responses for prompt:* {escaped_prompt}"]
    sorted_results = sorted(results.items())

    for model_key_display, response_text in sorted_results:
        escaped_model = escape_markdown_v2(model_key_display)
        escaped_response = escape_markdown_v2(response_text)
        response_parts.append(f"\\n\\n___\\n*Model: `{escaped_model}`*\\n___\\n{escaped_response}")

    final_response_text = "".join(response_parts)

    # Delete the placeholder "Asking..." message
    await placeholder_message.delete()

    # Use the modern, robust message splitting and sending logic
    message_parts = split_message_markdown_aware(final_response_text)
    for part in message_parts:
        try:
            await context.bot.send_message(
                chat_id,
                text=part,
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
        except BadRequest:
            # Fallback to sending as plain text if markdown fails
            logger.warning(f"MarkdownV2 parsing failed for a message part. Sending as plain text.")
            await context.bot.send_message(chat_id, text=part, parse_mode=None)


    # Clean up user_data
    context.user_data.pop('ask_selected_prompt', None)
    context.user_data.pop('ask_selected_models', None)
    context.user_data.pop('current_provider_selection', None)
    context.user_data.pop('model_metadata', None)

    return ConversationHandler.END

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the conversation."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Selection cancelled\\.")
    # Clean up user_data
    context.user_data.pop('ask_selected_prompt', None)
    context.user_data.pop('ask_selected_models', None)
    context.user_data.pop('current_provider_selection', None)
    context.user_data.pop('model_metadata', None)
    return ConversationHandler.END

async def conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles conversation timeout."""
    chat_id = update.effective_chat.id
    logger.warning(f"/ask_selected conversation timed out for chat_id: {chat_id}")
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text("Model selection timed out.")
        elif update.message:
             await context.bot.send_message(chat_id=chat_id, text="Model selection timed out.")
    except Exception as e:
        logger.error(f"Error sending timeout message: {e}")
    # Clean up user_data
    context.user_data.pop('ask_selected_prompt', None)
    context.user_data.pop('ask_selected_models', None)
    context.user_data.pop('current_provider_selection', None)
    context.user_data.pop('model_metadata', None)


# --- Handler Export ---
ask_selected_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("ask_selected", ask_selected_start)],
    states={
        SELECT_PROVIDER: [
            CallbackQueryHandler(select_provider_callback, pattern=f"^{CALLBACK_PROVIDER_PREFIX}"),
            CallbackQueryHandler(cancel_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}cancel$"),
        ],
    SELECT_MODELS: [
        CallbackQueryHandler(select_model_callback, pattern=f"^{CALLBACK_MODEL_PREFIX}"),
        CallbackQueryHandler(page_models_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}page:"),
        CallbackQueryHandler(back_to_providers_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}back_providers$"),
        CallbackQueryHandler(done_selecting_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}done$"),
        CallbackQueryHandler(cancel_callback, pattern=f"^{CALLBACK_ACTION_PREFIX}cancel$"),
    ],
    },
    fallbacks=[CommandHandler("cancel", cancel_callback)],
    conversation_timeout=300,
    per_user=True,
    per_chat=True,
)

# Export the single ConversationHandler
ask_selected_handlers = [ask_selected_conv_handler]
