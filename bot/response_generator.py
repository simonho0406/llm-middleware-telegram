"""
Response Generator Module
Handles orchestration of LLM requests, history processing, search tags, and UI updates.
"""
# pylint: disable=logging-fstring-interpolation, line-too-long, broad-exception-caught, unused-argument, missing-function-docstring, too-many-locals, too-many-branches, too-many-statements, unused-variable, redefined-outer-name, invalid-name, unused-import

import logging
import time
import asyncio
import re
import json
import random
from telegram import Update
from telegram.ext import ContextTypes
import config
from bot import providers

from storage import storage_manager
from bot.messaging import send_safe_message, finalize_draft, send_draft_message, send_plain_message
from utils.context_manager import ensure_context_fits
from utils.llm_utilities import is_error_response
from utils.concurrency import run_capped
from bot.settings import USER_SETTINGS

logger = logging.getLogger(__name__)


def _parse_xml_tool_calls(text: str) -> list:
    """Parse NVIDIA nemotron XML-style tool calls into the same dict structure as JSON tool_calls.

    Nemotron emits:  <tool_call><function=server__tool><parameter=key>val</parameter></function></tool_call>
    Output matches the JSON path: [{"id": ..., "function": {"name": ..., "arguments": {...}}}]
    """
    calls = []
    for i, block in enumerate(re.finditer(r'<tool_call>(.*?)</tool_call>', text, re.DOTALL)):
        content = block.group(1)
        fn_match = re.search(r'<function=([^>]+)>', content)
        if not fn_match:
            continue
        tool_name = fn_match.group(1).strip()
        args = {}
        for p in re.finditer(r'<parameter=([^>]+)>\s*(.*?)\s*</parameter>', content, re.DOTALL):
            args[p.group(1).strip()] = p.group(2).strip()
        calls.append({"id": f"xml_tc_{i}", "function": {"name": tool_name, "arguments": args}})
    return calls


def _build_tool_catalog_section(mcp_tools: list, skill_tools: list, chat_id: int = None, thread_id: str = None) -> str:
    """
    Builds a markdown section listing active MCP servers and skills for injection
    into the system prompt.  Grouped by server so the model understands what
    each data source is for; individual tool schemas are already passed via the
    tools= API parameter.

    When the sqlite history server is connected, also injects a concrete
    "how to query your history" cheat-sheet (view name, columns, this chat's id,
    an example query) — without it the model guesses table names and fails.
    """
    if not mcp_tools and not skill_tools:
        return ""

    lines = ["\n\n---\n\n# Connected Tools\n"]

    if mcp_tools:
        by_server: dict[str, list] = {}
        for tool in mcp_tools:
            name = tool["function"]["name"]
            server = name.split("__")[0] if "__" in name else name
            by_server.setdefault(server, []).append(tool)

        lines.append("## MCP Servers\n")
        for server, tools in sorted(by_server.items()):
            count = len(tools)
            samples = [
                (n.split("__", 1)[1] if "__" in n else n)
                for t in tools[:3]
                for n in [t["function"]["name"]]
            ]
            sample_str = ", ".join(f"`{s}`" for s in samples)
            if count > 3:
                sample_str += f" … ({count} total)"
            lines.append(f"- **{server}** — {count} tool(s). Example calls: {sample_str}")
        lines.append("")

        # History cheat-sheet: only when the sqlite history server is connected.
        _has_sqlite = any(
            t["function"]["name"].startswith("sqlite-tools__") for t in mcp_tools
        )
        if _has_sqlite:
            chat_scope = f"chat_id = {chat_id}" if chat_id is not None else "chat_id = <this chat's id>"
            thread_lit = f"'{thread_id}'" if thread_id is not None else "'<this thread id>'"
            here = f"chat_id={chat_id}, thread_id={thread_lit}" if chat_id is not None else f"thread_id={thread_lit}"
            lines.append("### Querying conversation history\n")
            lines.append(
                "Your stored history is the read-only SQLite view **`conversation_history`** "
                "with columns: `id`, `chat_id`, `thread_id`, `thread_name`, `role`, "
                "`content`, `timestamp` (unix seconds). Query it via `sqlite-tools__read_query`."
            )
            lines.append(
                f"- **You are here:** {here}. A chat has multiple threads (e.g. `default` plus "
                f"any the user created); history is per-thread."
            )
            lines.append(
                f"- **Default scope = the current thread:** `WHERE {chat_scope} AND thread_id = {thread_lit}`. "
                f"Use this unless the user asks about other/all threads."
            )
            lines.append(
                f"- To look **across all of this user's threads**, drop the thread filter: `WHERE {chat_scope}` "
                f"(the `thread_id` / `thread_name` columns distinguish them). Never query without at least the chat_id filter."
            )
            lines.append(
                f"- Example (current thread): `SELECT role, content, timestamp FROM conversation_history "
                f"WHERE {chat_scope} AND thread_id = {thread_lit} ORDER BY timestamp DESC LIMIT 50`"
            )
            lines.append(
                "- Recent turns are already in your context — only query for older or aggregate data "
                "(counts, date ranges, keyword lookups). The database is read-only."
            )
            lines.append("")

    if skill_tools:
        lines.append("## Skills\n")
        for tool in skill_tools:
            fn = tool["function"]
            desc = fn.get("description", "")
            lines.append(f"- **{fn['name']}** — {desc}" if desc else f"- **{fn['name']}**")
        lines.append("")

    return "\n".join(lines)

