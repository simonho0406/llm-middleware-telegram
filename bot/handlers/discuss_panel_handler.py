import logging
import asyncio
import telegram
from telegram import Update, BotCommand
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram import BotCommandScopeChat
from telegram import constants
import config
from bot.menu_setup import setup_bot_commands_and_menu
from storage import storage_manager
from bot.settings import USER_SETTINGS
from bot.messaging import send_safe_message, send_plain_message
from utils.concurrency import run_capped
from bot.handlers.panel_workflow import (
    _run_panel_workflow,
    _format_panel_summary,
    _plan_deep_dive_searches,
    _execute_panel_tool_calls,
    _run_refinement_cycle,
)

# Per-chat panel locks. Stored at module scope (not in user_data) so the Lock's
# event-loop binding stays consistent with the live loop. If PTB persistence
# were ever enabled, asyncio primitives in user_data would carry a dead loop
# binding after a polling-loop restart. The dict is repopulated lazily.
# Reset by reset_panel_locks() in cleanup_services on shutdown.
_panel_locks: dict[int, asyncio.Lock] = {}


def _get_panel_lock(chat_id: int) -> asyncio.Lock:
    """Lazily create and cache a per-chat asyncio.Lock bound to the live loop."""
    lock = _panel_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _panel_locks[chat_id] = lock
    return lock


def reset_panel_locks() -> None:
    """Discard all cached panel locks. Call on polling-loop restart so the next
    iteration creates fresh locks bound to the new event loop."""
    _panel_locks.clear()

# Define conversation states
AWAITING_FOLLOW_UP, = range(1)

logger = logging.getLogger(__name__)


async def set_panel_commands(application, chat_id: int) -> None:
    """Sets the bot's command list to panel-specific commands."""
    panel_commands = [
        BotCommand("reroll", "Rerun the last panel turn"),
        BotCommand("search", "Inject web search results into the discussion"),
        BotCommand("end_discussion", "End the current panel discussion"),
        BotCommand("configure_panel", "Configure panel models"),
        BotCommand("cancel", "Cancel the current operation"),
    ]
    try:
        await application.bot.set_my_commands(
            commands=panel_commands,
            scope=BotCommandScopeChat(chat_id)
        )
        logger.info(f"Set panel-specific commands for chat {chat_id}")
    except Exception as e:
        logger.exception(f"Failed to set panel-specific commands for chat {chat_id}: {e}")


async def _run_panel_task_background(update: Update, context: ContextTypes.DEFAULT_TYPE, user_prompt: str, assembling_msg, chat_id: int):
    """Background task wrapper for the panel workflow."""
    try:
        # Incremental Archival: Save USER prompt IMMEDIATELY to prevent orphans on crash
        pk = await storage_manager.save_message(chat_id, 'user', user_prompt)
        context.user_data['pending_panel_message_pk'] = pk

        inject_history = await storage_manager.get_user_setting(chat_id, 'inject_history_in_panel', USER_SETTINGS['inject_history_in_panel']['default'])
        initial_history = []
        if inject_history:
            initial_history = await storage_manager.get_thread_history(chat_id)

        panel_results, final_answer, proposer_response = await _run_panel_workflow(
            update, context, user_prompt, initial_history, assembling_msg, chat_id
        )
        
        # Store state
        context.user_data['panel_state'] = {
            "original_prompt": user_prompt,
            "panel_results": panel_results,
            "final_answer": final_answer,
            "full_transcript": [
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": final_answer}
            ],
            # Lock lives in the module-level _panel_locks dict (see ticket 030);
            # storing it here would carry a dead event-loop binding across a
            # polling-loop restart if user_data persistence were enabled.
        }

        await assembling_msg.delete()
        
        # AST-Based Architecture: Parse, Split, and Send
        pure_summary = _format_panel_summary(panel_results)
        pure_markdown_content = f"{pure_summary}\n\n---\n\n{final_answer}"

        # Use the centralized send_safe_message function
        if await send_safe_message(context, update, pure_markdown_content):
            # Incremental Archival: Save the panel's result
            await storage_manager.save_message(chat_id, 'assistant:panel', final_answer)
            # Clear pending PK since the interaction block is now complete and stable
            context.user_data.pop('pending_panel_message_pk', None)
        
    except asyncio.CancelledError:
        logger.warning(f"Panel workflow in background task for chat {chat_id} was cancelled.")
        await _cleanup_discussion_state(context, chat_id, assembling_msg)
    except Exception as e:
        logger.error(f"Panel workflow failed in background task: {e}", exc_info=True)
        
        error_message = f"An error occurred: {str(e)}"
        try:
            if assembling_msg:
                await assembling_msg.edit_text(error_message, parse_mode=None)
            else:
                await send_plain_message(context, chat_id, error_message)
        except Exception as send_error:
            logger.exception(f"Failed to send error message: {send_error}")
            
        await _cleanup_discussion_state(context, chat_id, assembling_msg)

