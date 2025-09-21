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
from utils.text_processing import escape_markdown_v2
import hashlib
from bot.settings import USER_SETTINGS

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
    
    # Initialize conversation data
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
    
    # Load current configuration (custom or default)
    current_config = await load_panel_config(chat_id)
    
    # Build menu text
    menu_text = "*🔧 Expert Panel Configuration*\n\n"
    menu_text += "*Current Configuration:*\n"
    
    roles_section = current_config.get('roles', {})
    for role in CONFIGURABLE_ROLES:
        role_config = roles_section.get(role, {})
        provider = role_config.get('provider', 'Not Set')
        model = role_config.get('model', 'Not Set')
        
        # Truncate long model names for display
        display_model = model if len(model) <= 30 else f"{model[:27]}..."
        
        menu_text += f"├ *{role}:* `{provider}` / `{display_model}`\n"
    
    menu_text += "\n*🎛️ Actions:*"
    
    # Create buttons for each role
    keyboard = []
    for role in CONFIGURABLE_ROLES:
        keyboard.append([InlineKeyboardButton(
            f"🔧 Configure {role}", 
            callback_data=f"{ROLE_CALLBACK_PREFIX}{role}"
        )])
    
    # Action buttons
    action_row = [
        InlineKeyboardButton("🔄 Reset to Defaults", callback_data=RESET_CONFIG_CALLBACK),
        InlineKeyboardButton("💾 Save & Exit", callback_data=SAVE_CONFIG_CALLBACK)
    ]
    keyboard.append(action_row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text=menu_text,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
        else:
            await update.message.reply_text(
                text=menu_text,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
    except BadRequest as e:
        # Fallback to plain text if MarkdownV2 fails
        plain_text = menu_text.replace('*', '').replace('`', '').replace('├', '-').replace('🔧', '').replace('🎛️', '').replace('🔄', '').replace('💾', '')
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text=plain_text,
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                text=plain_text,
                reply_markup=reply_markup
            )
    
    return MAIN_MENU


async def handle_role_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle role selection from main menu."""
    query = update.callback_query
    await query.answer()
    
    # Extract role from callback data
    role = query.data[len(ROLE_CALLBACK_PREFIX):]
    context.user_data['config_temp']['selected_role'] = role
    
    # Show provider selection for this role
    return await show_provider_selection(update, context, role)


async def show_provider_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> int:
    """Display available providers for the selected role."""
    chat_id = update.effective_chat.id
    
    # Get current configuration for this role
    current_config = await load_panel_config(chat_id)
    current_role_config = current_config.get('roles', {}).get(role, {})
    current_provider = current_role_config.get('provider', None)
    
    # Get available providers
    available_providers = providers.get_available_provider_names()
    if not available_providers:
        await query.edit_message_text("❌ Error: No providers available. Please check your configuration.")
        return MAIN_MENU
    
    # Build provider selection menu
    menu_text = f"*🔧 Configure {role}*\n\n"
    menu_text += f"*Current Provider:* `{current_provider or 'Not Set'}`\n"
    menu_text += f"*Current Model:* `{current_role_config.get('model', 'Not Set')}`\n\n"
    menu_text += "*Select a Provider:*"
    
    # Create provider buttons
    keyboard = []
    for provider in available_providers:
        button_text = f"✅ {provider}" if provider == current_provider else provider
        keyboard.append([InlineKeyboardButton(
            button_text,
            callback_data=f"{PROVIDER_CALLBACK_PREFIX}{provider}"
        )])
    
    # Back button
    keyboard.append([InlineKeyboardButton("◀️ Back to Menu", callback_data=BACK_TO_MENU_CALLBACK)])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await update.callback_query.edit_message_text(
            text=menu_text,
            reply_markup=reply_markup,
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )
    except BadRequest:
        # Fallback to plain text
        plain_text = menu_text.replace('*', '').replace('`', '').replace('✅', '').replace('🔧', '').replace('◀️', '')
        await update.callback_query.edit_message_text(
            text=plain_text,
            reply_markup=reply_markup
        )
    
    return SELECT_PROVIDER


async def handle_provider_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle provider selection and show model list."""
    query = update.callback_query
    await query.answer()
    
    # Extract provider from callback data
    provider = query.data[len(PROVIDER_CALLBACK_PREFIX):]
    context.user_data['config_temp']['selected_provider'] = provider
    
    role = context.user_data['config_temp']['selected_role']
    
    # Show model selection for this provider
    return await show_model_selection(update, context, role, provider, page=1)


async def show_model_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str, provider: str, page: int = 1) -> int:
    """Display available models for the selected provider with pagination."""
    chat_id = update.effective_chat.id
    
    # Get current configuration
    current_config = await load_panel_config(chat_id)
    current_role_config = current_config.get('roles', {}).get(role, {})
    current_model = current_role_config.get('model', None)
    
    # Get provider configuration and service
    provider_config = providers.get_config_for_provider(provider)
    if not provider_config:
        await update.callback_query.edit_message_text(
            f"❌ Error: Could not find configuration for provider '{provider}'.\n\n"
            "Please go back and select a different provider.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back to Providers", callback_data=f"{ROLE_CALLBACK_PREFIX}{role}")
            ]])
        )
        return SELECT_PROVIDER
    
    service = providers.get_service_for_provider(provider)
    
    # Get available models
    models_result = []
    try:
        if service and hasattr(service, 'list_models'):
            models_result = await service.list_models()
        elif provider_config.get('allowed_models'):
            models_result = provider_config.get('allowed_models')
    except Exception as e:
        logger.error(f"Failed to get models for provider '{provider}': {e}")
        await update.callback_query.edit_message_text(
            f"❌ Error fetching models for '{provider}'.\n\n"
            "Please go back and try a different provider.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back to Providers", callback_data=f"{ROLE_CALLBACK_PREFIX}{role}")
            ]])
        )
        return SELECT_PROVIDER
    
    if not models_result:
        await update.callback_query.edit_message_text(
            f"❌ No models found for provider '{provider}'.\n\n"
            "Please go back and select a different provider.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back to Providers", callback_data=f"{ROLE_CALLBACK_PREFIX}{role}")
            ]])
        )
        return SELECT_PROVIDER
    
    # Sort and paginate models
    models_result.sort(key=lambda m: m['name'].lower() if isinstance(m, dict) else m.lower())
    total_models = len(models_result)
    start_index = (page - 1) * MODELS_PER_PAGE
    end_index = min(start_index + MODELS_PER_PAGE, total_models)
    models_page = models_result[start_index:end_index]
    
    # Build model selection menu with proper escaping
    escaped_role = escape_markdown_v2(role)
    escaped_provider = escape_markdown_v2(provider)
    escaped_current_model = escape_markdown_v2(current_model or 'Not Set')
    total_pages = (total_models - 1) // MODELS_PER_PAGE + 1
    
    menu_text = f"*🔧 Configure {escaped_role} → {escaped_provider}*\n\n"
    menu_text += f"*Current Model:* `{escaped_current_model}`\n\n"
    menu_text += f"*Select a Model* \\(Page {page}/{total_pages}\\):"
    
    # Create model buttons with safe callback data
    keyboard = []
    for model in models_page:
        # Extract both display name and API model ID
        if isinstance(model, dict):
            model_id = model.get('id', model.get('name', ''))  # API-compatible ID
            display_name = model.get('name', model_id)  # Human-readable name
        else:
            model_id = model  # For simple string models
            display_name = model

        # Truncate long display names for button display
        truncated_display = display_name if len(display_name) <= 40 else f"{display_name[:37]}..."
        button_text = f"✅ {truncated_display}" if model_id == current_model else truncated_display

        # Create safe callback data using API model ID (not display name)
        callback_data = f"{MODEL_CALLBACK_PREFIX}{model_id}"
        if len(callback_data) > 60:  # Leave room for prefix
            # Use hash for long model IDs and store mapping in context
            model_hash = hashlib.md5(model_id.encode()).hexdigest()[:16]
            callback_data = f"{MODEL_CALLBACK_PREFIX}hash_{model_hash}"
            # Store the mapping in context for later retrieval (use model_id, not display name)
            if 'model_hash_map' not in context.user_data:
                context.user_data['model_hash_map'] = {}
            context.user_data['model_hash_map'][model_hash] = model_id
        
        keyboard.append([InlineKeyboardButton(
            button_text,
            callback_data=callback_data
        )])
    
    # Pagination buttons
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(
            "◀️ Previous", 
            callback_data=f"{MODEL_PAGE_CALLBACK_PREFIX}{page-1}"
        ))
    if end_index < total_models:
        nav_buttons.append(InlineKeyboardButton(
            "Next ▶️", 
            callback_data=f"{MODEL_PAGE_CALLBACK_PREFIX}{page+1}"
        ))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    # Back button
    keyboard.append([InlineKeyboardButton("◀️ Back to Providers", callback_data=f"{ROLE_CALLBACK_PREFIX}{role}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await update.callback_query.edit_message_text(
            text=menu_text,
            reply_markup=reply_markup,
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )
    except BadRequest as e:
        logger.warning(f"MarkdownV2 parsing failed in model selection: {e}")
        # Fallback to plain text without markdown
        plain_text = (
            f"🔧 Configure {role} → {provider}\n\n"
            f"Current Model: {current_model or 'Not Set'}\n\n"
            f"Select a Model (Page {page}/{total_pages}):"
        )
        try:
            await update.callback_query.edit_message_text(
                text=plain_text,
                reply_markup=reply_markup
            )
        except BadRequest as e2:
            logger.error(f"Even plain text failed in model selection: {e2}")
            # Last resort - send new message
            await update.callback_query.message.reply_text(
                "Error displaying model selection. Please try /configure_panel again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Main Menu", callback_data=BACK_TO_MENU_CALLBACK)
                ]])
            )
    
    return SELECT_MODEL