async def _notify_user_failure(context: ContextTypes.DEFAULT_TYPE, update, chat_id: int, text: str) -> None:
    """Best-effort failure notification that NEVER raises.

    The harness's core promise: a user turn must end in an answer OR a visible
    error — never silence. This is the "fail loud to the user" primitive. It
    tries the rich AST sender first, then falls back to a plain send by chat_id.
    Both attempts are swallowed so notification can't itself become a new failure.
    """
    try:
        if update is not None:
            ok = await send_safe_message(context, update, text, chat_id=chat_id)
        else:
            ok = await send_safe_message(context, None, text, chat_id=chat_id)
        if ok:
            return
    except Exception as e:
        logger.warning(f"(Chat {chat_id}) _notify_user_failure: rich send failed: {e}")
    try:
        await send_plain_message(context, chat_id, text)
    except Exception as e:
        logger.error(f"(Chat {chat_id}) _notify_user_failure: plain send also failed: {e}")


async def _generate_and_send_response(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, prompt: str, current_thread_id: str, is_reroll: bool = False, force_truncate: bool = False, placeholder_message = None, skip_save: bool = False, task_key: str = 'llm_task', save_input: bool = True) -> None:
    """Wraps the response generation in a cancellable task.

    This is the harness choke point for normal chat / reroll / edit turns: it
    guarantees the turn ends in a delivered answer or a delivered, human-readable
    error. An *expected* cancellation (user /cancel, edit-supersede, deliberate
    zombie-cancel — flagged via task._expected_cancel) stays silent; any other
    cancellation or exception is surfaced to the user via _notify_user_failure.

    save_input=False keeps an already-persisted user prompt in place (the assistant
    reply is still saved). Used by startup recovery, which resumes an existing
    stranded user row without deleting/re-creating it (no data-loss window).
    """

    # SYSTEMIC FIX: Defensively cancel any existing task on this key to prevent zombie leak.
    # Also clean up the orphan's pending user-message PK so its row doesn't sit in DB forever.
    # Identity guard: never cancel (and reap the PK of) the task we are running in —
    # a nested caller would otherwise abort itself and delete its own just-saved
    # user message. Safe today (always called from a fresh handler task) but explicit.
    old_task = context.chat_data.get(task_key)
    if old_task and old_task is not asyncio.current_task() and not old_task.done():
        logger.warning(f"(Chat {chat_id}) Systemic Concurrency Catch: Cancelling zombie '{task_key}' before spinning up new task.")
        _orphan_pk = getattr(old_task, '_pending_user_message_pk', None)
        # This is a deliberate supersede — mark expected so the old task's wrapper
        # doesn't surface a spurious "interrupted" notice to the user.
        try:
            old_task._expected_cancel = True  # type: ignore[attr-defined]
        except AttributeError:
            pass
        old_task.cancel()
        if _orphan_pk is not None:
            try:
                await storage_manager.delete_messages(chat_id, [_orphan_pk])
                logger.info(f"(Chat {chat_id}) Reaped orphan user-message pk={_orphan_pk} from cancelled zombie task.")
            except Exception as _cleanup_err:
                logger.warning(f"(Chat {chat_id}) Failed to delete orphan user-message pk={_orphan_pk}: {_cleanup_err}")

    # run_capped: hold one global generation permit for the whole turn (bounds peak
    # RAM/CPU on small VMs). Safe to wrap here — this task body is non-recursive and the
    # in-turn auto-search delegation generates inline (no nested permit acquisition).
    task = asyncio.create_task(
        run_capped(_generate_and_send_response_task(update, context, chat_id, user_id, prompt, current_thread_id, is_reroll, force_truncate, placeholder_message, skip_save, save_input))
    )
    context.chat_data[task_key] = task
    try:
        await task
    except asyncio.CancelledError:
        # Cancel ONLY this task's own tracked background tasks (draft finalizers,
        # etc.). The set is attached to the task via _llm_bg_tasks so concurrent
        # handlers in the same chat don't cancel each other's drafts.
        bg_tasks = getattr(task, '_llm_bg_tasks', None)
        if bg_tasks:
            for t in list(bg_tasks):
                if not t.done():
                    t.cancel()

        if getattr(task, '_expected_cancel', False):
            logger.info(f"(Chat {chat_id}) LLM task '{task_key}' was cancelled cleanly (expected).")
        else:
            # Unexpected cancellation (bug, stray cancel) — do NOT fail silently.
            logger.warning(f"(Chat {chat_id}) LLM task '{task_key}' cancelled UNEXPECTEDLY — surfacing to user.")
            await _notify_user_failure(
                context, update, chat_id,
                "⚠️ That response was interrupted before it finished. Please try again."
            )
        # Don't re-raise: the caller (job/handler) treats completion as done; the
        # cancellation has been fully handled here.
    except Exception as e:
        # Catch-all backstop: any exception escaping the generation task is logged
        # with full traceback and surfaced to the user rather than vanishing into
        # the job runner (which would log "executed successfully" with no reply).
        logger.exception(f"(Chat {chat_id}) LLM task '{task_key}' raised an unhandled exception: {e}")
        await _notify_user_failure(
            context, update, chat_id,
            "⚠️ Something went wrong while generating a reply. Please try again."
        )

