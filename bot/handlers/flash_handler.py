import logging
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from bot.response_generator import _generate_and_send_response
from bot.messaging import send_safe_message
from storage import storage_manager

logger = logging.getLogger(__name__)

async def flash_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the /flash command for one-off, non-persisted interactions.
    Usage: /flash <text> OR reply to a message with /flash
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # 1. Determine the prompt
    prompt = ""
    if context.args:
        prompt = " ".join(context.args)
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        prompt = update.message.reply_to_message.text
    
    if not prompt.strip():
        await send_safe_message(context, update, 
            "🕵️‍♂️ **Flash Mode (Burn After Reading)**\n\n"
            "This command generates a response that is **NOT saved** to history.\n"
            "Usage:\n"
            "• `/flash <your question>`\n"
            "• Reply to a message with `/flash`"
        )
        return

    logger.info(f"(Chat {chat_id}) User {user_id} triggered /flash (skip_save=True)")

    # 2. Send a placeholder
    placeholder_message = None
    try:
        from bot.messaging import send_safe_message
        # We manually send a message and get the object to pass as placeholder
        # Note: send_safe_message handles splitting, but for short placeholders it returns the last message object usually.
        # But send_safe_message returns True/False usually in my code?
        # Let's check messaging.py. 
        # Actually send_safe_message returns bool. I need the actual message object to delete it.
        # I'll use context.bot.send_message directly for the placeholder, relying on standard behavior.
        placeholder_message = await context.bot.send_message(
            chat_id=chat_id, 
            text="🕵️‍♂️ *Running Flash Request...*", 
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception(f"Failed to send placeholder: {e}")

    # 3. Get Thread ID
    current_thread_id = await storage_manager.get_current_thread_id(chat_id)

    # 4. Generate Response with skip_save=True
    await _generate_and_send_response(
        update=update,
        context=context,
        chat_id=chat_id,
        user_id=user_id,
        prompt=prompt,
        current_thread_id=current_thread_id,
        is_reroll=False,
        force_truncate=False,
        placeholder_message=placeholder_message,
        skip_save=True,  # <--- CRITICAL
        task_key='flash_task' # Isolate from main chat cancellation
    )

flash_handler = CommandHandler("flash", flash_command)