async def handle_model_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle model selection and save the configuration."""
    query = update.callback_query
    await query.answer()
    
    # Extract model from callback data
    model_data = query.data[len(MODEL_CALLBACK_PREFIX):]
    
    # Handle hashed model names
    if model_data.startswith('hash_'):
        model_hash = model_data[5:]  # Remove 'hash_' prefix
        model_hash_map = context.user_data.get('model_hash_map', {})
        model = model_hash_map.get(model_hash, model_data)  # Fallback to original if not found
    else:
        model = model_data
    
    role = context.user_data['config_temp']['selected_role']
    provider = context.user_data['config_temp']['selected_provider']
    chat_id = context.user_data['config_temp']['chat_id']
    
    # Save the role configuration
    await save_role_config(chat_id, role, provider, model)
    context.user_data['config_temp']['changes_made'] = True
    
    # Show confirmation message and return to main menu
    confirmation_text = f"✅ *Configuration Updated*\n\n"
    confirmation_text += f"*{role}* is now configured to use:\n"
    confirmation_text += f"• Provider: `{provider}`\n"
    confirmation_text += f"• Model: `{model}`\n\n"
    confirmation_text += "Returning to main menu\\.\\.\\."
    
    try:
        await query.edit_message_text(
            text=confirmation_text,
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )
    except BadRequest:
        # Fallback to plain text
        plain_text = confirmation_text.replace('*', '').replace('`', '').replace('\\', '').replace('✅', '')
        await query.edit_message_text(text=plain_text)
    
    # Brief pause then show main menu
    import asyncio
    await asyncio.sleep(2)
    
    return await show_main_menu(update, context)


async def handle_model_page_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle pagination in model selection."""
    query = update.callback_query
    await query.answer()
    
    # Extract page number
    page = int(query.data[len(MODEL_PAGE_CALLBACK_PREFIX):])
    
    role = context.user_data['config_temp']['selected_role']
    provider = context.user_data['config_temp']['selected_provider']
    
    return await show_model_selection(update, context, role, provider, page)


