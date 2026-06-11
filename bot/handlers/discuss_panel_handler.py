import logging
import re
import asyncio
import telegram
import json
from telegram import Update, BotCommand
from telegram.error import BadRequest
from telegram.error import TimedOut
from httpx import ConnectTimeout
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram import BotCommandScopeChat

from utils import text_processing
from utils.hooks import hook_runner
from utils.llm_utilities import get_robust_llm_response, get_expert_panel_fallback_config
from telegram import constants
import config
from bot import providers
from services import web_search_service
from bot.menu_setup import setup_bot_commands_and_menu
from storage import storage_manager
from bot.settings import USER_SETTINGS  # Added for settings access
from bot.messaging import send_safe_message, send_plain_message
from bot.handlers.configure_panel_handler import load_panel_config
from utils.context_manager import ensure_context_fits, get_model_context_limits, truncate_text_to_tokens
from .misc_commands import cancel_command

# Per-chat panel locks. Stored at module scope (not in user_data) so the Lock's
# event-loop binding stays consistent with the live loop. If PTB persistence
# were ever enabled, asyncio primitives in user_data would carry a dead loop
# binding after a polling-loop restart. The dict is repopulated lazily.
# Reset by reset_panel_locks() in cleanup_services on shutdown.
_panel_locks: dict[int, asyncio.Lock] = {}


def _get_panel_lock(chat_id: int) -> asyncio.Lock:
    """Lazily create and cache a per-chat asyncio.Lock bound to the live loop."""
    lock = _panel_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _panel_locks[chat_id] = lock
    return lock


def reset_panel_locks() -> None:
    """Discard all cached panel locks. Call on polling-loop restart so the next
    iteration creates fresh locks bound to the new event loop."""
    _panel_locks.clear()
from bot.errors import ProviderUnavailableError

# Define conversation states
AWAITING_FOLLOW_UP, PANEL_IN_PROGRESS = range(2)

logger = logging.getLogger(__name__)


async def _plan_deep_dive_searches(
    orchestrator_provider: str,
    orchestrator_model: str,
    user_prompt: str,
    original_query: str,
    initial_results: str,
    timeout: int,
    fallback_provider: str,
    fallback_model: str
) -> list[str]:
    """
    Uses an LLM to plan deep-dive search queries based on initial search results.
    """
    logger.info("Planning deep-dive searches...")
    plan_prompt_template = config.PROMPTS.get_prompt('panel_orchestrator_analyze')
    plan_prompt = plan_prompt_template.format(
        user_prompt=user_prompt,
        original_query=original_query,
        search_results=initial_results
    )

    llm_result = await get_robust_llm_response(
        provider_name=orchestrator_provider,
        model=orchestrator_model,
        prompt=plan_prompt,
        history=None,
        role_name='Deep Dive Planner',
        request_timeout=timeout,
        fallback_provider=fallback_provider,
        fallback_model=fallback_model
    )
    
    response_text = llm_result['response']
    if llm_result['is_error']:
        logger.error(f"Deep-dive planning failed: {response_text}")
        return []

    try:
        # Strategy 1: Look for a JSON array within a markdown code block
        match = re.search(r"```json\s*(\[.*?\])\s*```", response_text, re.DOTALL)
        if not match:
            # Try without the json specifier
            match = re.search(r"```\s*(\[.*?\])\s*```", response_text, re.DOTALL)

        json_str = ""
        if match:
            json_str = match.group(1)
        else:
            # Strategy 2: Fallback to finding the first '[' and last ']'
            start_index = response_text.find('[')
            end_index = response_text.rfind(']')
            if start_index != -1 and end_index != -1 and end_index > start_index:
                json_str = response_text[start_index:end_index+1]

        if not json_str:
            # If no JSON is found, log and return empty list
            logger.error("No valid JSON array found in the planner's response.")
            logger.debug(f"Problematic response: {response_text}")
            return []

        # The response is expected to be a JSON list of strings
        deep_dive_queries = json.loads(json_str)
        if isinstance(deep_dive_queries, list) and all(isinstance(q, str) for q in deep_dive_queries):
            logger.info(f"Planned {len(deep_dive_queries)} deep-dive searches.")
            return deep_dive_queries
        else:
            logger.error(f"Deep-dive planning returned invalid format: {json_str}")
            return []
    except json.JSONDecodeError:
        logger.error(f"Failed to decode JSON from deep-dive planner: {json_str}")
        return []

def _format_tools_for_plan_prompt(tools: list) -> str:
    """Minimal tool listing grouped by MCP server for the Initial Orchestrator plan prompt.
    Grouping by server helps the Planner distinguish workspace tools (notion-workspace)
    from web search tools (tavily-search) and route workspace_queries vs requires_search correctly.
    """
    if not tools:
        return "(none)"
    groups: dict = {}
    for t in tools:
        fn = t.get('function', {})
        name = fn.get('name', '?')
        desc = fn.get('description', '')
        server = name.split('__')[0] if '__' in name else 'other'
        groups.setdefault(server, []).append(f"  - {name}: {desc}")
    lines = []
    for server, tool_lines in groups.items():
        lines.append(f"[{server}]")
        lines.extend(tool_lines)
    return "\n".join(lines)


def _format_tools_for_prompt(tools: list) -> str:
    """Formats a list of OpenAI-style tool dicts into a readable string for LLM prompts.

    Includes parameter names and types so the Orchestrator knows how to call each tool,
    not just that it exists. Without schema info the model defaults to guessing 'query'
    for all tools, which only works for search-style tools.
    """
    if not tools:
        return "No tools available."
    lines = []
    for t in tools:
        func = t.get('function', {})
        name = func.get('name', '')
        desc = func.get('description', '')
        params = func.get('parameters', {})
        properties = params.get('properties', {})
        required = set(params.get('required', []))

        if properties:
            param_parts = []
            for prop_name, prop_schema in properties.items():
                prop_type = prop_schema.get('type', 'any')
                req_marker = '*' if prop_name in required else '?'
                param_parts.append(f"{prop_name}{req_marker}: {prop_type}")
            args_str = ", ".join(param_parts)
            lines.append(f"- {name}({args_str}): {desc}")
        else:
            lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


