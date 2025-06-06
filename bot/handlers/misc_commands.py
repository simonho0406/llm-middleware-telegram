import logging
import asyncio
import time # Added for new_command

# --- Telegram Imports ---
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    # BotCommand, # No longer defining commands here
    # MenuButtonCommands, # No longer setting menu button here
    # WebAppInfo # Keep if used elsewhere, otherwise remove
)
from telegram.ext import (
    # Application, # No longer needed for post_init
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters
)
from telegram.helpers import escape_markdown
from telegram.constants import ParseMode

# --- Project Imports ---
import config # Assuming your config file
# Use the new provider helpers
from bot.providers import get_provider_details, get_available_provider_names, get_config_for_provider, get_service_for_provider
from storage import file_storage # Assuming your storage module

# Import provider-specific list commands

logger = logging.getLogger(__name__)

# --- Constants ---
PROVIDER_CALLBACK_PREFIX = "set_provider_"
MODEL_CALLBACK_PREFIX = "set_model_"
MODEL_LIST_PAGE_CALLBACK_PREFIX = "list_models_page_"
SET_MODEL_TYPING = 1
MODELS_PER_PAGE = 10

# --- Logging Setup --- (Ensure this is configured appropriately elsewhere, e.g. config.py or main.py)
# logging.basicConfig(...) # Usually configured once globally

# --- REMOVED Bot Startup Configuration ---
# The post_init function belongs in main.py's startup sequence


# --- Command Handlers ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends structured help message with command categories"""
    help_text = """*Core Functionality*:
├ /start - Initialize the bot
├ /help - Show this menu
├ /provider - Show current provider & switch AI service
└ /new - Start a new conversation thread

*Model Management*:
├ /model - Show current model for active provider
├ /list_models - List available models for active provider
└ /set_model `<model_name>` - Set model for active provider (or select from /list_models)

*Thread Management*:
├ /rename_thread <name> - Rename the current thread
└ /threads - List and manage conversation threads"""
    # Using escape_markdown V1 as V2 is stricter and might break with complex text
    await update.message.reply_text(escape_markdown(help_text), parse_mode='Markdown')