async def _process_history_for_llm(context_history: list, prompt: str, is_reroll: bool, log_prefix: str) -> list:
    if context_history and context_history[-1].get('role') == 'user' and context_history[-1].get('content') == prompt:
        context_history.pop()

    processed_history = []
    for message in context_history:
        role = message.get('role')
        content = message.get('content')

        if role == 'panel_discussion':
            try:
                panel_data = json.loads(content)
                summary = (
                    f"A previous expert panel discussion was held on the topic: '{panel_data.get('original_prompt')}'..\n"
                    f"The final synthesized answer was: '{panel_data.get('final_answer')}'"
                )
                processed_history.append({'role': 'assistant', 'content': f"[Summary of Prior Panel Discussion]:\n{summary}"})
            except (json.JSONDecodeError, TypeError):
                processed_history.append({'role': 'assistant', 'content': "[A complex panel discussion occurred previously.]"})
        elif role == 'assistant:panel':
            processed_history.append({'role': 'assistant', 'content': f"**[Previous Expert Panel Discussion Result]**\n\n{content}"})
        else:
            processed_history.append(message)

    if is_reroll and processed_history and processed_history[-1].get('role') == 'assistant':
        logger.info(f"{log_prefix}Reroll detected in history processing. Removing last assistant message.")
        processed_history.pop()

    return processed_history

async def _get_provider_configuration(chat_id: int, log_prefix: str) -> tuple:
    session_provider = await storage_manager.get_thread_key(chat_id, 'provider', config.get_default_provider())
    provider_details = providers.get_provider_details()

    if session_provider not in provider_details:
        logger.error(f"{log_prefix}Invalid provider '{session_provider}', falling back to default.")
        session_provider = config.get_default_provider()
        await storage_manager.set_thread_key(chat_id, 'provider', session_provider)

    provider_config = provider_details[session_provider]
    service = provider_config['service']
    model_key = 'model'
    default_model = provider_config['default_model']

    model_to_use = await storage_manager.get_thread_key(chat_id, model_key, default_model)
    provider_name_display = session_provider.capitalize()
    logger.info(f"{log_prefix}Using service: {service.__class__.__name__ if hasattr(service, '__class__') else service.__name__}, Model: {model_to_use}")

    provider_info = {
        'provider': session_provider,
        'provider_display': provider_name_display,
        'model': model_to_use,
        'service': service
    }
    return session_provider, model_to_use, provider_config, service, provider_info

def _extract_and_process_search_tags(raw_response: str, autosearch_enabled: bool, log_prefix: str) -> tuple[str, list | None]:
    extracted_search_queries = None
    search_queries_raw = re.findall(r"<search>(.*?)</search>", raw_response, re.DOTALL)
    if search_queries_raw:
        extracted_search_queries = [sq.strip() for sq in search_queries_raw if sq.strip()]

        if not autosearch_enabled:
            logger.info(f"{log_prefix}Auto-search disabled. Removing search tags and providing fallback answer.")
            raw_response = re.sub(r"<search>.*?</search>", "", raw_response, flags=re.DOTALL).strip()
            if not raw_response:
                queries_str = ", ".join(f"'{q}'" for q in extracted_search_queries)
                raw_response = f"I'd need to search for current information about {queries_str} to give you an accurate answer. Auto-search is disabled - you can enable it in /config or try the /search command directly."
            extracted_search_queries = None

    return raw_response.strip(), extracted_search_queries


