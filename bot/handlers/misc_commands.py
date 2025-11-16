import logging
import asyncio
import time
import hashlib
import telegram

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
from storage import storage_manager
from bot.messaging import send_safe_message
from services import web_search_service
from bot.response_generator import _generate_and_send_response

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
├ /reroll - Regenerate the last AI response
└ /cancel - Cancel the current operation

*Configuration*:
└ /config - Manage bot settings (auto-search, preferences)

*AI Tools & Search*:
├ /search <query> - Answer a query using web search
├ /ask_selected <prompt> - Query multiple selected models at once
├ /discuss <prompt> - Start a multi-model, multi-provider discussion
├ /discuss_panel - Orchestrate an expert AI panel
├ /configure_panel - Customize your Expert Panel agents
└ /end_discussion - Conclude an ongoing panel discussion

*Provider & Model Management*:
├ /provider - Show/switch AI provider
├ /model - Show current model
├ /list_models - List available models for the provider
├ /set_model `<model_name>` - Set a new model
└ /provider_status - Check the status of all configured providers

*Thread Management*:
├ /threads - List and manage conversation threads
└ /rename_thread <name> - Rename the current thread

*💡 Smart Features*:
• Auto-search: I can automatically web search when needed
• Configure in /config → Auto-Search settings"""
    await send_safe_message(context, update, help_text)

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE, placeholder_message = None) -> None:
    """
    Performs a web search, gets a response from the LLM, and saves the original
    query to history, not the augmented prompt.
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    log_prefix = f"(Chat {chat_id}) "

    if not context.args:
        await send_safe_message(context, update, "Please provide a query to search. Usage: /search <query>")
        return

    query = " ".join(context.args)
    logger.info(f"{log_prefix}User {user_id} initiated /search with query: '{query}'")

    try:
        if placeholder_message is None:
            placeholder_message = await context.bot.send_message(chat_id, f'Searching the web for: "{query}"...', parse_mode=None)
        else:
            await placeholder_message.edit_text(f'Searching the web for: "{query}"...', parse_mode=None)
    except telegram.error.NetworkError as e:
        logger.error(f"Network error while sending initial message in search_command: {e}")
        try:
            await send_safe_message(context, update, "A network error occurred, please try again.")
        except Exception as e_inner:
            logger.error(f"Failed to send network error message to user: {e_inner}")
        return

    search_response = await web_search_service.perform_search(query)

    if search_response['status'] == 'error':
        await placeholder_message.edit_text(f"⚠️ Web search failed: {search_response['message']}", parse_mode=None)
        return
    search_results = search_response['content']

    augmented_prompt = (
        f"Based on the following web search results, please provide a comprehensive answer to the user's query.\n\n"
        f"--- USER QUERY ---\n{query}\n\n"
        f"--- WEB SEARCH RESULTS ---\n{search_results}"
    )

    session_provider = await storage_manager.get_thread_key(chat_id, 'provider', config.get_default_provider())
    provider_details = providers.get_provider_details()
    provider_config = provider_details.get(session_provider, provider_details[config.get_default_provider()])
    
    service = provider_config['service']
    model_to_use = await storage_manager.get_thread_key(chat_id, 'model', provider_config['default_model'])

    await placeholder_message.edit_text(f"Found results. Asking {session_provider.capitalize()} ({model_to_use}) for analysis...", parse_mode=None)

    final_response = ""
    try:
        async for chunk in service.generate_response(model=model_to_use, prompt=augmented_prompt, context_history=[]):
            final_response += chunk
    except Exception as e:
        logger.error(f"{log_prefix}Error during search's LLM call: {e}", exc_info=True)
        await placeholder_message.edit_text("Sorry, an error occurred while processing the search results.", parse_mode=None)
        return

    await send_safe_message(context, update, final_response, placeholder_message)

    try:
        history = await storage_manager.get_thread_history(chat_id)
        history.extend([
            {'role': 'user', 'content': query},
            {'role': 'assistant', 'content': final_response}
        ])
        await storage_manager.set_thread_history(chat_id, history)
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
        await storage_manager.create_thread(chat_id, new_thread_id)
        await storage_manager.set_current_thread_id(chat_id, new_thread_id)
        await storage_manager.set_thread_history(chat_id, [])
        default_provider = config.get_default_provider()
        await storage_manager.set_thread_key(chat_id, 'provider', default_provider)
        provider_config = providers.get_config_for_provider(default_provider)
        if provider_config:
            default_model = provider_config.get('default_model')
            if default_model:
                await storage_manager.set_thread_key(chat_id, 'model', default_model)
        msg = f"Started a new thread: `{new_thread_id}`"
        await send_safe_message(context, update, msg)
    except Exception as e:
        logger.error(f"Error creating new thread for chat {chat_id}: {e}", exc_info=True)
        await send_safe_message(context, update, "An error occurred while creating a new thread.")

