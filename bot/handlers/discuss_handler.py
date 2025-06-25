# File to be replaced: bot/handlers/discuss_handler.py

import logging
import hashlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)
from telegram.error import BadRequest
from bot.providers import get_available_provider_names, get_config_for_provider, get_service_for_provider
from storage import file_storage
from utils.text_processing import escape_markdown_v2, split_message_markdown_aware

logger = logging.getLogger(__name__)

# --- Conversation States ---
SELECT_PROVIDER, SELECT_MODELS = range(2)

# --- Constants ---
MODELS_PER_PAGE = 8
PROVIDERS_PER_PAGE = 6

# --- Callback Data Prefixes ---
CALLBACK_PROVIDER_PREFIX = "discuss_prov_"
CALLBACK_PROVIDER_PAGE_PREFIX = "discuss_prov_page_"
CALLBACK_MODEL_PREFIX = "discuss_mod_"
CALLBACK_MODEL_PAGE_PREFIX = "discuss_mod_page_"
BACK_TO_PROVIDERS = "discuss_back_prov"
DONE_SELECTING_MODELS = "discuss_done"
CANCEL_DISCUSSION = "discuss_cancel"


def build_paginated_keyboard(items, page, item_callback_prefix, page_callback_prefix, items_per_page):
    """Generic function to build a paginated keyboard for a list of strings."""
    buttons = []
    total_items = len(items)
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_items = items[start_idx:end_idx]

    for item in page_items:
        buttons.append([InlineKeyboardButton(item, callback_data=f"{item_callback_prefix}{item}")])

    pagination_row = []
    if page > 1:
        pagination_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{page_callback_prefix}{page-1}"))
    if end_idx < total_items:
        pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"{page_callback_prefix}{page+1}"))
    if pagination_row:
        buttons.append(pagination_row)
    return buttons

def build_provider_selection_keyboard(page=1):
    """Builds a paginated provider selection keyboard."""
    providers_list = sorted(get_available_provider_names())
    buttons = build_paginated_keyboard(
        providers_list, page, CALLBACK_PROVIDER_PREFIX, CALLBACK_PROVIDER_PAGE_PREFIX, PROVIDERS_PER_PAGE
    )
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=CANCEL_DISCUSSION)])
    return InlineKeyboardMarkup(buttons)

def build_discussion_model_keyboard(context: ContextTypes.DEFAULT_TYPE):
    """Builds paginated model selection keyboard with hashed callback data."""
    discussion_data = context.user_data['discussion_data']
    available_models = discussion_data.get('available_models', [])
    selected_models = discussion_data.get('selected_models', [])
    page = discussion_data.get('current_page', 1)
    provider_name = discussion_data.get('provider')

    context.user_data.setdefault('model_metadata', {})
    
    sorted_models = sorted(available_models, key=lambda m: m.get('name', m.get('id')).lower())
    
    total_models = len(sorted_models)
    start_idx = (page - 1) * MODELS_PER_PAGE
    end_idx = start_idx + MODELS_PER_PAGE
    page_models = sorted_models[start_idx:end_idx]
    
    buttons = []
    for model in page_models:
        model_id = model.get('id')
        model_name = model.get('name', model_id)
        
        unique_key = f"{provider_name}_{model_id}".encode()
        model_hash = hashlib.sha256(unique_key).hexdigest()[:12]
        context.user_data['model_metadata'][model_hash] = model_id
        
        prefix = ""
        if model_id in selected_models:
            order_idx = selected_models.index(model_id) + 1
            prefix = f"✅ {order_idx}. "
            
        buttons.append([InlineKeyboardButton(
            f"{prefix}{model_name}", 
            callback_data=f"{CALLBACK_MODEL_PREFIX}{model_hash}"
        )])
    
    pagination_row = []
    if page > 1:
        pagination_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{CALLBACK_MODEL_PAGE_PREFIX}{page-1}"))
    if end_idx < total_models:
        pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"{CALLBACK_MODEL_PAGE_PREFIX}{page+1}"))
    if pagination_row:
        buttons.append(pagination_row)
    
    nav_row = [InlineKeyboardButton("🔙 To Providers", callback_data=BACK_TO_PROVIDERS)]
    if len(selected_models) >= 2:
        nav_row.append(InlineKeyboardButton("✅ Done", callback_data=DONE_SELECTING_MODELS))
    buttons.append(nav_row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=CANCEL_DISCUSSION)])
        
    return InlineKeyboardMarkup(buttons)