async def _run_refinement_cycle(
    update: Update, context: ContextTypes.DEFAULT_TYPE, proposer_task, critic_task, user_prompt, full_history, placeholder_msg, panel_results,
    orchestrator_service, orchestrator_model, orchestrator_timeout, orchestrator_config, panel_config: dict,
    mcp_service=None, skill_service=None, available_tools_text: str = "No tools available.",
    panel_execution_tool_names: frozenset = frozenset(),
    quality_gate_tools_text: str = "No tools available."
):
    """
    Executes the Master & Apprentice iterative refinement cycle.
    The Orchestrator quality gate may request MCP tool calls to ground the next iteration.

    Returns:
        tuple: (proposer_response, quality_score, iteration_count)
    """
    # Extract configuration from user's panel_config
    quality_threshold = panel_config.get('quality_threshold', 85)
    max_iterations = panel_config.get('max_refinement_iterations', 3)
    role_configs = panel_config.get('roles', {})
    
    # Setup role configurations
    proposer_role_config = role_configs.get('Proposer', {})
    critic_role_config = role_configs.get('Critic', {})
    proposer_provider = proposer_role_config.get('provider')
    proposer_model = proposer_role_config.get('model')
    critic_provider = critic_role_config.get('provider')
    critic_model = critic_role_config.get('model')
    
    if not all([proposer_provider, proposer_model, critic_provider, critic_model]):
        raise RuntimeError("Proposer or Critic configuration is incomplete.")
    
    # Initialize iteration variables
    original_proposer_prompt = proposer_task.get('prompt') or proposer_task.get('content')
    current_proposer_prompt = original_proposer_prompt
    critic_prompt_template = critic_task.get('prompt') or critic_task.get('content')
    quality_score = 0
    proposer_response = ""

    # Best-response tracking: the Refiner receives the highest-scoring draft,
    # not the last one (which may be worse if the Proposer regressed due to timeouts/fallbacks).
    best_score = -1
    best_proposer_response = ""
    prev_score = -1
    consecutive_declines = 0
    _quality_gate_emergency = False  # True when the gate parse failed; score is synthetic

    # Stateful Persona History
    # quality_gate_history stores ONLY compact audit entries (score + instructions), never the
    # full proposer/critic response text. That text is already in each round's current prompt,
    # so re-embedding it in history causes context overflow and score anchoring.
    proposer_history = []
    critic_history = []
    quality_gate_history = []

    # Iterative refinement loop
    for iteration in range(1, max_iterations + 1):
        _quality_gate_emergency = False  # reset each iteration; True only when gate parse fails this round
        try:
            await placeholder_msg.edit_text(f"Round {iteration}/{max_iterations}: Proposer is working...", parse_mode=None)
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            logger.warning(f"Failed to update placeholder message (Proposer working): {e}")
        
        # Execute Proposer
        fallback_provider, fallback_model = get_expert_panel_fallback_config()
        proposer_llm_result = await get_robust_llm_response(
            provider_name=proposer_provider,
            model=proposer_model,
            prompt=current_proposer_prompt,
            history=proposer_history,
            role_name='Proposer',
            request_timeout=proposer_role_config.get('request_timeout_seconds'),
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        proposer_response = proposer_llm_result['response']
        proposer_retries = proposer_llm_result['retries']
        proposer_fallback_used = proposer_llm_result['fallback_used']

        proposer_history.append({"role": "user", "content": current_proposer_prompt})
        proposer_history.append({"role": "assistant", "content": proposer_response})

        # Update panel results with Proposer response
        panel_results['Proposer'] = {
            'provider': proposer_provider,
            'model': proposer_model,
            'status': 'Success' if not proposer_llm_result['is_error'] else 'Failure',
            'response': proposer_response,
            'retries': proposer_retries,
            'fallback_used': proposer_fallback_used
        }

        if proposer_llm_result['is_error']:
            logger.error(f"Proposer failed: {proposer_response}")
            logger.info("Attempting Orchestrator's backup fallback for failed proposer...")

            # Use the Orchestrator's backup (fallback_provider/fallback_model) - proper hierarchy
            if fallback_provider and fallback_model:
                proposer_template = config.PROMPTS.get_prompt('panel_proposer')
                fallback_proposer_prompt = proposer_template.format(
                    description=proposer_task.get('description', ''),
                    user_prompt=user_prompt
                )

                try:
                    fallback_llm_result = await get_robust_llm_response(
                        provider_name=fallback_provider,
                        model=fallback_model,
                        prompt=fallback_proposer_prompt,
                        history=proposer_history,
                        role_name='Backup Proposer',
                        request_timeout=orchestrator_timeout,
                        fallback_provider=None,  # No further fallback for backup
                        fallback_model=None
                    )
                    fallback_response = fallback_llm_result['response']
                    fallback_retries = fallback_llm_result['retries']
                    fallback_fallback_used = fallback_llm_result['fallback_used'] # This will always be False here

                    if not fallback_llm_result['is_error']:
                        logger.info("Orchestrator's backup successfully provided fallback response")
                        # Update panel results to reflect backup fallback
                        panel_results['Proposer'] = {
                            'provider': fallback_provider,
                            'model': fallback_model,
                            'status': 'Success (Backup Fallback)',
                            'response': fallback_response,
                            'retries': fallback_retries,
                            'fallback_used': True # Explicitly set to True as this is a fallback
                        }
                        proposer_response = fallback_response
                    else:
                        logger.error(f"Orchestrator's backup also failed: {fallback_response}")
                        return proposer_response, 0, iteration

                except Exception as fallback_error:
                    logger.exception(f"Orchestrator's backup failed with exception: {fallback_error}")
                    return proposer_response, 0, iteration
            else:
                logger.error("No fallback provider/model configured for Orchestrator. Cannot proceed with backup.")
                return proposer_response, 0, iteration
        
        try:
            await asyncio.wait_for(
                placeholder_msg.edit_text(f"Round {iteration}/{max_iterations}: Critic is reviewing...", parse_mode=None),
                timeout=8.0
            )
        except (asyncio.TimeoutError, telegram.error.TimedOut, telegram.error.NetworkError) as e:
            logger.warning(f"Timeout updating status to 'Critic reviewing' in round {iteration}: {e}")
        except Exception as e:
            logger.exception(f"Failed to update status to 'Critic reviewing' in round {iteration}: {e}")
        
        # Execute Critic
        critic_template = config.PROMPTS.get_prompt('panel_critic')
        enhanced_critic_prompt = critic_template.format(
            critic_prompt_template=critic_prompt_template,
            proposer_response=proposer_response,
            user_prompt=user_prompt
        )
        
        critic_llm_result = await get_robust_llm_response(
            provider_name=critic_provider,
            model=critic_model,
            prompt=enhanced_critic_prompt,
            history=critic_history,
            role_name='Critic',
            request_timeout=critic_role_config.get('request_timeout_seconds'),
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        critic_response = critic_llm_result['response']
        critic_retries = critic_llm_result['retries']
        critic_fallback_used = critic_llm_result['fallback_used']

        critic_history.append({"role": "user", "content": enhanced_critic_prompt})
        critic_history.append({"role": "assistant", "content": critic_response})

        panel_results['Critic'] = {
            'provider': critic_provider,
            'model': critic_model,
            'status': 'Success' if not critic_llm_result['is_error'] else 'Failure',
            'response': critic_response,
            'retries': critic_retries,
            'fallback_used': critic_fallback_used
        }

        # Handle Critic failure by proceeding to Quality Gate with modified prompt
        critic_failed = critic_llm_result['is_error']
        if critic_failed:
            logger.error(f"Critic failed: {critic_response}")
            # Replace critic response with failure explanation for the Master
            critic_response = "[The Critic agent failed to provide a review. Please assess the Proposer's work directly.]"
        
        try:
            await placeholder_msg.edit_text(f"Round {iteration}/{max_iterations}: Master is assessing quality...", parse_mode=None)
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            logger.warning(f"Failed to update placeholder message (Master assessing quality): {e}")
        
        quality_template = config.PROMPTS.get_prompt('panel_orchestrator_quality')
        quality_gate_prompt = quality_template.format(
            user_prompt=user_prompt,
            proposer_response=proposer_response,
            critic_response=critic_response,
            quality_threshold=quality_threshold,
            available_tools=quality_gate_tools_text
        )
        
        quality_llm_result = await get_robust_llm_response(
            provider_name=orchestrator_config.get('provider'),
            model=orchestrator_config.get('model'),
            prompt=quality_gate_prompt,
            history=quality_gate_history,
            role_name='Master Orchestrator',
            request_timeout=orchestrator_timeout,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        quality_response = quality_llm_result['response']
        quality_retries = quality_llm_result['retries']
        quality_fallback_used = quality_llm_result['fallback_used']

        # Compact audit entry — score + instructions only, no full response text
        quality_gate_history.append({"role": "user", "content": f"[Round {iteration} assessment request]"})
        quality_gate_history.append({"role": "assistant", "content": quality_response})

        # Store quality gate metrics in panel_results
        panel_results['Quality_Gate'] = {
            'provider': orchestrator_config.get('provider'),
            'model': orchestrator_config.get('model'),
            'status': 'Success' if not quality_llm_result['is_error'] else 'Failure',
            'response': quality_response,
            'retries': quality_retries,
            'fallback_used': quality_fallback_used
        }

        # Parse quality assessment using robust JSON extraction
        requested_tool_calls = []
        try:
            # Find the first '{' and the last '}' to extract the JSON block.
            # This is more robust against conversational text from the LLM.
            start_index = quality_response.find('{')
            end_index = quality_response.rfind('}')

            if start_index != -1 and end_index != -1 and end_index > start_index:
                json_str = quality_response[start_index:end_index+1]
                quality_assessment = json.loads(json_str)

                # Rubric schema: compute quality_score from sub-criteria (deterministic aggregation).
                # Fall back to legacy holistic quality_score field for backward compatibility.
                if 'scores' in quality_assessment and isinstance(quality_assessment['scores'], dict):
                    scores = quality_assessment['scores']
                    numeric_scores = {}
                    for k, v in scores.items():
                        if isinstance(v, (int, float)):
                            numeric_scores[k] = max(0, int(v))
                        else:
                            logger.warning(
                                f"Quality Gate returned non-numeric score for criterion '{k}': {v!r}. "
                                f"Treating as 0. Model may not be following the rubric schema."
                            )
                            numeric_scores[k] = 0
                    quality_score = sum(numeric_scores.values())
                    logger.info(
                        f"Master quality scores — "
                        f"grounding:{numeric_scores.get('factual_grounding', 0)} "
                        f"completeness:{numeric_scores.get('completeness', 0)} "
                        f"accuracy:{numeric_scores.get('accuracy', 0)} "
                        f"clarity:{numeric_scores.get('clarity', 0)} "
                        f"→ total:{quality_score}/{quality_threshold}"
                    )
                else:
                    quality_score = quality_assessment.get('quality_score', 0)
                    logger.info(f"Master quality assessment - Score: {quality_score}, Threshold: {quality_threshold}")

                # Use `or ''` rather than a default in .get() so that explicit JSON null
                # is also normalised to empty string (dict.get default only covers missing keys).
                refinement_instructions = quality_assessment.get('refinement_instructions') or ''
                requested_tool_calls = quality_assessment.get('tool_calls', [])
                if not isinstance(requested_tool_calls, list):
                    requested_tool_calls = []
                logger.info(f"Tool calls requested: {len(requested_tool_calls)}")
            else:
                raise ValueError("No valid JSON object found in the quality gate response.")

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Quality gate parsing failed: {e}")
            logger.debug(f"Problematic quality response (first 500 chars): {quality_response[:500]}")
            logger.warning("Quality gate failed, using emergency fallback to break loop.")
            quality_score = quality_threshold  # Set to threshold to break loop
            refinement_instructions = ""
            _quality_gate_emergency = True

        # Track the best response seen across all iterations.
        # The Proposer can regress when its model times out and the fallback takes over,
        # causing later iterations to score LOWER than earlier ones. We give the Refiner
        # the best draft, not the last one.
        # Skip update when quality_score is a synthetic emergency value — it is not a real measurement.
        if not _quality_gate_emergency and quality_score > best_score:
            best_score = quality_score
            best_proposer_response = proposer_response

        # Early-termination: if the score declines two iterations in a row the loop is
        # converging in the wrong direction. Stop now and use the best response we saw.
        _prev_for_log = prev_score
        if prev_score >= 0 and quality_score < prev_score:
            consecutive_declines += 1
        else:
            consecutive_declines = 0
        prev_score = quality_score

        if consecutive_declines >= 2:
            logger.warning(
                f"Quality declining for 2 consecutive iterations "
                f"(now {quality_score}, was {_prev_for_log}). "
                f"Stopping early; using best response (score {best_score})."
            )
            break

        # Check if quality meets threshold
        if quality_score >= quality_threshold:
            logger.info(f"Quality threshold met (Score: {quality_score} >= {quality_threshold}). Finalizing response.")
            break
        elif iteration < max_iterations:
            # Execute any tool calls the Orchestrator requested to ground the next Proposer iteration
            tool_results_text = ""
            if requested_tool_calls and (mcp_service or skill_service):
                tool_results_parts = []
                for tc in requested_tool_calls:
                    tool_name = tc.get('name', '')
                    args = tc.get('arguments', {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Panel tool '{tool_name}': failed to parse arguments JSON ({e}). Using empty args.")
                            args = {}
                    try:
                        # skill_ prefix checked BEFORE __ to prevent a skill named `server__foo`
                        # from being misrouted to MCP execution.
                        if tool_name.startswith("skill_") and skill_service:
                            skill_name_str = tool_name[len("skill_"):]
                            result = skill_service.get_skill_playbook(skill_name_str)
                            logger.info(f"Panel skill '{tool_name}' executed for iteration {iteration + 1}.")
                        elif "__" in tool_name and mcp_service:
                            server, tool = tool_name.split("__", 1)
                            # Gate 1: authority allowlist — only panel_execution: true servers.
                            # Empty frozenset means no servers are authorized → deny all (fail-closed).
                            if not panel_execution_tool_names:
                                logger.warning(
                                    f"Panel: Gate 1 has no authorized tools (empty authority set). "
                                    f"Denying '{tool_name}'. Set panel_execution: true in config.yaml."
                                )
                                result = f"[Denied: Panel tool authority set is empty. Check config.yaml panel_execution flags.]"
                            elif tool_name not in panel_execution_tool_names:
                                logger.warning(f"Panel: Orchestrator requested unauthorized tool '{tool_name}' — blocked by authority policy.")
                                result = f"[Denied: '{tool_name}' is not authorised in the panel context.]"
                            else:
                                # Gate 2: hook validation — same path as normal chat tool execution
                                try:
                                    hook_runner.run_pre_tool_use(tool_name, {"arguments": args})
                                except PermissionError as hook_err:
                                    logger.warning(f"Panel: Tool '{tool_name}' denied by security hook: {hook_err}")
                                    result = f"[Denied by security hook: {hook_err}]"
                                else:
                                    from utils.service_registry import touch_mcp_last_used
                                    touch_mcp_last_used(getattr(context, 'application', None))
                                    result = await mcp_service.execute_tool(server, tool, args)
                                    logger.info(f"Panel tool '{tool_name}' executed for iteration {iteration + 1}.")
                        else:
                            result = f"[Error: Unknown tool or service unavailable for '{tool_name}']"
                        # Truncate large results to prevent context overflow in the next Proposer call.
                        # Web search / DB results can be 100k+ characters; cap at ~8 000 chars (~2 000 tokens).
                        _MAX_RESULT_CHARS = 8_000
                        if isinstance(result, str) and len(result) > _MAX_RESULT_CHARS:
                            logger.warning(f"Panel tool '{tool_name}' result truncated from {len(result)} to {_MAX_RESULT_CHARS} chars.")
                            result = result[:_MAX_RESULT_CHARS] + "\n[Result truncated to prevent context overflow]"
                        tool_results_parts.append(f"Tool: {tool_name}\nResult: {result}")
                    except Exception as tool_exc:
                        logger.exception(f"Panel tool call failed for '{tool_name}': {tool_exc}")
                        tool_results_parts.append(f"Tool: {tool_name}\nResult: [Error: {tool_exc}]")

                if tool_results_parts:
                    tool_results_text = "\n\n".join(tool_results_parts)
                    logger.info(f"Panel orchestrator provided {len(tool_results_parts)} tool result(s) to Proposer for iteration {iteration + 1}.")
                    # Feed compact results into quality_gate_history so the next Quality Gate
                    # invocation knows which queries succeeded or failed (e.g. "no such table").
                    # Without this, the gate repeats identical failing queries every round.
                    _qg_summary = tool_results_text[:1500] + ("\n[truncated]" if len(tool_results_text) > 1500 else "")
                    quality_gate_history.append({
                        "role": "user",
                        "content": f"[Tool execution results from Round {iteration}]:\n{_qg_summary}"
                    })

            try:
                await placeholder_msg.edit_text(f"Quality score: {quality_score}/{quality_threshold}. Refining... (Round {iteration+1})", parse_mode=None)
            except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                logger.warning(f"Failed to update placeholder message (Refining): {e}")
            proposer_refine_template = config.PROMPTS.get_prompt('panel_proposer_refine')
            current_proposer_prompt = proposer_refine_template.format(
                user_prompt=user_prompt,
                proposer_response=proposer_response,
                quality_score=quality_score,
                refinement_instructions=refinement_instructions,
                tool_results=tool_results_text
            )
        else:
            logger.warning(f"Max iterations reached. Final quality score: {quality_score}")
            break

    # Return the best-scoring response, not necessarily the last one.
    if best_proposer_response:
        if best_score != quality_score:
            logger.info(f"Using best response from earlier iteration (score {best_score} vs final {quality_score}).")
        return best_proposer_response, best_score, iteration
    return proposer_response, quality_score, iteration


async def set_panel_commands(application, chat_id: int) -> None:
    """Sets the bot's command list to panel-specific commands."""
    panel_commands = [
        BotCommand("reroll", "Rerun the last panel turn"),
        BotCommand("search", "Inject web search results into the discussion"),
        BotCommand("end_discussion", "End the current panel discussion"),
        BotCommand("cancel", "Cancel the current operation"), # Add this line
    ]
    try:
        await application.bot.set_my_commands(
            commands=panel_commands,
            scope=BotCommandScopeChat(chat_id)
        )
        logger.info(f"Set panel-specific commands for chat {chat_id}")
    except Exception as e:
        logger.exception(f"Failed to set panel-specific commands for chat {chat_id}: {e}")


def _format_panel_summary(panel_results: dict) -> str:
    """Formats the results of the panel execution into a pure markdown string."""
    summary_parts = ["Panel Execution Summary:"]

    quality_metrics = panel_results.get('Quality_Metrics', {})

    for role, result in panel_results.items():
        if role == 'Quality_Metrics':
            continue

        status_icon = "✅" if result.get('status').startswith('Success') else "⚠️"
        provider = result.get('provider', 'Unknown')
        model = result.get('model', 'Unknown')
        status = result.get('status', 'Unknown')
        
        # Get retry and fallback data
        retries = result.get('retries', 0)
        fallback_used = result.get('fallback_used', False)
        
        extra_info = []
        if retries > 0:
            extra_info.append(f"{retries} retries")
        if fallback_used:
            extra_info.append("fallback used")
        
        extra_info_str = f" ({', '.join(extra_info)})" if extra_info else ""
        
        summary_parts.append(f"{status_icon} {role}: {provider}/{model} ({status}){extra_info_str}")

    if quality_metrics:
        final_score = quality_metrics.get('final_score', 'N/A')
        threshold = quality_metrics.get('threshold', 'N/A')
        iterations_used = quality_metrics.get('iterations_used', 'N/A')
        max_iterations = quality_metrics.get('max_iterations', 'N/A')

        quality_icon = "🎯" if isinstance(final_score, (int, float)) and isinstance(threshold, (int, float)) and final_score >= threshold else "📈"

        summary_parts.append("")
        summary_parts.append("Quality Metrics:")
        summary_parts.append(f"{quality_icon} Final Score: {final_score}/{threshold} (Achieved/Threshold)")
        summary_parts.append(f"🔄 Refinement Rounds: `{iterations_used}/{max_iterations}` (Used/Max)")

    return "\n".join(summary_parts)

async def _run_panel_workflow(update: Update, context: ContextTypes.DEFAULT_TYPE, user_prompt: str, full_history: list, placeholder_msg, chat_id: int) -> tuple:
    """Runs the full panel workflow, updating a placeholder message, and returns a dictionary of results and the final answer."""
    panel_results = {}

    # --- 0. Configuration Validation ---
    try:
        await placeholder_msg.edit_text("Assembling panel... Validating configuration...", parse_mode=None)
    except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
        logger.warning(f"Failed to update placeholder message (Validating configuration): {e}")
    
    # Validate expert panel configuration
    try:
        # Import the necessary helper functions

        # Use the centralized function to load and merge the config
        panel_config = await load_panel_config(chat_id)
        
        if panel_config != config.get_expert_panel_config():
             logger.info(f"Using custom panel configuration for chat {chat_id}")
        else:
             logger.debug(f"Using default panel configuration for chat {chat_id}")

        # Load configuration needed for the workflow
        quality_threshold = panel_config.get('quality_threshold', 85)
        max_iterations = panel_config.get('max_refinement_iterations', 3)
        
        orchestrator_config = panel_config.get('orchestrator', {})
        orchestrator_provider = orchestrator_config.get('provider')
        orchestrator_model = orchestrator_config.get('model')
        orchestrator_timeout = orchestrator_config.get('request_timeout_seconds', 600)  # Default 10 minutes
        
        if not orchestrator_config:
            raise ValueError("Configuration Error: The 'orchestrator' section is missing from your panel configuration. Use /configure_panel to set up your Expert Panel.")
        if not orchestrator_provider:
            raise ValueError("Configuration Error: The 'provider' field is missing from orchestrator configuration. Use /configure_panel to fix this.")
        if not orchestrator_model:
            raise ValueError("Configuration Error: The 'model' field is missing from orchestrator configuration. Use /configure_panel to fix this.")
        
        # Validate role configurations
        role_configs = panel_config.get('roles', {})
        required_roles = ['Proposer', 'Critic']
        
        for role in required_roles:
            role_config = role_configs.get(role, {})
            if not role_config:
                raise ValueError(f"Configuration Error: The '{role}' role is missing from your panel configuration. Use /configure_panel to configure this role.")
            if not role_config.get('provider'):
                raise ValueError(f"Configuration Error: The 'provider' field is missing for {role} role. Use /configure_panel to fix this.")
            if not role_config.get('model'):
                raise ValueError(f"Configuration Error: The 'model' field is missing for {role} role. Use /configure_panel to fix this.")
        
        # Validate Refiner role if present
        refiner_config = role_configs.get('Refiner', {})
        if refiner_config and (not refiner_config.get('provider') or not refiner_config.get('model')):
            raise ValueError("Configuration Error: The 'Refiner' role is incomplete - missing provider or model. Use /configure_panel to fix this.")
            
    except (ValueError, ImportError) as config_error:
        # Return user-friendly configuration error
        try:
            await placeholder_msg.edit_text(
                f"⚠️ {str(config_error)} Please check your configuration and use /reroll to try again.",
                parse_mode=None
            )
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            logger.warning(f"Failed to update placeholder message (Config error): {e}")
        return {}, f"[{str(config_error)}]", ""

    # --- 1. Deconstruct Task ---
    try:
        await placeholder_msg.edit_text("Assembling panel... Decomposing task...", parse_mode=None)
    except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
        logger.warning(f"Failed to update placeholder message (Decomposing task): {e}")

    try:
        orchestrator_service = providers.get_service_for_provider(orchestrator_provider)
        if orchestrator_service is None:
            raise ValueError(f"Orchestrator service '{orchestrator_provider}' is not available.")
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        try:
            await placeholder_msg.edit_text(
                f"⚠️ Configuration Error: {e}. Please check your panel configuration.",
                parse_mode=None
            )
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            logger.warning(f"Failed to update placeholder message (Config error): {e}")
        return {}, f"[System Error: {e}]", ""

    # --- Initialize MCP / Skill services for Orchestrator tool calling ---
    from utils.service_registry import get_or_init_mcp_service, get_or_init_skill_service
    app = getattr(context, 'application', None)
    enable_mcp = await storage_manager.get_user_setting(chat_id, 'enable_mcp', USER_SETTINGS['enable_mcp']['default'])
    enable_skills = await storage_manager.get_user_setting(chat_id, 'enable_skills', USER_SETTINGS['enable_skills']['default'])
    mcp_service = await get_or_init_mcp_service(app, enable_mcp)
    skill_service = await get_or_init_skill_service(app, enable_skills)

    # Build two tool sets:
    #   all_mcp_tools       — every connected MCP tool (for normal chat, diagnostics)
    #   panel_execution_*   — only servers with panel_execution: true in config.yaml
    # The panel Quality Gate only sees and can invoke panel_execution_tools.
    # This enforces the principle of least privilege: sqlite-tools (bot DB) is never
    # available to the autonomous panel flow even though it is available in normal chat.
    _server_cfg_map = {c['name']: c for c in config._yaml_config.get("mcp_servers", [])}

    all_mcp_tools = []
    if enable_mcp and mcp_service:
        all_mcp_tools = await mcp_service.get_all_tools()

    panel_execution_tools = []
    for _tool in all_mcp_tools:
        _server_name = _tool['function']['name'].split('__')[0]
        if _server_cfg_map.get(_server_name, {}).get('panel_execution', False):
            panel_execution_tools.append(_tool)

    if enable_skills and skill_service:
        panel_execution_tools.extend(skill_service.get_skills_as_tools())

    panel_execution_tool_names = frozenset(t['function']['name'] for t in panel_execution_tools)
    available_tools_text = _format_tools_for_prompt(panel_execution_tools)

    _excluded_servers = [n for n, c in _server_cfg_map.items() if not c.get('panel_execution', False)]
    if _excluded_servers:
        logger.info(f"Panel tool authority: excluded servers (not panel_execution) = {_excluded_servers}")

    plan_template = config.PROMPTS.get_prompt('panel_orchestrator_plan')

    # Cap history for the plan prompt at the last 30 non-system messages.
    # ensure_context_fits cannot be used here because the plan template itself (with full tool
    # schemas injected) already exceeds the Orchestrator model's context limit before any history
    # is added — the function silently drops all history and the Planner runs blind.
    # Recent context (last 30 turns) is all the Planner needs to understand what was just asked.
    _PLAN_HISTORY_CAP = 30
    _non_system = [m for m in full_history if m.get("role") != "system"]
    _plan_history = _non_system[-_PLAN_HISTORY_CAP:]

    meta_prompt = plan_template.format(
        user_prompt=user_prompt,
        full_history_json=json.dumps(_plan_history, indent=2),
        available_tools=_format_tools_for_plan_prompt(panel_execution_tools)
    )

    # Use consolidated LLM response function for initial Orchestrator call with integrated JSON parsing retry
    logger.info("Invoking Initial Orchestrator (Project Manager) with retry logic...")
    
    tasks_list = None
    requires_search = False
    search_query = ""
    
    # Use consolidated LLM response function for orchestrator call
    fallback_provider, fallback_model = get_expert_panel_fallback_config()
    orchestrator_llm_result = await get_robust_llm_response(
        provider_name=orchestrator_provider,
        model=orchestrator_model,
        prompt=meta_prompt,
        history=None,  # No history for the initial plan
        role_name='Initial Orchestrator',
        request_timeout=orchestrator_timeout,
        fallback_provider=fallback_provider,
        fallback_model=fallback_model
    )
    orchestrator_response = orchestrator_llm_result['response']
    orchestrator_retries = orchestrator_llm_result['retries']
    orchestrator_fallback_used = orchestrator_llm_result['fallback_used']

    panel_results['Initial_Orchestrator'] = {
        'provider': orchestrator_provider,
        'model': orchestrator_model,
        'status': 'Success' if not orchestrator_llm_result['is_error'] else 'Failure',
        'response': orchestrator_response,
        'retries': orchestrator_retries,
        'fallback_used': orchestrator_fallback_used
    }

    logger.debug(f"Initial Orchestrator response: {orchestrator_response[:200]}...")  # Log first 200 chars

    # Handle potential error responses from get_robust_llm_response
    if orchestrator_llm_result['is_error']:
        try:
            await placeholder_msg.edit_text(
                f"⚠️ The orchestrator failed to create a valid plan. "
                f"Please use /reroll to try again or /cancel to exit.",
                parse_mode=None
            )
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            logger.warning(f"Failed to update placeholder message (Orchestrator plan failed): {e}")
        return {}, f"[System Error: Orchestrator planning failed. Use /reroll to retry.]", ""
    
    # Parse JSON from the response using the established extraction strategies
    json_str = None
    
    # Try multiple strategies to extract valid JSON
    # Strategy 1: Look for complete JSON object with balanced braces
    brace_count = 0
    start_pos = None
    for i, char in enumerate(orchestrator_response):
        if char == '{':
            if start_pos is None:
                start_pos = i
            brace_count += 1
        elif char == '}':
            brace_count -= 1
            if brace_count == 0 and start_pos is not None:
                json_str = orchestrator_response[start_pos:i+1]
                break
    
    # Strategy 2: Fallback to regex if brace counting didn't work
    if not json_str:
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', orchestrator_response)
        if json_match:
            json_str = json_match.group(0)
    
    # Strategy 3: Last resort - try the original greedy approach
    if not json_str:
        json_match = re.search(r'{[\s\S]*}', orchestrator_response)
        if json_match:
            json_str = json_match.group(0)
    
    if not json_str:
        logger.error("No valid JSON found in the orchestrator's response.")
        try:
            await placeholder_msg.edit_text(
                f"⚠️ The orchestrator response was invalid. "
                f"Please use /reroll to try again or /cancel to exit.",
                parse_mode=None
            )
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            logger.warning(f"Failed to update placeholder message (Invalid orchestrator response): {e}")
        return {}, f"[System Error: Invalid orchestrator response format. Use /reroll to retry.]", ""
    
    try:
        orchestrator_plan = json.loads(json_str)
    except json.JSONDecodeError as parse_error:
        logger.error(f"JSON parsing failed for extracted string: {json_str[:200]}...")
        try:
            await placeholder_msg.edit_text(
                f"⚠️ The orchestrator response could not be parsed. "
                f"Please use /reroll to try again or /cancel to exit.",
                parse_mode=None
            )
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            logger.warning(f"Failed to update placeholder message (Orchestrator response parsing failed): {e}")
        return {}, f"[System Error: Orchestrator response parsing failed. Use /reroll to retry.]", ""
    
    # Extract search requirements and tasks from the plan
    requires_search = orchestrator_plan.get("requires_search", False)
    search_query = orchestrator_plan.get("search_query", "")
    tasks_list = orchestrator_plan.get("tasks", [])
    
    if not tasks_list or len(tasks_list) < 2:  # Need at least Proposer and Critic
        logger.error(f"Invalid orchestrator plan: insufficient tasks ({len(tasks_list) if tasks_list else 0})")
        try:
            await placeholder_msg.edit_text(
                f"⚠️ The orchestrator plan was incomplete. "
                f"Please use /reroll to try again or /cancel to exit.",
                parse_mode=None
            )
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            logger.warning(f"Failed to update placeholder message (Incomplete orchestrator plan): {e}")
        return {}, f"[System Error: Incomplete orchestrator plan. Use /reroll to retry.]", ""
    
    workspace_queries = orchestrator_plan.get("workspace_queries", [])
    logger.info(f"Successfully parsed orchestrator's plan. Search required: {requires_search}, Workspace queries: {len(workspace_queries)}, Tasks: {len(tasks_list)}")

    # Execute workspace pre-queries (e.g. notion-workspace) BEFORE the Proposer drafts.
    # Workspace tools surface the user's own stored content; they must run at this stage so the
    # Proposer can base its draft on actual workspace data rather than generic knowledge.
    if workspace_queries and mcp_service and panel_execution_tool_names:
        workspace_context_parts = []
        for wq in workspace_queries:
            _wq_tool = wq.get("tool", "")
            _wq_args = wq.get("arguments", {})
            if "__" not in _wq_tool:
                logger.warning(f"Workspace pre-query has invalid tool name '{_wq_tool}' — skipped.")
                continue
            _wq_server, _wq_name = _wq_tool.split("__", 1)
            if _wq_tool not in panel_execution_tool_names:
                logger.warning(
                    f"Workspace pre-query '{_wq_tool}' not in panel authority set — skipped. "
                    f"Available panel tools: {sorted(panel_execution_tool_names)}"
                )
                continue
            try:
                hook_runner.run_pre_tool_use(_wq_tool, {"arguments": _wq_args})
                _wq_result = await mcp_service.execute_tool(_wq_server, _wq_name, _wq_args)
                if isinstance(_wq_result, str) and len(_wq_result) > 8_000:
                    _wq_result = _wq_result[:8_000] + "\n[Truncated]"
                workspace_context_parts.append(f"[{_wq_tool}]\n{_wq_result}")
                logger.info(f"Workspace pre-query '{_wq_tool}' executed successfully.")
            except PermissionError as _hook_err:
                logger.warning(f"Workspace pre-query '{_wq_tool}' denied by hook: {_hook_err}")
            except Exception as _wq_exc:
                logger.warning(f"Workspace pre-query '{_wq_tool}' failed: {_wq_exc}")

        if workspace_context_parts:
            _workspace_context = "\n\n".join(workspace_context_parts)
            # Cap the aggregate workspace context to 4 000 tokens (~16 KB) so multiple
            # large Notion results don't overflow the Proposer's context budget.
            _max_workspace_tokens = 4000
            _workspace_context = truncate_text_to_tokens(_workspace_context, _max_workspace_tokens)
            _proposer_task = next((t for t in tasks_list if t.get("role") == "Proposer"), None)
            if _proposer_task:
                _original_prompt = _proposer_task.get("prompt", "")
                _proposer_task["prompt"] = (
                    "The following workspace data was retrieved from your connected tools to help you "
                    "address the user's query. Base your draft on this actual content — do not invent or "
                    "generalize where specific data is available.\n\n"
                    f"--- WORKSPACE CONTEXT ---\n{_workspace_context}\n\n"
                    f"--- ORIGINAL TASK ---\n{_original_prompt}"
                )
                logger.info(f"Augmented Proposer's prompt with {len(workspace_context_parts)} workspace pre-query result(s).")
            else:
                logger.warning("Workspace pre-queries ran but no Proposer task found to augment.")

    # Store the tasks in the SQLite Scratchpad for Agentic context injection
    if hasattr(storage_manager, 'clear_panel_tasks') and storage_manager.clear_panel_tasks:
        try:
            await storage_manager.clear_panel_tasks(chat_id)
            for task_data in tasks_list:
                await storage_manager.save_panel_task(chat_id, task_data.get('role', 'Unknown'), json.dumps(task_data))
            logger.info("Saved Panel Plan to Agentic Scratchpad.")
        except Exception as e:
            logger.error(f"Failed to persist orchestrator plan to state db: {e}")

    # If search is required, check user setting and conditionally perform it
    if requires_search and search_query:
                        # Check if advanced search is enabled for panel discussions
                        advanced_search_enabled = await storage_manager.get_user_setting(
                            chat_id,
                            'advanced_search_panel',
                            USER_SETTINGS['advanced_search_panel']['default']
                        )
                
                        # Check if basic auto-search is enabled (if advanced is not)
                        autosearch_enabled = await storage_manager.get_user_setting(
                            chat_id,
                            'autosearch_panel',
                            USER_SETTINGS['autosearch_panel']['default']
                        )
                

                

                        if advanced_search_enabled:
                            try:
                                await placeholder_msg.edit_text(f"Orchestrator requested advanced web search: \"{search_query}\"...", parse_mode=None)
                            except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                                logger.warning(f"Failed to update placeholder message (Orchestrator requested advanced web search): {e}")
                            
                            # Perform initial search
                            initial_search_results_data = await web_search_service.perform_search(search_query)
                            
                            if initial_search_results_data.get('status') == 'error':
                                error_message = initial_search_results_data.get('message', 'Unknown error')
                                logger.error(f"Initial web search failed: {error_message}")
                                try:
                                    await placeholder_msg.edit_text(f"⚠️ Initial web search failed: {error_message}", parse_mode=None)
                                except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                                    logger.warning(f"Failed to update placeholder message (Initial web search failed): {e}")
                                await asyncio.sleep(2)
                                # Proceed without search results if initial search fails
                                initial_search_results = ""
                            else:
                                initial_search_results = initial_search_results_data.get('content', '')

                            # Plan deep-dive searches
                            deep_dive_queries = await _plan_deep_dive_searches(
                                orchestrator_provider, orchestrator_model, user_prompt, search_query, initial_search_results, orchestrator_timeout, fallback_provider, fallback_model
                            )
                            
                            deep_dive_results = {}
                            if deep_dive_queries:
                                try:
                                    await placeholder_msg.edit_text(f"Executing {len(deep_dive_queries)} parallel deep-dive searches...", parse_mode=None)
                                except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                                    logger.warning(f"Failed to update placeholder message (Executing deep-dive searches): {e}")
                                deep_dive_results = await web_search_service.execute_parallel_google_searches(deep_dive_queries)
                                logger.info(f"Completed {len(deep_dive_results)} deep-dive searches.")
                            
                            # Combine all search results into a single dossier
                            research_dossier_parts = []
                            if initial_search_results:
                                research_dossier_parts.append(f"--- INITIAL WEB SEARCH RESULTS ---\n{initial_search_results}")
                            if deep_dive_results:
                                for query, result in deep_dive_results.items():
                                    research_dossier_parts.append(f"--- DEEP DIVE SEARCH: {query} ---\n{result}")
                            
                            research_dossier = "\n\n".join(research_dossier_parts)
                            
                            # Augment Proposer's prompt with the combined research dossier
                            proposer_task = next((task for task in tasks_list if task.get("role") == "Proposer"), None)
                            if proposer_task:
                                role_configs = panel_config.get('roles', {})
                                proposer_config = role_configs.get('Proposer', {})
                                proposer_provider_name = proposer_config.get('provider', config.get_default_provider())
                                proposer_model_name = proposer_config.get('model', 'unknown')
                                
                                limits = get_model_context_limits(proposer_model_name, proposer_provider_name)
                                max_dossier_tokens = int(limits.effective_input_limit * 0.5)
                                truncated_dossier = truncate_text_to_tokens(research_dossier, max_dossier_tokens)
                                
                                if len(truncated_dossier) < len(research_dossier):
                                    logger.warning(f"Expert Panel research dossier truncated from {len(research_dossier)} chars to fit {max_dossier_tokens} token budget.")

                                original_prompt = proposer_task.get("prompt", "")
                                augmented_prompt = (
                                    f"Based on the following comprehensive research dossier, please address the user's original query.\n\n"
                                    f"--- RESEARCH DOSSIER ---\n{truncated_dossier}\n\n"
                                    f"--- ORIGINAL TASK ---\n{original_prompt}"
                                )
                                proposer_task["prompt"] = augmented_prompt
                                logger.info("Augmented Proposer's prompt with comprehensive research dossier.")
                                logger.info("Successfully created research dossier.") # Log for test assertion
                            else:
                                logger.warning("Could not find Proposer task to augment with research dossier.")

                        elif autosearch_enabled: # Only basic auto-search is enabled
                            try:
                                await placeholder_msg.edit_text(f"Orchestrator requested web search: \"{search_query}\"...", parse_mode=None)
                            except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                                logger.warning(f"Failed to update placeholder message (Orchestrator requested web search): {e}")
                            search_results_data = await web_search_service.perform_search(search_query)
                            
                            if search_results_data.get('status') == 'error':
                                error_message = search_results_data.get('message', 'Unknown error')
                                logger.error(f"Web search failed: {error_message}")
                                try:
                                    await placeholder_msg.edit_text(f"⚠️ Web search failed: {error_message}", parse_mode=None)
                                except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                                    logger.warning(f"Failed to update placeholder message (Web search failed): {e}")
                                await asyncio.sleep(2)
                            else:
                                search_results = search_results_data.get('content', '')
                                # Find the Proposer's task and augment its prompt with search results
                                proposer_task = next((task for task in tasks_list if task.get("role") == "Proposer"), None)
                                if proposer_task:
                                    original_prompt = proposer_task.get("prompt", "")
                                    augmented_prompt = (
                                        f"Based on the following fresh web search results, please address the user's original query.\n\n"
                                        f"--- WEB SEARCH RESULTS ---\n{search_results}\n\n"
                                        f"--- ORIGINAL TASK ---\n{original_prompt}"
                                    )
                                    proposer_task["prompt"] = augmented_prompt
                                    logger.info("Augmented Proposer's prompt with web search results.")
                                else:
                                    logger.warning("Could not find Proposer task to augment with search results.")
                        else:
                            # Auto-search is disabled - inform the Proposer but don't perform search
                            logger.info(f"Auto-search disabled for panel discussion. Skipping search for: '{search_query}'")
                            try:
                                await placeholder_msg.edit_text(f"Auto-search disabled. Proceeding without web search...", parse_mode=None)
                            except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                                logger.warning(f"Failed to update placeholder message (Auto-search disabled): {e}")
                            
                            # Find the Proposer's task and inform them about the disabled search
                            proposer_task = next((task for task in tasks_list if task.get("role") == "Proposer"), None)
                            if proposer_task:
                                original_prompt = proposer_task.get("prompt", "")
                                informed_prompt = (
                                    f"Note: The orchestrator suggested searching for '{search_query}' but auto-search is disabled. "
                                    f"Please provide your best answer based on existing knowledge.\n\n"
                                    f"--- ORIGINAL TASK ---\n{original_prompt}"
                                )
                                proposer_task["prompt"] = informed_prompt
                                logger.info("Informed Proposer about disabled search.")
                            await asyncio.sleep(1)  # Brief pause for user feedback    # The retry logic and error handling is now handled by get_robust_llm_response

    # --- 2. Master & Apprentice Architecture: Iterative Quality Loop ---
    quality_threshold = panel_config.get('quality_threshold', 85)
    max_iterations = panel_config.get('max_refinement_iterations', 3)
    iteration = 1
    quality_score = 0  # Initialize quality score
    proposer_response = ""
    critic_response = ""
    role_configs = panel_config.get('roles', {})
    
    # Find Proposer and Critic in tasks_list
    proposer_task = next((t for t in tasks_list if t.get('role') == 'Proposer'), None)
    critic_task = next((t for t in tasks_list if t.get('role') == 'Critic'), None)
    
    if not proposer_task or not critic_task:
        raise RuntimeError("Orchestrator's plan must include Proposer and Critic roles.")

    # Scope Quality Gate tools to only what the Planner identified as relevant.
    # Showing all 33 tools leads the model to cross-namespace hallucinate (e.g. calling
    # Notion tools during a SQLite query). Use the Planner's own intent as the filter.
    _qg_servers: set[str] = set()
    if requires_search:
        _qg_servers.add('tavily-search')
    for _wq in workspace_queries:
        _wq_tool_name = _wq.get('tool', '')
        if '__' in _wq_tool_name:
            _qg_servers.add(_wq_tool_name.split('__')[0])
    if not _qg_servers:
        _qg_servers.add('tavily-search')  # default: external verification only
    quality_gate_tools = [
        t for t in panel_execution_tools
        if t['function']['name'].split('__')[0] in _qg_servers
    ]
    quality_gate_tools_text = _format_tools_for_prompt(quality_gate_tools)
    logger.info(f"Quality Gate tool scope: {sorted(_qg_servers)} ({len(quality_gate_tools)} tools)")

    # Execute the Master & Apprentice refinement cycle using the helper function
    proposer_response, quality_score, iteration = await _run_refinement_cycle(
        update, context, proposer_task, critic_task, user_prompt, full_history, placeholder_msg, panel_results,
        orchestrator_service, orchestrator_model, orchestrator_timeout, orchestrator_config, panel_config,
        mcp_service=mcp_service, skill_service=skill_service, available_tools_text=available_tools_text,
        panel_execution_tool_names=panel_execution_tool_names,
        quality_gate_tools_text=quality_gate_tools_text
    )
    if quality_score < quality_threshold and iteration == max_iterations:
        try:
            await placeholder_msg.edit_text(f"Reached maximum iterations. Final quality score: {quality_score}/{quality_threshold}", parse_mode=None)
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            logger.warning(f"Failed to update placeholder message (Max iterations reached): {e}")
    # --- 3. Synthesize Final Answer ---
    try:
        await placeholder_msg.edit_text("Synthesizing final answer...", parse_mode=None)
    except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
        logger.warning(f"Failed to update placeholder message (Synthesizing final answer): {e}")
    # proposer_response is already available from the refinement cycle
    critic_response = panel_results.get("Critic", {}).get('response', 'No response from critic.')
    
    synthesis_template = config.PROMPTS.get_prompt('panel_synthesis')

    # Determine which model the synthesis prompt will be sent to.
    # If a Refiner is configured, the synthesis result feeds into the Refiner;
    # otherwise, it goes directly to the orchestrator's synthesizer.
    # We use the orchestrator model as the conservative baseline for truncation.
    base_synthesis_est = synthesis_template.format(
        full_history="",
        user_prompt=user_prompt,
        proposer_response=proposer_response,
        critic_response=critic_response
    )
    trimmed_synthesis_history, _ = await ensure_context_fits(
        prompt=base_synthesis_est,
        history=full_history,
        model=orchestrator_model,
        provider=orchestrator_provider,
        safety_margin=0.85
    )

    synthesis_prompt = synthesis_template.format(
        full_history=json.dumps(trimmed_synthesis_history, indent=2),
        user_prompt=user_prompt,
        proposer_response=proposer_response,
        critic_response=critic_response
    )
    
    # Step 4: Optional Final Polish with Refiner
    # After quality gate loop completes, proposer_response holds the final approved draft
    refiner_task = next((t for t in tasks_list if t.get('role') == 'Refiner'), None)
    refiner_role_config = role_configs.get('Refiner', {})
    refiner_provider = refiner_role_config.get('provider') if refiner_task else None
    refiner_model = refiner_role_config.get('model') if refiner_task else None
    
    if refiner_task and refiner_provider and refiner_model:
        # Refiner is configured - polish the final proposer_response AND format for Telegram
        base_refiner_prompt = refiner_task.get('prompt', 'Polish and refine the following response for clarity and style.')
        
        refiner_template = config.PROMPTS.get_prompt('panel_refiner')
        full_refiner_prompt = refiner_template.format(
            base_refiner_prompt=base_refiner_prompt,
            proposer_response=proposer_response
        )
        
        refiner_llm_result = await get_robust_llm_response(
            provider_name=refiner_provider,
            model=refiner_model,
            prompt=full_refiner_prompt,
            history=[],  # proposer_response already embedded in prompt; full history causes context overflow
            role_name='Refiner',
            request_timeout=refiner_role_config.get('request_timeout_seconds'),
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        refiner_response = refiner_llm_result['response']
        refiner_retries = refiner_llm_result['retries']
        refiner_fallback_used = refiner_llm_result['fallback_used']

        # Safety net: strip any tool-call JSON the model generated without authorization.
        # qwen3.5-397b (and similar heavily tool-use-fine-tuned models) may emit delta.tool_calls
        # even when `tools` is absent from the API request, causing the text to be truncated
        # at the point where the function call starts and a JSON suffix to be appended.
        tc_json_start = refiner_response.rfind('{"tool_calls"')
        if tc_json_start > 0:
            logger.warning(
                f"Refiner emitted unsolicited tool-call JSON (pos {tc_json_start}); "
                f"stripping JSON suffix. Text length before: {len(refiner_response)}"
            )
            refiner_response = refiner_response[:tc_json_start].rstrip()

        # Check if Refiner failed and gracefully fall back to proposer_response
        if refiner_llm_result['is_error'] or not refiner_response.strip():
            logger.warning(f"Refiner failed or returned empty: {refiner_response}. Using proposer response as final answer.")
            final_answer = f"⚠️ **Warning:** The final refinement step was skipped due to an error. The following is the unpolished response.\n\n---\n\n{proposer_response}"
            refiner_status = 'Failure'
        else:
            final_answer = refiner_response
            refiner_status = 'Success'
        
        panel_results['Refiner'] = {
            'provider': refiner_provider,
            'model': refiner_model,
            'status': refiner_status,
            'response': refiner_response,
            'retries': refiner_retries,
            'fallback_used': refiner_fallback_used
        }
        logger.info("Master & Apprentice workflow completed with Refiner polish.")
    else:
        # Refiner not configured - proposer_response IS the final answer
        final_answer = proposer_response
        logger.info("Master & Apprentice workflow completed. No Refiner configured - using Proposer response as final answer.")
    
    # Add quality metrics to results for transparency
    panel_results['Quality_Metrics'] = {
        'final_score': quality_score,
        'threshold': quality_threshold,
        'iterations_used': iteration,
        'max_iterations': max_iterations
    }
    
    return panel_results, final_answer, proposer_response

async def _run_panel_task_background(update: Update, context: ContextTypes.DEFAULT_TYPE, user_prompt: str, assembling_msg, chat_id: int):
    """Background task wrapper for the panel workflow."""
    try:
        # Incremental Archival: Save USER prompt IMMEDIATELY to prevent orphans on crash
        pk = await storage_manager.save_message(chat_id, 'user', user_prompt)
        context.user_data['pending_panel_message_pk'] = pk

        inject_history = await storage_manager.get_user_setting(chat_id, 'inject_history_in_panel', USER_SETTINGS['inject_history_in_panel']['default'])
        initial_history = []
        if inject_history:
            initial_history = await storage_manager.get_thread_history(chat_id)

        panel_results, final_answer, proposer_response = await _run_panel_workflow(
            update, context, user_prompt, initial_history, assembling_msg, chat_id
        )
        
        # Store state
        context.user_data['panel_state'] = {
            "original_prompt": user_prompt,
            "panel_results": panel_results,
            "final_answer": final_answer,
            "full_transcript": [
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": final_answer}
            ],
            # Lock lives in the module-level _panel_locks dict (see ticket 030);
            # storing it here would carry a dead event-loop binding across a
            # polling-loop restart if user_data persistence were enabled.
        }

        await assembling_msg.delete()
        
        # AST-Based Architecture: Parse, Split, and Send
        pure_summary = _format_panel_summary(panel_results)
        pure_markdown_content = f"{pure_summary}\n\n---\n\n{final_answer}"

        # Use the centralized send_safe_message function
        if await send_safe_message(context, update, pure_markdown_content):
            # Incremental Archival: Save the panel's result
            await storage_manager.save_message(chat_id, 'assistant:panel', final_answer)
            # Clear pending PK since the interaction block is now complete and stable
            context.user_data.pop('pending_panel_message_pk', None)
        
    except asyncio.CancelledError:
        logger.warning(f"Panel workflow in background task for chat {chat_id} was cancelled.")
        await _cleanup_discussion_state(context, chat_id, assembling_msg)
    except Exception as e:
        logger.error(f"Panel workflow failed in background task: {e}", exc_info=True)
        
        error_message = f"An error occurred: {str(e)}"
        try:
            if assembling_msg:
                await assembling_msg.edit_text(error_message, parse_mode=None)
            else:
                await send_plain_message(context, chat_id, error_message)
        except Exception as send_error:
            logger.exception(f"Failed to send error message: {send_error}")
            
        await _cleanup_discussion_state(context, chat_id, assembling_msg)

async def start_panel_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the /discuss_panel command."""
    chat_id = update.effective_chat.id
    user_prompt = " ".join(context.args).strip()
    if not user_prompt:
        await update.message.reply_text("Usage: /discuss_panel <topic>", parse_mode=None)
        return ConversationHandler.END

    try:
        assembling_msg = await send_plain_message(
            context,
            chat_id,
            "Assembling an expert panel..."
        )
    except telegram.error.NetworkError as e:
        logger.error(f"Network error while sending initial message in start_panel_discussion: {e}")
        try:
            await update.message.reply_text("A network error occurred, please try again.", parse_mode=None)
        except Exception as e_inner:
            logger.exception(f"Failed to send network error message to user: {e_inner}")
        return ConversationHandler.END
    
    await set_panel_commands(context.application, chat_id)

    # Create and store task, but DO NOT await it
    panel_task = asyncio.create_task(
        _run_panel_task_background(update, context, user_prompt, assembling_msg, chat_id)
    )
    context.user_data['panel_task'] = panel_task
    
    # Return immediately to allow ConversationHandler to enter state
    return AWAITING_FOLLOW_UP

async def handle_follow_up(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles a follow-up question from the user."""
    chat_id = update.effective_chat.id
    follow_up_prompt = update.message.text
    logger.info(f"[{chat_id}] Handling follow-up: '{follow_up_prompt}'")

    panel_state = context.user_data.get('panel_state')
    if not panel_state:
        await update.message.reply_text("Error: Discussion context was lost. Please start a new discussion with /discuss_panel.", parse_mode=None)
        return ConversationHandler.END

    async with _get_panel_lock(chat_id):
        placeholder = await update.message.reply_text("Panel is reconvening...", parse_mode=None)

        try:
            panel_task = asyncio.create_task(
                _run_panel_workflow(
                    update, 
                    context, 
                    follow_up_prompt, 
                    panel_state['full_transcript'],
                    placeholder,
                    chat_id
                )
            )
            # Incremental Archival: Save USER prompt IMMEDIATELY
            pk = await storage_manager.save_message(chat_id, 'user', follow_up_prompt)
            context.user_data['pending_panel_message_pk'] = pk

            context.user_data['panel_task'] = panel_task
            new_panel_results, new_final_answer, new_proposer_response = await panel_task
        except asyncio.CancelledError:
            logger.warning(f"Panel workflow in handle_follow_up for chat {chat_id} was cancelled.")
            await _cleanup_discussion_state(context, chat_id, placeholder)
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Panel workflow failed in handle_follow_up: {e}", exc_info=True)
            try:
                if placeholder:
                    await asyncio.wait_for(
                        placeholder.edit_text(f"An error occurred: {str(e)}", parse_mode=None),
                        timeout=8.0
                    )
            except (asyncio.TimeoutError, telegram.error.TimedOut) as timeout_e:
                logger.warning(f"Timeout editing error message in follow_up: {timeout_e}")
            except Exception as edit_e:
                logger.exception(f"Failed to edit error message in follow_up: {edit_e}")
            await _cleanup_discussion_state(context, chat_id, placeholder)
            return ConversationHandler.END

        panel_state['full_transcript'].append({"role": "user", "content": follow_up_prompt})
        panel_state['full_transcript'].append({"role": "assistant", "content": new_final_answer})
        panel_state['panel_results'] = new_panel_results
        panel_state['final_answer'] = new_final_answer

        await placeholder.delete()
        
        # AST-Based Architecture: Parse, Split, and Send
        pure_summary = _format_panel_summary(new_panel_results)
        pure_markdown_content = f"{pure_summary}\n\n---\n\n{new_final_answer}"
        if await send_safe_message(context, update, pure_markdown_content):
            # Incremental Archival: Save the panel's result immediately
            await storage_manager.save_message(chat_id, 'assistant:panel', new_final_answer)
            # Clear pending PK since the interaction block is now complete and stable
            context.user_data.pop('pending_panel_message_pk', None)

    return AWAITING_FOLLOW_UP

async def _cleanup_discussion_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, placeholder_msg=None) -> None:
    """Safely cancels any running panel task, clears user_data, and resets commands.
    
    Args:
        context: The callback context
        chat_id: The chat ID
        placeholder_msg: Optional message object to update with cancellation status
    """
    panel_task = context.user_data.get('panel_task')

    # Try to find the placeholder in user_data if not explicitly provided
    if not placeholder_msg:
        placeholder_msg = context.user_data.get('panel_placeholder')

    if panel_task and not panel_task.done():
        panel_task.cancel()
        logger.info(f"Cancelled in-flight panel task for chat {chat_id}.")
        
        # Update placeholder message if provided
        if placeholder_msg:
            try:
                await placeholder_msg.edit_text("Discussion cancelled.", parse_mode=None)
            except telegram.error.TelegramError as e:
                logger.warning(f"Could not update placeholder message during cleanup: {e}")
            except Exception as e:
                logger.exception(f"Unexpected error updating placeholder during cleanup: {e}")
        
        try:
            # Await the task to allow it to process the cancellation
            try:
                await panel_task
            except asyncio.CancelledError:
                logger.info(f"Panel task for chat {chat_id} was already cancelled.")
            except Exception as e:
                logger.exception(f"Error awaiting cancelled panel task for chat {chat_id}: {e}")
        except asyncio.CancelledError:
            logger.info(f"Panel task for chat {chat_id} successfully processed cancellation.")
        except Exception as e:
            logger.exception(f"Error awaiting cancelled panel task for chat {chat_id}: {e}")

    context.user_data.pop('panel_task', None)
    context.user_data.pop('panel_state', None)
    context.user_data.pop('panel_placeholder', None)  # Clear the placeholder reference
    
    # Surgical cleanup of orphaned user prompt preventing data loss history wipes
    pending_pk = context.user_data.pop('pending_panel_message_pk', None)
    if pending_pk is not None:
        await storage_manager.delete_messages(chat_id, [pending_pk])
        logger.info(f"Cleaned up orphaned panel prompt PK {pending_pk} due to cancellation in chat {chat_id}.")
    
    # Reset the command menu back to the default
    await setup_bot_commands_and_menu(context.application, chat_id)
    logger.info(f"Cleaned up panel state and reset commands for chat {chat_id}.")

async def end_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End the panel discussion, save its final answer, and clear context."""
    chat_id = update.effective_chat.id
    panel_state = context.user_data.get('panel_state')

    if panel_state:
        # Lock is not needed here as the conversation is ending, no race conditions.
        # final_answer = panel_state.get("final_answer", "No final answer was recorded.")
        # We no longer save here to avoid duplication, as it's saved incrementally now.
        # await storage_manager.save_message(chat_id, 'assistant:panel', final_answer)
        await update.message.reply_text("✅ Panel discussion concluded.", parse_mode=None)
        await _cleanup_discussion_state(context, chat_id)
    else:
        await update.message.reply_text("⚠️ No active discussion to end.", parse_mode=None)

    return ConversationHandler.END



async def search_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /search command within a panel discussion."""
    chat_id = update.effective_chat.id
    logger.info(f"[{chat_id}] User triggered /search within a discussion.")

    if not context.args:
        await update.message.reply_text("Please provide a query to search. Usage: /search <your query>", parse_mode=None)
        return AWAITING_FOLLOW_UP

    query = " ".join(context.args)
    placeholder_msg = await update.message.reply_text(f"Searching the web for: \"{query}\"...", parse_mode=None)

    search_results = await web_search_service.perform_search(query, manual=True)

    if search_results.get('status') == 'error':
        error_msg = search_results.get('message', 'Unknown error occurred.')
        await placeholder_msg.edit_text(f"⚠️ Search error: {error_msg}", parse_mode=None)
        return AWAITING_FOLLOW_UP

    search_content = search_results.get('content', '')
    panel_state = context.user_data.get('panel_state')
    if panel_state and panel_state.get('full_transcript'):
        async with _get_panel_lock(chat_id):
            panel_state['full_transcript'].append({'role': 'user', 'content': f"Search results for '{query}':\n{search_content}"})
            await placeholder_msg.edit_text("✅ Search results have been added to the discussion context." , parse_mode=None)
    else:
        await placeholder_msg.edit_text("⚠️ Could not find an active discussion to add search results to.", parse_mode=None)

    return AWAITING_FOLLOW_UP

async def handle_panel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles edited messages during a panel discussion.
    Cancels current run, updates transcript, and restarts workflow.
    """
    chat_id = update.effective_chat.id
    edited_text = update.edited_message.text
    message_id = update.edited_message.message_id
    
    panel_state = context.user_data.get('panel_state')
    if not panel_state:
        return

    logger.info(f"(Chat {chat_id}) Handling panel edit for message {message_id}")

    # 1. Cancel any running task
    panel_task = context.user_data.get('panel_task')
    if panel_task and not panel_task.done():
        panel_task.cancel()
        logger.info(f"(Chat {chat_id}) Cancelled active panel task due to edit.")
        try:
            await panel_task
        except asyncio.CancelledError:
            pass
    
    # 2. Update Transcript
    full_transcript = panel_state.get('full_transcript', [])
    
    # Find the message in the transcript
    # We assume the transcript stores message_ids if possible, or we rely on position?
    # The current implementation of `_run_panel_workflow` appends to `full_transcript`.
    # But `full_transcript` in `panel_state` is a list of dicts.
    # We need to find the user message that matches.
    # If we can't find by ID (because we might not be storing it), we assume it's the LAST user message?
    # Let's assume it's the last user message for now, as that's the most common edit case.
    
    target_index = -1
    for i in range(len(full_transcript) - 1, -1, -1):
        if full_transcript[i]['role'] == 'user':
            # If we stored message_id, check it. If not, assume last user msg.
            # The current `full_transcript` structure is just {'role':..., 'content':...}
            # So we assume last user message.
            target_index = i
            break
            
    if target_index == -1:
        logger.warning(f"(Chat {chat_id}) Could not find user message to edit in panel transcript.")
        return

    # Update content
    full_transcript[target_index]['content'] = edited_text
    
    # Truncate anything after this message (e.g. old assistant response)
    panel_state['full_transcript'] = full_transcript[:target_index + 1]
    
    logger.info(f"(Chat {chat_id}) Updated panel transcript and truncated history.")
    
    # 3. Restart Workflow
    placeholder_msg = context.user_data.get('panel_placeholder')
    if not placeholder_msg:
         # If no placeholder, send a new one
         placeholder_msg = await send_safe_message(context, update, "🔄 Restarting panel due to edit...")
         context.user_data['panel_placeholder'] = placeholder_msg
    else:
        try:
            await placeholder_msg.edit_text("🔄 Restarting panel due to edit...", parse_mode=None)
        except Exception:
             placeholder_msg = await send_safe_message(context, update, "🔄 Restarting panel due to edit...")
             context.user_data['panel_placeholder'] = placeholder_msg

    # Re-run the workflow
    # We need to wrap it in a task like in `start_panel_discussion` or `handle_follow_up`
    # But `handle_follow_up` logic is complex.
    # We can reuse `_run_panel_workflow` but we need to handle the result (save to history etc).
    # Actually, `reroll_discussion` does exactly this: calls `_run_panel_workflow` and handles result.
    # But `reroll_discussion` expects to be called as a command.
    # We can extract the "run and handle result" logic or just call `_run_panel_workflow` 
    # and duplicate the result handling (which is short).
    
    # Let's duplicate the result handling for safety and clarity, similar to `reroll_discussion`
    
    async def _run_and_handle():
        try:
            panel_results, final_answer, proposer_response = await _run_panel_workflow(
                update, context, edited_text, panel_state['full_transcript'], placeholder_msg, chat_id
            )
            
            # Update transcript with final answer
            panel_state['full_transcript'].append({'role': 'assistant', 'content': final_answer})
            
            # Send final answer
            await send_safe_message(context, update, final_answer)
            
            # Update placeholder with summary
            summary = _format_panel_summary(panel_results)
            try:
                await placeholder_msg.edit_text(summary, parse_mode=constants.ParseMode.MARKDOWN_V2)
            except Exception as e:
                logger.exception(f"Failed to update summary: {e}")
                
        except asyncio.CancelledError:
            logger.info("Panel task cancelled.")
            raise
        except Exception as e:
            logger.error(f"Error in panel edit workflow: {e}", exc_info=True)
            await send_safe_message(context, update, "An error occurred during the panel discussion.")

    # Create and store task
    task = asyncio.create_task(_run_and_handle())
    context.user_data['panel_task'] = task
    
    # Ensure we await the task if we are in a test environment? 
    # No, in production it runs in background.
    # But for the test to pass, we might need to await it?
    # The test mocks `_run_panel_workflow` so it finishes instantly.

async def reroll_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /reroll command within a panel discussion."""
    chat_id = update.effective_chat.id
    logger.info(f"[{chat_id}] User triggered /reroll within a discussion.")

    panel_state = context.user_data.get('panel_state')
    if not panel_state or not panel_state.get('full_transcript'):
        await update.message.reply_text("⚠️ No discussion history found to reroll. Please start a new one.", parse_mode=None)
        return AWAITING_FOLLOW_UP

    async with _get_panel_lock(chat_id):
        last_user_prompt = next((msg['content'] for msg in reversed(panel_state['full_transcript']) if msg['role'] == 'user'), panel_state.get('original_prompt'))

        if not last_user_prompt:
            await update.message.reply_text("⚠️ Could not find the last user prompt to reroll.", parse_mode=None)
            return AWAITING_FOLLOW_UP

        placeholder_msg = await update.message.reply_text(f'Re-running panel for: \"{last_user_prompt[:50]}...\"', parse_mode=None)

        history_for_reroll = list(panel_state['full_transcript'])

        if history_for_reroll and history_for_reroll[-1]['role'] == 'assistant':
            history_for_reroll.pop()
            # Also remove from database to prevent duplication, matching standard reroll behavior
            try:
                await storage_manager.remove_last_assistant_message(chat_id)
                logger.info(f"[{chat_id}] Removed last assistant message from DB for panel reroll.")
            except Exception as e:
                logger.exception(f"Failed to remove last assistant message during panel reroll: {e}")

        try:
            panel_task = asyncio.create_task(
                _run_panel_workflow(update, context, last_user_prompt, history_for_reroll, placeholder_msg, chat_id)
            )
            context.user_data['panel_task'] = panel_task
            panel_results, final_answer, proposer_response = await panel_task
        except asyncio.CancelledError:
            logger.warning(f"Panel workflow in reroll_discussion for chat {chat_id} was cancelled.")
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Panel workflow failed during reroll: {e}", exc_info=True)
            # Escape the error message for safe display in MarkdownV2
            escaped_error = telegram.helpers.escape_markdown(str(e), version=2)
            error_message = f"An error occurred during the reroll: `{escaped_error}`"
            try:
                if placeholder_msg:
                    # Use edit_text for an existing message
                    await placeholder_msg.edit_text(error_message, parse_mode=None)
                else:
                    # Fallback to send_plain_message if placeholder doesn't exist
                    await send_plain_message(context, chat_id, error_message)
            except Exception as send_e:
                logger.exception(f"Failed to send error message to user after reroll failure: {send_e}")
            
            # Do not end the conversation on reroll error, allow user to retry
            return AWAITING_FOLLOW_UP

        if panel_state['full_transcript'] and panel_state['full_transcript'][-1]['role'] == 'assistant':
            panel_state['full_transcript'].pop()
        
        panel_state['full_transcript'].append({"role": "assistant", "content": final_answer})
        panel_state['panel_results'] = panel_results
        panel_state['final_answer'] = final_answer

        await placeholder_msg.delete()

        # Step 1: Generate pure markdown content
        pure_summary = _format_panel_summary(panel_results)
        pure_markdown_content = f"{pure_summary}\n\n---\n\n{final_answer}"

        # Use the centralized send_safe_message function
        if await send_safe_message(context, update, pure_markdown_content):
             # Incremental Archival: Save the panel's result immediately
            await storage_manager.save_message(chat_id, 'assistant:panel', final_answer)

    return AWAITING_FOLLOW_UP

async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles conversation timeout."""
    chat_id = context.job.chat_id
    logger.info(f"Panel discussion timed out for chat {chat_id}.")
    if 'panel_state' in context.user_data:
        # We no longer save here to avoid duplication. The last message was already saved.
        # panel_state = context.user_data['panel_state']
        # final_answer = panel_state.get("final_answer")
        # if final_answer:
        #     try:
        #         await storage_manager.save_message(chat_id, 'assistant:panel', final_answer)
        #         logger.info(f"Saved final answer for timed-out panel in chat {chat_id}")
        #     except Exception as e:
        #         logger.error(f"Failed to save final answer during timeout for chat {chat_id}: {e}")

        await send_plain_message(context, chat_id, "Panel discussion has timed out due to inactivity.")
        await _cleanup_discussion_state(context, chat_id)
    return ConversationHandler.END

async def blocked_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles commands that are disabled during panel discussions."""
    command = update.message.text.split()[0] if update.message.text else "command"
    await update.message.reply_text(
        f"⚠️ The {command} command is disabled during a panel discussion. "
        f"Please use /end_discussion first, or continue with your follow-up question.",
        parse_mode=None
    )
    return AWAITING_FOLLOW_UP

async def panel_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current panel discussion and ends the conversation."""
    chat_id = update.effective_chat.id
    logger.info(f"(Chat {chat_id}) User cancelled panel discussion.")
    
    # Cancel any running panel task
    panel_task = context.user_data.get('panel_task')
    if panel_task and not panel_task.done():
        panel_task.cancel()
        logger.info(f"(Chat {chat_id}) Cancelled active panel task.")
    
    await send_safe_message(context, update, "Panel discussion cancelled.")
    await _cleanup_discussion_state(context, chat_id)
    return ConversationHandler.END

async def resume_panel_discussion_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for resuming a panel from a context history block."""
    query = update.callback_query
    await query.answer()

    # Format: ctx_pnl_<page>_<start_pk>
    try:
        data = query.data
        _, _, page_str, start_pk_str = data.split("_")
        start_pk = int(start_pk_str)
    except ValueError:
        await query.edit_message_text("Invalid panel resumption data.")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    raw_history = await storage_manager.get_thread_history_with_pk(chat_id, limit=200)

    user_prompt = ""
    assistant_contents = []
    found_start = False

    for msg in raw_history:
        if msg['id'] == start_pk:
            found_start = True
            if msg['role'] == 'user':
                user_prompt = msg['content']
            elif msg.get('content'):  # skip tool-call turns (content=None)
                assistant_contents.append(msg['content'])
            continue

        if found_start:
            if msg['role'] == 'user':
                break
            if msg.get('content'):  # skip tool-call turns (content=None)
                assistant_contents.append(msg['content'])

    if not user_prompt and not assistant_contents:
        await query.edit_message_text("Could not find the interaction in history.", parse_mode=None)
        return ConversationHandler.END

    final_answer = "\n\n".join(assistant_contents) if assistant_contents else "(No previous AI response)"

    context.user_data['panel_state'] = {
        "original_prompt": user_prompt,
        "panel_results": {},
        "final_answer": final_answer,
        "full_transcript": [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": final_answer}
        ],
        "lock": asyncio.Lock()
    }

    await set_panel_commands(context.application, chat_id)
    await query.edit_message_text(
        f"🏛️ **Panel Session Resumed**\n\n_Original User Prompt:_\n{user_prompt}\n\nPlease type your follow-up prompt to resume discussion:",
        parse_mode="Markdown"
    )

    return AWAITING_FOLLOW_UP

discuss_panel_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler('discuss_panel', start_panel_discussion),
        CallbackQueryHandler(resume_panel_discussion_entry, pattern="^ctx_pnl_")
    ],
    states={
        AWAITING_FOLLOW_UP: [
            CommandHandler('reroll', reroll_discussion),
            CommandHandler('search', search_discussion),
            # Block common commands that shouldn't work during panel discussions
            CommandHandler(['config', 'set_model', 'set_ollama_model', 'set_gemini_model', 'providers', 'models'], blocked_command_handler),
            CommandHandler(['ask_all_gemini', 'discuss', 'help'], blocked_command_handler),
            MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_panel_edit),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_follow_up),
        ],
    },
    fallbacks=[CommandHandler('end_discussion', end_discussion), CommandHandler('cancel', panel_cancel_command), CommandHandler('timeout', timeout_handler)],
    per_user=True,
    per_chat=True,
    block=True,
    per_message=False,
    allow_reentry=True
)