async def provider_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows current provider and buttons to switch."""
    chat_id = update.effective_chat.id
    current_provider = await storage_manager.get_thread_key(chat_id, 'provider', config.get_default_provider())
    available_providers = providers.get_available_provider_names()
    if not available_providers:
         await send_safe_message(context, update, "Error: No providers available.")
         return
    buttons = [InlineKeyboardButton(f"✅ {p}" if p == current_provider else p, callback_data=f"{PROVIDER_CALLBACK_PREFIX}{p}") for p in available_providers]
    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_safe_message(context, update, f"Current provider: *{current_provider}*\nChoose a new provider:", reply_markup=reply_markup)

async def set_provider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses for setting the provider."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    provider_name = query.data.replace(PROVIDER_CALLBACK_PREFIX, "")
    await storage_manager.set_thread_key(chat_id, 'provider', provider_name)
    logger.info(f"Chat {chat_id} provider set to '{provider_name}'")
    
    available_providers = providers.get_available_provider_names()
    buttons = [InlineKeyboardButton(f"✅ {p}" if p == provider_name else p, callback_data=f"{PROVIDER_CALLBACK_PREFIX}{p}") for p in available_providers]
    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await send_safe_message(context, update, f"Provider set to *{provider_name}*.\nChoose a new provider:", reply_markup=reply_markup)

async def list_threads_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists all threads for the user with switch/delete buttons."""
    chat_id = update.effective_chat.id
    threads = await storage_manager.list_threads(chat_id)
    current_thread = await storage_manager.get_current_thread_id(chat_id)

    if not threads:
        await send_safe_message(context, update, "No threads found.")
        return

    keyboard = []
    for thread_info in threads:
        thread_id = thread_info.get("id")
        custom_name = thread_info.get("name")

        display_text = f"{custom_name.strip()} ({thread_id})" if custom_name and custom_name.strip() else thread_id
        label = f"✅   {display_text}" if thread_id == current_thread else display_text

        action_row = []
        if thread_id != current_thread:
            action_row.append(InlineKeyboardButton("Switch", callback_data=f"switch_thread:{thread_id}"))
        if thread_id != "default":
            action_row.append(InlineKeyboardButton("Delete", callback_data=f"delete_thread:{thread_id}"))

        keyboard.append([InlineKeyboardButton(label, callback_data="noop")] + action_row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_safe_message(context, update, "Your conversation threads:", reply_markup=reply_markup)

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    provider_name = await storage_manager.get_thread_key(chat_id, 'provider', config.get_default_provider())
    provider_config = providers.get_config_for_provider(provider_name)
    current_model = await storage_manager.get_thread_key(chat_id, 'model', provider_config['default_model'])

    message_text = f"Current model for *{provider_name}*: `{current_model}`"
    await send_safe_message(context, update, message_text)

async def list_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1, provider_name_from_callback: str | None = None) -> None:
    """Lists available/allowed models for the current provider with pagination."""
    chat_id = update.effective_chat.id
    provider_name = provider_name_from_callback or await storage_manager.get_thread_key(chat_id, 'provider', config.get_default_provider())
    
    provider_config = providers.get_config_for_provider(provider_name)
    if not provider_config:
        await send_safe_message(context, update, f"Error: Could not find configuration for provider '{provider_name}'.")
        return
        
    service = providers.get_service_for_provider(provider_name)
    
    models_result = []
    try:
        if service and hasattr(service, 'list_models'):
            models_result = await service.list_models()
        elif provider_config.get('allowed_models'):
            models_result = provider_config.get('allowed_models')
    except Exception as e:
        logger.error(f"Failed to get models for provider '{provider_name}': {e}")
        await send_safe_message(context, update, f"An error occurred while fetching models for '{provider_name}'.")
        return

    if not models_result:
        await send_safe_message(context, update, f"No models found or configured for provider '{provider_name}'.")
        return

    models_result.sort(key=lambda m: m['name'].lower() if isinstance(m, dict) else m.lower())

    total_models = len(models_result)
    start_index = (page - 1) * MODELS_PER_PAGE
    end_index = start_index + MODELS_PER_PAGE
    paginated_models = models_result[start_index:end_index]

    context.user_data.setdefault('model_metadata', {})

    buttons = []
    for model in paginated_models:
        model_id = model['id'] if isinstance(model, dict) else model
        display_name = model['name'] if isinstance(model, dict) else model
        
        unique_key = f"{provider_name}_{model_id}".encode()
        model_hash = hashlib.sha256(unique_key).hexdigest()[:12]
        
        context.user_data['model_metadata'][model_hash] = {'provider': provider_name, 'model_id': model_id}
        
        display_name_short = (display_name[:25] + '...') if len(display_name) > 28 else display_name
        buttons.append([InlineKeyboardButton(display_name_short, callback_data=f"{MODEL_CALLBACK_PREFIX}{model_hash}")])

    pagination_row = []
    if start_index > 0:
        pagination_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{MODEL_LIST_PAGE_CALLBACK_PREFIX}{provider_name}:{page-1}"))
    if end_index < total_models:
        pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"{MODEL_LIST_PAGE_CALLBACK_PREFIX}{provider_name}:{page+1}"))
    
    if pagination_row:
        buttons.append(pagination_row)

    reply_markup = InlineKeyboardMarkup(buttons)
    total_pages = (total_models + MODELS_PER_PAGE - 1) // MODELS_PER_PAGE
    message_text = f"Select a model for *{provider_name}* (Page {page}/{total_pages}):"
    
    await send_safe_message(context, update, message_text, reply_markup=reply_markup)

