# File: bot/handlers/config_handler.py
# This is the canonical, correct implementation.

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)

from storage import storage_manager
from bot.settings import USER_SETTINGS
from bot.messaging import send_safe_message

logger = logging.getLogger(__name__)

# --- Constants ---
CONFIG_MENU, = range(1)
CALLBACK_SETTING_PREFIX = "config_toggle_"

# --- Helper Functions ---

async def get_settings_summary_text(chat_id: int) -> str:
    """Builds a read-only markdown summary of user settings."""
    summary_parts = ["*Current Bot Settings:*"]
    for key, details in USER_SETTINGS.items():
        if details['type'] != bool:
            continue
        current_value = await storage_manager.get_user_setting(
            chat_id, key, details['default']
        )
        status = "Enabled" if current_value else "Disabled"
        desc = details.get('description', '')
        summary_parts.append(f"\\- `{details['display_name']}`: *{status}*")
        if desc:
            summary_parts.append(f"  _{desc}_")
    
    summary_parts.append("\nTo change these, please ensure no panel discussion is active and use /config")
    return "\n".join(summary_parts)

def _build_settings_description_text() -> str:
    """Builds a description block explaining each boolean setting."""
    lines = ["⚙️ *User Settings*\n"]
    for key, details in USER_SETTINGS.items():
        if details['type'] != bool:
            continue
        desc = details.get('description', '')
        if desc:
            lines.append(f"• *{details['display_name']}* — _{desc}_")
    lines.append("\nTap a toggle to change:")
    return "\n".join(lines)


async def build_settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Dynamically builds the settings keyboard based on the registry."""
    buttons = []
    for key, details in USER_SETTINGS.items():
        if details['type'] != bool:
            continue
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
    """
    Entry point for the /config command.
    Acts as a gatekeeper: if a panel is active, it shows a helpful message and ends.
    Otherwise, it starts the interactive configuration menu.
    """
    chat_id = update.effective_chat.id

    if 'panel_state' in context.user_data:
        await send_safe_message(context, update, "Configuration cannot be changed during an active panel discussion. Please use /end_discussion first.")
        return ConversationHandler.END
    
    logger.debug(f"Config entry point accessed for chat {chat_id} - clearing any stale states")
    
    keyboard = await build_settings_keyboard(chat_id)
    description_text = _build_settings_description_text()
    await send_safe_message(context, update, description_text, reply_markup=keyboard)
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
        error_msg = str(e).lower()
        if "message is not modified" in error_msg or "exactly the same" in error_msg:
            logger.debug("Ignoring redundant message update in config menu.")
        else:
            logger.error(f"BadRequest in config handler: {e}")
            raise
    return CONFIG_MENU

async def config_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ends the configuration conversation successfully."""
    query = update.callback_query
    await query.answer()
    await send_safe_message(context, update, "✅ Settings saved.", is_edit=True)
    return ConversationHandler.END

async def cancel_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the configuration conversation."""
    if update.callback_query:
        await update.callback_query.answer()
        await send_safe_message(context, update, "❌ Configuration cancelled.", is_edit=True)
    else:
        await send_safe_message(context, update, "❌ Configuration cancelled.")
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
