#!/usr/bin/env python3
"""
End-to-end panel QA script.

Fires three _run_panel_workflow() calls — one per MCP type (Tavily, Notion, SQLite) —
using real API keys, real MCP servers, and the real LLM models configured in config.yaml.
Does NOT require a live Telegram session.

Usage (from project root):
    python scripts/panel_qa.py

Prerequisites:
    - .env with API keys (TAVILY_API_KEY, NOTION_TOKEN, NVIDIA/etc.)
    - npx and uvx available in PATH (for MCP subprocesses)
    - data/bot_sessions.db exists for SQLite test (or SQLite test will connect but find empty tables)
"""
import asyncio
import logging
import sys
import os
import time
from unittest.mock import AsyncMock, MagicMock

# ── bootstrap ──────────────────────────────────────────────────────────────────
# Must be done before any project imports so config.py picks up .env
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import config  # noqa: E402 — loads .env a second time harmlessly
from services.mcp_service import McpClientService  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("panel_qa")

# ── constants ──────────────────────────────────────────────────────────────────
QA_CHAT_ID = 0  # Real chat ID — results logged to stdout, nothing sent to Telegram

TEST_CASES = [
    {
        "name": "TAVILY — current AI news",
        "prompt": (
            "What are the most significant AI model releases announced in the past 30 days? "
            "For each one include: provider name, model name, context window size, and pricing tier. "
            "This requires current web data."
        ),
        "expected_mcp": "tavily-search",
    },
    {
        "name": "NOTION — workspace inventory",
        "prompt": (
            "List all the pages and databases in my Notion workspace. "
            "Then suggest a top-level organizational hierarchy that reduces clutter and improves findability."
        ),
        "expected_mcp": "notion-workspace",
    },
    {
        "name": "SQLITE — conversation history",
        "prompt": (
            "Query my conversation history database to find the topics I have been researching most frequently. "
            "Summarize the top 5 recurring themes and suggest follow-up questions I might want to explore."
        ),
        "expected_mcp": "sqlite-tools",
    },
]


# ── minimal Telegram mocks ────────────────────────────────────────────────────
class PrintingPlaceholder:
    """Replaces the Telegram placeholder message with stdout logging."""
    def __init__(self, case_name: str):
        self.case_name = case_name

    async def edit_text(self, text: str, **kwargs):
        logger.info(f"[{self.case_name}] Status: {text[:120]}")

    async def reply_text(self, text: str, **kwargs):
        logger.info(f"[{self.case_name}] Reply: {text[:120]}")


def make_mock_context(bot_data: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.application.bot_data = bot_data
    ctx.user_data = {}
    ctx.chat_data = {}
    return ctx


def make_mock_update(chat_id: int) -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    return upd


# ── MCP discovery ─────────────────────────────────────────────────────────────
async def discover_and_print_tools(mcp: McpClientService) -> dict[str, list[str]]:
    """Connect all MCP servers and print every discovered tool name."""
    logger.info("═" * 60)
    logger.info("MCP TOOL DISCOVERY")
    logger.info("═" * 60)

    all_tools = await mcp.get_all_tools()
    by_server: dict[str, list[str]] = {}
    for tool in all_tools:
        name = tool["function"]["name"]
        server = name.split("__")[0] if "__" in name else "unknown"
        by_server.setdefault(server, []).append(name)

    for server, names in by_server.items():
        logger.info(f"  [{server}] {len(names)} tool(s):")
        for n in sorted(names):
            logger.info(f"      {n}")

    if not by_server:
        logger.warning("  No tools discovered — check MCP server connectivity and API keys.")

    logger.info("═" * 60)
    return by_server


# ── single test runner ────────────────────────────────────────────────────────
async def run_test_case(case: dict, mcp: McpClientService) -> dict:
    """Runs one panel workflow end-to-end and returns a result dict."""
    from bot.handlers.discuss_panel_handler import _run_panel_workflow

    name = case["name"]
    logger.info("")
    logger.info("▶" * 3 + f"  RUNNING: {name}")
    logger.info(f"   Prompt: {case['prompt'][:120]}…")

    bot_data = {"mcp_service": mcp}
    context = make_mock_context(bot_data)
    update = make_mock_update(QA_CHAT_ID)
    placeholder = PrintingPlaceholder(name)

    start = time.monotonic()
    try:
        panel_results, final_answer, synthesis_response = await _run_panel_workflow(
            update=update,
            context=context,
            user_prompt=case["prompt"],
            full_history=[],
            placeholder_msg=placeholder,
            chat_id=QA_CHAT_ID,
        )
        elapsed = time.monotonic() - start

        # Check if the expected MCP tool was used
        expected = case["expected_mcp"]
        mcp_used = any(
            expected in str(v)
            for v in panel_results.values()
            if isinstance(v, dict) and v.get("status") == "Success"
        )

        logger.info(f"   ✓ Completed in {elapsed:.0f}s")
        logger.info(f"   Final answer length: {len(final_answer or '')} chars")
        logger.info(f"   Expected MCP '{expected}' confirmed in results: {mcp_used}")
        logger.info(f"   Answer preview: {(final_answer or '')[:300]}")

        return {
            "name": name,
            "ok": True,
            "elapsed": elapsed,
            "answer_len": len(final_answer or ""),
            "expected_mcp_seen": mcp_used,
            "error": None,
        }

    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error(f"   ✗ FAILED after {elapsed:.0f}s: {exc}", exc_info=True)
        return {
            "name": name,
            "ok": False,
            "elapsed": elapsed,
            "answer_len": 0,
            "expected_mcp_seen": False,
            "error": str(exc),
        }


# ── main ───────────────────────────────────────────────────────────────────────
async def main():
    logger.info("Panel QA — End-to-End MCP Verification")
    logger.info(f"Chat ID: {QA_CHAT_ID} | Test cases: {len(TEST_CASES)}")

    # Build MCP service from config
    server_configs = config._yaml_config.get("mcp_servers", [])
    if not server_configs:
        logger.error("No mcp_servers found in config.yaml — aborting.")
        sys.exit(1)

    mcp = McpClientService(server_configs)
    logger.info("Connecting to MCP servers…")
    await mcp.connect_all()

    passed = 0
    try:
        # Discover and print all tool names — critical for debugging Notion name mismatches
        tool_map = await discover_and_print_tools(mcp)

        # Run all test cases sequentially
        results = []
        for case in TEST_CASES:
            result = await run_test_case(case, mcp)
            results.append(result)

        # Summary
        logger.info("")
        logger.info("═" * 60)
        logger.info("QA SUMMARY")
        logger.info("═" * 60)
        passed = sum(1 for r in results if r["ok"])
        for r in results:
            status = "✓ PASS" if r["ok"] else "✗ FAIL"
            mcp_flag = "MCP✓" if r["expected_mcp_seen"] else "MCP?"
            logger.info(
                f"  {status}  [{mcp_flag}]  {r['name']}  "
                f"({r['elapsed']:.0f}s, {r['answer_len']} chars)"
                + (f"  ERROR: {r['error']}" if r["error"] else "")
            )
        logger.info(f"\n  {passed}/{len(results)} cases passed.")
        logger.info("═" * 60)
    finally:
        # Always clean up MCP subprocesses, even if a test case raises
        await mcp.cleanup_all()

    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
