import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram import MessageEntity
from telegram.ext import CommandHandler, ContextTypes
from telegram.helpers import escape_markdown

from services import ollama_service
from storage import file_storage
import config

logger = logging.getLogger(__name__)

# --- Command Handlers ---

async def list_ollama_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists available Ollama models using inline buttons."""
    chat_id = update.effective_chat.id
    logger.info(f"Received /list_ollama_models command from chat_id: {chat_id}")

    models = await ollama_service.list_models()
    
    if not models:
        await update.message.reply_text("No Ollama models available.")
        return

    keyboard = []
    row = []
    # Create buttons in rows of 2 with model names
    for model in sorted(models):
        display_name = model if len(model) < 25 else model[:22] + "..."
        callback_data = f"ollama_select_{model}"
        row.append(InlineKeyboardButton(display_name, callback_data=callback_data))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    current_model = await file_storage.get_thread_key(chat_id, 'ollama_model', config.DEFAULT_OLLAMA_MODEL)
    
    await update.message.reply_markdown_v2(
        text=f"Select an Ollama model for this thread \\(current: `{escape_markdown(current_model, version=2)}`\\):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_ollama_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles Ollama model selection from inline buttons."""
    query = update.callback_query
    await query.answer()
    
    model_name = query.data[len("ollama_select_"):]
    chat_id = update.effective_chat.id
    
    try:
        await file_storage.set_thread_key(chat_id, 'ollama_model', model_name)
        await file_storage.set_thread_key(chat_id, 'provider', 'ollama')
        
        await query.edit_message_text(
            f"✅ Ollama model set to `{escape_markdown(model_name, version=2)}` for this thread\\.",
            parse_mode='MarkdownV2'
        )
    except Exception as e:
        logger.error(f"Failed to save Ollama model selection: {e}")
        await query.edit_message_text("Failed to save selection. Please try again.")


async def set_ollama_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /set_ollama_model command."""
    chat_id = update.effective_chat.id
    logger.info(f"Received /set_ollama_model command from chat_id: {chat_id}")

    if not context.args:
        await update.message.reply_text(
            "Please provide a model name. Usage: /set_ollama_model <model_name>"
        )
        return

    target_model = context.args[0]
    logger.info(f"Attempting to set Ollama model to '{target_model}' for chat_id: {chat_id}")

    available_models = await ollama_service.list_models()

    if not available_models:
        await update.message.reply_text(
            "Could not fetch models from Ollama to verify your choice. Please try again later."
        )
        return

    # Simple check if the model exists (Ollama names can include tags like :latest)
    if target_model not in available_models:
        # Maybe the user provided the base name without the tag?
        base_model_name = target_model.split(':')[0]
        matching_models = [m for m in available_models if m.startswith(base_model_name + ':')]
        if matching_models:
             # If there's an exact match or only one tagged version, use it
             if target_model in matching_models:
                 pass # Use the exact match provided
             elif len(matching_models) == 1:
                 logger.info(f"Found single matching tagged model '{matching_models[0]}' for base '{base_model_name}'. Using it.")
                 target_model = matching_models[0]
             else:
                 # Multiple tagged versions exist, ask user to be specific
                 escaped_matches = [f"`{escape_markdown(m, version=2)}`" for m in matching_models]
                 await update.message.reply_markdown_v2(
                     f"Model `{escape_markdown(target_model, version=2)}` not found\\. Did you mean one of these\\?\n"
                     + '\n'.join(escaped_matches)
                     + f"\n\nPlease use the full name including the tag \\(e\\.g\\., `/set_ollama_model {escape_markdown(matching_models[0], version=2)}`\\)\\."
                 )
                 return
        else:
             # No exact match and no matching base name found
             escaped_target = escape_markdown(target_model, version=2)
             await update.message.reply_markdown_v2(
                 f"Model `{escaped_target}` not found in available Ollama models\\. "
                 f"Use `/list_ollama_models` to see available models\\."
             )
             return

    # Model exists, update session for the current thread
    try:
        current_thread_id = await file_storage.get_current_thread_id(chat_id)
        await file_storage.set_thread_key(chat_id, 'ollama_model', target_model) # Defaults to current thread
        # Also set the provider to ollama for the current thread
        await file_storage.set_thread_key(chat_id, 'provider', 'ollama')
        logger.info(f"Set Ollama model to '{target_model}' for thread '{current_thread_id}' in chat_id: {chat_id}")
        escaped_target = escape_markdown(target_model, version=2)
        escaped_thread = escape_markdown(current_thread_id, version=2)
        await update.message.reply_markdown_v2(
            f"Ollama model set to `{escaped_target}` for thread `{escaped_thread}`\\."
        )
    except Exception as e:
        logger.error(f"Failed to save session for chat_id {chat_id}: {e}")
        await update.message.reply_text(
            "An error occurred while saving your preference. Please try again."
        )


# --- Handler Exports ---
from telegram.ext import CallbackQueryHandler

ollama_handlers = [
    CommandHandler("list_ollama_models", list_ollama_models_command),
    CommandHandler("set_ollama_model", set_ollama_model_command),
    CallbackQueryHandler(handle_ollama_selection_callback, pattern=r"^ollama_select_"),
]