async def handle_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle back to menu button."""
    query = update.callback_query
    await query.answer()
    
    return await show_main_menu(update, context)


async def handle_reset_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle reset to defaults button."""
    query = update.callback_query
    await query.answer()
    
    chat_id = context.user_data['config_temp']['chat_id']
    
    # Delete custom configuration
    await storage_manager.set_user_setting(chat_id, 'panel_config', None)
    context.user_data['config_temp']['changes_made'] = True
    
    # Show confirmation and return to menu
    await query.edit_message_text(
        "🔄 *Configuration Reset*\n\n"
        "All settings have been reset to defaults\\.\n"
        "Returning to menu\\.\\.\\.",
        parse_mode=constants.ParseMode.MARKDOWN_V2
    )
    
    import asyncio
    await asyncio.sleep(2)
    
    return await show_main_menu(update, context)


async def handle_save_and_exit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle save and exit button."""
    query = update.callback_query
    await query.answer()
    
    changes_made = context.user_data.get('config_temp', {}).get('changes_made', False)
    
    if changes_made:
        await query.edit_message_text(
            "💾 *Configuration Saved*\n\n"
            "Your Expert Panel configuration has been saved\\.\n"
            "Use `/discuss_panel` to test your new setup\\!",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )
    else:
        await query.edit_message_text(
            "👋 *Configuration Menu Closed*\n\n"
            "No changes were made\\.\n"
            "Your current configuration remains active\\.",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )
    
    # Clean up temp data
    context.user_data.pop('config_temp', None)
    
    return ConversationHandler.END


async def cancel_configure_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle conversation cancellation."""
    # Clean up temp data
    context.user_data.pop('config_temp', None)
    
    await update.message.reply_text(
        "🚫 Panel configuration cancelled\\.\n"
        "Your existing configuration remains unchanged\\.",
        parse_mode=constants.ParseMode.MARKDOWN_V2
    )
    
    return ConversationHandler.END