async def list_models_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles pagination for model list."""
    query = update.callback_query
    await query.answer()
    try:
        rest = query.data[len(MODEL_LIST_PAGE_CALLBACK_PREFIX):]
        provider_name, page_str = rest.split(':', 1)
        page = int(page_str)
        await list_models_command(update, context, page=page, provider_name_from_callback=provider_name)
    except (ValueError, IndexError) as e:
        logger.error(f"Error processing pagination callback: {e}", exc_info=True)
        await send_safe_message(context, update, "Error processing pagination. Please try the /list_models command again.")

async def set_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses for setting the model."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    try:
        model_hash = query.data[len(MODEL_CALLBACK_PREFIX):]
        
        model_metadata = context.user_data.get('model_metadata', {})
        model_info = model_metadata.get(model_hash)
        
        if not model_info:
            await send_safe_message(context, update, "Model selection has expired or the bot was restarted. Please use /list_models again.")
            return
            
        provider_name = model_info['provider']
        model_name = model_info['model_id']
        
        provider_config = providers.get_config_for_provider(provider_name)
        if not provider_config:
            await send_safe_message(context, update, f"Error: Provider '{provider_name}' not found.")
            return
            
        await storage_manager.set_thread_key(chat_id, 'model', model_name)
        
        context.user_data.pop('model_metadata', None)
            
        await send_safe_message(context, update, f"Model for *{provider_name}* set to: `{model_name}`")
    except Exception as e:
        logger.error(f"Error in set_model_callback: {e}", exc_info=True)
        await send_safe_message(context, update, "An error occurred while setting the model. Please try again.")

async def set_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to set a model by typing."""
    chat_id = update.effective_chat.id
    provider_name = await storage_manager.get_thread_key(chat_id, 'provider', config.get_default_provider())
    await send_safe_message(context, update, f"Please type the name of the model for *{provider_name}*.")
    return SET_MODEL_TYPING

async def set_model_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles user typing a model name."""
    chat_id = update.effective_chat.id
    model_name = update.message.text.strip()
    provider_name = await storage_manager.get_thread_key(chat_id, 'provider', config.get_default_provider())
    provider_config = providers.get_config_for_provider(provider_name)
    if provider_config:
        await storage_manager.set_thread_key(chat_id, 'model', model_name)
        await send_safe_message(context, update, f"Model for *{provider_name}* set to `{model_name}`.")
    else:
        await send_safe_message(context, update, f"Error: Provider '{provider_name}' not found.")
    return ConversationHandler.END

async def cancel_set_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the set model conversation."""
    await send_safe_message(context, update, "Model selection cancelled.")
    return ConversationHandler.END

