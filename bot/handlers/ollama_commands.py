import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram import MessageEntity
from telegram.ext import CommandHandler, ContextTypes
from telegram.helpers import escape_markdown

from services import ollama_service
from storage import storage_manager
import config

logger = logging.getLogger(__name__)

# --- Command Handlers ---


# --- Handler Exports ---
from telegram.ext import CallbackQueryHandler

ollama_handlers = []
