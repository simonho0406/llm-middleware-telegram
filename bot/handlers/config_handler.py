# File: bot/handlers/config_handler.py
# This is the canonical, correct implementation.

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.error import BadRequest
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)

from storage import storage_manager
from bot.settings import USER_SETTINGS

logger = logging.getLogger(__name__)

# --- Constants ---
CONFIG_MENU, = range(1)
CALLBACK_SETTING_PREFIX = "config_toggle_"

# --- Helper Functions ---

async def build_settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Dynamically builds the settings keyboard based on the registry."""
    buttons = []
    for key, details in USER_SETTINGS.items():
        current_value = await storage_manager.get_user_setting(
            chat_id, key, details['default']
        )
        status_emoji = "✅" if current_value else "❌"
        button_text = f"{status_emoji} {details['display_name']}"
        buttons.append([
            InlineKeyboardButton(button_text, callback_data=f"{CALLBACK_SETTING_PREFIX}{key}")
        ])
    buttons.append([InlineKeyboardButton("Done", callback_data="config_done")])
    return InlineKeyboardMarkup(buttons)

# --- Conversation Handlers ---

async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the /config command."""
    chat_id = update.effective_chat.id
    keyboard = await build_settings_keyboard(chat_id)
    await update.message.reply_text("User Settings:", reply_markup=keyboard, parse_mode=None)
    return CONFIG_MENU

async def handle_setting_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Generic handler to toggle a boolean setting and refresh the menu in-place."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    key = query.data[len(CALLBACK_SETTING_PREFIX):]
    
    if key not in USER_SETTINGS:
        logger.warning(f"Received callback for unknown setting '{key}'")
        return CONFIG_MENU

    details = USER_SETTINGS[key]
    current_value = await storage_manager.get_user_setting(chat_id, key, details['default'])
    new_value = not current_value
    await storage_manager.set_user_setting(chat_id, key, new_value)
    
    keyboard = await build_settings_keyboard(chat_id)
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except BadRequest as e:
        if "Message is not modified" in str(e).lower():
            logger.debug("Ignoring redundant message update in config menu.")
        else:
            raise
    return CONFIG_MENU

async def config_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ends the configuration conversation successfully."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Settings saved.", parse_mode=None)
    return ConversationHandler.END

async def cancel_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the configuration conversation."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("❌ Configuration cancelled.", parse_mode=None)
    else:
        await update.message.reply_text("❌ Configuration cancelled.", parse_mode=None)
    return ConversationHandler.END

# --- Handler Export ---
config_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("config", config_command)],
    states={
        CONFIG_MENU: [
            CallbackQueryHandler(handle_setting_toggle, pattern=f"^{CALLBACK_SETTING_PREFIX}"),
            CallbackQueryHandler(config_done, pattern="^config_done$"),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_config)],
    per_user=True,
    per_chat=True,
    per_message=False
)