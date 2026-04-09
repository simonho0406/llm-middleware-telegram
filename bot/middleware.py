import logging
from telegram import Update
from telegram.ext import ContextTypes, ApplicationHandlerStop
import config

logger = logging.getLogger(__name__)

async def auth_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Middleware to check if the user/chat is allowed to interact with the bot.
    If unauthorized, logs the attempt and stops further handler execution.
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    
    if chat_id:
        allowed_chat_ids = config.get_allowed_chat_ids()
        if allowed_chat_ids and chat_id not in allowed_chat_ids:
            logger.warning(f"Unauthorized access attempt from user_id {user_id} in chat {chat_id}")
            raise ApplicationHandlerStop()
