"""
Chat Handlers Module
Handles text messages and user interactions for the bot.
"""
# pylint: disable=logging-fstring-interpolation, line-too-long, broad-exception-caught, unused-import

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
    
    logger.info(f"{log_prefix}Processing combined message (len: {len(full_message_text)}).")
    logger.debug(f"{log_prefix}Combined message content: '{full_message_text[:100]}...'")

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
            logger.exception(f"Failed to send network error message to user: {e_inner}")
    except error.TelegramError as e:
        logger.error(f"{log_prefix}Telegram API error in process_buffered_message: {e}", exc_info=True)
        await send_safe_message(context, update, "Sorry, a Telegram API error occurred while handling your message.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages with debouncing."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message_text = update.message.text
    log_prefix = f"(Chat {chat_id}) "

    if not message_text:
        return

    logger.info(f"{log_prefix}Buffering message from User {user_id} (len: {len(message_text)}).")
    logger.debug(f"{log_prefix}Buffered message content: '{message_text[:50]}...'")

    # Explicitly ignore any message starting with '/' to prevent eating commands
    # that failed strict CommandHandler filtering (e.g. strict entity checks).
    if message_text.strip().startswith('/'):
        logger.info(f"{log_prefix}Ignoring message starting with '/' (detected as potential command).")
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

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    edited_text = update.edited_message.text
    log_prefix = f"(Chat {chat_id}) "

    logger.info(f"{log_prefix}User {user_id} edited a message.")

    # Check if the edited message is a command
    if edited_text.startswith('/'):
        # NOTE: previously we built a synthetic Update via copy.copy() and called
        # application.process_update(new_update) — that re-entered the entire
        # handler dispatch (auth middleware, conversation handlers) from inside
        # an already-running handler task. PTB counts both the original AND the
        # synthetic dispatch toward the concurrent_updates semaphore, which can
        # deadlock under pressure. Re-entry also bypasses PTB's normal update-
        # queue serialization, allowing two ConversationHandler entries to race
        # for the same chat (e.g. /discuss_panel edited mid-discussion would
        # spawn a second panel task, leaving a zombie with the same MCP refs).
        # The safe behavior is to inform the user instead of re-dispatching.
        logger.info(f"{log_prefix}Edit detected as command '{edited_text}'. Asking user to resend rather than re-dispatching.")
        try:
            await update.edited_message.reply_text(
                "I can't re-run a command from an edit. Please send the command as a new message.",
                parse_mode=None,
            )
        except Exception as e:
            logger.warning(f"{log_prefix}Failed to notify user about edited command: {e}")
        return

    history = await storage_manager.get_thread_history_with_pk(chat_id)

    if not history:
        logger.info(f"{log_prefix}Ignoring edit in empty history.")
        return

    # Find the last user message in the history
    last_user_message = None
    for i in range(len(history) - 1, -1, -1):
        if history[i]['role'] == 'user':
            last_user_message = history[i]
            break

    # If there's no user message, we can't do anything
    if not last_user_message:
        logger.info(f"{log_prefix}Ignoring edit, no previous user message found.")
        return

    # Cancel any in-flight task for the previous prompt. This is a deliberate
    # supersede (the edit will regenerate), so flag it expected — the cancelled
    # task's harness wrapper must not show a spurious "interrupted" notice.
    if 'llm_task' in context.chat_data and not context.chat_data['llm_task'].done():
        _superseded = context.chat_data['llm_task']
        try:
            _superseded._expected_cancel = True  # type: ignore[attr-defined]
        except AttributeError:
            pass
        _superseded.cancel()
        logger.info(f"{log_prefix}Cancelled in-flight LLM task due to message edit.")

    target_pk = last_user_message['id']

    # Update the content of the last user message atomically
    await storage_manager.update_message_content(target_pk, edited_text)

    # Remove all messages after the last user message (i.e., any assistant responses) atomically
    await storage_manager.delete_messages_after(chat_id, target_pk)
    logger.info(f"{log_prefix}Corrected history atomically after edit (User PK: {target_pk}).")

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
