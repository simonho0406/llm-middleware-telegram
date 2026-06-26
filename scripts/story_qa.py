#!/usr/bin/env python3
"""
Story-driven, end-to-end QA — validates the system against the REAL user stories
mined from the thread-history DB (see docs/USER_STORIES.md).

Runs against real MCP servers + real LLM models (no Telegram session). Each story
asserts grounding/behavior and prints a pass/fail table.

Usage (from project root):
    python scripts/story_qa.py
    python scripts/story_qa.py 1 4      # run only stories 1 and 4

Prerequisites:
    - .env with API keys (TELEGRAM not needed; TAVILY_API_KEY, NOTION_TOKEN, provider keys)
    - npx and uvx available in PATH (for MCP subprocesses)
    - data/bot_sessions.db exists (real Notion/history for stories 1-3)
"""
import asyncio
import logging
import os
import sys
import time
from unittest.mock import MagicMock

# ── bootstrap ──────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import config  # noqa: E402
from services.mcp_service import McpClientService  # noqa: E402
from storage import database_storage as db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s — %(message)s", stream=sys.stdout)
for _noisy in ("services.openai_compatible_service", "utils.context_manager", "bot.prompt_loader", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger("story_qa")

# Real chat — has the user's actual Notion + panel config; _run_panel_workflow does NOT
# write to message history, so this is safe for the panel story.
REAL_CHAT_ID = 0
# Dedicated chat for normal-chat stories (seeded, no real-data pollution).
QA_CHAT_ID = 700000012
QA_HIST_FACT = "ZEPHYR-9"  # distinctive token seeded into history for the mining story
# Separate CLEAN chat for the auto-search story so seeded/prior history can't let the
# model answer from context instead of actually triggering a real-time search.
QA_CLEAN_CHAT_ID = 700000013


class PrintingPlaceholder:
    def __init__(self, tag): self.tag = tag
    async def edit_text(self, text, **k): logger.info(f"[{self.tag}] status: {text[:110]}")
    async def reply_text(self, text, **k): logger.info(f"[{self.tag}] reply: {text[:110]}")


def make_context(mcp):
    ctx = MagicMock()
    ctx.application.bot_data = {"mcp_service": mcp}
    ctx.user_data = {}
    ctx.chat_data = {}
    return ctx


def make_update(chat_id):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    return upd


# Markers of a non-answer / punt — the panel admitting it didn't get the data, or
# deferring the actual deliverable back to the user. A reply containing any of these
# has NOT fulfilled the request, regardless of how high the quality gate scored it.
_FAILURE_MARKERS = (
    "not retrieved", "could not be retrieved", "not successfully retrieved",
    "extraction failure", "not included", "unable to retrieve", "content was not",
    "no content", "failed to retrieve", "cannot be extracted", "could not be extracted",
    "please share", "please provide", "paste the", "share the text",
    "provide access", "share the content", "was not included in the search results",
    "core text is missing", "body content", "once i have", "once you provide",
)


async def _llm_judge(user_prompt: str, answer: str, criteria: str) -> tuple:
    """Semantic QA judge — the check the broken quality gate failed to do.

    Asks a model whether the answer ACTUALLY fulfills the request. Returns
    (passed: bool, reason: str). This catches punts/non-answers that string
    matching alone can miss.
    """
    from utils.llm_utilities import get_robust_llm_response
    judge_prompt = (
        "You are a strict QA judge. Decide whether the ASSISTANT ANSWER actually "
        "fulfills the USER REQUEST.\n\n"
        f"--- USER REQUEST ---\n{user_prompt}\n\n"
        f"--- ASSISTANT ANSWER ---\n{answer}\n\n"
        f"--- PASS CRITERIA ---\n{criteria}\n\n"
        "A reply that admits it could not retrieve the data, asks the user to paste/share/"
        "provide the content, or only describes what it WOULD do is a FAIL. "
        "Respond with ONLY a JSON object: {\"verdict\": \"PASS\" or \"FAIL\", \"reason\": \"<one sentence>\"}."
    )
    try:
        res = await get_robust_llm_response(
            provider_name=config.get_utility_model_provider(),
            model=config.get_utility_model_name(),
            prompt=judge_prompt,
            history=[],
            role_name="QA Judge",
            request_timeout=120,
        )
        if res.get("is_error"):
            return False, f"judge model error: {str(res.get('response',''))[:120]}"
        import json as _json
        raw = res.get("response", "") or ""
        s, e = raw.find("{"), raw.rfind("}")
        verdict = _json.loads(raw[s:e + 1]) if s != -1 and e > s else {}
        passed = str(verdict.get("verdict", "")).upper() == "PASS"
        return passed, verdict.get("reason", raw[:160])
    except Exception as ex:
        return False, f"judge error: {ex}"


# ── Story 1: Panel deep Notion retrieval + verify ───────────────────────────────
async def story1_panel_notion(mcp) -> dict:
    from bot.handlers.panel_workflow import _run_panel_workflow
    name = "1 · Panel deep Notion retrieval + verify"
    prompt = (
        "List me the exact REDACTED in my ingredients. "
        "It's in the Notion page 'Reference Catalog' — a certain h3 section contains what you need. "
        "Retrieve it, then verify with your sources or research."
    )
    criteria = (
        "PASS only if the answer lists actual gin names WITH concrete flavor descriptors "
        "(botanicals, tasting notes) drawn from the page content. FAIL if it asks the user to "
        "provide/paste the content, says the page body/content could not be retrieved, or only "
        "returns the page's metadata."
    )
    # Panel output is non-deterministic and the failure is intermittent — run it
    # multiple times and require EVERY run to truly fulfill the request.
    runs = int(os.environ.get("PANEL_QA_RUNS", "2"))
    start = time.monotonic()
    sub = []
    for i in range(1, runs + 1):
        try:
            panel_results, final_answer, _ = await _run_panel_workflow(
                update=make_update(REAL_CHAT_ID), context=make_context(mcp),
                user_prompt=prompt, full_history=[], placeholder_msg=PrintingPlaceholder(f"S1.{i}"), chat_id=REAL_CHAT_ID,
            )
            ans = (final_answer or "")
            low = ans.lower()
            notion_seen = any("notion-workspace" in str(v) for v in panel_results.values() if isinstance(v, dict))
            punt = next((m for m in _FAILURE_MARKERS if m in low), None)
            judged_ok, judge_reason = await _llm_judge(prompt, ans, criteria)
            run_ok = bool(ans) and len(ans) > 200 and punt is None and notion_seen and judged_ok
            logger.info(f"[S1.{i}] preview: {ans[:240]}")
            logger.info(f"[S1.{i}] ok={run_ok} len={len(ans)} notion={notion_seen} punt={punt!r} judge={judged_ok} ({judge_reason})")
            sub.append(run_ok)
        except Exception as e:
            logger.exception(f"S1.{i} crashed")
            sub.append(False)
    ok = all(sub) and len(sub) == runs
    return _r(name, ok, start, f"runs={sub} (all must PASS)")


# ── Story 2: Chat-history mining (conversation_history view) ─────────────────────
async def story2_history_mining(mcp) -> dict:
    from bot.response_generator import _generate_llm_response
    name = "2 · Chat-history mining"
    prompt = (
        "Dive into our chat history and tell me: what internal project codename did I mention earlier, "
        "and what process node was it? Look it up in the history, don't guess."
    )
    start = time.monotonic()
    try:
        result = await _generate_llm_response(make_context(mcp), QA_CHAT_ID, prompt)
        content = result.get("content", "") or ""
        err = result.get("error")
        # Normalize Unicode hyphens (e.g. U+2011 non-breaking hyphen) to ASCII before matching.
        _normalized = content.replace('‐', '-').replace('‑', '-').replace('‒', '-').replace('–', '-')
        found = QA_HIST_FACT.lower() in _normalized.lower()
        ok = (err is None) and found
        logger.info(f"[S2] preview: {content[:280]}")
        return _r(name, ok, start, f"err={err} found_'{QA_HIST_FACT}'={found}")
    except Exception as e:
        logger.exception("S2 crashed")
        return _r(name, False, start, f"exception: {e}")


# ── Story 3: Multi-source overview ──────────────────────────────────────────────
async def story3_multi_source(mcp) -> dict:
    from bot.response_generator import _generate_llm_response
    name = "3 · Multi-source overview"
    prompt = (
        "Give me a brief overview combining three things: a quick scan of my Notion workspace, "
        "what's in our recent chat history, and any current public news about SpaceX. "
        "Three short sections."
    )
    start = time.monotonic()
    try:
        result = await _generate_llm_response(make_context(mcp), QA_CHAT_ID, prompt)
        content = result.get("content", "") or ""
        err = result.get("error")
        # Either it produced a substantive integrated answer, or it legitimately asked to search.
        ok = (err is None) and (len(content) > 250 or bool(result.get("search_queries")))
        logger.info(f"[S3] preview: {content[:280]}")
        return _r(name, ok, start, f"err={err} len={len(content)} search={bool(result.get('search_queries'))}")
    except Exception as e:
        logger.exception("S3 crashed")
        return _r(name, False, start, f"exception: {e}")


# ── Story 4: Real-time auto-search trigger ──────────────────────────────────────
async def story4_autosearch(mcp) -> dict:
    from bot.response_generator import _generate_llm_response
    name = "4 · Real-time auto-search trigger"
    prompt = "等等REDACTED會下雨嗎？我想知道接下來幾小時的降雨機率。"
    start = time.monotonic()
    try:
        # Clean chat: no seeded/prior history, so a real-time question must trigger search.
        result = await _generate_llm_response(make_context(mcp), QA_CLEAN_CHAT_ID, prompt)
        err = result.get("error")
        triggered = bool(result.get("search_queries"))
        ok = (err is None) and triggered
        logger.info(f"[S4] search_queries: {result.get('search_queries')}")
        return _r(name, ok, start, f"err={err} search_triggered={triggered}")
    except Exception as e:
        logger.exception("S4 crashed")
        return _r(name, False, start, f"exception: {e}")


# ── Story 5: Normal chat / coherent answer ──────────────────────────────────────
async def story5_normal_chat(mcp) -> dict:
    from bot.response_generator import _generate_llm_response
    name = "5 · Normal chat coherence"
    prompt = "In two sentences, explain what CPU binning is and why it matters for yield."
    start = time.monotonic()
    try:
        result = await _generate_llm_response(make_context(mcp), QA_CHAT_ID, prompt)
        content = result.get("content", "") or ""
        err = result.get("error")
        ok = (err is None) and len(content) > 40
        logger.info(f"[S5] preview: {content[:200]}")
        return _r(name, ok, start, f"err={err} len={len(content)}")
    except Exception as e:
        logger.exception("S5 crashed")
        return _r(name, False, start, f"exception: {e}")


def _r(name, ok, start, detail):
    return {"name": name, "ok": ok, "elapsed": time.monotonic() - start, "detail": detail}


async def _seed_history():
    """Seed the dedicated QA chat with distinctive history for stories 2/3."""
    await db.init_database()
    # Fresh start so the mining assertion is deterministic, and a clean chat for auto-search.
    for _cid in (QA_CHAT_ID, QA_CLEAN_CHAT_ID):
        try:
            await db.delete_thread(_cid, "default")
        except Exception:
            pass
    seed = [
        ("user", f"For the record, our internal project codename is {QA_HIST_FACT}, a 7nm test chip."),
        ("assistant", f"Noted — {QA_HIST_FACT} is a 7nm test chip. I'll remember that."),
    ]
    for role, content in seed:
        await db.save_message(QA_CHAT_ID, role, content)
    logger.info(f"Seeded {len(seed)} history rows into QA chat {QA_CHAT_ID}.")


ALL = {1: story1_panel_notion, 2: story2_history_mining, 3: story3_multi_source,
       4: story4_autosearch, 5: story5_normal_chat}


async def main():
    selected = [int(a) for a in sys.argv[1:] if a.isdigit()] or list(ALL.keys())
    logger.info(f"Story QA — running stories {selected}")

    server_configs = config._yaml_config.get("mcp_servers", [])
    if not server_configs:
        logger.error("No mcp_servers in config.yaml — aborting.")
        sys.exit(1)

    await _seed_history()

    mcp = McpClientService(server_configs)
    logger.info("Connecting MCP servers…")
    await mcp.connect_all()

    results = []
    try:
        for sid in selected:
            logger.info("")
            logger.info("▶▶▶ " + ALL[sid].__name__)
            results.append(await ALL[sid](mcp))
    finally:
        await mcp.cleanup_all()

    logger.info("")
    logger.info("═" * 70)
    logger.info("STORY QA SUMMARY")
    logger.info("═" * 70)
    passed = sum(1 for r in results if r["ok"])
    for r in results:
        status = "✓ PASS" if r["ok"] else "✗ FAIL"
        logger.info(f"  {status}  {r['name']}  ({r['elapsed']:.0f}s)  — {r['detail']}")
    logger.info(f"\n  {passed}/{len(results)} stories passed.")
    logger.info("═" * 70)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