async def start_discussion_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the /discuss command."""
    chat_id = update.effective_chat.id
    logger.debug(f"[{chat_id}] Entering /discuss conversation, state: SELECT_PROVIDER")
    
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await update.effective_message.reply_text(
            "Please provide a prompt. Usage: /discuss <your prompt>", 
            parse_mode=None
        )
        return ConversationHandler.END
    
    context.user_data['discussion_data'] = {
        'user_prompt': prompt, 'selected_models': [], 'provider': None,
        'available_models': [], 'current_page': 1, 'provider_page': 1,
    }
    context.user_data['model_metadata'] = {}
    
    keyboard = build_provider_selection_keyboard(page=1)
    await update.effective_message.reply_text("Select a provider for the discussion:", reply_markup=keyboard)
    
    return SELECT_PROVIDER

async def provider_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles provider list pagination."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    page = int(query.data.replace(CALLBACK_PROVIDER_PAGE_PREFIX, ""))
    logger.debug(f"[{chat_id}] Paginating providers to page {page}, state: SELECT_PROVIDER")
    
    context.user_data['discussion_data']['provider_page'] = page
    keyboard = build_provider_selection_keyboard(page=page)
    await query.edit_message_text("Select a provider for the discussion:", reply_markup=keyboard)
    return SELECT_PROVIDER

async def select_provider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles provider selection and displays the first page of models."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    provider_name = query.data.replace(CALLBACK_PROVIDER_PREFIX, "")
    logger.debug(f"[{chat_id}] Selected provider '{provider_name}', transitioning to state: SELECT_MODELS")

    discussion_data = context.user_data['discussion_data']
    discussion_data.update({'provider': provider_name, 'selected_models': [], 'current_page': 1})

    service = get_service_for_provider(provider_name)
    if not service:
        await query.edit_message_text(f"Error: Provider '{provider_name}' service not found.", parse_mode=None)
        return ConversationHandler.END

    try:
        await query.edit_message_text(f"Fetching models for {provider_name}...", parse_mode=None)
        models = await service.list_models()
    except Exception as e:
        logger.error(f"[{chat_id}] Failed to list models for '{provider_name}': {e}")
        await query.edit_message_text(f"Error fetching models for {provider_name}. Please try again.", parse_mode=None)
        return SELECT_PROVIDER

    if not models:
        await query.edit_message_text(f"No models found for provider '{provider_name}'.", parse_mode=None)
        return SELECT_PROVIDER
    
    discussion_data['available_models'] = [{'id': m, 'name': m} if isinstance(m, str) else m for m in models]
    
    keyboard = build_discussion_model_keyboard(context)
    message_text = f"Select at least 2 models from *{escape_markdown_v2(provider_name)}* \\(in order\\):"
    await query.edit_message_text(message_text, reply_markup=keyboard, parse_mode=constants.ParseMode.MARKDOWN_V2)
    
    return SELECT_MODELS

async def model_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles model list pagination."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    page = int(query.data.replace(CALLBACK_MODEL_PAGE_PREFIX, ""))
    logger.debug(f"[{chat_id}] Paginating models to page {page}, state: SELECT_MODELS")
    
    context.user_data['discussion_data']['current_page'] = page
    
    keyboard = build_discussion_model_keyboard(context)
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"[{chat_id}] Error editing model page keyboard: {e}")
            
    return SELECT_MODELS

async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles model selection/deselection."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    model_hash = query.data.replace(CALLBACK_MODEL_PREFIX, "")
    
    model_id = context.user_data.get('model_metadata', {}).get(model_hash)
    if not model_id:
        logger.warning(f"[{chat_id}] Stale model hash received: {model_hash}. Ignoring.")
        await query.answer("Model selection has expired. Please try again.", show_alert=True)
        return SELECT_MODELS

    logger.debug(f"[{chat_id}] Toggling model selection for '{model_id}', state: SELECT_MODELS")
    discussion_data = context.user_data['discussion_data']
    selected_models = discussion_data['selected_models']
    
    if model_id in selected_models:
        selected_models.remove(model_id)
    else:
        selected_models.append(model_id)
    
    keyboard = build_discussion_model_keyboard(context)
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"[{chat_id}] Error editing model selection keyboard: {e}")
            
    return SELECT_MODELS

async def back_to_providers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Returns to the provider selection screen."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    logger.debug(f"[{chat_id}] User going back to provider selection, state: SELECT_PROVIDER")
    
    page = context.user_data.get('discussion_data', {}).get('provider_page', 1)
    keyboard = build_provider_selection_keyboard(page=page)
    await query.edit_message_text("Select a provider for the discussion:", reply_markup=keyboard)
    return SELECT_PROVIDER

async def run_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes the sequential discussion with robust error handling."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    log_prefix = f"[{chat_id}]"
    logger.debug(f"{log_prefix} Starting discussion execution.")
    
    placeholder = None
    try:
        discussion_data = context.user_data['discussion_data']
        
        if len(discussion_data['selected_models']) < 2:
            await query.edit_message_text("Please select at least 2 models to begin.", parse_mode=None)
            return SELECT_MODELS

        placeholder = await query.edit_message_text("Starting discussion...", parse_mode=None)
        
        discussion_transcript = [{"role": "user", "content": discussion_data['user_prompt']}]
        provider_name = discussion_data['provider']
        service = get_service_for_provider(provider_name)
        
        for i, model_id in enumerate(discussion_data['selected_models']):
            turn_info = f"Turn {i+1}/{len(discussion_data['selected_models'])}: `{escape_markdown_v2(model_id)}` is thinking\\.\\.\\."
            await placeholder.edit_text(turn_info, parse_mode=constants.ParseMode.MARKDOWN_V2)
            
            prompt_text = "You are in a multi-turn discussion. Please critique the previous response and provide a refined, improved answer." if i > 0 else discussion_transcript[0]['content']
            history_for_call = discussion_transcript.copy() if i > 0 else []
            
            response = ""
            async for chunk in service.generate_response(model=model_id, prompt=prompt_text, context_history=history_for_call):
                response += chunk
            response = response.strip()
            
            discussion_transcript.append({"role": "assistant", "content": response})
        
        final_transcript_parts = [f"*Original Query:*\n{escape_markdown_v2(discussion_transcript[0]['content'])}"]
        for i, msg in enumerate(discussion_transcript[1:]):
            model_id = discussion_data['selected_models'][i]
            separator = "\n\n\\-\\-\-\\-\n"
            model_header = f"*Turn {i+1}: `{escape_markdown_v2(model_id)}`*\n"
            content_body = escape_markdown_v2(msg['content'])
            final_transcript_parts.append(separator + model_header + separator + content_body)
        
        final_transcript = "".join(final_transcript_parts)
        
        await placeholder.delete()
        message_parts = split_message_markdown_aware(final_transcript)
        for part in message_parts:
            await context.bot.send_message(chat_id, text=part, parse_mode=constants.ParseMode.MARKDOWN_V2)

    except Exception as e:
        logger.error(f"{log_prefix} Critical failure in run_discussion: {e}", exc_info=True)
        if placeholder:
            await placeholder.edit_message_text(
                "A critical error occurred during the discussion. The process has been stopped.", 
                parse_mode=None
            )
    finally:
        context.user_data.pop('discussion_data', None)
        context.user_data.pop('model_metadata', None)
        logger.debug(f"{log_prefix} Concluding /discuss conversation.")
        return ConversationHandler.END

async def cancel_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the discussion conversation."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    logger.debug(f"[{chat_id}] /discuss conversation cancelled by user.")
    
    await query.edit_message_text("Discussion canceled.", parse_mode=None)
    context.user_data.pop('discussion_data', None)
    context.user_data.pop('model_metadata', None)
    return ConversationHandler.END

discuss_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("discuss", start_discussion_command)],
    states={
        SELECT_PROVIDER: [
            CallbackQueryHandler(provider_page_callback, pattern=f"^{CALLBACK_PROVIDER_PAGE_PREFIX}"),
            CallbackQueryHandler(select_provider_callback, pattern=f"^{CALLBACK_PROVIDER_PREFIX}"),
            CallbackQueryHandler(cancel_discussion, pattern=f"^{CANCEL_DISCUSSION}$")
        ],
        SELECT_MODELS: [
            CallbackQueryHandler(select_model_callback, pattern=f"^{CALLBACK_MODEL_PREFIX}"),
            CallbackQueryHandler(model_page_callback, pattern=f"^{CALLBACK_MODEL_PAGE_PREFIX}"),
            CallbackQueryHandler(run_discussion, pattern=f"^{DONE_SELECTING_MODELS}$"),
            CallbackQueryHandler(back_to_providers_callback, pattern=f"^{BACK_TO_PROVIDERS}$"),
            CallbackQueryHandler(cancel_discussion, pattern=f"^{CANCEL_DISCUSSION}$")
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_discussion)],
    per_user=True,
    per_chat=True,
)