async def start_panel_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the /discuss_panel command."""
    chat_id = update.effective_chat.id
    user_prompt = " ".join(context.args).strip()
    if not user_prompt:
        await update.message.reply_text("Usage: /discuss_panel <topic>", parse_mode=None)
        return ConversationHandler.END

    try:
        assembling_msg = await send_plain_message(
            context,
            chat_id,
            "Assembling an expert panel..."
        )
    except telegram.error.NetworkError as e:
        logger.error(f"Network error while sending initial message in start_panel_discussion: {e}")
        try:
            await update.message.reply_text("A network error occurred, please try again.", parse_mode=None)
        except Exception as e_inner:
            logger.exception(f"Failed to send network error message to user: {e_inner}")
        return ConversationHandler.END
    
    await set_panel_commands(context.application, chat_id)

    # Create and store task, but DO NOT await it.
    # run_capped: a panel holds one global generation permit for its duration so
    # concurrent panels/chats can't OOM a small VM. Panels don't nest into chat tasks
    # (or vice versa), so no deadlock.
    panel_task = asyncio.create_task(
        run_capped(_run_panel_task_background(update, context, user_prompt, assembling_msg, chat_id))
    )
    context.user_data['panel_task'] = panel_task
    
    # Return immediately to allow ConversationHandler to enter state
    return AWAITING_FOLLOW_UP

async def handle_follow_up(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles a follow-up question from the user."""
    chat_id = update.effective_chat.id
    follow_up_prompt = update.message.text
    logger.info(f"[{chat_id}] Handling follow-up: '{follow_up_prompt}'")

    panel_state = context.user_data.get('panel_state')
    if not panel_state:
        await update.message.reply_text("Error: Discussion context was lost. Please start a new discussion with /discuss_panel.", parse_mode=None)
        return ConversationHandler.END

    async with _get_panel_lock(chat_id):
        placeholder = await update.message.reply_text("Panel is reconvening...", parse_mode=None)

        try:
            panel_task = asyncio.create_task(
                _run_panel_workflow(
                    update, 
                    context, 
                    follow_up_prompt, 
                    panel_state['full_transcript'],
                    placeholder,
                    chat_id
                )
            )
            # Incremental Archival: Save USER prompt IMMEDIATELY
            pk = await storage_manager.save_message(chat_id, 'user', follow_up_prompt)
            context.user_data['pending_panel_message_pk'] = pk

            context.user_data['panel_task'] = panel_task
            new_panel_results, new_final_answer, new_proposer_response = await panel_task
        except asyncio.CancelledError:
            logger.warning(f"Panel workflow in handle_follow_up for chat {chat_id} was cancelled.")
            await _cleanup_discussion_state(context, chat_id, placeholder)
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Panel workflow failed in handle_follow_up: {e}", exc_info=True)
            try:
                if placeholder:
                    await asyncio.wait_for(
                        placeholder.edit_text(f"An error occurred: {str(e)}", parse_mode=None),
                        timeout=8.0
                    )
            except (asyncio.TimeoutError, telegram.error.TimedOut) as timeout_e:
                logger.warning(f"Timeout editing error message in follow_up: {timeout_e}")
            except Exception as edit_e:
                logger.exception(f"Failed to edit error message in follow_up: {edit_e}")
            await _cleanup_discussion_state(context, chat_id, placeholder)
            return ConversationHandler.END

        panel_state['full_transcript'].append({"role": "user", "content": follow_up_prompt})
        panel_state['full_transcript'].append({"role": "assistant", "content": new_final_answer})
        panel_state['panel_results'] = new_panel_results
        panel_state['final_answer'] = new_final_answer

        await placeholder.delete()
        
        # AST-Based Architecture: Parse, Split, and Send
        pure_summary = _format_panel_summary(new_panel_results)
        pure_markdown_content = f"{pure_summary}\n\n---\n\n{new_final_answer}"
        if await send_safe_message(context, update, pure_markdown_content):
            # Incremental Archival: Save the panel's result immediately
            await storage_manager.save_message(chat_id, 'assistant:panel', new_final_answer)
            # Clear pending PK since the interaction block is now complete and stable
            context.user_data.pop('pending_panel_message_pk', None)

    return AWAITING_FOLLOW_UP