set_model_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("set_model", set_model_command)],
    states={
        SET_MODEL_TYPING: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_model_typed)],
    },
    fallbacks=[CommandHandler("cancel", cancel_set_model)],
    per_message=False
)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    safe_user_name = user.mention_markdown_v2()
    await send_safe_message(context, update, rf'Hi {safe_user_name}\! I am your friendly LLM bot\. Use /help to see what I can do\.')

async def thread_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles switch/delete thread button presses."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    action, thread_id = query.data.split(":", 1)
    
    if action == "switch_thread":
        await storage_manager.set_current_thread_id(chat_id, thread_id)
        await send_safe_message(context, update, f"Switched to thread: {thread_id}")
    elif action == "delete_thread":
        await storage_manager.delete_thread(chat_id, thread_id)
        await send_safe_message(context, update, f"Deleted thread: {thread_id}")
    
    await list_threads_command(update, context)

async def rename_thread_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Renames the current thread."""
    chat_id = update.effective_chat.id
    new_name = " ".join(context.args)
    if not new_name:
        await send_safe_message(context, update, "Usage: /rename_thread <new_name>")
        return
    
    success = await storage_manager.rename_thread(chat_id, new_name)
    if success:
        await send_safe_message(context, update, f"Thread renamed to: {new_name}")
    else:
        await send_safe_message(context, update, "An error occurred while renaming the thread.")

async def delete_thread_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes a specific thread by ID."""
    chat_id = update.effective_chat.id
    if not context.args:
        await send_safe_message(context, update, "Usage: /delete_thread <thread_id>")
        return
    thread_id = context.args[0].strip()
    
    threads = await storage_manager.list_threads(chat_id)
    thread_ids = [t.get("id") if isinstance(t, dict) else t for t in threads]
    
    if thread_id not in thread_ids:
        await send_safe_message(context, update, f"Error: Thread `{thread_id}` not found. Use /threads to list all threads.")
        return
    
    await storage_manager.delete_thread(chat_id, thread_id)
    await send_safe_message(context, update, f"Thread `{thread_id}` deleted successfully.")

async def reroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Regenerates the last AI response."""


    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    log_prefix = f"(Chat {chat_id}) "
    logger.info(f"{log_prefix}User {user_id} triggered /reroll.")
    try:
        current_thread_id = await storage_manager.get_current_thread_id(chat_id)
        last_user_prompt = await storage_manager.get_thread_key(chat_id, 'last_user_prompt')
        if not last_user_prompt:
            await send_safe_message(context, update, "There is no previous prompt to reroll.")
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
        await send_safe_message(context, update, "An error occurred while trying to reroll.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels an active, non-conversation LLM task."""
    chat_id = update.effective_chat.id
    llm_task = context.chat_data.get('llm_task')
    if llm_task and not llm_task.done():
        llm_task.cancel()
        logger.info(f"(Chat {chat_id}) Normal chat LLM task cancelled by user.")
        await send_safe_message(context, update, "The current AI response generation has been cancelled.")
        context.chat_data.pop('llm_task', None)
    else:
        await send_safe_message(context, update, "There is no active response generation to cancel.")

async def provider_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks the status of all configured AI providers."""
    statuses = []
    all_providers = providers.get_provider_details()

    for provider_name, provider_config in all_providers.items():
        service = provider_config.get('service')
        status_message = f"❓ {provider_name.capitalize()}: Status check not implemented."

        if service and hasattr(service, 'check_status'):
            is_ok, message = await service.check_status()
            icon = "✅" if is_ok else "❌"
            status_message = f"{icon} {provider_name.capitalize()}: {message}"
        
        statuses.append(status_message)

    if not statuses:
        message = "No providers are configured."
    else:
        message = "Provider Status:\n\n" + "\n".join(sorted(statuses))
        
    await send_safe_message(context, update, message)

misc_handlers = [
    CommandHandler("help", help_command),
    CommandHandler("search", search_command),
    CommandHandler("reroll", reroll_command),
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
    CommandHandler("cancel", cancel_command),
    CommandHandler("provider_status", provider_status_command),
]