import logging
import asyncio
import hashlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)
from telegram.error import BadRequest
from telegram.helpers import escape_markdown
from bot.providers import get_available_provider_names, get_service_for_provider
from utils.text_processing import parse_markdown_to_ast, split_document_ast_aware, render_ast_to_telegram_v2
from bot.messaging import send_safe_message
from storage import storage_manager

logger = logging.getLogger(__name__)

# --- Conversation States ---
SELECT_MODELS = 0

# --- Constants ---
MODELS_PER_PAGE = 8

# --- Callback Data Prefixes ---
CALLBACK_MODEL_SELECT_PREFIX = "discuss_mod_select_"
CALLBACK_MODEL_PAGE_PREFIX = "discuss_mod_page_"
DONE_SELECTING_MODELS = "discuss_done"
CANCEL_DISCUSSION = "discuss_cancel"

async def get_all_models():
    """Fetches and consolidates models from all available providers."""
    all_models = []
    provider_names = get_available_provider_names()
    tasks = [get_service_for_provider(p).list_models() for p in provider_names]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for provider_name, result in zip(provider_names, results):
        if isinstance(result, Exception):
            logger.error(f"Failed to fetch models for provider '{provider_name}': {result}")
            continue
        for model_info in result:
            model_id = model_info if isinstance(model_info, str) else model_info.get('id')
            model_name = model_info if isinstance(model_info, str) else model_info.get('name', model_id)
            all_models.append({
                'provider': provider_name,
                'id': model_id,
                'name': f"[{provider_name}] {model_name}"
            })
    return sorted(all_models, key=lambda x: x['name'].lower())

def build_model_selection_keyboard(context: ContextTypes.DEFAULT_TYPE):
    """Builds the paginated keyboard for multi-provider model selection."""
    discussion_data = context.user_data.get('discussion_data', {})
    available_models = discussion_data.get('available_models', [])
    selected_models = discussion_data.get('selected_models', [])
    page = discussion_data.get('current_page', 1)

    context.user_data.setdefault('model_metadata', {})

    total_models = len(available_models)
    start_idx = (page - 1) * MODELS_PER_PAGE
    end_idx = start_idx + MODELS_PER_PAGE
    page_models = available_models[start_idx:end_idx]

    buttons = []
    for model in page_models:
        unique_key = f"{model['provider']}_{model['id']}".encode()
        model_hash = hashlib.sha256(unique_key).hexdigest()[:12]
        context.user_data['model_metadata'][model_hash] = model

        prefix = ""
        # Find the model in selected_models by provider and id
        for i, selected in enumerate(selected_models):
            if selected['provider'] == model['provider'] and selected['id'] == model['id']:
                prefix = f"✅ {i+1}. "
                break
        
        buttons.append([InlineKeyboardButton(
            f"{prefix}{model['name']}",
            callback_data=f"{CALLBACK_MODEL_SELECT_PREFIX}{model_hash}"
        )])

    pagination_row = []
    if page > 1:
        pagination_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{CALLBACK_MODEL_PAGE_PREFIX}{page-1}"))
    if end_idx < total_models:
        pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"{CALLBACK_MODEL_PAGE_PREFIX}{page+1}"))
    if pagination_row:
        buttons.append(pagination_row)

    nav_row = []
    if len(selected_models) >= 2:
        nav_row.append(InlineKeyboardButton("✅ Done", callback_data=DONE_SELECTING_MODELS))
    nav_row.append(InlineKeyboardButton("❌ Cancel", callback_data=CANCEL_DISCUSSION))
    buttons.append(nav_row)

    return InlineKeyboardMarkup(buttons)

