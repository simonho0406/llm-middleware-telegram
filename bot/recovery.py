"""
Startup take-over of turns a prior session failed to answer.

Why this exists
---------------
The harness in ``response_generator`` guarantees a *live* turn ends in an answer
or a visible error. But a turn the bot consumed and then died on mid-flight
(crash, OOM, abrupt restart) leaves the user with silence. The Telegram Bot API
cannot read chat history, and PTB only replays updates that arrived while the bot
was *offline* — it cannot re-deliver an update it already acked. The only durable
record of such a stranded turn is our own DB: a ``user`` message with no
``assistant`` message after it.

On startup we scan for that pattern and, for the most recent stranded message per
chat within a freshness window, re-drive the *same* generation pipeline so the
recovered answer is itself harness-protected.

The dangling user row is deleted first and re-saved by the pipeline, so we reuse
the normal save path (which also persists the assistant reply) without needing a
separate "skip input save but keep output save" flag.
"""
import logging
import time

import config
from storage import storage_manager

logger = logging.getLogger(__name__)


async def reconcile_unanswered_messages(app) -> int:
    """Take over the most recent unanswered user message per chat. Returns count resumed."""
    if not config.get_recovery_enabled():
        logger.info("Startup recovery disabled by config (recovery.enabled=false).")
        return 0

    # File storage can't query message-level history with PKs/timestamps.
    if getattr(storage_manager, 'get_thread_history_with_pk', None) is None:
        logger.info("Startup recovery skipped: storage backend lacks per-message history.")
        return 0

    window = config.get_recovery_window_seconds()
    now = int(time.time())

    try:
        chat_ids = await storage_manager.get_all_chat_ids()
    except Exception as e:
        logger.exception(f"Recovery: failed to list chats: {e}")
        return 0

    if not chat_ids:
        return 0

    resumed = 0
    for chat_id in chat_ids:
        try:
            if await _reconcile_one_chat(app, chat_id, now, window):
                resumed += 1
        except Exception as e:
            # Per-chat isolation: one failure must not abort recovery for the rest.
            logger.exception(f"Recovery: error reconciling chat {chat_id}: {e}")

    if resumed:
        logger.info(f"Recovery: took over {resumed} stranded message(s) across {len(chat_ids)} chat(s).")
    else:
        logger.info(f"Recovery: no stranded messages to resume across {len(chat_ids)} chat(s).")
    return resumed


async def _reconcile_one_chat(app, chat_id: int, now: int, window: int) -> bool:
    """Resume the single most-recent stranded user message for one chat, if any."""
    history = await storage_manager.get_thread_history_with_pk(chat_id, limit=1)
    if not history:
        return False

    last = history[-1]
    if last.get('role') != 'user':
        return False  # last turn was answered (or isn't a plain user message)

    content = last.get('content')
    if not content or not content.strip():
        return False

    ts = last.get('timestamp') or 0
    age = now - ts
    if age > window:
        logger.info(
            f"Recovery: chat {chat_id} has a stranded message but it's {age}s old "
            f"(> {window}s window) — leaving it alone."
        )
        return False

    logger.info(
        f"Recovery: taking over stranded message for chat {chat_id} "
        f"(age {age}s, pk {last.get('id')}): {content[:60]!r}"
    )

    # Build a headless context (no Update) bound to this chat. For a private bot,
    # the chat_id is the user_id; user_id is only used for logging/hooks.
    from telegram.ext import CallbackContext
    context = CallbackContext(application=app, chat_id=chat_id, user_id=chat_id)

    from bot.messaging import send_plain_message
    await send_plain_message(context, chat_id, "🔄 Catching up on your earlier message…")

    current_thread_id = await storage_manager.get_current_thread_id(chat_id)

    # Resume the EXISTING stranded user row in place: save_input=False keeps it
    # (no delete → no window where a crash mid-recovery could lose the user's
    # message), while the assistant reply is still generated and saved. Reuses the
    # normal harness-protected pipeline end-to-end.
    from bot.response_generator import _generate_and_send_response
    await _generate_and_send_response(
        update=None,
        context=context,
        chat_id=chat_id,
        user_id=chat_id,
        prompt=content,
        current_thread_id=current_thread_id,
        is_reroll=False,
        skip_save=False,
        save_input=False,
    )
    return True