async def _attempt_forced_synthesis(
    service, model: str, prompt: str, history: list, log_prefix: str,
    request_timeout: int | None = None,
) -> str | None:
    """Request a final answer with tools disabled. Returns text on success, None on failure."""
    try:
        chunks = []
        async for chunk in service.generate_response(
            model=model, prompt=prompt, context_history=history, tools=None,
            request_timeout=request_timeout,
        ):
            chunks.append(chunk)
        result = "".join(chunks)
        if (not result.strip()
                or is_error_response(result.lstrip())
                or result.lstrip().startswith('{"tool_calls"')):
            raise RuntimeError("synthesis returned empty, error-sentinel, or another tool call")
        logger.info(f"{log_prefix}Forced synthesis succeeded (len={len(result)}).")
        return result
    except Exception as err:
        logger.warning(f"{log_prefix}Forced synthesis failed: {err}")
        return None


async def _generate_llm_response(context: ContextTypes.DEFAULT_TYPE, chat_id: int, prompt: str, is_reroll: bool = False, force_truncate: bool = False, operation_id: str = "chat_response", is_retry: bool = False) -> dict:
    """
    Core LLM response generation logic, decoupled from message formatting and sending.
    Returns a response dict with 'content', 'error', 'truncated_history', and 'provider_info'.
    """
    log_prefix = f"(Chat {chat_id}) "

    context_history = await storage_manager.get_thread_history(chat_id)
    processed_history = await _process_history_for_llm(context_history, prompt, is_reroll, log_prefix)

    # Dynamically inject CHAT_SYSTEM_PROMPT into historical context before truncation and generation
    try:
        system_prompt = config.PROMPTS.get_prompt('CHAT_SYSTEM_PROMPT')
        processed_history = [{"role": "system", "content": system_prompt}] + processed_history
    except Exception as e:
        logger.warning(f"{log_prefix}Failed to dynamically inject CHAT_SYSTEM_PROMPT: {e}")

    session_provider, model_to_use, provider_config, service, provider_info = await _get_provider_configuration(chat_id, log_prefix)

    # Automatically ensure context fits within model limits
    safety_margin = 0.75 if force_truncate else 1.0

    final_history, context_info = await ensure_context_fits(
        prompt=prompt,
        history=processed_history,
        model=model_to_use,
        provider=session_provider,
        safety_margin=safety_margin,
        max_input_tokens=config.get_chat_max_context_tokens()  # None by default: chat scales with model capability, same as panels; only an operator-set override tightens it further
    )

    if context_info:
        logger.info(f"{log_prefix}{context_info}")

    truncated_history = final_history

    autosearch_enabled = await storage_manager.get_user_setting(
        chat_id,
        'autosearch_chat',
        USER_SETTINGS['autosearch_chat']['default']
    )

    enable_mcp = await storage_manager.get_user_setting(
        chat_id,
        'enable_mcp',
        USER_SETTINGS['enable_mcp']['default']
    )

    enable_skills = await storage_manager.get_user_setting(
        chat_id,
        'enable_skills',
        USER_SETTINGS['enable_skills']['default']
    )

    from utils.service_registry import get_or_init_mcp_service, get_or_init_skill_service
    app = getattr(context, 'application', None)
    mcp_service = await get_or_init_mcp_service(app, enable_mcp)
    skill_service = await get_or_init_skill_service(app, enable_skills)

    # Pre-fetch tools once — sessions don't change mid-conversation.
    # This also lets us build the tool catalog for the system prompt.
    _mcp_tools: list = []
    _skill_tools: list = []
    if enable_mcp and mcp_service:
        _mcp_tools = await mcp_service.get_all_tools()
    if enable_skills and skill_service:
        _skill_tools = skill_service.get_skills_as_tools()

    # Resolve the current thread so the history cheat-sheet can tell the model
    # exactly *where it is* — history is per-thread, and a chat has many threads
    # (including 'default'). Without this the model scopes only by chat_id and its
    # history queries spill across every thread.
    try:
        current_thread_id = await storage_manager.get_current_thread_id(chat_id)
    except Exception as e:
        logger.warning(f"{log_prefix}Could not resolve current thread id for catalog: {e}")
        current_thread_id = None

    # Inject live tool catalog into the system message so the model knows
    # what servers are connected and can make informed routing decisions.
    catalog_section = _build_tool_catalog_section(_mcp_tools, _skill_tools, chat_id=chat_id, thread_id=current_thread_id)
    if catalog_section:
        # truncated_history[0] is the system message (preserved by ensure_context_fits)
        if truncated_history and truncated_history[0].get("role") == "system":
            truncated_history[0] = {
                **truncated_history[0],
                "content": truncated_history[0]["content"] + catalog_section,
            }

    raw_full_llm_response = ""
    llm_error_reported_by_model = False

    # Tracked background tasks for this generation. Per-task (not per-chat) so
    # concurrent_updates=True doesn't let two handlers' drafts cancel each other.
    # Recursion (auto-retry calls _generate_llm_response from within itself)
    # must REUSE the parent's set, not overwrite it — otherwise the parent's
    # pending finalizers become unreachable from the outer cancel cleanup.
    _current_task = asyncio.current_task()
    bg_tasks = getattr(_current_task, '_llm_bg_tasks', None) if _current_task is not None else None
    if bg_tasks is None:
        bg_tasks = set()
        if _current_task is not None:
            try:
                _current_task._llm_bg_tasks = bg_tasks  # type: ignore[attr-defined]
            except AttributeError:
                pass

    def _track_task(coro):
        """Create a tracked fire-and-forget task."""
        task = asyncio.create_task(coro)
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return task

    enable_streaming = config.get_enable_streaming()
    if provider_config.get('enable_streaming') is False:
         enable_streaming = False

    MAX_TOOL_TURNS = config.get_chat_max_tool_turns()
    augmented_prompt = prompt
    if autosearch_enabled:
         search_instruction = "If you need to perform a web search for current information, include the search query inside <search> tags like <search>latest news on the Artemis mission</search>, but ALWAYS also provide your best answer based on your existing knowledge after the search tags."
         augmented_prompt = f"{search_instruction}\n\n{prompt}"

    for turn in range(MAX_TOOL_TURNS):
        # 1. Use the pre-fetched tool list (avoid redundant MCP round-trips per turn)
        tools = list(_mcp_tools) + list(_skill_tools)
            
        # 2. Call LLM
        raw_full_llm_response = ""
        llm_error_reported_by_model = False
        
        # We need a draft ID for this turn
        draft_id = random.randint(100000, 999999)
        last_draft_time = time.time()
        draft_throttle_seconds = config.get_chat_draft_throttle_seconds()
        
        # active_draft_id is scoped to THIS asyncio task (not chat_data) so
        # concurrent handlers in the same chat don't trample each other's
        # streaming drafts. The previous chat-wide slot was the same race
        # class as bg_tasks/pending_pk (fixes #5/#6).
        if enable_streaming:
            old_draft_id = getattr(_current_task, '_active_draft_id', None) if _current_task is not None else None
            if old_draft_id is not None:
                _track_task(finalize_draft(context, chat_id, old_draft_id))
            if _current_task is not None:
                try:
                    _current_task._active_draft_id = draft_id  # type: ignore[attr-defined]
                except AttributeError:
                    pass

        def _is_still_my_draft() -> bool:
            return (
                _current_task is not None
                and getattr(_current_task, '_active_draft_id', None) == draft_id
            )

        try:
            logger.info(f"{log_prefix}Starting LLM generation (Turn {turn})...")
            # Inactivity watchdog: bound the gap *between* streamed chunks, not the
            # total runtime. Each chunk re-arms the deadline, so a healthy long
            # generation is never cut off — only a genuinely stalled stream (no
            # output for idle_timeout seconds) is aborted. Streaming-only: a
            # non-streaming single-shot provider yields once at the end and is
            # left to its own client read-timeout (we can't tell slow from stuck).
            idle_timeout = config.get_generation_idle_timeout_seconds()
            _watchdog_active = enable_streaming and isinstance(idle_timeout, (int, float)) and idle_timeout > 0
            _stalled = False
            _agen = service.generate_response(
                model=model_to_use,
                prompt=augmented_prompt,
                context_history=truncated_history,
                tools=tools if tools else None
            )
            while True:
                try:
                    if _watchdog_active:
                        chunk = await asyncio.wait_for(_agen.__anext__(), timeout=idle_timeout)
                    else:
                        chunk = await _agen.__anext__()
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning(f"{log_prefix}Turn {turn}: generation stalled — no output for {idle_timeout}s. Aborting turn.")
                    try:
                        await _agen.aclose()  # release the provider's streaming connection
                    except Exception:
                        pass
                    _stalled = True
                    break

                raw_full_llm_response += chunk

                if enable_streaming and (time.time() - last_draft_time) > draft_throttle_seconds:
                    if _is_still_my_draft():
                        _track_task(send_draft_message(context, chat_id, draft_id, raw_full_llm_response + " █"))
                    last_draft_time = time.time()

            if _stalled:
                # Surface as an error sentinel so it flows through the normal
                # "[Error: ...]" delivery path (and triggers one auto-retry if enabled).
                raw_full_llm_response = (
                    f"[Error: The model stopped responding (no output for {idle_timeout}s). "
                    f"Please try again, or switch models with /provider.]"
                )
                llm_error_reported_by_model = True

            if enable_streaming and _is_still_my_draft():
                _track_task(finalize_draft(context, chat_id, draft_id))
                if _current_task is not None:
                    try:
                        _current_task._active_draft_id = None  # type: ignore[attr-defined]
                    except AttributeError:
                        pass

            # Check the fully-assembled response for the error sentinel — only the
            # bracket-delimited form [Error: ...] is the provider sentinel; checking
            # individual streaming chunks risks false positives on partial sentences.
            if is_error_response(raw_full_llm_response.lstrip()):
                llm_error_reported_by_model = True

            if llm_error_reported_by_model:
                break

            logger.info(f"{log_prefix}LLM generation complete for Turn {turn}. Length: {len(raw_full_llm_response)}")
            
        except Exception as e:
            logger.exception(f"{log_prefix}Critical error during LLM stream: {e}")
            raw_full_llm_response = "[Error: An unexpected error occurred while communicating with the AI.]"
            llm_error_reported_by_model = True
            break
            
        # Check if the output is a tool call request.
        # Use rfind to handle thinking/reasoning models (e.g. Gemini Flash Thinking) that
        # emit text before the tool-call JSON, producing: "reasoning text...{\"tool_calls\":[...]}"
        is_tool_call = False
        parsed_tool_calls = []
        try:
            cleaned_response = raw_full_llm_response.strip()
            json_start = cleaned_response.rfind('{"tool_calls"')
            if json_start >= 0:
                parsed = json.loads(cleaned_response[json_start:])
                if isinstance(parsed, dict) and "tool_calls" in parsed:
                    is_tool_call = True
                    parsed_tool_calls = parsed["tool_calls"]
        except json.JSONDecodeError as e:
            logger.warning(f"{log_prefix}Turn {turn}: Malformed tool_calls JSON at pos {json_start}: {e}. Treating as plain text.")
        except Exception as e:
            logger.exception(f"{log_prefix}Turn {turn}: Unexpected error parsing tool call response: {e}")

        # Fallback: detect NVIDIA nemotron XML-style tool calls (<tool_call><function=...>)
        if not is_tool_call and '<tool_call>' in cleaned_response:
            xml_calls = _parse_xml_tool_calls(cleaned_response)
            if xml_calls:
                is_tool_call = True
                parsed_tool_calls = xml_calls
                logger.info(f"{log_prefix}Turn {turn}: XML tool call format detected, parsed {len(xml_calls)} call(s).")

        if is_tool_call and parsed_tool_calls:
            # Names at INFO; full arguments (may contain user content / queries) at DEBUG
            # to avoid PII in retained logs of a multi-user bot.
            _tc_names = [tc.get('function', {}).get('name') for tc in parsed_tool_calls]
            logger.info(f"{log_prefix}Turn {turn}: {len(parsed_tool_calls)} tool call(s) requested: {_tc_names}")
            logger.debug(f"{log_prefix}Turn {turn}: tool call args: {parsed_tool_calls}")
            
            # If we used an augmented user prompt, we must append it to context history
            if augmented_prompt:
                truncated_history.append({"role": "user", "content": augmented_prompt})
                augmented_prompt = None # Reset so we don't pass it again
                
            # Defensively call save_message
            save_kwargs = {}
            if config.get_storage_backend() == "database":
                save_kwargs["tool_calls"] = parsed_tool_calls
            await storage_manager.save_message(chat_id, "assistant", None, **save_kwargs)
            
            # Add to memory history
            truncated_history.append({
                "role": "assistant",
                "content": None,
                "tool_calls": parsed_tool_calls
            })
            
            for tc in parsed_tool_calls:
                tc_id = tc.get("id")
                func = tc.get("function", {})
                tool_name = func.get("name")
                args_str = func.get("arguments", "{}")
                
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except Exception:
                    args = {"arguments": args_str}
                    
                # a. Send dynamic progress message
                progress_text = f"🔧 Executing {tool_name}..."
                temp_draft_id = random.randint(100000, 999999)
                await send_draft_message(context, chat_id, temp_draft_id, f"[{progress_text}]")
                
                # b. Validate tool call with hook_runner
                from utils.hooks import hook_runner
                tool_result = ""
                try:
                    hook_runner.run_pre_tool_use(tool_name, {"arguments": args})
                    
                    # c. Execute tool.
                    # skill_ checked BEFORE __ to prevent a skill named `server__foo`
                    # from being misrouted to MCP execution.
                    if tool_name.startswith("skill_"):
                        skill_name = tool_name[len("skill_"):]
                        if skill_service:
                            tool_result = skill_service.get_skill_playbook(skill_name)
                        else:
                            tool_result = "[Error: Skill Registry Service is not initialized]"
                    elif "__" in tool_name:
                        from utils.service_registry import touch_mcp_last_used
                        touch_mcp_last_used(app)  # keep watchdog from shutting down mid-call
                        parts = tool_name.split("__", 1)
                        server, tool = parts[0], parts[1]
                        if mcp_service:
                            tool_result = await mcp_service.execute_tool(server, tool, args)
                        else:
                            tool_result = "[Error: MCP Client Service is not initialized]"
                    else:
                        tool_result = f"[Error: Unknown tool namespace '{tool_name}']"
                        
                except PermissionError as pe:
                    logger.warning(f"{log_prefix}Hook runner blocked tool {tool_name}: {pe}")
                    tool_result = f"[Error: Permission denied by hook validation: {str(pe)}]"
                except Exception as e:
                    logger.exception(f"{log_prefix}Exception executing tool {tool_name}: {e}")
                    tool_result = f"[Error: Exception during tool execution: {str(e)}]"

                # Distill large tool results to only what's relevant to the user's query
                # BEFORE they enter context. Without this the agentic loop appended raw
                # results (a 100k+ char page) with no cap → overflow + grounding dilution.
                from utils.tool_distiller import distill_tool_result, frame_untrusted_tool_output
                tool_result = await distill_tool_result(tool_result, query=prompt, tool_name=tool_name)
                # Untrusted-data framing: mark tool output as data-not-instructions so
                # injected text in a web page / DB row / Notion doc can't steer the model
                # (indirect prompt-injection defense). Applied once, after distillation, and
                # persisted so reloaded history stays framed.
                tool_result = frame_untrusted_tool_output(tool_result)

                # finalize progress draft
                await finalize_draft(context, chat_id, temp_draft_id)
                
                # d. Save the system tool_result message
                save_tool_kwargs = {}
                if config.get_storage_backend() == "database":
                    save_tool_kwargs["tool_call_id"] = tc_id
                    
                await storage_manager.save_message(chat_id, "tool", tool_result, **save_tool_kwargs)
                
                # Add tool response to memory history
                truncated_history.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tool_name,
                    "content": tool_result
                })
                
            # e. Continue loop
            continue
            
        else:
            # Output is standard text: exit loop
            break
            
    else:
        logger.warning(f"{log_prefix}Maximum tool turns ({MAX_TOOL_TURNS}) reached — forcing final synthesis.")
        _synthesis_prompt = (
            "You have used the maximum number of tool-call rounds. "
            "Based on all the information gathered in this conversation, "
            "write a complete final answer to the user's question now. "
            "Do not request any more tools."
        )
        raw_full_llm_response = await _attempt_forced_synthesis(
            service, model_to_use, _synthesis_prompt, truncated_history, log_prefix,
            request_timeout=config.get_request_timeout_seconds(),
        ) or (
            f"[Warning: Reached the {MAX_TOOL_TURNS}-turn tool-call limit and the "
            f"forced synthesis also failed. Try /reroll or switch models with /provider.]"
        )

    # Strip <thinking> blocks BEFORE search tag extraction so that <search> tags
    # nested inside a model's internal monologue are never treated as real queries.
    pre_processed = re.sub(r'<thinking>.*?</thinking>\s*', '', raw_full_llm_response, flags=re.DOTALL).strip()
    final_content, extracted_search_queries = _extract_and_process_search_tags(pre_processed, autosearch_enabled, log_prefix)

    if not final_content:
        if not force_truncate and not llm_error_reported_by_model:
             logger.exception(f"{log_prefix}Empty response received from model. Retrying with forced context truncation...")
             return await _generate_llm_response(context, chat_id, prompt, is_reroll, force_truncate=True, operation_id=operation_id, is_retry=is_retry)
        
        final_content = "[Error: The AI returned an empty response. This might be due to a content filter or an issue with the selected model. Please try rerolling or using a different model.]"
        llm_error_reported_by_model = True

    # Auto-retry: If we got an error and this is not already a retry, check the user setting
    if llm_error_reported_by_model and not is_retry:
        auto_retry = await storage_manager.get_user_setting(
            chat_id, 'auto_retry_on_error',
            USER_SETTINGS['auto_retry_on_error']['default']
        )
        if auto_retry:
            # Back off before retrying so we don't immediately re-hit a model that's
            # overloaded/rate-limited (the provider services already retry transient 5xx
            # internally; this guards the app-level retry against hammering).
            backoff = config.get_server_error_backoff_seconds()
            logger.warning(f"{log_prefix}LLM error detected. Backing off {backoff}s then auto-retrying once...")
            await asyncio.sleep(backoff)
            return await _generate_llm_response(
                context, chat_id, prompt, is_reroll,
                force_truncate=force_truncate,
                operation_id=operation_id,
                is_retry=True
            )

    return {
        'content': final_content,
        'error': 'llm_error' if llm_error_reported_by_model else None,
        'truncated_history': truncated_history,
        'provider_info': provider_info,
        'search_queries': extracted_search_queries,
        'processed_history': processed_history
    }

