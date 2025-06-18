import logging
import asyncio
import time

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    constants,
)
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown
from telegram.error import BadRequest

import config
from bot import providers
from storage import file_storage
from bot.handlers.chat import _generate_and_send_response, escape_markdown_v2
from services import web_search_service
from utils.text_processing import split_message_markdown_aware

logger = logging.getLogger(__name__)

# --- Constants ---
PROVIDER_CALLBACK_PREFIX = "set_provider_"
MODEL_CALLBACK_PREFIX = "set_model_"
MODEL_LIST_PAGE_CALLBACK_PREFIX = "list_models_page_"
SET_MODEL_TYPING = 1
MODELS_PER_PAGE = 10

# --- Command Handlers ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends structured help message with command categories"""
    help_text = """*Core Commands*:
├ /start - Initialize the bot
├ /help - Show this menu
├ /new - Start a new conversation thread
└ /reroll - Regenerate the last AI response

*Provider & Model Management*:
├ /provider - Show/switch AI provider
├ /model - Show current model
├ /list_models - List available models for the provider
└ /set_model `<model_name>` - Set a new model

*Advanced Tools*:
├ /search <query> - Answer a query using web search
└ /ask_selected <prompt> - Query multiple selected models at once

*Thread Management*:
├ /threads - List and manage conversation threads
└ /rename_thread <name> - Rename the current thread"""
    await update.message.reply_text(escape_markdown(help_text, version=2), parse_mode='MarkdownV2')

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Performs a web search, gets a response from the LLM, and saves the original
    query to history, not the augmented prompt.
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    log_prefix = f"(Chat {chat_id}) "

    if not context.args:
        await update.message.reply_text("Please provide a query to search. Usage: /search <your query>", parse_mode=None)
        return

    query = " ".join(context.args)
    logger.info(f"{log_prefix}User {user_id} initiated /search with query: '{query}'")

    placeholder_message = await update.message.reply_text(f"Searching the web for: \"{query}\"...", parse_mode=None)
    search_results = await web_search_service.perform_search(query)

    if search_results.startswith("Error:"):
        await placeholder_message.edit_text(search_results, parse_mode=None)
        return

    augmented_prompt = (
        f"Based on the following web search results, please provide a comprehensive answer to the user's query.\n\n"
        f"--- USER QUERY ---\n{query}\n\n"
        f"--- WEB SEARCH RESULTS ---\n{search_results}"
    )

    session_provider = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    provider_details = providers.get_provider_details()
    provider_config = provider_details.get(session_provider, provider_details[config.DEFAULT_PROVIDER])
    
    service = provider_config['service']
    model_key = provider_config['model_session_key']
    default_model = provider_config['default_model']
    model_to_use = await file_storage.get_thread_key(chat_id, model_key, default_model)

    await placeholder_message.edit_text(f"Found results. Asking {session_provider.capitalize()} ({model_to_use}) for analysis...", parse_mode=None)

    final_response = ""
    try:
        async for chunk in service.generate_response(model=model_to_use, prompt=augmented_prompt, context_history=[]):
            final_response += chunk
    except Exception as e:
        logger.error(f"{log_prefix}Error during search's LLM call: {e}", exc_info=True)
        await placeholder_message.edit_text("Sorry, an error occurred while processing the search results.", parse_mode=None)
        return

    try:
        await placeholder_message.delete()
        message_parts = split_message_markdown_aware(final_response)
        for part in message_parts:
            await context.bot.send_message(chat_id, text=escape_markdown_v2(part), parse_mode=constants.ParseMode.MARKDOWN_V2)
    except BadRequest:
        await context.bot.send_message(chat_id, text=final_response, parse_mode=None)
    except Exception as e:
        logger.error(f"{log_prefix}Failed to send final search response: {e}", exc_info=True)

    try:
        history = await file_storage.get_thread_key(chat_id, 'history', [])
        history.extend([
            {'role': 'user', 'content': query},
            {'role': 'assistant', 'content': final_response}
        ])
        await file_storage.set_thread_key(chat_id, 'history', history)
        logger.info(f"{log_prefix}Search command successful. History updated with original query.")
    except Exception as e:
        logger.error(f"{log_prefix}Failed to save history after search: {e}", exc_info=True)

