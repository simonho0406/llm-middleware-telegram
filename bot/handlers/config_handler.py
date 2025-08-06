import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters
)
from storage import storage_manager
from bot.handlers.misc_commands import cancel_command

logger = logging.getLogger(__name__)

# Conversation states
MAIN_MENU, = range(1)

# Helper function for robust boolean conversion
def _to_bool(value: str) -> bool:
    """Converts string to boolean using common truthy values."""
    return value.lower() in ['true', '1', 't', 'y', 'yes']

# Setting class to encapsulate configuration properties
class Setting:
    def __init__(self, key, name, setting_type, default):
        self.key = key
        self.name = name
        self.type = setting_type
        self.default = str(default)

# Centralized list of settings
SETTINGS = [
    Setting("autosearch_enabled", "Auto-Search", bool, True)
]

async def _get_config_menu(chat_id: int) -> InlineKeyboardMarkup:
    """Dynamically generates the configuration menu based on defined settings."""
    keyboard = []
    
    for setting in SETTINGS:
        # Get current setting value with proper type conversion
        value_str = await storage_manager.get_user_setting(chat_id, setting.key, setting.default)
        if setting.type == bool:
            value = _to_bool(value_str)
            status = "✅ Enabled" if value else "❌ Disabled"
        else:
            status = value_str
            
        keyboard.append([InlineKeyboardButton(f"{setting.name}: {status}", callback_data=setting.key)])
    
    keyboard.append([InlineKeyboardButton("Done", callback_data="done")])
    return InlineKeyboardMarkup(keyboard)

async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the user configuration conversation."""
    chat_id = update.effective_chat.id
    reply_markup = await _get_config_menu(chat_id)
    await update.message.reply_text("User Configuration", reply_markup=reply_markup)
    return MAIN_MENU

async def handle_setting_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles toggling of any setting."""
    query = update.callback_query
    await query.answer()
    chat_id = query.effective_chat.id
    setting_key = query.data

    # Find the setting by key
    setting = next((s for s in SETTINGS if s.key == setting_key), None)
    if not setting:
        logger.error(f"Unknown setting key: {setting_key}")
        await query.edit_message_text("Error: Unknown setting. Please try again.")
        return MAIN_MENU

    # Toggle or update the setting based on its type
    if setting.type == bool:
        current_value_str = await storage_manager.get_user_setting(chat_id, setting.key, setting.default)
        current_value = _to_bool(current_value_str)
        new_value = not current_value
        await storage_manager.set_user_setting(chat_id, setting.key, str(new_value))
    else:
        # For non-boolean settings, we'd implement different update logic
        # Currently only boolean is supported
        pass

    # Update the menu with new values
    reply_markup = await _get_config_menu(chat_id)
    await query.edit_message_text("User Configuration", reply_markup=reply_markup)
    return MAIN_MENU

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ends the configuration conversation."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Configuration saved.")
    return ConversationHandler.END

async def handle_config_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles text messages during configuration by reminding users to use buttons."""
    await update.message.reply_text("Please use the buttons to configure settings. Type /cancel to exit configuration.")
    return MAIN_MENU

# Create pattern for all setting callbacks
setting_patterns = "|".join(f"^{s.key}$" for s in SETTINGS)

config_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("config", config_command)],
    states={
        MAIN_MENU: [
            CallbackQueryHandler(handle_setting_toggle, pattern=setting_patterns),
            CallbackQueryHandler(done_command, pattern="^done$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_config_text),
        ],
    },
    fallbacks=[
        CommandHandler("config", config_command),
        CommandHandler("cancel", cancel_command)
    ],
    per_user=True,
    per_chat=True,
)