import logging
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, 
    CallbackQueryHandler, MessageHandler, filters
)
from telegram.error import BadRequest

import config
from bot import providers
from storage import storage_manager
from bot.messaging import send_safe_message
import hashlib

logger = logging.getLogger(__name__)

# Define conversation states
MAIN_MENU, SELECT_ROLE, SELECT_PROVIDER, SELECT_MODEL = range(4)

# Callback prefixes
ROLE_CALLBACK_PREFIX = "config_role_"
PROVIDER_CALLBACK_PREFIX = "config_provider_"
MODEL_CALLBACK_PREFIX = "config_model_"
BACK_TO_MENU_CALLBACK = "config_back_menu"
RESET_CONFIG_CALLBACK = "config_reset"
SAVE_CONFIG_CALLBACK = "config_save"

MODELS_PER_PAGE = 8
MODEL_PAGE_CALLBACK_PREFIX = "config_model_page_"

# Available roles that can be configured
CONFIGURABLE_ROLES = ["Proposer", "Critic", "Refiner"]

async def start_configure_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the /configure_panel command."""
    chat_id = update.effective_chat.id
    
    context.user_data['config_temp'] = {
        'chat_id': chat_id,
        'selected_role': None,
        'selected_provider': None,
        'changes_made': False
    }
    
    return await show_main_menu(update, context)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display the main configuration menu with current settings."""
    chat_id = update.effective_chat.id
    current_config = await load_panel_config(chat_id)
    
    menu_text = "*🔧 Expert Panel Configuration*\n\n*Current Configuration:*\n"
    roles_section = current_config.get('roles', {})
    for role in CONFIGURABLE_ROLES:
        role_config = roles_section.get(role, {})
        provider = role_config.get('provider', 'Not Set')
        model = role_config.get('model', 'Not Set')
        display_model = model if len(model) <= 30 else f"{model[:27]}..."
        menu_text += f"├ *{role}:* `{provider}` / `{display_model}`\n"
    
    menu_text += "\n*🎛️ Actions:*"

    
    keyboard = []
    for role in CONFIGURABLE_ROLES:
        keyboard.append([InlineKeyboardButton(f"🔧 Configure {role}", callback_data=f"{ROLE_CALLBACK_PREFIX}{role}")])
    
    action_row = [
        InlineKeyboardButton("🔄 Reset to Defaults", callback_data=RESET_CONFIG_CALLBACK),
        InlineKeyboardButton("💾 Save & Exit", callback_data=SAVE_CONFIG_CALLBACK)
    ]
    keyboard.append(action_row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await send_safe_message(context, update, menu_text, reply_markup=reply_markup)
    
    return MAIN_MENU

async def handle_role_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle role selection from main menu."""
    query = update.callback_query
    await query.answer()
    
    role = query.data[len(ROLE_CALLBACK_PREFIX):]
    context.user_data['config_temp']['selected_role'] = role
    
    return await show_provider_selection(update, context, role)


async def show_provider_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> int:
    """Display available providers for the selected role."""
    chat_id = update.effective_chat.id
    current_config = await load_panel_config(chat_id)
    current_role_config = current_config.get('roles', {}).get(role, {})
    current_provider = current_role_config.get('provider', None)
    
    available_providers = providers.get_available_provider_names()
    if not available_providers:
        await send_safe_message(context, update, "❌ Error: No providers available. Please check your configuration.")
        return MAIN_MENU
    
    menu_text = f"*🔧 Configure {role}*\n\n*Current Provider:* `{current_provider or 'Not Set'}`\n*Current Model:* `{current_role_config.get('model', 'Not Set')}`\n\n*Select a Provider:*"

    
    keyboard = []
    for provider in available_providers:
        button_text = f"✅ {provider}" if provider == current_provider else provider
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"{PROVIDER_CALLBACK_PREFIX}{provider}")])
    
    keyboard.append([InlineKeyboardButton("◀️ Back to Menu", callback_data=BACK_TO_MENU_CALLBACK)])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_safe_message(context, update, menu_text, reply_markup=reply_markup)
    
    return SELECT_PROVIDER


async def handle_provider_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle provider selection and show model list."""
    query = update.callback_query
    await query.answer()
    
    provider = query.data[len(PROVIDER_CALLBACK_PREFIX):]
    context.user_data['config_temp']['selected_provider'] = provider
    
    role = context.user_data['config_temp']['selected_role']
    
    return await show_model_selection(update, context, role, provider, page=1)


async def show_model_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str, provider: str, page: int = 1) -> int:
    """Display available models for the selected provider with pagination."""
    provider_config = providers.get_config_for_provider(provider)
    if not provider_config:
        await send_safe_message(context, update, f"❌ Error: Could not find configuration for provider '{provider}'.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Providers", callback_data=f"{ROLE_CALLBACK_PREFIX}{role}")]]))
        return SELECT_PROVIDER
    
    service = providers.get_service_for_provider(provider)
    models_result = []
    try:
        if service and hasattr(service, 'list_models'):
            models_result = await service.list_models()
        elif provider_config.get('allowed_models'):
            models_result = provider_config.get('allowed_models')
    except Exception as e:
        logger.exception(f"Failed to get models for provider '{provider}': {e}")
        await send_safe_message(context, update, f"❌ Error fetching models for '{provider}'.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Providers", callback_data=f"{ROLE_CALLBACK_PREFIX}{role}")]]))
        return SELECT_PROVIDER
    
    if not models_result:
        await send_safe_message(context, update, f"❌ No models found for provider '{provider}'.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Providers", callback_data=f"{ROLE_CALLBACK_PREFIX}{role}")]]))
        return SELECT_PROVIDER
    
    models_result.sort(key=lambda m: m['name'].lower() if isinstance(m, dict) else m.lower())
    total_models = len(models_result)
    start_index = (page - 1) * MODELS_PER_PAGE
    end_index = min(start_index + MODELS_PER_PAGE, total_models)
    models_page = models_result[start_index:end_index]
    
    current_config = await load_panel_config(update.effective_chat.id)
    current_model = current_config.get('roles', {}).get(role, {}).get('model', None)
    
    menu_text = f"*🔧 Configure {role} → {provider}*\n\n*Current Model:* `{current_model or 'Not Set'}`\n\n*Select a Model* (Page {page}/{(total_models - 1) // MODELS_PER_PAGE + 1}):"
    
    keyboard = []
    for model in models_page:
        model_id = model.get('id', model.get('name', '')) if isinstance(model, dict) else model
        display_name = model.get('name', model_id) if isinstance(model, dict) else model
        truncated_display = display_name if len(display_name) <= 40 else f"{display_name[:37]}..."
        button_text = f"✅{truncated_display}" if model_id == current_model else truncated_display

        callback_data = f"{MODEL_CALLBACK_PREFIX}{model_id}"
        if len(callback_data) > 60:
            model_hash = hashlib.md5(model_id.encode()).hexdigest()[:16]
            callback_data = f"{MODEL_CALLBACK_PREFIX}hash_{model_hash}"
            if 'model_hash_map' not in context.user_data:
                context.user_data['model_hash_map'] = {}
            context.user_data['model_hash_map'][model_hash] = model_id
        
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
    
    total_pages = (total_models - 1) // MODELS_PER_PAGE + 1
    if total_pages > 1:
        prev_page = ((page - 2) % total_pages) + 1
        next_page = (page % total_pages) + 1
        keyboard.append([
            InlineKeyboardButton("◀️ Previous", callback_data=f"{MODEL_PAGE_CALLBACK_PREFIX}{prev_page}"),
            InlineKeyboardButton("Next ▶️", callback_data=f"{MODEL_PAGE_CALLBACK_PREFIX}{next_page}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Back to Providers", callback_data=f"{ROLE_CALLBACK_PREFIX}{role}")])
    
    await send_safe_message(context, update, menu_text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    return SELECT_MODEL

async def handle_model_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    model_data = query.data[len(MODEL_CALLBACK_PREFIX):]
    if model_data.startswith('hash_'):
        model_hash = model_data[5:]
        model = context.user_data.get('model_hash_map', {}).get(model_hash, model_data)
    else:
        model = model_data
    
    role = context.user_data['config_temp']['selected_role']
    provider = context.user_data['config_temp']['selected_provider']
    chat_id = context.user_data['config_temp']['chat_id']
    
    await save_role_config(chat_id, role, provider, model)
    context.user_data['config_temp']['changes_made'] = True
    
    confirmation_text = f"✅ *Configuration Updated*\n\n*{role}* is now configured to use:\n• Provider: `{provider}`\n• Model: `{model}`\n\nReturning to main menu..."
    
    await send_safe_message(context, update, confirmation_text)
    
    import asyncio
    await asyncio.sleep(2)
    
    return await show_main_menu(update, context)

async def handle_model_page_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    page = int(query.data[len(MODEL_PAGE_CALLBACK_PREFIX):])
    role = context.user_data['config_temp']['selected_role']
    provider = context.user_data['config_temp']['selected_provider']
    
    return await show_model_selection(update, context, role, provider, page)

async def handle_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await show_main_menu(update, context)

async def handle_reset_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    chat_id = context.user_data['config_temp']['chat_id']
    await storage_manager.set_user_setting(chat_id, 'panel_config', None)
    context.user_data['config_temp']['changes_made'] = True
    
    await send_safe_message(context, update, "🔄 *Configuration Reset*\n\nAll settings have been reset to defaults.\nReturning to menu...")
    
    import asyncio
    await asyncio.sleep(2)
    
    return await show_main_menu(update, context)

async def handle_save_and_exit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    changes_made = context.user_data.get('config_temp', {}).get('changes_made', False)
    
    if changes_made:
        await send_safe_message(context, update, "💾 *Configuration Saved*\n\nYour Expert Panel configuration has been saved.\nUse `/discuss_panel` to test your new setup!")
    else:
        await send_safe_message(context, update, "👋 *Configuration Menu Closed*\n\nNo changes were made.\nYour current configuration remains active.")
    
    context.user_data.pop('config_temp', None)
    return ConversationHandler.END

async def cancel_configure_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop('config_temp', None)
    await send_safe_message(context, update, "🚫 Panel configuration cancelled.\nYour existing configuration remains unchanged.")
    return ConversationHandler.END

# Helper functions
def deep_merge_configs(base_config: dict, user_overrides: dict) -> dict:
    if not isinstance(user_overrides, dict):
        return base_config
    import copy
    merged = copy.deepcopy(base_config)
    for key, value in user_overrides.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge_configs(merged[key], value)
        else:
            merged[key] = value
    return merged

async def load_panel_config(chat_id: int) -> dict:
    import copy
    default_config = copy.deepcopy(config.get_expert_panel_config())
    try:
        custom_overrides_json = await storage_manager.get_user_setting(chat_id, 'panel_config', None)
        if custom_overrides_json:
            custom_overrides = json.loads(custom_overrides_json)
            merged_config = deep_merge_configs(default_config, custom_overrides)
            logger.debug(f"Successfully merged custom panel config for chat {chat_id}")
            return merged_config
        else:
            logger.debug(f"No custom panel config found for chat {chat_id}, using defaults")
            return default_config
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        logger.warning(f"Corrupted panel_config detected for chat {chat_id}: {e}. Falling back to defaults.")
        try:
            await storage_manager.set_user_setting(chat_id, 'panel_config', None)
            logger.info(f"Cleared corrupted panel_config for chat {chat_id}")
        except Exception as clear_error:
            logger.exception(f"Failed to clear corrupted panel_config for chat {chat_id}: {clear_error}")
        return default_config
    except Exception as e:
        logger.exception(f"Unexpected error loading panel_config for chat {chat_id}: {e}. Using defaults.")
        return default_config

async def save_role_config(chat_id: int, role: str, provider: str, model: str) -> None:
    try:
        overrides_json = await storage_manager.get_user_setting(chat_id, 'panel_config')
        current_overrides = json.loads(overrides_json) if overrides_json else {}
    except (json.JSONDecodeError, TypeError):
        current_overrides = {}
    
    if 'roles' not in current_overrides:
        current_overrides['roles'] = {}
    current_overrides['roles'][role] = {
        'provider': provider,
        'model': model
    }
    
    await storage_manager.set_user_setting(chat_id, 'panel_config', json.dumps(current_overrides))
    logger.info(f"Saved override for {role} in chat {chat_id}: {provider}/{model}")

# ConversationHandler setup
configure_panel_conv_handler = ConversationHandler(
    entry_points=[CommandHandler('configure_panel', start_configure_panel)],
    states={
        MAIN_MENU: [
            CallbackQueryHandler(handle_role_selection, pattern=f"^{ROLE_CALLBACK_PREFIX}"),
            CallbackQueryHandler(handle_reset_config, pattern=f"^{RESET_CONFIG_CALLBACK}$"),
            CallbackQueryHandler(handle_save_and_exit, pattern=f"^{SAVE_CONFIG_CALLBACK}$"),
        ],
        SELECT_ROLE: [
            CallbackQueryHandler(show_provider_selection, pattern=f"^{ROLE_CALLBACK_PREFIX}"),
        ],
        SELECT_PROVIDER: [
            CallbackQueryHandler(handle_provider_selection, pattern=f"^{PROVIDER_CALLBACK_PREFIX}"),
            CallbackQueryHandler(handle_back_to_menu, pattern=f"^{BACK_TO_MENU_CALLBACK}$"),
            CallbackQueryHandler(handle_role_selection, pattern=f"^{ROLE_CALLBACK_PREFIX}"),
        ],
        SELECT_MODEL: [
            CallbackQueryHandler(handle_model_page_change, pattern=f"^{MODEL_PAGE_CALLBACK_PREFIX}"),
            CallbackQueryHandler(handle_model_selection, pattern=f"^{MODEL_CALLBACK_PREFIX}"),
            CallbackQueryHandler(handle_role_selection, pattern=f"^{ROLE_CALLBACK_PREFIX}"),
            CallbackQueryHandler(handle_back_to_menu, pattern=f"^{BACK_TO_MENU_CALLBACK}$"),
        ],
    },
    fallbacks=[
        CommandHandler('cancel', cancel_configure_panel),
        MessageHandler(filters.TEXT, cancel_configure_panel)
    ],
    per_user=True,
    per_chat=True,
    block=True,
    per_message=False
)