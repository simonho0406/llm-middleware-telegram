import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from telegram.helpers import escape_markdown

import config
from services import gemini_service
from storage import file_storage

logger = logging.getLogger(__name__)

# --- Command Handlers ---

async def set_gemini_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the Gemini model for the current chat thread."""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Please provide a model name. Usage: /gemini <model_name>")
        return

    model_name = context.args[0]
    # Basic validation (can be enhanced)
    if not model_name.startswith("gemini-"):
        await update.message.reply_text("Invalid Gemini model format. Should start with 'gemini-'.")
        return

    try:
        await file_storage.set_thread_key(chat_id, 'gemini_model', model_name)
        await file_storage.set_thread_key(chat_id, 'provider', 'gemini') # Switch provider too
        logger.info(f"Set Gemini model to '{model_name}' and provider to 'gemini' for chat {chat_id}")
        await update.message.reply_text(f"Switched to Gemini model: `{escape_markdown(model_name, version=2)}`", parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error setting Gemini model for chat {chat_id}: {e}")
        await update.message.reply_text("An error occurred while setting the model.")

async def list_gemini_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists available Gemini models."""
    chat_id = update.effective_chat.id
    logger.info(f"Received /list_gemini_models command from chat_id: {chat_id}")
    
    try:
        models = await gemini_service.list_gemini_models()
        if not models:
            await update.message.reply_text("Could not fetch Gemini models or no models available.")
            return

        # Create inline keyboard buttons
        keyboard = []
        for model in models:
            # Use a prefix 'gemini_select_' for the callback data
            callback_data = f"gemini_select_{model['id']}"
            # Truncate button text if too long (Telegram limit)
            button_text = f"{model['name']} ({model['id']})"
            if len(button_text) > 60: # Approx limit, adjust if needed
                 button_text = f"{model['name'][:30]}... ({model['id']})"

            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Please choose a Gemini model:", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error listing Gemini models for chat {chat_id}: {e}")
        await update.message.reply_text("An error occurred while fetching Gemini models.")

# --- Callback Handler ---

async def gemini_model_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the selection of a Gemini model from the inline keyboard."""
    query = update.callback_query
    await query.answer() # Acknowledge the callback query

    chat_id = update.effective_chat.id
    callback_data = query.data

    if not callback_data.startswith("gemini_select_"):
        logger.warning(f"Received unexpected callback data: {callback_data}")
        await query.edit_message_text(text="Invalid selection.")
        return

    model_id = callback_data.replace("gemini_select_", "")

    try:
        # Verify model exists (optional but good practice)
        # models = await gemini_service.list_gemini_models() # Could re-fetch to verify
        # if not any(m['id'] == model_id for m in models):
        #     await query.edit_message_text(text=f"Model '{escape_markdown(model_id, version=2)}' not found or invalid.")
        #     return

        await file_storage.set_thread_key(chat_id, 'gemini_model', model_id)
        await file_storage.set_thread_key(chat_id, 'provider', 'gemini') # Switch provider too
        logger.info(f"Set Gemini model to '{model_id}' and provider to 'gemini' via callback for chat {chat_id}")
        await query.edit_message_text(text=f"Switched to Gemini model: `{escape_markdown(model_id, version=2)}`", parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error setting Gemini model via callback for chat {chat_id}: {e}")
        await query.edit_message_text(text="An error occurred while setting the model.")


# --- Export Handlers ---
gemini_handlers = [
    CommandHandler("gemini", set_gemini_model), # Keep direct setting command
    CommandHandler("list_gemini_models", list_gemini_models_command),
    CallbackQueryHandler(gemini_model_callback_handler, pattern="^gemini_select_") # Add callback handler
]