# --- REMOVED refresh_menu_command ---
# This command is redundant as setup happens on startup in main.py.
# Manually setting commands here without setting the menu button type is incorrect.


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a new conversation thread with a unique ID."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    # import time # Already imported at top
    # from telegram.helpers import escape_markdown # Already imported at top
    new_thread_id = f"thread_{int(time.time())}"
    logger.info(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: Received /new command. Generating new thread ID: {new_thread_id}")

    try:
        # Use asyncio timeout to prevent hanging
        logger.debug(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: Creating thread {new_thread_id} in file_storage.")
        create_result = await asyncio.wait_for(file_storage.create_thread(chat_id, new_thread_id), timeout=5)
        logger.info(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: create_thread({chat_id}, {new_thread_id}) -> {create_result}")

        logger.debug(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: Setting current thread to {new_thread_id}.")
        set_current_result = await asyncio.wait_for(file_storage.set_current_thread_id(chat_id, new_thread_id), timeout=5)
        logger.info(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: set_current_thread_id({chat_id}, {new_thread_id}) -> {set_current_result}")

        logger.debug(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: Initializing thread history for {new_thread_id}.")
        set_history_result = await asyncio.wait_for(file_storage.set_thread_key(chat_id, 'history', []), timeout=5)
        logger.info(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: set_thread_key({chat_id}, 'history', []) -> {set_history_result}")

        # Initialize provider and model for the new thread
        default_provider = config.DEFAULT_PROVIDER
        logger.debug(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: Setting provider to {default_provider} for thread {new_thread_id}.")
        set_provider_result = await asyncio.wait_for(file_storage.set_thread_key(chat_id, 'provider', default_provider), timeout=5)
        logger.info(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: set_thread_key({chat_id}, 'provider', {default_provider}) -> {set_provider_result}")

        provider_config = get_config_for_provider(default_provider)
        if provider_config:
            model_key = provider_config.get('model_session_key')
            default_model = provider_config.get('default_model')
            logger.debug(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: Provider config found: model_key={model_key}, default_model={default_model}")
            if model_key and default_model:
                set_model_result = await asyncio.wait_for(file_storage.set_thread_key(chat_id, model_key, default_model), timeout=5)
                logger.info(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: set_thread_key({chat_id}, {model_key}, {default_model}) -> {set_model_result}")
            else:
                logger.warning(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: Could not find model_key or default_model for provider {default_provider} in config.")
        else:
            logger.warning(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: Could not find provider config for default provider {default_provider}.")

        logger.info(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: New thread {new_thread_id} created and initialized successfully.")
        # Use MarkdownV2 for IDs which might contain underscores
        msg = f"Started a new thread: `{escape_markdown(new_thread_id, version=2)}`"
        reply_func = update.message.reply_markdown_v2 if update.message else update.effective_chat.send_message
        await reply_func(msg, parse_mode='MarkdownV2') # Explicitly set parse_mode for send_message

        logger.debug(f"[NEW_THREAD] User {user_id}, Chat {chat_id}: /new command completed successfully for thread {new_thread_id}.")
    except asyncio.TimeoutError as te:
        logger.error(f"[NEW_THREAD][ERROR] User {user_id}, Chat {chat_id}, Thread {new_thread_id}: Timeout during thread creation: {te}")
        err_msg = "Thread creation timed out. Please try again later."
        if update.message:
            await update.message.reply_text(err_msg, parse_mode=None)
        else:
            await update.effective_chat.send_message(err_msg)
    except Exception as e:
        logger.error(f"[NEW_THREAD][ERROR] User {user_id}, Chat {chat_id}, Thread {new_thread_id}: Exception during thread creation: {e}", exc_info=True)
        err_msg = "An error occurred while creating a new thread. Please try again later."
        if update.message:
            await update.message.reply_text(err_msg, parse_mode=None)
        else:
            await update.effective_chat.send_message(err_msg)
    except asyncio.TimeoutError:
        logger.error(f"Timeout while creating new thread for chat {chat_id}")
        err_msg = "Thread creation timed out. Please try again later."
        if update.message:
            await update.message.reply_text(err_msg, parse_mode=None)
        else:
            await update.effective_chat.send_message(err_msg)
    except Exception as e:
        logger.error(f"Error creating new thread for chat {chat_id}: {e}")
        err_msg = "An error occurred while creating a new thread. Please try again."
        if update.message:
            await update.message.reply_text(err_msg, parse_mode=None)
        else:
            await update.effective_chat.send_message(err_msg)


async def provider_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows current provider and buttons to switch."""
    chat_id = update.effective_chat.id
    current_provider = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    
    # Use the new helper
    available_providers = get_available_provider_names()
    if not available_providers:
         await update.message.reply_text("Error: No providers available.")
         return

    buttons = []
    for provider in available_providers:
        button_text = f"✅ {provider}" if provider == current_provider else provider
        buttons.append(InlineKeyboardButton(button_text, callback_data=f"{PROVIDER_CALLBACK_PREFIX}{provider}"))

    # Arrange buttons in rows (e.g., 3 per row)
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
    await query.answer() # Acknowledge the button press

    chat_id = update.effective_chat.id
    provider_name = query.data.replace(PROVIDER_CALLBACK_PREFIX, "")

    # Use the new helper
    available_providers = get_available_provider_names()
    if provider_name not in available_providers:
        logger.warning(f"Callback received for unavailable provider: {provider_name}")
        await query.edit_message_text(f"Error: Provider '{escape_markdown(provider_name)}' is not available.")
        return

    # Save the new provider
    await file_storage.set_thread_key(chat_id, 'provider', provider_name)
    logger.info(f"Chat {chat_id} provider set to '{provider_name}'")

    # Update the message to show the new selection
    # Also reset the model when provider changes? Maybe not, let user manage.
    # await file_storage.delete_thread_key(chat_id, 'current_model') # Example if reset needed

    # Update the message to show the new selection
    current_provider = provider_name # The one just selected
    buttons = []
    for provider in available_providers:
        button_text = f"✅ {provider}" if provider == current_provider else provider
        buttons.append(InlineKeyboardButton(button_text, callback_data=f"{PROVIDER_CALLBACK_PREFIX}{provider}"))

    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            f"Provider set to *{escape_markdown(current_provider)}*.\nChoose a new provider:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to edit provider message: {e}") # Handle potential errors if message is old


async def list_threads_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists all threads for the user with switch/delete buttons."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    from telegram.helpers import escape_markdown
    logger.info(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: Received /threads command.")

    try:
        logger.debug(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: Listing threads from file_storage.")
        threads = await asyncio.wait_for(file_storage.list_threads(chat_id), timeout=5)
        logger.info(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: list_threads({chat_id}) -> {threads}")

        logger.debug(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: Getting current thread ID from file_storage.")
        current_thread = await asyncio.wait_for(file_storage.get_current_thread_id(chat_id), timeout=5)
        logger.info(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: get_current_thread_id({chat_id}) -> {current_thread}")

        if not threads:
            msg = "No threads found."
            logger.info(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: No threads found.")
            if update.message:
                await update.message.reply_text(msg)
            else:
                await update.effective_chat.send_message(msg)
            return

        keyboard = []
        for thread_id in threads:
            row = []
            label = f"✅ {escape_markdown(thread_id)}" if thread_id == current_thread else escape_markdown(thread_id)
            logger.debug(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: Preparing thread entry for thread_id={thread_id}, current={thread_id == current_thread}")
            if thread_id != current_thread:
                row.append(InlineKeyboardButton("Switch", callback_data=f"switch_thread:{thread_id}"))
                logger.debug(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: Added Switch button for thread_id={thread_id}")
            if thread_id != "default":
                row.append(InlineKeyboardButton("Delete", callback_data=f"delete_thread:{thread_id}"))
                logger.debug(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: Added Delete button for thread_id={thread_id}")
            keyboard.append([InlineKeyboardButton(label, callback_data="noop")] + row)

        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = "Your conversation threads:"
        logger.info(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: Sending thread list to user. Threads: {threads}, Current: {current_thread}")
        if update.message:
            await update.message.reply_text(
                msg,
                reply_markup=reply_markup
            )
        else:
            await update.effective_chat.send_message(
                msg,
                reply_markup=reply_markup
            )
        logger.debug(f"[LIST_THREADS] User {user_id}, Chat {chat_id}: /threads command completed successfully.")
    except asyncio.TimeoutError as te:
        logger.error(f"[LIST_THREADS][ERROR] User {user_id}, Chat {chat_id}: Timeout while listing threads: {te}")
        err_msg = "Listing threads timed out. Please try again later."
        if update.message:
            await update.message.reply_text(err_msg, parse_mode=None)
        else:
            await update.effective_chat.send_message(err_msg)
    except Exception as e:
        logger.error(f"[LIST_THREADS][ERROR] User {user_id}, Chat {chat_id}: Exception while listing threads: {e}", exc_info=True)
        err_msg = "An error occurred while listing threads. Please try again later."
        if update.message:
            await update.message.reply_text(err_msg, parse_mode=None)
        else:
            await update.effective_chat.send_message(err_msg)

# --- Generic Model Commands ---

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the currently selected model for the active provider."""
    chat_id = update.effective_chat.id
    provider_name = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    provider_config = get_config_for_provider(provider_name)

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

# Modified function to handle pagination
async def list_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1, provider_name_from_callback: str | None = None) -> None:
    """Lists available/allowed models for the current provider using the provider abstraction, with pagination."""
    chat_id = update.effective_chat.id
    models_result = []
    error_message = None
    provider_name = provider_name_from_callback # Use provider from callback if available

    # Check if this is called from a pagination callback (now handled by list_models_page_callback)
    # We rely on list_models_page_callback to call this function with the correct page and provider_name
    if not provider_name_from_callback:
        logger.info(f"Received /list_models command from chat_id: {chat_id}")
    else:
        logger.info(f"Generating page {page} for provider '{provider_name}' for chat_id: {chat_id}")

    try:
        # Get provider name if not passed from callback
        if not provider_name:
             provider_name = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)

        if not provider_name:
            await update.effective_message.reply_text("Error: No provider configured for this chat.")
            return

        provider_config = get_config_for_provider(provider_name)
        service = get_service_for_provider(provider_name)

        if not service:
            error_message = f"Service for provider '{escape_markdown(provider_name)}' not available."
        else:
            list_models_func = getattr(service, 'list_models', None)
            if list_models_func is None or not callable(list_models_func):
                # Check if allowed_models exist in config as a fallback
                if provider_config and provider_config.get('allowed_models'):
                    models_result = provider_config['allowed_models']
                    logger.info(f"Provider '{provider_name}' service has no list_models method, using configured allowed_models.")
                else:
                    error_message = f"Model listing is not supported for provider '{escape_markdown(provider_name)}'."
            else:
                try:
                    # Attempt to call the service's list_models function
                    # Cache results in context.chat_data to avoid refetching on pagination
                    cache_key = f"models_{provider_name}"
                    if cache_key not in context.chat_data:
                        logger.debug(f"Fetching and caching models for provider '{provider_name}'")
                        fetched_models = await list_models_func()
                        # Ensure fetched_models is a list, even if None is returned
                        context.chat_data[cache_key] = fetched_models if isinstance(fetched_models, list) else []
                        logger.debug(f"Cached {len(context.chat_data[cache_key])} models for '{provider_name}'")

                    models_result = context.chat_data[cache_key]

                    # Check again if allowed_models should be used (e.g., if API failed or returned empty)
                    if not models_result and provider_config and provider_config.get('allowed_models'):
                         models_result = provider_config['allowed_models']
                         # Update cache if we fell back to allowed_models
                         context.chat_data[cache_key] = models_result
                         logger.info(f"Using configured allowed_models for '{provider_name}' as API returned empty or failed.")

                except Exception as e:
                    logger.error(f"Error calling list_models for provider '{provider_name}': {e}", exc_info=True)
                    error_message = f"An error occurred while fetching models for {escape_markdown(provider_name)}."

        # --- Process results and send message ---
        # Determine the reply function based on whether it's a command or callback query edit
        reply_func = update.callback_query.edit_message_text if update.callback_query else update.effective_message.reply_text
        reply_md_func = update.callback_query.edit_message_text if update.callback_query else update.effective_message.reply_markdown_v2

        if error_message:
            await reply_func(error_message, parse_mode='Markdown')
            return

        if not models_result:
            await reply_func(f"No models found or available for provider '{escape_markdown(provider_name)}'.", parse_mode='Markdown')
            return

        # Prepare buttons
        keyboard = []
        row = []
        # Sort models for consistent display (handle strings and dicts)
        def get_sort_key(item):
            if isinstance(item, dict):
                return item.get('name', item.get('id', '')).lower()
            elif isinstance(item, str):
                return item.lower()
            return ''
        sorted_models = sorted(models_result, key=get_sort_key)

        # --- Pagination Logic ---
        total_models = len(sorted_models)
        total_pages = (total_models + MODELS_PER_PAGE - 1) // MODELS_PER_PAGE
        page = max(1, min(page, total_pages)) # Clamp page number
        start_index = (page - 1) * MODELS_PER_PAGE
        end_index = start_index + MODELS_PER_PAGE
        models_on_page = sorted_models[start_index:end_index]

        # Prepare buttons for the current page
        skipped_count = 0
        for model_item in models_on_page:
            model_id = None
            display_name = None

            if isinstance(model_item, str):
                model_id = model_item
                display_name = model_id
            elif isinstance(model_item, dict):
                model_id = model_item.get('id')
                display_name = model_item.get('name', model_id) # Prefer name, fallback to id

            if not model_id or not display_name:
                logger.warning(f"Skipping invalid model item: {model_item}")
                continue

            # Shorten name if too long for button
            button_text = display_name if len(display_name) < 30 else display_name[:27] + "..."
            # Use format: set_model_<provider>:<model_id>
            callback_data = f"{MODEL_CALLBACK_PREFIX}{provider_name}:{model_id}"

            # Check callback data length (Telegram limit is 64 bytes)
            if len(callback_data.encode('utf-8')) > 64:
                 logger.warning(f"Callback data too long ({len(callback_data.encode('utf-8'))} bytes), skipping model: {callback_data}")
                 skipped_count += 1
                 continue # Skip this model

            row.append(InlineKeyboardButton(button_text, callback_data=callback_data))
            if len(row) == 2: # Adjust number per row if needed
                keyboard.append(row)
                row = []
        if row: # Add the last row if it's not full
            keyboard.append(row)

        if not keyboard and skipped_count == len(models_on_page) and total_pages <= 1 :
             # Handle case where all models on the *only* page were skipped
             await reply_func(f"No valid models could be displayed for provider '{escape_markdown(provider_name)}'.", parse_mode='Markdown')
             return
        elif not keyboard and skipped_count == len(models_on_page):
             # Handle case where all models on *this* page were skipped, but other pages might exist
             await reply_func(f"No valid models could be displayed for provider '{escape_markdown(provider_name)}' on page {page}.", parse_mode='Markdown')
             # We might still want pagination buttons if other pages exist
             # Let's add them below regardless for now.
             pass # Continue to add pagination buttons if applicable

        # Add pagination buttons
        pagination_row = []
        if page > 1:
            prev_callback = f"{MODEL_LIST_PAGE_CALLBACK_PREFIX}{provider_name}:{page-1}"
            pagination_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=prev_callback))
        if page < total_pages:
            next_callback = f"{MODEL_LIST_PAGE_CALLBACK_PREFIX}{provider_name}:{page+1}"
            pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=next_callback))

        if pagination_row:
            keyboard.append(pagination_row)

        # Only create markup if there are any buttons (models or pagination)
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        # Get current model for display
        current_model = "N/A"
        if provider_config:
             model_session_key = provider_config.get('model_session_key')
             default_model = provider_config.get('default_model')
             if model_session_key and default_model:
                  current_model = await file_storage.get_thread_key(chat_id, model_session_key, default_model)

        page_info = f"Page {page}/{total_pages}"
        message_text = (
             f"Select a model for *{escape_markdown(provider_name, version=2)}* "
             f"\\(current: `{escape_markdown(current_model, version=2)}`\\) \\- {escape_markdown(page_info, version=2)}:"
        )

        try:
            await reply_md_func(
                text=message_text,
                reply_markup=reply_markup # Pass markup even if None (to potentially remove old one)
            )
        except Exception as e:
             # Handle potential edit errors (e.g., message not modified)
             if "Message is not modified" in str(e):
                 logger.debug("Message not modified during list_models update.")
             else:
                 logger.error(f"Failed to send/edit message for list_models: {e}")


    except Exception as e:
        logger.error(f"Unexpected error in list_models_command for chat {chat_id}: {e}", exc_info=True)
        error_reply_func = update.callback_query.edit_message_text if update.callback_query else update.effective_message.reply_text
        try:
            await error_reply_func("An unexpected error occurred while listing models.")
        except Exception as inner_e:
             logger.error(f"Failed to send error message in list_models_command: {inner_e}")

# --- Callback handler for model list pagination ---
async def list_models_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 'Next'/'Previous' button clicks for model list pagination."""
    query = update.callback_query
    if not query or not query.data.startswith(MODEL_LIST_PAGE_CALLBACK_PREFIX):
        logger.warning("Invalid call to list_models_page_callback")
        if query: await query.answer("Invalid request.")
        return

    try:
        callback_data = query.data.replace(MODEL_LIST_PAGE_CALLBACK_PREFIX, "")
        provider_name, page_str = callback_data.split(":", 1)
        page = int(page_str)
        await query.answer() # Acknowledge callback
        # Call the main list_models_command function with the specific page and provider
        await list_models_command(update, context, page=page, provider_name_from_callback=provider_name)
    except (ValueError, IndexError, TypeError) as e:
        logger.error(f"Error parsing pagination callback data '{query.data}': {e}")
        await query.edit_message_text("Error processing pagination request.")
    except Exception as e:
         logger.error(f"Unexpected error in list_models_page_callback: {e}", exc_info=True)
         await query.edit_message_text("An internal error occurred during pagination.")


async def set_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses for setting the model."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    try:
        # Extract provider and model from callback data, e.g., "set_model_groq:llama3-8b-8192"
        callback_data = query.data.replace(MODEL_CALLBACK_PREFIX, "")
        provider_name, model_name = callback_data.split(":", 1)
    except ValueError:
        logger.error(f"Invalid model callback data format: {query.data}")
        await query.edit_message_text("Error: Invalid callback data.")
        return

    provider_config = get_config_for_provider(provider_name)
    if not provider_config:
        await query.edit_message_text(f"Error: Provider '{escape_markdown(provider_name)}' not found.")
        return

    # Validate model - Step 1: Check service cache, Step 2: Check allowed_models, Step 3: Fallback logic
    is_allowed = False
    cache_key = f"models_{provider_name}"
    cached_models = context.chat_data.get(cache_key, [])
    
    # First try service.list_models() cache if available
    if cached_models:
        if isinstance(cached_models[0], str):
            is_allowed = model_name in cached_models
        else:  # list of dicts
            is_allowed = any(model.get('id') == model_name for model in cached_models)
    
    # Then try provider_config allowed_models
    if not is_allowed:
        allowed_models_list = provider_config.get('allowed_models', [])
        is_allowed = model_name in allowed_models_list
    
    # Finally apply fallback logic
    if not is_allowed:
        is_allowed = (provider_name == 'ollama' or not allowed_models_list or
                     model_name == provider_config['default_model'])

    if not is_allowed:
         logger.warning(f"Attempt to set disallowed model '{model_name}' for provider '{provider_name}' in chat {chat_id}")
         await query.edit_message_text(f"Error: Model `{escape_markdown(model_name)}` is not allowed for provider *{escape_markdown(provider_name)}*.", parse_mode='Markdown')
         return

    # Save the new model using the correct session key
    model_session_key = provider_config['model_session_key']
    await file_storage.set_thread_key(chat_id, model_session_key, model_name)
    logger.info(f"Chat {chat_id} model for provider '{provider_name}' set to '{model_name}'")

    # --- Simplify Callback Response ---
    # Just confirm the selection with plain text, remove the keyboard.
    # This avoids potential errors from re-rendering the list or using Markdown.
    try:
        # Escape provider and model names for MarkdownV2
        escaped_provider = escape_markdown(provider_name, version=2)
        escaped_model_name = escape_markdown(model_name, version=2)
        await query.edit_message_text(
            f"Model for *{escaped_provider}* set to: `{escaped_model_name}`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=None # Remove buttons after selection
        )
    except Exception as e:
         # Log error if editing fails (e.g., message too old)
         logger.error(f"Failed to edit message after model selection: {e}")


async def set_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to set a model by typing its name."""
    chat_id = update.effective_chat.id
    provider_name = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)

    await update.message.reply_text(
        f"Please type the name of the model you want to use for the current provider (*{escape_markdown(provider_name)}*).\n"
        f"You can see available models with /list_models.",
        parse_mode='Markdown'
    )
    return SET_MODEL_TYPING

async def set_model_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user typing a model name."""
    chat_id = update.effective_chat.id
    model_name = update.message.text.strip()
    provider_name = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    provider_config = get_config_for_provider(provider_name)

    if not provider_config:
        await update.message.reply_text(f"Error: Provider '{escape_markdown(provider_name)}' not found.")
        return ConversationHandler.END

    # Validate model - Step 1: Check service cache, Step 2: Check allowed_models, Step 3: Fallback logic
    is_allowed = False
    cache_key = f"models_{provider_name}"
    cached_models = context.chat_data.get(cache_key, [])
    
    # First try service.list_models() cache if available
    if cached_models:
        if isinstance(cached_models[0], str):
            is_allowed = model_name in cached_models
        else:  # list of dicts
            is_allowed = any(model.get('id') == model_name for model in cached_models)
    
    # Then try provider_config allowed_models
    if not is_allowed:
        allowed_models_list = provider_config.get('allowed_models', [])
        is_allowed = model_name in allowed_models_list
    
    # Finally apply fallback logic
    if not is_allowed:
        is_allowed = (provider_name == 'ollama' or not allowed_models_list or
                     model_name == provider_config['default_model'])

    # For Ollama, we might want to dynamically check if the model exists via API?
    # This adds complexity and delay. For now, let's trust the user input for Ollama
    # or rely on the /list_models command.

    if not is_allowed and provider_name != 'ollama':
        await update.message.reply_text(
            f"Error: Model `{escape_markdown(model_name)}` is not in the allowed list for *{escape_markdown(provider_name)}*.\n"
            f"Use /list_models to see options.",
            parse_mode='Markdown'
        )
        return SET_MODEL_TYPING # Ask again

    # Save the model
    model_session_key = provider_config['model_session_key']
    await file_storage.set_thread_key(chat_id, model_session_key, model_name)
    logger.info(f"Chat {chat_id} model for provider '{provider_name}' set to '{model_name}' by typing.")
    await update.message.reply_text(
        f"Model for *{escape_markdown(provider_name)}* set to `{escape_markdown(model_name)}`.",
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def cancel_set_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the set model conversation."""
    await update.message.reply_text("Model selection cancelled.")
    return ConversationHandler.END


# --- Conversation Handler for /set_model ---
set_model_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("set_model", set_model_command)],
    states={
        SET_MODEL_TYPING: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_model_typed)],
    },
    fallbacks=[CommandHandler("cancel", cancel_set_model)],
)


# --- New Start Command ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message with provider selection buttons."""
    chat_id = update.effective_chat.id
    current_provider = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
    available_providers = get_available_provider_names()
    if not available_providers:
        await update.message.reply_text("Error: No providers available.")
        return

    buttons = []
    for provider in available_providers:
        button_text = f"✅ {provider}" if provider == current_provider else provider
        buttons.append(InlineKeyboardButton(button_text, callback_data=f"{PROVIDER_CALLBACK_PREFIX}{provider}"))

    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Welcome! Select a provider to get started:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def thread_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles switch/delete thread button presses."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    try:
        if data.startswith("switch_thread:"):
            thread_id = data.split(":", 1)[1]
            await file_storage.set_current_thread_id(chat_id, thread_id)
            # Fetch last user message in the thread
            thread_data = await file_storage.get_thread_data(chat_id, thread_id)
            history = thread_data.get("history", [])
            last_user_msg = None
            for msg in reversed(history):
                if msg.get("role") == "user" and msg.get("content"):
                    last_user_msg = msg["content"]
                    break
            if last_user_msg is None:
                last_user_msg = "_none_"
            from telegram.helpers import escape_markdown
            try:
                await query.edit_message_text(
                    f"Switched to thread: `{escape_markdown(thread_id)}`\n"
                    f"Last user request: {escape_markdown(last_user_msg)}",
                    parse_mode='Markdown'
                )
            except Exception as e:
                if "Message is not modified" in str(e):
                    pass  # Ignore benign error
                else:
                    logger.error(f"Error editing message after switch: {e}")
        elif data.startswith("delete_thread:"):
            thread_id = data.split(":", 1)[1]
            await file_storage.delete_thread(chat_id, thread_id)
            # After deletion, refresh the thread list
            threads = await file_storage.list_threads(chat_id)
            current_thread = await file_storage.get_current_thread_id(chat_id)
            if not threads:
                try:
                    await query.edit_message_text("No threads found.")
                except Exception as e:
                    if "Message is not modified" in str(e):
                        pass
                    else:
                        logger.error(f"Error editing message after delete: {e}")
                return
            keyboard = []
            for tid in threads:
                row = []
                label = f"✅ {tid}" if tid == current_thread else tid
                if tid != current_thread:
                    row.append(InlineKeyboardButton("Switch", callback_data=f"switch_thread:{tid}"))
                if tid != "default":
                    row.append(InlineKeyboardButton("Delete", callback_data=f"delete_thread:{tid}"))
                keyboard.append([InlineKeyboardButton(label, callback_data="noop")] + row)
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await query.edit_message_text("Your conversation threads:", reply_markup=reply_markup)
            except Exception as e:
                if "Message is not modified" in str(e):
                    pass
                else:
                    logger.error(f"Error editing message after delete refresh: {e}")
    except Exception as e:
        logger.error(f"Error handling thread callback: {e}")
        try:
            await query.edit_message_text("An error occurred. Please try again.", parse_mode=None)
        except:
            pass

async def rename_thread_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /rename_thread <new_name> command"""
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /rename_thread <new_name>")
        return
    new_name = " ".join(args).strip()
    if len(new_name) < 3 or len(new_name) > 30:
        await update.message.reply_text("Name must be 3-30 characters long")
        return
    if not new_name.isalnum():
        await update.message.reply_text("Name can only contain letters/numbers")
        return
    try:
        await file_storage.rename_thread(chat_id, new_name)
        await update.message.reply_text(f"Thread renamed to: {new_name}")
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")

misc_handlers = [
    CommandHandler("help", help_command),
    # CommandHandler("refresh_menu", refresh_menu_command),
    CommandHandler("new", new_command),
    CommandHandler("provider", provider_command),
    CallbackQueryHandler(set_provider_callback, pattern=f"^{PROVIDER_CALLBACK_PREFIX}.*$"),
    CommandHandler("threads", list_threads_command),
    CommandHandler("model", model_command),
    CommandHandler("list_models", list_models_command), # Entry point for the command
    CallbackQueryHandler(list_models_page_callback, pattern=f"^{MODEL_LIST_PAGE_CALLBACK_PREFIX}.*$"), # Handler for pagination buttons
    CallbackQueryHandler(set_model_callback, pattern=f"^{MODEL_CALLBACK_PREFIX}.*$"), # Handler for model selection buttons
    set_model_conv_handler,
    CommandHandler("start", start_command),
    CallbackQueryHandler(thread_callback_handler, pattern="^(switch_thread:|delete_thread:).*"),
    CommandHandler("rename_thread", rename_thread_command),
]