async def _cleanup_discussion_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, placeholder_msg=None) -> None:
    """Safely cancels any running panel task, clears user_data, and resets commands.
    
    Args:
        context: The callback context
        chat_id: The chat ID
        placeholder_msg: Optional message object to update with cancellation status
    """
    panel_task = context.user_data.get('panel_task')

    # Try to find the placeholder in user_data if not explicitly provided
    if not placeholder_msg:
        placeholder_msg = context.user_data.get('panel_placeholder')

    if panel_task and not panel_task.done():
        panel_task.cancel()
        logger.info(f"Cancelled in-flight panel task for chat {chat_id}.")
        
        # Update placeholder message if provided
        if placeholder_msg:
            try:
                await placeholder_msg.edit_text("Discussion cancelled.", parse_mode=None)
            except telegram.error.TelegramError as e:
                logger.warning(f"Could not update placeholder message during cleanup: {e}")
            except Exception as e:
                logger.exception(f"Unexpected error updating placeholder during cleanup: {e}")
        
        try:
            # Await the task to allow it to process the cancellation
            try:
                await panel_task
            except asyncio.CancelledError:
                logger.info(f"Panel task for chat {chat_id} was already cancelled.")
            except Exception as e:
                logger.exception(f"Error awaiting cancelled panel task for chat {chat_id}: {e}")
        except asyncio.CancelledError:
            logger.info(f"Panel task for chat {chat_id} successfully processed cancellation.")
        except Exception as e:
            logger.exception(f"Error awaiting cancelled panel task for chat {chat_id}: {e}")

    context.user_data.pop('panel_task', None)
    context.user_data.pop('panel_state', None)
    context.user_data.pop('panel_placeholder', None)  # Clear the placeholder reference
    
    # Surgical cleanup of orphaned user prompt preventing data loss history wipes
    pending_pk = context.user_data.pop('pending_panel_message_pk', None)
    if pending_pk is not None:
        await storage_manager.delete_messages(chat_id, [pending_pk])
        logger.info(f"Cleaned up orphaned panel prompt PK {pending_pk} due to cancellation in chat {chat_id}.")
    
    # Reset the command menu back to the default
    await setup_bot_commands_and_menu(context.application, chat_id)
    logger.info(f"Cleaned up panel state and reset commands for chat {chat_id}.")

async def end_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End the panel discussion, save its final answer, and clear context."""
    chat_id = update.effective_chat.id
    panel_state = context.user_data.get('panel_state')

    if panel_state:
        # Lock is not needed here as the conversation is ending, no race conditions.
        # final_answer = panel_state.get("final_answer", "No final answer was recorded.")
        # We no longer save here to avoid duplication, as it's saved incrementally now.
        # await storage_manager.save_message(chat_id, 'assistant:panel', final_answer)
        await update.message.reply_text("✅ Panel discussion concluded.", parse_mode=None)
        await _cleanup_discussion_state(context, chat_id)
    else:
        await update.message.reply_text("⚠️ No active discussion to end.", parse_mode=None)

    return ConversationHandler.END



async def search_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /search command within a panel discussion."""
    chat_id = update.effective_chat.id
    logger.info(f"[{chat_id}] User triggered /search within a discussion.")

    if not context.args:
        await update.message.reply_text("Please provide a query to search. Usage: /search <your query>", parse_mode=None)
        return AWAITING_FOLLOW_UP

    query = " ".join(context.args)
    placeholder_msg = await update.message.reply_text(f"Searching the web for: \"{query}\"...", parse_mode=None)

    search_results = await web_search_service.perform_search(query, manual=True)

    if search_results.get('status') == 'error':
        error_msg = search_results.get('message', 'Unknown error occurred.')
        await placeholder_msg.edit_text(f"⚠️ Search error: {error_msg}", parse_mode=None)
        return AWAITING_FOLLOW_UP

    search_content = search_results.get('content', '')
    panel_state = context.user_data.get('panel_state')
    if panel_state and panel_state.get('full_transcript'):
        async with _get_panel_lock(chat_id):
            panel_state['full_transcript'].append({'role': 'user', 'content': f"Search results for '{query}':\n{search_content}"})
            await placeholder_msg.edit_text("✅ Search results have been added to the discussion context." , parse_mode=None)
    else:
        await placeholder_msg.edit_text("⚠️ Could not find an active discussion to add search results to.", parse_mode=None)

    return AWAITING_FOLLOW_UP

