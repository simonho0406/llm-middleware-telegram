"""Global concurrency cap for heavy LLM work (chat turns + panels).

On a small VM an unbounded burst (e.g. a post-restart backlog of queued Telegram
updates) can spawn many large generations at once and OOM the container. A single
process-wide semaphore bounds how many run concurrently; callers wrap each top-level
heavy task with `run_capped(...)`.

The semaphore is rebound per event loop because the polling loop is recreated on
NetworkError restarts (same reason _panel_locks / provider caches are reset on restart);
an asyncio primitive from a previous loop must not leak into the next.
"""
import asyncio
import logging

import config

logger = logging.getLogger(__name__)

_sem = None
_sem_loop = None


def get_generation_semaphore() -> asyncio.Semaphore:
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        limit = config.get_max_concurrent_generations()
        _sem = asyncio.Semaphore(limit)
        _sem_loop = loop
        logger.info(f"Generation semaphore initialized (limit={limit}) for the current event loop.")
    return _sem


async def run_capped(coro):
    """Await `coro` while holding one generation permit. Wrap each top-level heavy task
    (chat generation task, panel background task) — NOT the recursive inner LLM functions,
    which would deadlock against their own held permit."""
    async with get_generation_semaphore():
        return await coro
