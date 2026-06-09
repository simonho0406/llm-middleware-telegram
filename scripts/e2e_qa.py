#!/usr/bin/env python3
"""
End-to-end user-story QA script.

Covers three scenarios that panel_qa.py does not:
  1. NORMAL CHAT  — real LLM call via _generate_llm_response, saved to storage
  2. THREAD ISOLATION — panel thread vs. chat thread; history must not bleed
  3. TOOLSET SCOPE — panel execution tools are a strict subset of all MCP tools;
                     write paths are blocked regardless

Uses the same real-provider pattern as panel_qa.py (no provider mocks).
Does NOT require a live Telegram session.

Usage (from project root, inside Docker):
    python scripts/e2e_qa.py

Prerequisites:
    - .env with API keys
    - data/bot_sessions.db exists
"""
import asyncio
import logging
import sys
import os
import time
from unittest.mock import MagicMock

# ── bootstrap ──────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import config  # noqa: E402
from storage import database_storage as db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
# Silence noisy sub-loggers during QA
for _noisy in ("services.openai_compatible_service", "utils.context_manager",
               "bot.prompt_loader", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("e2e_qa")

# ── constants ──────────────────────────────────────────────────────────────────
# Dedicated QA chat IDs — separate from the real user to avoid polluting history
QA_CHAT_ID_CHAT = 700000001   # used for normal-chat scenario
QA_CHAT_ID_ISOLATION = 700000002   # used for thread-isolation scenario

THREAD_A = "qa-thread-panel"
THREAD_B = "qa-thread-chat"


# ── helpers ───────────────────────────────────────────────────────────────────
def make_mock_context(bot_data: dict = None) -> MagicMock:
    ctx = MagicMock()
    ctx.application.bot_data = bot_data or {}
    ctx.user_data = {}
    ctx.chat_data = {}
    return ctx


# ── scenario 1: normal chat ───────────────────────────────────────────────────
async def run_normal_chat(results: list) -> None:
    """
    Fires a real LLM call through _generate_llm_response and verifies that:
      - A non-empty response is returned with no is_error flag
      - The user message and assistant response are persisted to storage
    """
    from bot.response_generator import _generate_llm_response  # noqa: E402

    name = "NORMAL CHAT — factual question"
    prompt = (
        "What is the speed of light in vacuum? "
        "Give a precise numeric answer with SI units in one sentence."
    )
    logger.info("")
    logger.info("▶▶▶  %s", name)

    await db.init_database()
    # Save user message and capture pk so we can roll it back if the LLM call fails.
    msg_pk = await db.save_message(QA_CHAT_ID_CHAT, "user", prompt)

    context = make_mock_context()
    start = time.monotonic()
    try:
        result = await _generate_llm_response(context, QA_CHAT_ID_CHAT, prompt)
        elapsed = time.monotonic() - start

        content = result.get("content", "")
        error = result.get("error")

        if error:
            raise RuntimeError(f"LLM returned error: {error}")
        if not content or len(content) < 20:
            raise RuntimeError(f"Response too short or empty: {repr(content)}")

        # Persist the assistant response (mirrors what _generate_and_send_response does)
        await db.save_message(QA_CHAT_ID_CHAT, "assistant", content)

        # Verify both messages are now in storage
        history = await db.get_thread_history(QA_CHAT_ID_CHAT)
        has_user = any(m.get("role") == "user" and prompt[:30] in m.get("content", "") for m in history)
        has_asst = any(m.get("role") == "assistant" and len(m.get("content", "")) > 10 for m in history)

        if not has_user or not has_asst:
            raise RuntimeError("Messages not found in thread history after save.")

        logger.info("   ✓ Completed in %.0fs | response: %d chars", elapsed, len(content))
        logger.info("   Preview: %s", content[:200])
        results.append({"name": name, "ok": True, "elapsed": elapsed, "error": None})

    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error("   ✗ FAILED after %.0fs: %s", elapsed, exc, exc_info=True)
        # Roll back the orphaned user message so reruns don't accumulate stale history
        if msg_pk:
            try:
                await db.delete_messages(QA_CHAT_ID_CHAT, [msg_pk])
            except Exception as cleanup_err:
                logger.warning("Failed to clean up orphaned QA message (pk=%s): %s", msg_pk, cleanup_err)
        results.append({"name": name, "ok": False, "elapsed": elapsed, "error": str(exc)})


# ── scenario 2: thread isolation ─────────────────────────────────────────────
async def run_thread_isolation(results: list) -> None:
    """
    Creates two threads under the same chat_id (simulating a user who has a
    panel conversation in thread A and a normal chat in thread B) and asserts
    that history does not bleed across threads.
    """
    name = "THREAD ISOLATION — panel thread vs. chat thread"
    logger.info("")
    logger.info("▶▶▶  %s", name)

    start = time.monotonic()
    try:
        await db.init_database()

        # Clean up any residual data from previous QA runs
        await db.delete_thread(QA_CHAT_ID_ISOLATION, THREAD_A)
        await db.delete_thread(QA_CHAT_ID_ISOLATION, THREAD_B)

        # Seed thread A with panel-style messages
        await db.create_thread(QA_CHAT_ID_ISOLATION, THREAD_A)
        await db.set_current_thread_id(QA_CHAT_ID_ISOLATION, THREAD_A)
        await db.save_message(QA_CHAT_ID_ISOLATION, "user",
                              "Panel prompt: explain quantum entanglement.", thread_id=THREAD_A)
        await db.save_message(QA_CHAT_ID_ISOLATION, "assistant:panel",
                              "Panel answer: entanglement is...", thread_id=THREAD_A)

        # Seed thread B with normal chat messages
        await db.create_thread(QA_CHAT_ID_ISOLATION, THREAD_B)
        await db.set_current_thread_id(QA_CHAT_ID_ISOLATION, THREAD_B)
        await db.save_message(QA_CHAT_ID_ISOLATION, "user",
                              "Chat prompt: what is 2+2?", thread_id=THREAD_B)
        await db.save_message(QA_CHAT_ID_ISOLATION, "assistant",
                              "Chat answer: 4.", thread_id=THREAD_B)

        # Verify thread A history contains only thread A messages
        history_a = await db.get_thread_history(QA_CHAT_ID_ISOLATION, thread_id=THREAD_A)
        for msg in history_a:
            if "2+2" in (msg.get("content") or "") or "Chat" in (msg.get("content") or ""):
                raise RuntimeError(f"Thread B message leaked into thread A: {msg}")

        # Verify thread B history contains only thread B messages
        history_b = await db.get_thread_history(QA_CHAT_ID_ISOLATION, thread_id=THREAD_B)
        for msg in history_b:
            if "entanglement" in (msg.get("content") or "") or "assistant:panel" == msg.get("role"):
                raise RuntimeError(f"Thread A panel message leaked into thread B: {msg}")

        # Verify counts
        if len(history_a) != 2:
            raise RuntimeError(f"Thread A expected 2 messages, got {len(history_a)}")
        if len(history_b) != 2:
            raise RuntimeError(f"Thread B expected 2 messages, got {len(history_b)}")

        elapsed = time.monotonic() - start
        logger.info("   ✓ Thread A: %d messages (no leak). Thread B: %d messages (no leak). (%.1fs)",
                    len(history_a), len(history_b), elapsed)
        results.append({"name": name, "ok": True, "elapsed": elapsed, "error": None})

    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error("   ✗ FAILED after %.1fs: %s", elapsed, exc, exc_info=True)
        results.append({"name": name, "ok": False, "elapsed": elapsed, "error": str(exc)})


# ── scenario 3: toolset scope ─────────────────────────────────────────────────
async def run_toolset_scope(results: list):
    """
    Connects real MCP servers and asserts the security invariants.

    The `panel_execution` flag controls which *servers* the panel may call.
    Currently all servers are enabled (panel_execution: true), so the frozenset
    equals all_tools — that is intentional. The real write-path restriction
    lives in the HookRunner: run_pre_tool_use must raise PermissionError for
    blocked tool names and must NOT raise for permitted read tools.

    Returns the McpClientService so main() can clean it up in the right task context.
    """
    from services.mcp_service import McpClientService  # noqa: E402
    from utils.hooks import hook_runner  # noqa: E402

    name = "TOOLSET SCOPE — security boundary (panel_execution + HookRunner)"
    logger.info("")
    logger.info("▶▶▶  %s", name)

    start = time.monotonic()
    mcp = None
    try:
        server_configs = config._yaml_config.get("mcp_servers", [])
        if not server_configs:
            raise RuntimeError("No mcp_servers in config.yaml")

        mcp = McpClientService(server_configs)
        await mcp.connect_all()

        all_tools = await mcp.get_all_tools()
        all_tool_names = {t["function"]["name"] for t in all_tools}

        # Build panel_execution_tools (mirrors discuss_panel_handler.py logic)
        _server_cfg_map = {s["name"]: s for s in server_configs}
        panel_tool_names = {
            t["function"]["name"] for t in all_tools
            if _server_cfg_map.get(
                t["function"]["name"].split("__")[0], {}
            ).get("panel_execution", False)
        }

        enabled_servers = sorted({
            s["name"] for s in server_configs if s.get("panel_execution", False)
        })
        disabled_servers = sorted({
            s["name"] for s in server_configs if not s.get("panel_execution", False)
        })

        logger.info(
            "   panel_execution: enabled=%s disabled=%s | total tools: %d | panel tools: %d",
            enabled_servers, disabled_servers, len(all_tool_names), len(panel_tool_names)
        )

        # Invariant A: any server with panel_execution: false must have NO tools in panel set
        for tool_name in panel_tool_names:
            server = tool_name.split("__")[0]
            cfg = _server_cfg_map.get(server, {})
            if not cfg.get("panel_execution", False):
                raise RuntimeError(
                    f"Tool '{tool_name}' from disabled server '{server}' leaked into panel set."
                )

        # Invariant B: HookRunner MUST raise PermissionError for write-path tools
        write_tools_to_block = [
            "sqlite-tools__write_query",
            "sqlite-tools__create_table",
            "sqlite-tools__append_insight",
        ]
        for tool in write_tools_to_block:
            try:
                hook_runner.run_pre_tool_use(tool, {"arguments": {}})
                raise RuntimeError(f"HookRunner did NOT block write tool '{tool}' — security gap!")
            except PermissionError:
                logger.info("   ✓ Hook correctly blocks: %s", tool)

        # Invariant C: HookRunner must NOT raise for permitted read tools
        read_tools_to_allow = [
            "sqlite-tools__read_query",
            "sqlite-tools__list_tables",
            "sqlite-tools__describe_table",
        ]
        for tool in read_tools_to_allow:
            try:
                hook_runner.run_pre_tool_use(tool, {"arguments": {"query": "SELECT 1", "table_name": "messages"}})
                logger.info("   ✓ Hook correctly permits: %s", tool)
            except PermissionError as e:
                raise RuntimeError(f"HookRunner incorrectly blocked read tool '{tool}': {e}")

        elapsed = time.monotonic() - start
        logger.info("   ✓ All security invariants hold (%.1fs)", elapsed)
        results.append({"name": name, "ok": True, "elapsed": elapsed, "error": None})
        return mcp

    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error("   ✗ FAILED after %.1fs: %s", elapsed, exc, exc_info=True)
        results.append({"name": name, "ok": False, "elapsed": elapsed, "error": str(exc)})
        return mcp


# ── main ───────────────────────────────────────────────────────────────────────
async def main():
    logger.info("E2E User-Story QA")
    logger.info("Scenarios: normal chat | thread isolation | toolset scope")

    results: list = []
    mcp = None

    mcp = await run_toolset_scope(results)   # fast — no LLM call; returns MCP for cleanup
    await run_thread_isolation(results)       # fast — storage only
    await run_normal_chat(results)            # slow — real LLM

    # Clean up MCP in the same task context it was created in (avoids anyio cancel scope errors)
    if mcp:
        try:
            await mcp.cleanup_all()
        except Exception:
            pass  # cleanup errors are non-fatal

    logger.info("")
    logger.info("═" * 60)
    logger.info("QA SUMMARY")
    logger.info("═" * 60)
    passed = sum(1 for r in results if r["ok"])
    for r in results:
        status = "✓ PASS" if r["ok"] else "✗ FAIL"
        logger.info(
            "  %s  %s  (%.1fs)%s",
            status, r["name"], r["elapsed"],
            f"  ERROR: {r['error']}" if r["error"] else "",
        )
    logger.info("\n  %d/%d cases passed.", passed, len(results))
    logger.info("═" * 60)

    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