async def handle_panel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles edited messages during a panel discussion.
    Cancels current run, updates transcript, and restarts workflow.
    """
    chat_id = update.effective_chat.id
    edited_text = update.edited_message.text
    message_id = update.edited_message.message_id
    
    panel_state = context.user_data.get('panel_state')
    if not panel_state:
        return

    logger.info(f"(Chat {chat_id}) Handling panel edit for message {message_id}")

    # 1. Cancel any running task
    panel_task = context.user_data.get('panel_task')
    if panel_task and not panel_task.done():
        panel_task.cancel()
        logger.info(f"(Chat {chat_id}) Cancelled active panel task due to edit.")
        try:
            await panel_task
        except asyncio.CancelledError:
            pass
    
    # 2. Update Transcript
    full_transcript = panel_state.get('full_transcript', [])
    
    # Find the message in the transcript
    # We assume the transcript stores message_ids if possible, or we rely on position?
    # The current implementation of `_run_panel_workflow` appends to `full_transcript`.
    # But `full_transcript` in `panel_state` is a list of dicts.
    # We need to find the user message that matches.
    # If we can't find by ID (because we might not be storing it), we assume it's the LAST user message?
    # Let's assume it's the last user message for now, as that's the most common edit case.
    
    target_index = -1
    for i in range(len(full_transcript) - 1, -1, -1):
        if full_transcript[i]['role'] == 'user':
            # If we stored message_id, check it. If not, assume last user msg.
            # The current `full_transcript` structure is just {'role':..., 'content':...}
            # So we assume last user message.
            target_index = i
            break
            
    if target_index == -1:
        logger.warning(f"(Chat {chat_id}) Could not find user message to edit in panel transcript.")
        return

    # Update content under the panel lock so handle_follow_up can't race us.
    async with _get_panel_lock(chat_id):
        full_transcript[target_index]['content'] = edited_text
        panel_state['full_transcript'] = full_transcript[:target_index + 1]

    logger.info(f"(Chat {chat_id}) Updated panel transcript and truncated history.")
    
    # 3. Restart Workflow
    placeholder_msg = context.user_data.get('panel_placeholder')
    if not placeholder_msg:
         # If no placeholder, send a new one
         placeholder_msg = await send_safe_message(context, update, "🔄 Restarting panel due to edit...")
         context.user_data['panel_placeholder'] = placeholder_msg
    else:
        try:
            await placeholder_msg.edit_text("🔄 Restarting panel due to edit...", parse_mode=None)
        except Exception:
             placeholder_msg = await send_safe_message(context, update, "🔄 Restarting panel due to edit...")
             context.user_data['panel_placeholder'] = placeholder_msg

    # Re-run the workflow
    # We need to wrap it in a task like in `start_panel_discussion` or `handle_follow_up`
    # But `handle_follow_up` logic is complex.
    # We can reuse `_run_panel_workflow` but we need to handle the result (save to history etc).
    # Actually, `reroll_discussion` does exactly this: calls `_run_panel_workflow` and handles result.
    # But `reroll_discussion` expects to be called as a command.
    # We can extract the "run and handle result" logic or just call `_run_panel_workflow` 
    # and duplicate the result handling (which is short).
    
    # Let's duplicate the result handling for safety and clarity, similar to `reroll_discussion`
    
    async def _run_and_handle():
        try:
            panel_results, final_answer, proposer_response = await _run_panel_workflow(
                update, context, edited_text, panel_state['full_transcript'], placeholder_msg, chat_id
            )
            
            # Update transcript with final answer
            panel_state['full_transcript'].append({'role': 'assistant', 'content': final_answer})
            
            # Send final answer
            await send_safe_message(context, update, final_answer)
            
            # Update placeholder with summary
            summary = _format_panel_summary(panel_results)
            try:
                await placeholder_msg.edit_text(summary, parse_mode=constants.ParseMode.MARKDOWN_V2)
            except Exception as e:
                logger.exception(f"Failed to update summary: {e}")
                
        except asyncio.CancelledError:
            logger.info("Panel task cancelled.")
            raise
        except Exception as e:
            logger.error(f"Error in panel edit workflow: {e}", exc_info=True)
            await send_safe_message(context, update, "An error occurred during the panel discussion.")

    # Create and store task
    task = asyncio.create_task(_run_and_handle())
    context.user_data['panel_task'] = task
    
    # Ensure we await the task if we are in a test environment? 
    # No, in production it runs in background.
    # But for the test to pass, we might need to await it?
    # The test mocks `_run_panel_workflow` so it finishes instantly.

async def reroll_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /reroll command within a panel discussion."""
    chat_id = update.effective_chat.id
    logger.info(f"[{chat_id}] User triggered /reroll within a discussion.")

    panel_state = context.user_data.get('panel_state')
    if not panel_state or not panel_state.get('full_transcript'):
        await update.message.reply_text("⚠️ No discussion history found to reroll. Please start a new one.", parse_mode=None)
        return AWAITING_FOLLOW_UP

    async with _get_panel_lock(chat_id):
        last_user_prompt = next((msg['content'] for msg in reversed(panel_state['full_transcript']) if msg['role'] == 'user'), panel_state.get('original_prompt'))

        if not last_user_prompt:
            await update.message.reply_text("⚠️ Could not find the last user prompt to reroll.", parse_mode=None)
            return AWAITING_FOLLOW_UP

        placeholder_msg = await update.message.reply_text(f'Re-running panel for: \"{last_user_prompt[:50]}...\"', parse_mode=None)

        history_for_reroll = list(panel_state['full_transcript'])

        if history_for_reroll and history_for_reroll[-1]['role'] == 'assistant':
            history_for_reroll.pop()
            # Also remove from database to prevent duplication, matching standard reroll behavior
            try:
                await storage_manager.remove_last_assistant_message(chat_id)
                logger.info(f"[{chat_id}] Removed last assistant message from DB for panel reroll.")
            except Exception as e:
                logger.exception(f"Failed to remove last assistant message during panel reroll: {e}")

        try:
            panel_task = asyncio.create_task(
                _run_panel_workflow(update, context, last_user_prompt, history_for_reroll, placeholder_msg, chat_id)
            )
            context.user_data['panel_task'] = panel_task
            panel_results, final_answer, proposer_response = await panel_task
        except asyncio.CancelledError:
            logger.warning(f"Panel workflow in reroll_discussion for chat {chat_id} was cancelled.")
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Panel workflow failed during reroll: {e}", exc_info=True)
            # Escape the error message for safe display in MarkdownV2
            escaped_error = telegram.helpers.escape_markdown(str(e), version=2)
            error_message = f"An error occurred during the reroll: `{escaped_error}`"
            try:
                if placeholder_msg:
                    # Use edit_text for an existing message
                    await placeholder_msg.edit_text(error_message, parse_mode=None)
                else:
                    # Fallback to send_plain_message if placeholder doesn't exist
                    await send_plain_message(context, chat_id, error_message)
            except Exception as send_e:
                logger.exception(f"Failed to send error message to user after reroll failure: {send_e}")
            
            # Do not end the conversation on reroll error, allow user to retry
            return AWAITING_FOLLOW_UP

        if panel_state['full_transcript'] and panel_state['full_transcript'][-1]['role'] == 'assistant':
            panel_state['full_transcript'].pop()
        
        panel_state['full_transcript'].append({"role": "assistant", "content": final_answer})
        panel_state['panel_results'] = panel_results
        panel_state['final_answer'] = final_answer

        await placeholder_msg.delete()

        # Step 1: Generate pure markdown content
        pure_summary = _format_panel_summary(panel_results)
        pure_markdown_content = f"{pure_summary}\n\n---\n\n{final_answer}"

        # Use the centralized send_safe_message function
        if await send_safe_message(context, update, pure_markdown_content):
             # Incremental Archival: Save the panel's result immediately
            await storage_manager.save_message(chat_id, 'assistant:panel', final_answer)

    return AWAITING_FOLLOW_UP

