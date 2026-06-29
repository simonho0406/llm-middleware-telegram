import logging
import time
from telegram import Update
from telegram.ext import ContextTypes, ApplicationHandlerStop
import config

logger = logging.getLogger(__name__)

# Throttle/one-shot state for access-denial logging so a stranger flood (the bot is
# now public) can't spam the log.
_misconfig_warned = False
_last_denied_log_ts = 0.0
_DENIED_LOG_INTERVAL_S = 300


async def auth_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fail-closed access control. A chat may interact ONLY if:
      * `open_access: true` is set in config.yaml (explicit opt-in to a public bot), OR
      * its chat_id is listed in `allowed_chat_ids`.

    If NEITHER is configured, all access is denied (deny-by-default). The repo is public,
    so a fresh deploy must not be open to the world by accident — the operator has to make
    an explicit choice. Unauthorized attempts raise ApplicationHandlerStop to halt all
    downstream handlers.
    """
    global _misconfig_warned, _last_denied_log_ts

    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None

    if chat_id is None:
        return  # No chat to authorize (e.g. an inline/non-chat update).

    # Explicit opt-in to open access.
    if config.get_open_access():
        return

    allowed_chat_ids = config.get_allowed_chat_ids()

    # Deny-by-default when nothing is configured, and tell the operator how to enable (once).
    if not allowed_chat_ids:
        if not _misconfig_warned:
            _misconfig_warned = True
            logger.warning(
                "Access denied for ALL chats: no `allowed_chat_ids` configured and "
                "`open_access` is not true. Set `allowed_chat_ids` (recommended) or "
                "`open_access: true` in config.yaml to enable the bot."
            )
        raise ApplicationHandlerStop()

    if chat_id not in allowed_chat_ids:
        now = time.monotonic()
        if now - _last_denied_log_ts > _DENIED_LOG_INTERVAL_S:
            _last_denied_log_ts = now
            logger.warning(f"Unauthorized access attempt from user_id {user_id} in chat {chat_id}")
        raise ApplicationHandlerStop()
