import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.helpers import escape_markdown

import config
from services import openrouter_service # Import the service
from storage import file_storage

logger = logging.getLogger(__name__)

# Constants for Callback Data Prefix
CALLBACK_PREFIX_SELECT_MODEL = "or_select_"


# --- Handler Exports ---
openrouter_handlers = []