# Helper functions

def deep_merge_configs(base_config: dict, user_overrides: dict) -> dict:
    """
    Recursively merge user overrides on top of base configuration.
    
    This implements the "Partial Override" strategy where users can change
    individual settings without having to redefine the entire configuration.
    
    Args:
        base_config: The default configuration from config.yaml
        user_overrides: User's custom settings to merge on top
        
    Returns:
        Merged configuration with user overrides applied
    """
    if not isinstance(user_overrides, dict):
        return base_config
        
    # Create a deep copy of base config to avoid modifying original
    import copy
    merged = copy.deepcopy(base_config)
    
    for key, value in user_overrides.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            # Recursively merge nested dictionaries
            merged[key] = deep_merge_configs(merged[key], value)
        else:
            # Direct override for non-dict values or new keys
            merged[key] = value
    
    return merged


async def load_panel_config(chat_id: int) -> dict:
    """
    Load the user's panel configuration with partial override strategy.
    
    Implements Challenge A fix: Deep merge user overrides on top of defaults.
    Implements Challenge D fix: JSON corruption protection.
    """
    # Always start with default config from config.yaml as base
    default_config = {
        'quality_threshold': config.EXPERT_PANEL_CONFIG.get('quality_threshold', 85),
        'max_refinement_iterations': config.EXPERT_PANEL_CONFIG.get('max_refinement_iterations', 3),
        'orchestrator': config.EXPERT_PANEL_CONFIG.get('orchestrator', {}),
        'roles': config.EXPERT_PANEL_CONFIG.get('roles', {})
    }
    
    # Try to load custom configuration with JSON corruption protection
    try:
        config_json = await storage_manager.get_user_setting(chat_id, 'panel_config', None)
        
        if config_json:
            # Parse JSON string back to dictionary
            custom_overrides = json.loads(config_json)
            
            # Deep merge user overrides on top of default config
            merged_config = deep_merge_configs(default_config, custom_overrides)
            logger.debug(f"Successfully merged custom panel config for chat {chat_id}")
            return merged_config
        else:
            # No custom config - return defaults
            logger.debug(f"No custom panel config found for chat {chat_id}, using defaults")
            return default_config
            
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        # Handle corrupted JSON data or invalid config structure
        logger.warning(f"Corrupted panel_config detected for chat {chat_id}: {e}. Falling back to defaults.")
        
        # Clear the corrupted data to prevent future issues
        try:
            await storage_manager.set_user_setting(chat_id, 'panel_config', None)
            logger.info(f"Cleared corrupted panel_config for chat {chat_id}")
        except Exception as clear_error:
            logger.error(f"Failed to clear corrupted panel_config for chat {chat_id}: {clear_error}")
        
        # Return default configuration
        return default_config
    except Exception as e:
        # Handle any other unexpected errors
        logger.error(f"Unexpected error loading panel_config for chat {chat_id}: {e}. Using defaults.")
        return default_config


async def save_role_config(chat_id: int, role: str, provider: str, model: str) -> None:
    """Save a role configuration to the user's panel config."""
    # Load current config
    current_config = await load_panel_config(chat_id)
    
    # Ensure roles section exists
    if 'roles' not in current_config:
        current_config['roles'] = {}
    
    # Update the specific role
    current_config['roles'][role] = {
        'provider': provider,
        'model': model,
        'request_timeout_seconds': 600  # Use default timeout
    }
    
    # Save back to storage - serialize to JSON string
    config_json = json.dumps(current_config)
    await storage_manager.set_user_setting(chat_id, 'panel_config', config_json)
    logger.info(f"Updated {role} configuration for chat {chat_id}: {provider}/{model}")


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
            # Order matters! More specific patterns must come first
            CallbackQueryHandler(handle_model_page_change, pattern=f"^{MODEL_PAGE_CALLBACK_PREFIX}"),
            CallbackQueryHandler(handle_model_selection, pattern=f"^{MODEL_CALLBACK_PREFIX}"),
            CallbackQueryHandler(handle_role_selection, pattern=f"^{ROLE_CALLBACK_PREFIX}"),
            CallbackQueryHandler(handle_back_to_menu, pattern=f"^{BACK_TO_MENU_CALLBACK}$"),
        ],
    },
    fallbacks=[
        CommandHandler('cancel', cancel_configure_panel),
        MessageHandler(filters.TEXT, cancel_configure_panel)  # Any text input cancels
    ],
    per_user=True,
    per_chat=True,
    block=True,
    per_message=False
)