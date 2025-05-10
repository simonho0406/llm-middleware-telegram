import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.helpers import escape_markdown

import config
from services import openrouter_service # Import the service
from storage import file_storage

logger = logging.getLogger(__name__)

# Constants for Callback Data Prefix
CALLBACK_PREFIX_SELECT_MODEL = "or_select_"

async def list_openrouter_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists available free OpenRouter models using inline buttons."""
    chat_id = update.effective_chat.id
    logger.info(f"Received /list_openrouter_models command from chat_id: {chat_id}")

    free_models = await openrouter_service.get_free_models()

    if not free_models:
        await update.message.reply_text("Could not fetch free models from OpenRouter, or none are available.")
        return

    # Sort models by name for consistent display
    free_models.sort(key=lambda x: x.get('name', x.get('id')).lower())

    keyboard = []
    # Create buttons in rows of 2
    row = []
    for model in free_models:
        model_id = model.get('id')
        model_name = model.get('name', model_id) # Display name, fallback to ID
        # Shorten name if too long for button
        display_name = model_name if len(model_name) < 30 else model_name[:27] + "..."
        callback_data = f"{CALLBACK_PREFIX_SELECT_MODEL}{model_id}"
        row.append(InlineKeyboardButton(display_name, callback_data=callback_data))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: # Add the last row if it's not full
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    current_model = await file_storage.get_thread_key(chat_id, 'openrouter_model', config.DEFAULT_OPENROUTER_MODEL)

    await update.message.reply_markdown_v2(
        text=f"Select a free OpenRouter model to use for this thread \\(current: `{escape_markdown(current_model, version=2)}`\\):",
        reply_markup=reply_markup
    )

async def handle_model_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses for selecting an OpenRouter model."""
    query = update.callback_query
    await query.answer() # Acknowledge the button press

    chat_id = update.effective_chat.id
    callback_data = query.data

    if not callback_data.startswith(CALLBACK_PREFIX_SELECT_MODEL):
        logger.warning(f"Received unexpected callback data: {callback_data}")
        return

    selected_model_id = callback_data[len(CALLBACK_PREFIX_SELECT_MODEL):]
    logger.info(f"User {chat_id} selected OpenRouter model: {selected_model_id}")

    # Optional: Verify the selected model ID is still valid/free?
    # For simplicity, we trust the button data for now.

    try:
        # Set provider to openrouter and save the selected model for the current thread
        await file_storage.set_thread_key(chat_id, 'provider', 'openrouter')
        await file_storage.set_thread_key(chat_id, 'openrouter_model', selected_model_id)

        logger.info(f"Set provider to 'openrouter' and model to '{selected_model_id}' for chat_id: {chat_id}")

        # Edit the original message to confirm selection
        await query.edit_message_text(
            text=f"✅ OpenRouter model set to `{escape_markdown(selected_model_id, version=2)}` for this thread\\.",
            parse_mode='MarkdownV2'
        )
    except Exception as e:
        logger.error(f"Failed to save OpenRouter model selection for chat_id {chat_id}: {e}")
        await query.edit_message_text(
            text="An error occurred while saving your selection\\. Please try again\\.",
            parse_mode='MarkdownV2'
        )

# --- Command Handlers ---
# Keep /use_openrouter_model for manual setting if needed, but prioritize buttons
async def use_openrouter_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /use_openrouter_model [model_name] command (manual override)."""
    chat_id = update.effective_chat.id
    args = context.args
    logger.info(f"Received manual /use_openrouter_model command from chat_id: {chat_id}")

    if not args:
        await update.message.reply_text(
            "Usage: `/use_openrouter_model <model_id>` \\(e\\.g\\., `/use_openrouter_model mistralai/mistral-7b-instruct`\\)\\.\n"
            "Use `/list_openrouter_models` to see available free models with selection buttons\\."
        )
        return

    model_id = args[0].strip()

    # Validate against fetched free models for safety? Or allow any model?
    # For now, let's check against the fetched free list
    free_models = await openrouter_service.get_free_models()
    allowed_ids = [m['id'] for m in free_models]

    if not free_models:
         await update.message.reply_text("Could not verify model list\\. Please try `/list_openrouter_models` first\\.")
         return

    if model_id not in allowed_ids:
        # Maybe it's a non-free model the user wants to use?
        # For now, restrict to free models found by the list command.
        escaped_id = escape_markdown(model_id, version=2)
        await update.message.reply_markdown_v2(
            f"🚫 Model `{escaped_id}` not found in the current list of free models\\. "
            f"Please use `/list_openrouter_models` to select a model\\."
        )
        return

    try:
        # Set provider and model
        await file_storage.set_thread_key(chat_id, 'provider', 'openrouter')
        await file_storage.set_thread_key(chat_id, 'openrouter_model', model_id)
        logger.info(f"Manually set provider to 'openrouter' and model to '{model_id}' for chat_id: {chat_id}")

        await update.message.reply_markdown_v2(
            f"✅ OpenRouter model manually set to `{escape_markdown(model_id, version=2)}` for this thread\\.",
        )
    except Exception as e:
        logger.error(f"Failed to save manual OpenRouter model selection for chat_id {chat_id}: {e}")
        await update.message.reply_text("An error occurred while saving your selection\\. Please try again\\.")


# --- Handler Exports ---
openrouter_handlers = [
    CommandHandler("list_openrouter_models", list_openrouter_models_command),
    CommandHandler("use_openrouter_model", use_openrouter_model_command), # Keep manual command
    # Removed ask_all_openrouter handler
    CallbackQueryHandler(handle_model_selection_callback, pattern=f"^{CALLBACK_PREFIX_SELECT_MODEL}") # Handle button clicks
]