async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles conversation timeout."""
    chat_id = context.job.chat_id
    logger.info(f"Panel discussion timed out for chat {chat_id}.")
    if 'panel_state' in context.user_data:
        # We no longer save here to avoid duplication. The last message was already saved.
        # panel_state = context.user_data['panel_state']
        # final_answer = panel_state.get("final_answer")
        # if final_answer:
        #     try:
        #         await storage_manager.save_message(chat_id, 'assistant:panel', final_answer)
        #         logger.info(f"Saved final answer for timed-out panel in chat {chat_id}")
        #     except Exception as e:
        #         logger.error(f"Failed to save final answer during timeout for chat {chat_id}: {e}")

        await send_plain_message(context, chat_id, "Panel discussion has timed out due to inactivity.")
        await _cleanup_discussion_state(context, chat_id)
    return ConversationHandler.END

async def blocked_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles commands that are disabled during panel discussions."""
    command = update.message.text.split()[0] if update.message.text else "command"
    await update.message.reply_text(
        f"⚠️ The {command} command is disabled during a panel discussion. "
        f"Please use /end_discussion first, or continue with your follow-up question.",
        parse_mode=None
    )
    return AWAITING_FOLLOW_UP

async def panel_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current panel discussion and ends the conversation."""
    chat_id = update.effective_chat.id
    logger.info(f"(Chat {chat_id}) User cancelled panel discussion.")
    
    # Cancel any running panel task
    panel_task = context.user_data.get('panel_task')
    if panel_task and not panel_task.done():
        panel_task.cancel()
        logger.info(f"(Chat {chat_id}) Cancelled active panel task.")
    
    await send_safe_message(context, update, "Panel discussion cancelled.")
    await _cleanup_discussion_state(context, chat_id)
    return ConversationHandler.END

async def resume_panel_discussion_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for resuming a panel from a context history block."""
    query = update.callback_query
    await query.answer()

    # Format: ctx_pnl_<page>_<start_pk>
    try:
        data = query.data
        _, _, page_str, start_pk_str = data.split("_")
        start_pk = int(start_pk_str)
    except ValueError:
        await query.edit_message_text("Invalid panel resumption data.")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    raw_history = await storage_manager.get_thread_history_with_pk(chat_id, limit=200)

    user_prompt = ""
    assistant_contents = []
    found_start = False

    for msg in raw_history:
        if msg['id'] == start_pk:
            found_start = True
            if msg['role'] == 'user':
                user_prompt = msg['content']
            elif msg.get('content'):  # skip tool-call turns (content=None)
                assistant_contents.append(msg['content'])
            continue

        if found_start:
            if msg['role'] == 'user':
                break
            if msg.get('content'):  # skip tool-call turns (content=None)
                assistant_contents.append(msg['content'])

    if not user_prompt and not assistant_contents:
        await query.edit_message_text("Could not find the interaction in history.", parse_mode=None)
        return ConversationHandler.END

    final_answer = "\n\n".join(assistant_contents) if assistant_contents else "(No previous AI response)"

    context.user_data['panel_state'] = {
        "original_prompt": user_prompt,
        "panel_results": {},
        "final_answer": final_answer,
        "full_transcript": [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": final_answer}
        ],
        "lock": asyncio.Lock()
    }

    await set_panel_commands(context.application, chat_id)
    await query.edit_message_text(
        f"🏛️ **Panel Session Resumed**\n\n_Original User Prompt:_\n{user_prompt}\n\nPlease type your follow-up prompt to resume discussion:",
        parse_mode="Markdown"
    )

    return AWAITING_FOLLOW_UP

discuss_panel_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler('discuss_panel', start_panel_discussion),
        CallbackQueryHandler(resume_panel_discussion_entry, pattern="^ctx_pnl_")
    ],
    states={
        AWAITING_FOLLOW_UP: [
            CommandHandler('reroll', reroll_discussion),
            CommandHandler('search', search_discussion),
            # Block common commands that shouldn't work during panel discussions
            CommandHandler(['config', 'set_model', 'set_ollama_model', 'set_gemini_model', 'providers', 'models'], blocked_command_handler),
            CommandHandler(['ask_all_gemini', 'discuss', 'help'], blocked_command_handler),
            MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_panel_edit),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_follow_up),
        ],
    },
    fallbacks=[CommandHandler('end_discussion', end_discussion), CommandHandler('cancel', panel_cancel_command), CommandHandler('timeout', timeout_handler)],
    per_user=True,
    per_chat=True,
    block=True,
    per_message=False,
    allow_reentry=True
)