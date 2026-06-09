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
import tiktoken
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter, TimedOut, BadRequest
import config
from bot import providers
from services import ollama_service, gemini_service
from services.openai_compatible_service import OpenAICompatibleService

from storage import storage_manager
from bot.messaging import send_safe_message, finalize_draft, send_draft_message
from utils.context_manager import ensure_context_fits
from bot.settings import USER_SETTINGS

logger = logging.getLogger(__name__)


def _build_tool_catalog_section(mcp_tools: list, skill_tools: list) -> str:
    """
    Builds a markdown section listing active MCP servers and skills for injection
    into the system prompt.  Grouped by server so the model understands what
    each data source is for; individual tool schemas are already passed via the
    tools= API parameter.
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

    if skill_tools:
        lines.append("## Skills\n")
        for tool in skill_tools:
            fn = tool["function"]
            desc = fn.get("description", "")
            lines.append(f"- **{fn['name']}** — {desc}" if desc else f"- **{fn['name']}**")
        lines.append("")

    return "\n".join(lines)

async def _generate_and_send_response(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, prompt: str, current_thread_id: str, is_reroll: bool = False, force_truncate: bool = False, placeholder_message = None, skip_save: bool = False, task_key: str = 'llm_task') -> None:
    """Wraps the response generation in a cancellable task."""
    
    # SYSTEMIC FIX: Defensively cancel any existing task on this key to prevent zombie leak
    old_task = context.chat_data.get(task_key)
    if old_task and not old_task.done():
        logger.warning(f"(Chat {chat_id}) Systemic Concurrency Catch: Cancelling zombie '{task_key}' before spinning up new task.")
        old_task.cancel()

    task = asyncio.create_task(
        _generate_and_send_response_task(update, context, chat_id, user_id, prompt, current_thread_id, is_reroll, force_truncate, placeholder_message, skip_save)
    )
    context.chat_data[task_key] = task
    try:
        await task
    except asyncio.CancelledError:
        logger.info(f"(Chat {chat_id}) LLM task '{task_key}' was cancelled cleanly.")
        # Cleanup any orphaned background tasks (draft finalization, etc.)
        bg_tasks = context.chat_data.get('_bg_tasks', set())
        for t in list(bg_tasks):
            if not t.done():
                t.cancel()

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
        safety_margin=safety_margin
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

    # Inject live tool catalog into the system message so the model knows
    # what servers are connected and can make informed routing decisions.
    catalog_section = _build_tool_catalog_section(_mcp_tools, _skill_tools)
    if catalog_section:
        # truncated_history[0] is the system message (preserved by ensure_context_fits)
        if truncated_history and truncated_history[0].get("role") == "system":
            truncated_history[0] = {
                **truncated_history[0],
                "content": truncated_history[0]["content"] + catalog_section,
            }

    raw_full_llm_response = ""
    llm_error_reported_by_model = False

    # Tracked background tasks set for safe cleanup on cancellation
    bg_tasks = context.chat_data.setdefault('_bg_tasks', set())

    def _track_task(coro):
        """Create a tracked fire-and-forget task."""
        task = asyncio.create_task(coro)
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return task

    enable_streaming = config.get_enable_streaming()
    if provider_config.get('enable_streaming') is False:
         enable_streaming = False

    MAX_TOOL_TURNS = 5
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
        draft_throttle_seconds = 0.5
        
        if enable_streaming:
            old_draft_id = context.chat_data.get('active_draft_id')
            if old_draft_id is not None:
                _track_task(finalize_draft(context, chat_id, old_draft_id))
            context.chat_data['active_draft_id'] = draft_id
            
        try:
            logger.info(f"{log_prefix}Starting LLM generation (Turn {turn})...")
            async for chunk in service.generate_response(
                model=model_to_use,
                prompt=augmented_prompt,
                context_history=truncated_history,
                tools=tools if tools else None
            ):
                raw_full_llm_response += chunk
                
                if enable_streaming and (time.time() - last_draft_time) > draft_throttle_seconds:
                    if context.chat_data.get('active_draft_id') == draft_id:
                        _track_task(send_draft_message(context, chat_id, draft_id, raw_full_llm_response + " █"))
                    last_draft_time = time.time()
                    
            if enable_streaming and context.chat_data.get('active_draft_id') == draft_id:
                _track_task(finalize_draft(context, chat_id, draft_id))
                context.chat_data.pop('active_draft_id', None)

            # Check the fully-assembled response for the error sentinel — only the
            # bracket-delimited form [Error: ...] is the provider sentinel; checking
            # individual streaming chunks risks false positives on partial sentences.
            if raw_full_llm_response.lstrip().startswith("[Error:"):
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

        if is_tool_call and parsed_tool_calls:
            logger.info(f"{log_prefix}Turn {turn}: Tool call request detected: {parsed_tool_calls}")
            
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
        logger.warning(f"{log_prefix}Maximum tool turns (5) reached!")
        raw_full_llm_response = "[Warning: Maximum tool execution depth of 5 turns reached to prevent runaway billing or infinite loops.]"

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
            logger.warning(f"{log_prefix}LLM error detected. Auto-retrying once...")
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

async def _generate_and_send_response_task(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, prompt: str, current_thread_id: str, is_reroll: bool = False, force_truncate: bool = False, placeholder_message = None, skip_save: bool = False) -> None:
    log_prefix = f"(Chat {chat_id}) "

    # --- Archival Step 1: Secure the Input ---
    if not skip_save:
        try:
            if is_reroll:
                # For reroll, we remove the faulty previous answer so the prompt is now the last message
                await storage_manager.remove_last_assistant_message(chat_id)
                # We don't save a new prompt, so there's no PK to track for cancellation cleanup
                context.chat_data['pending_user_message_pk'] = None
            else:
                # For normal messages, we APPEND the user prompt immediately
                pk = await storage_manager.save_message(chat_id, 'user', prompt)
                context.chat_data['pending_user_message_pk'] = pk
        except Exception as e:
            logger.exception(f"{log_prefix}Failed to save/update initial state: {e}")
            await send_safe_message(context, update, "⚠️ An error occurred while saving your message. Please try again.")
            return
    else:
        logger.info(f"{log_prefix}Skipping input archival (skip_save=True)")

    # --- Generate ---
    response_data = await _generate_llm_response(context, chat_id, prompt, is_reroll, force_truncate)

    if response_data.get('error') == 'context_limit_exceeded':
        # This logic remains in the handler as it's specific to the chat workflow
        await send_safe_message(context, update, "Context window is full. Please use /config to manage conversation history.")
        return

    if response_data.get('search_queries'):
        # Inline import prevents circular dependency since misc_commands imports _generate_and_send_response
        from .handlers import misc_commands
        logger.info(f"{log_prefix}Auto-search triggered. Delegating to search_command: {response_data['search_queries']}")
        await misc_commands.search_command(
            update, 
            context, 
            placeholder_message, 
            skip_save=skip_save, 
            automated=True, 
            fallback_content=response_data.get('content'),
            search_queries=response_data['search_queries'],
            original_prompt=prompt
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
        message_sent_successfully = await send_safe_message(context, update, final_content)
    except Exception as e:
        logger.exception(f"{log_prefix}Failed to send message: {e}")
        message_sent_successfully = False


    # Check if the task was cancelled before saving history
    if asyncio.current_task().cancelled():
        logger.info(f"{log_prefix}Task was cancelled, skipping history update.")
        return

    # --- Archival Step 2: Secure the Output ---
    if not skip_save and response_data.get('error') is None and message_sent_successfully:
        try:
            await storage_manager.save_message(chat_id, 'assistant', final_content)
            logger.info(f"{log_prefix}Assistant response saved to archive.")
            # Clear pending PK since the interaction block is now complete and stable
            context.chat_data.pop('pending_user_message_pk', None)
        except Exception as e_hist:
            logger.exception(f"{log_prefix}Failed to save assistant response: {e_hist}")
    elif skip_save:
        logger.info(f"{log_prefix}Skipping output archival (skip_save=True)")
    
    # Final safety cleanup for any leaked keys (e.g. if error prevented saving)
    context.chat_data.pop('pending_user_message_pk', None)