async def _generate_and_send_response_task(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, prompt: str, current_thread_id: str, is_reroll: bool = False, force_truncate: bool = False, placeholder_message = None, skip_save: bool = False, save_input: bool = True) -> None:
    log_prefix = f"(Chat {chat_id}) "

    # Pending-PK is attached to *this* task object (not chat_data) so concurrent
    # handlers in the same chat don't race for a single shared slot. /cancel
    # reads the PK from the task it just cancelled, guaranteeing it deletes the
    # row that belongs to that specific generation.
    pending_pk = None
    _current_task = asyncio.current_task()

    # --- Archival Step 1: Secure the Input ---
    # save_input=False means the user prompt is already persisted (recovery): skip
    # re-saving it, but still generate and save the assistant reply (Step 2).
    if not skip_save and save_input:
        try:
            if is_reroll:
                # For reroll, we remove the faulty previous answer so the prompt is now the last message
                await storage_manager.remove_last_assistant_message(chat_id)
                # No new user prompt → no PK to track
            else:
                # For normal messages, we APPEND the user prompt immediately
                pending_pk = await storage_manager.save_message(chat_id, 'user', prompt)
                if _current_task is not None:
                    try:
                        _current_task._pending_user_message_pk = pending_pk  # type: ignore[attr-defined]
                    except AttributeError:
                        pass
        except Exception as e:
            logger.exception(f"{log_prefix}Failed to save/update initial state: {e}")
            await send_safe_message(context, update, "⚠️ An error occurred while saving your message. Please try again.", chat_id=chat_id)
            return
    else:
        logger.info(f"{log_prefix}Skipping input archival (skip_save={skip_save}, save_input={save_input})")

    # --- Generate ---
    response_data = await _generate_llm_response(context, chat_id, prompt, is_reroll, force_truncate)

    if response_data.get('error') == 'context_limit_exceeded':
        # This logic remains in the handler as it's specific to the chat workflow
        await send_safe_message(context, update, "Context window is full. Please use /config to manage conversation history.", chat_id=chat_id)
        return

    if response_data.get('search_queries'):
        # Inline import prevents circular dependency since misc_commands imports _generate_and_send_response
        from .handlers import misc_commands
        logger.info(f"{log_prefix}Auto-search triggered. Delegating to search_command: {response_data['search_queries']}")
        # Pass chat_id/user_id explicitly so the search path works headlessly
        # (update is None during startup recovery take-over).
        await misc_commands.search_command(
            update,
            context,
            placeholder_message,
            skip_save=skip_save,
            automated=True,
            fallback_content=response_data.get('content'),
            search_queries=response_data['search_queries'],
            original_prompt=prompt,
            chat_id=chat_id,
            user_id=user_id
        )
        return

    final_content = response_data.get('content', "[Error: Empty response from AI]")

    # The handler is now responsible for placeholder deletion
    if placeholder_message:
        try:
            await placeholder_message.delete()
        except Exception as e:
            logger.exception(f"{log_prefix}Failed to delete placeholder message: {e}")

    # Centralized, safe sending
    try:
        message_sent_successfully = await send_safe_message(context, update, final_content, chat_id=chat_id)
    except Exception as e:
        logger.exception(f"{log_prefix}Failed to send message: {e}")
        message_sent_successfully = False


    # --- Archival Step 2: Secure the Output ---
    # CRITICAL: the assistant message has already been sent to the user. If
    # /cancel arrives between send_safe_message and save_message, the user
    # sees content that never landed in history — /reroll then shows the
    # *prior* turn, confusing the user. The previous guard was
    # `asyncio.current_task().cancelled()`, which always returns False from
    # within the task (it only flips True after the task has finished).
    # Use asyncio.shield() so the save survives cancellation; we still
    # propagate CancelledError so the wrapper sees the task as cancelled.
    if not skip_save and response_data.get('error') is None and message_sent_successfully:
        try:
            await asyncio.shield(
                storage_manager.save_message(chat_id, 'assistant', final_content)
            )
            logger.info(f"{log_prefix}Assistant response saved to archive.")
            # Clear pending PK — interaction block is now complete and stable
            if _current_task is not None:
                try:
                    _current_task._pending_user_message_pk = None  # type: ignore[attr-defined]
                except AttributeError:
                    pass
        except asyncio.CancelledError:
            # The save itself was not cancelled (shield ensures completion);
            # this CancelledError comes from the wrapper. Re-raise.
            raise
        except Exception as e_hist:
            # The answer was already delivered but could NOT be persisted (e.g. the DB write
            # failed after retries). This used to be logged only — a silent history gap the
            # user couldn't notice, so the next turn would be missing this exchange. Surface
            # it so the loss is visible and the user can retry / check.
            logger.exception(f"{log_prefix}Failed to save assistant response: {e_hist}")
            try:
                await send_plain_message(
                    context, chat_id,
                    "⚠️ I answered above, but couldn't save this turn to history — it may not "
                    "appear in my context next time. If continuity matters, please resend."
                )
            except Exception as _notify_err:
                logger.error(f"{log_prefix}Also failed to notify user of save failure: {_notify_err}")
    elif skip_save:
        logger.info(f"{log_prefix}Skipping output archival (skip_save=True)")
