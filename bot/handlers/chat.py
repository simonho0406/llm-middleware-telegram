import logging
import asyncio
import re
from telegram import Update, constants, error
from telegram.ext import MessageHandler, filters, ContextTypes
import config
from storage import storage_manager
from bot.messaging import send_safe_message
from bot.response_generator import _generate_and_send_response


logger = logging.getLogger(__name__)

STREAMING_THROTTLE_SECONDS = 3.0

def count_tokens(text: str) -> int:
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text, disallowed_special=()))
    except Exception as e:
        logger.warning(f"Token counting with tiktoken failed: {e}. Falling back to char count.")
        return len(text) // 4

def escape_meta_tags_for_markdown_attempt(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<reflect>.*?</reflect>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()

DEBOUNCE_INTERVAL = 1.0

async def process_buffered_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes the buffered messages after the debounce interval."""
    job_data = context.job.data
    chat_id = job_data['chat_id']
    user_id = job_data['user_id']
    update = job_data['update']
    
    # Cleanup job reference since it's now running
    if 'debounce_job' in context.chat_data:
        del context.chat_data['debounce_job']
    
    # Retrieve and clear the buffer
    buffer = context.chat_data.get('message_buffer', [])
    context.chat_data['message_buffer'] = []
    
    if not buffer:
        logger.warning(f"(Chat {chat_id}) Debounce fired with empty buffer, skipping.")
        return

    # Combine messages
    full_message_text = " ".join(buffer)
    log_prefix = f"(Chat {chat_id}) "
    
    logger.info(f"{log_prefix}Processing combined message (len: {len(full_message_text)}): '{full_message_text[:100]}...'")

    try:
        current_thread_id = await storage_manager.get_current_thread_id(chat_id)
        await storage_manager.set_thread_key(chat_id, 'last_user_prompt', full_message_text)
        
        await _generate_and_send_response(
            update=update,
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            prompt=full_message_text,
            current_thread_id=current_thread_id,
        )
    except error.NetworkError as e:
        logger.error(f"Network error in process_buffered_message: {e}")
        try:
            await send_safe_message(context, update, "A network error occurred, please try again.")
        except Exception as e_inner:
            logger.error(f"Failed to send network error message to user: {e_inner}")
    except Exception as e:
        logger.error(f"{log_prefix}Error in process_buffered_message: {e}", exc_info=True)
        await send_safe_message(context, update, "Sorry, a critical error occurred while handling your message.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages with debouncing."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message_text = update.message.text
    log_prefix = f"(Chat {chat_id}) "

    if not message_text:
        return

    logger.info(f"{log_prefix}Buffering message from User {user_id}: '{message_text[:50]}...'")

    # Explicitly ignore any message starting with '/' to prevent eating commands
    # that failed strict CommandHandler filtering (e.g. strict entity checks).
    if message_text.strip().startswith('/'):
        logger.info(f"{log_prefix}Ignoring message starting with '/' (detected as potential command).")
        return

    if config.get_allowed_chat_ids() and chat_id not in config.get_allowed_chat_ids():
        logger.warning(f"{log_prefix}Unauthorized chat ID. User: {user_id}.")
        return

    # Initialize buffer if needed
    if 'message_buffer' not in context.chat_data:
        context.chat_data['message_buffer'] = []
    
    # Append message to buffer
    context.chat_data['message_buffer'].append(message_text)

    # Cancel existing debounce job if any
    if 'debounce_job' in context.chat_data:
        old_job = context.chat_data['debounce_job']
        try:
            old_job.schedule_removal() # Remove the old job
        except Exception:
            # Job might already be gone or invalid, just ignore
            pass
    
    # Schedule new job
    # We use the JobQueue for robust scheduling
    context.chat_data['debounce_job'] = context.job_queue.run_once(
        process_buffered_message,
        DEBOUNCE_INTERVAL,
        data={
            'chat_id': chat_id,
            'user_id': user_id,
            'update': update # Store the update object for the callback
        },
        chat_id=chat_id # Associate job with chat
    )

async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles edited messages, treating them as new prompts if they were the last user message."""
    if not update.edited_message:
        return

    # Check if a panel discussion is active.
    if context.user_data and 'panel_state' in context.user_data:
        chat_id = update.effective_chat.id
        log_prefix = f"(Chat {chat_id}) "
        logger.info(f"{log_prefix}Delegating edit to panel handler.")
        from bot.handlers import discuss_panel_handler
        await discuss_panel_handler.handle_panel_edit(update, context)
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    edited_text = update.edited_message.text
    log_prefix = f"(Chat {chat_id}) "

    logger.info(f"{log_prefix}User {user_id} edited a message.")

    # Check if the edited message is a command
    if edited_text.startswith('/'):
        logger.info(f"{log_prefix}Edit detected as command: '{edited_text}'. Re-processing as new message.")
        # Create a shallow copy of the update but treat the edited message as a new message
        # We need to construct a new Update object to avoid modifying the original in place in a way that might confuse PTB
        # However, PTB updates are immutable-ish.
        # The cleanest way is to manually trigger the application's update processing with a modified update
        
        # Construct a new Update. We can't easily instantiate Update with all fields, 
        # but we can try to rely on the fact that handlers look at update.message.
        # A safer approach for PTB v20+ is to use the existing update but 'move' the edited_message to message.
        
        import copy
        new_update = copy.copy(update)
        new_update.message = update.edited_message
        new_update.edited_message = None
        
        # We must ensure we don't trigger an infinite loop. 
        # Since we set edited_message to None, the edited_message_handler shouldn't trigger.
        # The CommandHandler or MessageHandler should trigger.
        
        await context.application.process_update(new_update)
        return

    history = await storage_manager.get_thread_history(chat_id)

    if not history:
        logger.info(f"{log_prefix}Ignoring edit in empty history.")
        return

    # Find the last user message in the history
    last_user_message_index = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i]['role'] == 'user':
            last_user_message_index = i
            break

    # If there's no user message, we can't do anything
    if last_user_message_index == -1:
        logger.info(f"{log_prefix}Ignoring edit, no previous user message found.")
        return

    # Cancel any in-flight task for the previous prompt
    if 'llm_task' in context.chat_data and not context.chat_data['llm_task'].done():
        context.chat_data['llm_task'].cancel()
        logger.info(f"{log_prefix}Cancelled in-flight LLM task due to message edit.")

    # Remove all messages after the last user message (i.e., any assistant responses)
    history = history[:last_user_message_index + 1]

    # Update the content of the last user message
    history[last_user_message_index]['content'] = edited_text

    # Save the corrected history
    await storage_manager.set_thread_history(chat_id, history)
    logger.info(f"{log_prefix}Corrected history after edit.")

    # Trigger a new response generation
    current_thread_id = await storage_manager.get_current_thread_id(chat_id)
    await _generate_and_send_response(
        update=update,
        context=context,
        chat_id=chat_id,
        user_id=user_id,
        prompt=edited_text,
        current_thread_id=current_thread_id,
        is_reroll=True # Treat an edit like a reroll
    )

chat_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
edited_message_handler = MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edited_message)
