#!/usr/bin/env python3
"""
Load / OOM soak driver for pre-flight (runs INSIDE the production container).

Purpose: reproduce the "don't spike load and break the VM" risk on the dev box BEFORE
shipping. It drives several concurrent, large-context real generations (MCP enabled, long
seeded history) so that — when this script is the container's process under the production
`--memory=768m` limit — we can measure peak RSS and confirm the container is NOT OOM-killed
and that the global generation semaphore (config.max_concurrent_generations) actually bounds
concurrency. The container-level OOMKilled / peak-RSS verdict is captured by the wrapping
harness (scripts/preflight.sh); this script reports its own view and a non-zero exit on any
unexpected CRASH (not on ordinary provider errors, which are an upstream condition, not a bug).

Usage (inside the built image, under the prod memory limit):
    docker run --rm --memory=768m --memory-swap=768m --env-file .env \
        -v "$PWD/config.yaml:/app/config.yaml:ro" -v "$PWD/data:/app/data" \
        <image> python scripts/load_soak.py

Tunables (env):
    PREFLIGHT_LOAD_CONCURRENCY   number of concurrent generations (default 4; set > the
                                 semaphore limit to prove serialization/backpressure)
    PREFLIGHT_LOAD_HISTORY       messages to seed per chat (default 200)
    PREFLIGHT_LOAD_PROVIDER      optional provider override (else uses config default)
"""
import asyncio
import json
import logging
import os
import resource
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import config  # noqa: E402
from storage import database_storage as db  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-8s %(name)s — %(message)s", stream=sys.stdout)
logger = logging.getLogger("load_soak")
logger.setLevel(logging.INFO)

CONCURRENCY = int(os.getenv("PREFLIGHT_LOAD_CONCURRENCY", "4"))
HISTORY_PER_CHAT = int(os.getenv("PREFLIGHT_LOAD_HISTORY", "200"))
PROVIDER_OVERRIDE = os.getenv("PREFLIGHT_LOAD_PROVIDER") or None
BASE_CHAT_ID = 700900000  # dedicated soak range, isolated from real + other-QA chats


def _peak_rss_mb() -> float:
    # ru_maxrss is bytes on macOS, KiB on Linux. The container runs on Linux → KiB.
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru / 1024 if sys.platform == "linux" else ru / (1024 * 1024)


async def _seed_history(chat_id: int, n: int) -> None:
    await db.init_database()
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        # ~80-word messages so the seeded thread is substantial enough to exercise the
        # context-fit / truncation path and its memory, not a trivial handful of tokens.
        await db.save_message(chat_id, role, f"Seed message {i}: " + ("context " * 80))


async def _one_generation(chat_id: int, shared_app) -> dict:
    """Drive a single real generation through the semaphore-wrapped path, sharing the one
    MCP service (via shared_app.bot_data) exactly as production's supervisor does."""
    from bot.response_generator import _generate_llm_response
    from utils.concurrency import run_capped

    if PROVIDER_OVERRIDE:
        await db.set_thread_key(chat_id, "provider", PROVIDER_OVERRIDE)
    await db.set_user_setting(chat_id, "enable_mcp", True)      # exercise MCP tool catalog + subprocesses
    await db.set_user_setting(chat_id, "enable_skills", False)  # skills are in-process/cheap; MCP is the memory driver
    await db.set_user_setting(chat_id, "auto_retry_on_error", False)

    ctx = types.SimpleNamespace(
        application=shared_app, chat_data={}, user_data={}, bot=types.SimpleNamespace(token="preflight")
    )
    config.get_enable_streaming = lambda: False  # no Telegram draft round-trips

    prompt = "Summarize the key themes of this conversation in exactly three bullet points."
    t0 = time.monotonic()
    try:
        res = await run_capped(_generate_llm_response(ctx, chat_id, prompt))
        return {
            "chat_id": chat_id,
            "crashed": False,
            "llm_error": res.get("error"),
            "content_len": len(res.get("content") or ""),
            "elapsed": round(time.monotonic() - t0, 1),
        }
    except Exception as e:  # a real crash (not a provider error sentinel) — this is a bug
        logger.exception(f"Generation for chat {chat_id} CRASHED: {e}")
        return {"chat_id": chat_id, "crashed": True, "error": repr(e), "elapsed": round(time.monotonic() - t0, 1)}


async def main() -> int:
    logger.info(f"Load soak: concurrency={CONCURRENCY}, history/chat={HISTORY_PER_CHAT}, "
                f"semaphore_limit={config.get_max_concurrent_generations()}")

    chat_ids = [BASE_CHAT_ID + i for i in range(CONCURRENCY)]
    logger.info("Seeding history…")
    await asyncio.gather(*[_seed_history(cid, HISTORY_PER_CHAT) for cid in chat_ids])

    # One shared MCP service injected into a fake app.bot_data — get_or_init_mcp_service
    # returns this pre-injected instance for every generation (line ~185 of
    # service_registry), so all N concurrent turns share ONE set of MCP subprocesses, as
    # production's supervisor does (not N×3 spawned copies).
    from services.mcp_service import McpClientService
    server_configs = config._yaml_config.get("mcp_servers", [])
    shared_mcp = McpClientService(server_configs)
    logger.info("Connecting shared MCP service…")
    await shared_mcp.connect_all()
    shared_app = types.SimpleNamespace(bot_data={"mcp_service": shared_mcp})

    logger.info(f"Firing {CONCURRENCY} concurrent generations under the memory limit…")
    t0 = time.monotonic()
    try:
        results = await asyncio.gather(*[_one_generation(cid, shared_app) for cid in chat_ids])
    finally:
        elapsed = time.monotonic() - t0
        try:
            await shared_mcp.cleanup_all()
        except Exception:
            pass

    crashed = [r for r in results if r.get("crashed")]
    llm_errors = [r for r in results if not r.get("crashed") and r.get("llm_error")]
    ok = [r for r in results if not r.get("crashed") and not r.get("llm_error")]
    peak = _peak_rss_mb()

    summary = {
        "concurrency": CONCURRENCY,
        "semaphore_limit": config.get_max_concurrent_generations(),
        "elapsed_s": round(elapsed, 1),
        "ok": len(ok),
        "llm_errors": len(llm_errors),
        "crashed": len(crashed),
        "in_process_peak_rss_mb": round(peak, 1),
    }
    logger.info("SOAK_SUMMARY " + json.dumps(summary))
    if crashed:
        logger.error(f"{len(crashed)} generation(s) CRASHED (real bug): {crashed}")
    if llm_errors:
        logger.warning(f"{len(llm_errors)} generation(s) returned a provider error (upstream, not a crash).")

    # Exit non-zero ONLY on a real crash. Provider/LLM errors are an upstream condition and
    # do not fail pre-flight (the container surviving under load is what this check asserts).
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