async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a new conversation thread with a unique ID."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    new_thread_id = f"thread_{int(time.time())}"
    logger.info(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: Received /new command. Generating new thread ID: {new_thread_id}")
    try:
        await file_storage.create_thread(chat_id, new_thread_id)
        await file_storage.set_current_thread_id(chat_id, new_thread_id)
        await file_storage.set_thread_key(chat_id, 'history', [])
        default_provider = config.DEFAULT_PROVIDER
        await file_storage.set_thread_key(chat_id, 'provider', default_provider)
        provider_config = providers.get_config_for_provider(default_provider)
        if provider_config:
            model_key = provider_config.get('model_session_key')
            default_model = provider_config.get('default_model')
            if model_key and default_model:
                await file_storage.set_thread_key(chat_id, model_key, default_model)
        msg = f"Started a new thread: `{escape_markdown_v2(new_thread_id)}`"
        await update.message.reply_text(msg, parse_mode=constants.ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error creating new thread for chat {chat_id}: {e}", exc_info=True)
        await update.message.reply_text("An error occurred while creating a new thread.", parse_mode=None)

async def provider_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows current provider and buttons to switch."""
    chat_id = update.effective_chat.id
    current_provider = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    available_providers = providers.get_available_provider_names()
    if not available_providers:
         await update.message.reply_text("Error: No providers available.")
         return
    buttons = [InlineKeyboardButton(f"✅ {p}" if p == current_provider else p, callback_data=f"{PROVIDER_CALLBACK_PREFIX}{p}") for p in available_providers]
    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Current provider: *{escape_markdown(current_provider)}*\nChoose a new provider:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def set_provider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses for setting the provider."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    provider_name = query.data.replace(PROVIDER_CALLBACK_PREFIX, "")
    await file_storage.set_thread_key(chat_id, 'provider', provider_name)
    logger.info(f"Chat {chat_id} provider set to '{provider_name}'")
    
    available_providers = providers.get_available_provider_names()
    buttons = [InlineKeyboardButton(f"✅ {p}" if p == provider_name else p, callback_data=f"{PROVIDER_CALLBACK_PREFIX}{p}") for p in available_providers]
    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await query.edit_message_text(
            f"Provider set to *{escape_markdown(provider_name)}*.\nChoose a new provider:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to edit provider message: {e}")

async def list_threads_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists all threads for the user with switch/delete buttons."""
    chat_id = update.effective_chat.id
    threads = await file_storage.list_threads(chat_id)
    current_thread = await file_storage.get_current_thread_id(chat_id)
    if not threads:
        await update.message.reply_text("No threads found.")
        return

    keyboard = []
    for thread_id in threads:
        thread_data = await file_storage.get_thread_data(chat_id, thread_id)
        custom_name = thread_data.get('name')
        display_text = f"{custom_name.strip()} ({thread_id})" if custom_name and custom_name.strip() else thread_id
        label = f"✅ {display_text}" if thread_id == current_thread else display_text
        
        action_row = []
        if thread_id != current_thread:
            action_row.append(InlineKeyboardButton("Switch", callback_data=f"switch_thread:{thread_id}"))
        if thread_id != "default":
            action_row.append(InlineKeyboardButton("Delete", callback_data=f"delete_thread:{thread_id}"))
        
        keyboard.append([InlineKeyboardButton(label, callback_data="noop")] + action_row)
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Your conversation threads:", reply_markup=reply_markup)

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the currently selected model for the active provider."""
    chat_id = update.effective_chat.id
    provider_name = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    provider_config = providers.get_config_for_provider(provider_name)
    if not provider_config:
        await update.message.reply_text(f"Error: Provider '{escape_markdown(provider_name)}' not found or configured.")
        return
    model_session_key = provider_config['model_session_key']
    default_model = provider_config['default_model']
    current_model = await file_storage.get_thread_key(chat_id, model_session_key, default_model)
    await update.message.reply_text(
        f"Current model for provider *{escape_markdown(provider_name)}*: `{escape_markdown(current_model)}`",
        parse_mode='Markdown'
    )

async def list_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1, provider_name_from_callback: str | None = None) -> None:
    """Lists available/allowed models for the current provider."""
    chat_id = update.effective_chat.id
    provider_name = provider_name_from_callback or await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    
    provider_config = providers.get_config_for_provider(provider_name)
    service = providers.get_service_for_provider(provider_name)
    
    models_result = []
    if service and hasattr(service, 'list_models'):
        models_result = await service.list_models()
    elif provider_config and provider_config.get('allowed_models'):
        models_result = provider_config.get('allowed_models')

    if not models_result:
        await update.effective_message.reply_text(f"No models found for provider '{escape_markdown(provider_name)}'.")
        return

    buttons = []
    for model in models_result:
        model_id = model['id'] if isinstance(model, dict) else model
        display_name = model['name'] if isinstance(model, dict) else model
        buttons.append([InlineKeyboardButton(display_name, callback_data=f"{MODEL_CALLBACK_PREFIX}{provider_name}:{model_id}")])

    reply_markup = InlineKeyboardMarkup(buttons)
    await update.effective_message.reply_text(f"Select a model for *{escape_markdown(provider_name)}*:", reply_markup=reply_markup, parse_mode='Markdown')

async def list_models_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles pagination for model list."""
    query = update.callback_query
    await query.answer()
    try:
        _, provider_name, page_str = query.data.split(":")
        page = int(page_str)
        await list_models_command(update, context, page=page, provider_name_from_callback=provider_name)
    except (ValueError, IndexError):
        await query.edit_message_text("Error processing pagination.")

async def set_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses for setting the model."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    try:
        _, provider_name, model_name = query.data.split(":", 2)
        provider_config = providers.get_config_for_provider(provider_name)
        if not provider_config:
            await query.edit_message_text(f"Error: Provider '{escape_markdown(provider_name)}' not found.")
            return
        model_session_key = provider_config['model_session_key']
        await file_storage.set_thread_key(chat_id, model_session_key, model_name)
        await query.edit_message_text(f"Model for *{escape_markdown(provider_name)}* set to: `{escape_markdown(model_name)}`", parse_mode='Markdown')
    except (ValueError, IndexError):
        await query.edit_message_text("Error processing model selection.")

async def set_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to set a model by typing."""
    chat_id = update.effective_chat.id
    provider_name = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    await update.message.reply_text(
        f"Please type the name of the model for *{escape_markdown(provider_name)}*.",
        parse_mode='Markdown'
    )
    return SET_MODEL_TYPING

async def set_model_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles user typing a model name."""
    chat_id = update.effective_chat.id
    model_name = update.message.text.strip()
    provider_name = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    provider_config = providers.get_config_for_provider(provider_name)
    if provider_config:
        model_session_key = provider_config['model_session_key']
        await file_storage.set_thread_key(chat_id, model_session_key, model_name)
        await update.message.reply_text(
            f"Model for *{escape_markdown(provider_name)}* set to `{escape_markdown(model_name)}`.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f"Error: Provider '{escape_markdown(provider_name)}' not found.")
    return ConversationHandler.END

async def cancel_set_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the set model conversation."""
    await update.message.reply_text("Model selection cancelled.")
    return ConversationHandler.END

set_model_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("set_model", set_model_command)],
    states={
        SET_MODEL_TYPING: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_model_typed)],
    },
    fallbacks=[CommandHandler("cancel", cancel_set_model)],
)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    safe_user_name = user.mention_markdown_v2()
    await update.message.reply_markdown_v2(
        rf'Hi {safe_user_name}\! I am your friendly LLM bot\. How can I help you today\?'
    )

async def thread_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles switch/delete thread button presses."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    action, thread_id = query.data.split(":", 1)
    
    if action == "switch_thread":
        await file_storage.set_current_thread_id(chat_id, thread_id)
        await query.edit_message_text(f"Switched to thread: {thread_id}")
    elif action == "delete_thread":
        await file_storage.delete_thread(chat_id, thread_id)
        await query.edit_message_text(f"Deleted thread: {thread_id}")
    
    await list_threads_command(update, context)

async def rename_thread_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Renames the current thread."""
    chat_id = update.effective_chat.id
    new_name = " ".join(context.args)
    if not new_name:
        await update.message.reply_text("Usage: /rename_thread <new_name>")
        return
    await file_storage.rename_thread(chat_id, new_name)
    await update.message.reply_text(f"Thread renamed to: {new_name}")

async def reroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Regenerates the last AI response."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    log_prefix = f"(Chat {chat_id}) "
    logger.info(f"{log_prefix}User {user_id} triggered /reroll.")
    try:
        current_thread_id = await file_storage.get_current_thread_id(chat_id)
        last_user_prompt = await file_storage.get_thread_key(chat_id, 'last_user_prompt')
        if not last_user_prompt:
            await update.message.reply_text("There is no previous prompt to reroll.", parse_mode=None)
            return
        await _generate_and_send_response(
            update=update,
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            prompt=last_user_prompt,
            current_thread_id=current_thread_id,
            is_reroll=True
        )
    except Exception as e:
        logger.error(f"{log_prefix}Error during /reroll command: {e}", exc_info=True)
        await update.message.reply_text("An error occurred while trying to reroll.", parse_mode=None)

async def shrink_and_retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 'Shrink and Retry' button press."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    log_prefix = f"(Chat {chat_id}) "
    logger.info(f"{log_prefix}User {user_id} triggered 'Shrink and Retry'.")
    try:
        await query.edit_message_text("Understood. Retrying with a shortened context...", parse_mode=None)
        current_thread_id = await file_storage.get_current_thread_id(chat_id)
        last_user_prompt = await file_storage.get_thread_key(chat_id, 'last_user_prompt')
        if not last_user_prompt:
            await context.bot.send_message(chat_id, "Error: Couldn't find the last prompt to retry.", parse_mode=None)
            return
        await _generate_and_send_response(
            update=update,
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            prompt=last_user_prompt,
            current_thread_id=current_thread_id,
            is_reroll=False,
            force_truncate=True
        )
    except Exception as e:
        logger.error(f"{log_prefix}Error during shrink_and_retry_callback: {e}", exc_info=True)
        await context.bot.send_message(chat_id, "An error occurred during the retry.", parse_mode=None)

misc_handlers = [
    CommandHandler("help", help_command),
    CommandHandler("search", search_command),
    CommandHandler("reroll", reroll_command),
    CallbackQueryHandler(shrink_and_retry_callback, pattern="^shrink_and_retry$"),
    CommandHandler("new", new_command),
    CommandHandler("provider", provider_command),
    CallbackQueryHandler(set_provider_callback, pattern=f"^{PROVIDER_CALLBACK_PREFIX}.*$"),
    CommandHandler("threads", list_threads_command),
    CommandHandler("model", model_command),
    CommandHandler("list_models", list_models_command),
    CallbackQueryHandler(list_models_page_callback, pattern=f"^{MODEL_LIST_PAGE_CALLBACK_PREFIX}.*$"),
    CallbackQueryHandler(set_model_callback, pattern=f"^{MODEL_CALLBACK_PREFIX}.*$"),
    set_model_conv_handler,
    CommandHandler("start", start_command),
    CallbackQueryHandler(thread_callback_handler, pattern="^(switch_thread:|delete_thread:).*"),
    CommandHandler("rename_thread", rename_thread_command),
]