async def start_discussion_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the /discuss command."""
    chat_id = update.effective_chat.id
    logger.debug(f"[{chat_id}] Entering /discuss conversation, state: SELECT_MODELS")

    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await send_safe_message(context, update, "Please provide a prompt. Usage: /discuss <your prompt>")
        return ConversationHandler.END

    placeholder = await send_safe_message(context, update, "Fetching all available models...")
    
    all_models = await get_all_models()
    if not all_models:
        await send_safe_message(context, update, "Could not find any available models from any provider.", is_edit=True)
        return ConversationHandler.END

    context.user_data['discussion_data'] = {
        'user_prompt': prompt,
        'selected_models': [],
        'available_models': all_models,
        'current_page': 1,
    }
    context.user_data['model_metadata'] = {}

    keyboard = build_model_selection_keyboard(context)
    total_pages = (len(all_models) + MODELS_PER_PAGE - 1) // MODELS_PER_PAGE
    message_text = f"Select at least 2 models to join the discussion (Page 1/{total_pages}):"
    await send_safe_message(context, update, message_text, reply_markup=keyboard, is_edit=True)

    return SELECT_MODELS

async def model_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles model list pagination."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    
    try:
        page_str = query.data[len(CALLBACK_MODEL_PAGE_PREFIX):]
        page = int(page_str)
    except (ValueError, IndexError):
        logger.warning(f"[{chat_id}] Invalid pagination callback data: {query.data}")
        return SELECT_MODELS

    logger.debug(f"[{chat_id}] Paginating models to page {page}, state: SELECT_MODELS")
    context.user_data['discussion_data']['current_page'] = page

    keyboard = build_model_selection_keyboard(context)
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
    model_hash = query.data[len(CALLBACK_MODEL_SELECT_PREFIX):]

    model_info = context.user_data.get('model_metadata', {}).get(model_hash)
    if not model_info:
        logger.warning(f"[{chat_id}] Stale model hash received: {model_hash}. Ignoring.")
        await query.answer("Model selection has expired. Please try again.", show_alert=True)
        return SELECT_MODELS

    logger.debug(f"[{chat_id}] Toggling model selection for '{model_info['name']}', state: SELECT_MODELS")
    discussion_data = context.user_data['discussion_data']
    selected_models = discussion_data['selected_models']

    # Check if model is already selected
    is_selected = any(m['provider'] == model_info['provider'] and m['id'] == model_info['id'] for m in selected_models)

    if is_selected:
        selected_models[:] = [m for m in selected_models if not (m['provider'] == model_info['provider'] and m['id'] == model_info['id'])]
    else:
        selected_models.append(model_info)

    keyboard = build_model_selection_keyboard(context)
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"[{chat_id}] Error editing model selection keyboard: {e}")

    return SELECT_MODELS

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
        selected_models = discussion_data['selected_models']

        if len(selected_models) < 2:
            await send_safe_message(context, update, "Please select at least 2 models to begin.", is_edit=True)
            return SELECT_MODELS

        placeholder = await send_safe_message(context, update, "Starting discussion...", is_edit=True)

        # Fetch context history (Archival: Read)
        # We limit to 500 to provide context without blowing up the window immediately
        try:
            context_history = await storage_manager.get_thread_history(chat_id, limit=500)
        except Exception as e:
            logger.exception(f"{log_prefix} Failed to fetch thread history: {e}")
            context_history = []

        discussion_transcript = [{"role": "user", "content": discussion_data['user_prompt']}]
        # Initialize main transcript with user prompt
        
        for i, model_info in enumerate(selected_models):
            model_id = model_info['id']
            provider_name = model_info['provider']
            service = get_service_for_provider(provider_name)

            if not service:
                logger.error(f"{log_prefix} Could not find service for provider '{provider_name}'. Skipping turn.")
                discussion_transcript.append({"role": "assistant", "content": f"Error: Could not find service for provider '{provider_name}'."})
                continue

            turn_info = f"Turn {i+1}/{len(selected_models)}: `{escape_markdown(model_info['name'], version=2)}` is thinking..."
            await send_safe_message(context, update, turn_info, is_edit=True)

            # Create temporary history copy with FULL CONTEXT
            # Context History (Previous Chat) + Current Discussion So Far
            history_for_call = context_history + discussion_transcript.copy()
            
            # Add critique prompt to temporary history if needed
            if i > 0:
                prev_response = discussion_transcript[-1]['content']
                critique_prompt = (
                    "Critique and improve the following response. Focus on accuracy, clarity, and depth. "
                    "Provide a revised version that addresses any shortcomings.\n\n"
                    f"Previous response:\n\n{prev_response}"
                )
                history_for_call.append({"role": "user", "content": critique_prompt})
            
            try:
                response = ""
                async for chunk in service.generate_response(
                    context_history=history_for_call,  # Use FULL history
                    prompt="",  # Empty prompt since instruction is in history
                    model=model_id
                ):
                    response += chunk
                msg = {'content': response.strip()}
            except Exception as e:
                error_msg = f"Error generating response from {provider_name}/{model_id}: {e}"
                logger.exception(f"{log_prefix} {error_msg}")
                msg = {'content': f"⚠️ {error_msg}"}
            
            # Add only model response to main transcript
            discussion_transcript.append({"role": "assistant", "content": msg['content']})

        # Build final transcript from main discussion_transcript
        final_transcript_parts = [f"""*Original Query:*
{discussion_data['user_prompt']}"""]
        for i, entry in enumerate(discussion_transcript[1:]):  # Skip initial user prompt
            model_name = selected_models[i]['name']
            separator = "\n\n---\n"
            model_header = f"*Turn {i+1}: `{model_name}`*\n"
            content_body = entry['content']
            final_transcript_parts.append(separator + model_header + content_body)

        final_transcript = "".join(final_transcript_parts)

        # AST-Based Architecture: Parse, Split, and Send
        try:
            # Step 1: Parse Markdown to AST
            document = parse_markdown_to_ast(final_transcript)
            
            # Step 2: Split AST into logical chunks
            ast_chunks = split_document_ast_aware(document)
            
            # Step 3: Send each chunk with advanced splitting and error handling
            for i, chunk_doc in enumerate(ast_chunks):
                # Render AST chunk to MarkdownV2
                telegram_safe_text = render_ast_to_telegram_v2(chunk_doc)

                if not telegram_safe_text.strip():
                    continue

                await send_safe_message(context, update, telegram_safe_text, is_edit=(i==0))

            # --- Archival: Save to DB ---
            try:
                # Save User Prompt
                await storage_manager.save_message(chat_id, 'user', discussion_data['user_prompt'])
                # Save Assistant Response (Full Transcript)
                await storage_manager.save_message(chat_id, 'assistant', final_transcript)
                logger.info(f"{log_prefix} Archived /discuss interaction.")
            except Exception as e:
                logger.exception(f"{log_prefix} Failed to archive /discuss interaction: {e}")

        except Exception as ast_error:
            logger.exception(f"AST processing failed: {ast_error}. Using emergency fallback.")
            # Emergency: Send as single plain text message with length truncation
            await send_safe_message(context, update, final_transcript)
            
            # Attempt to archive even on render error
            try:
                await storage_manager.save_message(chat_id, 'user', discussion_data['user_prompt'])
                await storage_manager.save_message(chat_id, 'assistant', final_transcript)
            except Exception as e:
                logger.exception(f"Failed to save emergency backup message: {e}")
                pass

    except Exception as e:
        logger.error(f"{log_prefix} Critical failure in run_discussion: {e}", exc_info=True)
        if placeholder:
            await send_safe_message(context, update, "A critical error occurred during the discussion. The process has been stopped.", is_edit=True)
    finally:
        context.user_data.pop('discussion_data', None)
        context.user_data.pop('model_metadata', None)
        logger.debug(f"{log_prefix} Concluding /discuss conversation.")
        return ConversationHandler.END

async def cancel_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the discussion conversation."""
    query = update.callback_query
    if query:
        await query.answer()
        await send_safe_message(context, update, "Discussion canceled.", is_edit=True)
    else:
        await send_safe_message(context, update, "Discussion canceled.")
        
    context.user_data.pop('discussion_data', None)
    context.user_data.pop('model_metadata', None)
    return ConversationHandler.END

discuss_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("discuss", start_discussion_command)],
    states={
        SELECT_MODELS: [
            CallbackQueryHandler(select_model_callback, pattern=f"^{CALLBACK_MODEL_SELECT_PREFIX}"),
            CallbackQueryHandler(model_page_callback, pattern=f"^{CALLBACK_MODEL_PAGE_PREFIX}"),
            CallbackQueryHandler(run_discussion, pattern=f"^{DONE_SELECTING_MODELS}$"),
            CallbackQueryHandler(cancel_discussion, pattern=f"^{CANCEL_DISCUSSION}$")
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_discussion), CallbackQueryHandler(cancel_discussion, pattern=f"^{CANCEL_DISCUSSION}$ ")],
    per_user=True,
    per_chat=True,
    per_message=False
)
