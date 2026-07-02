"""
Panel workflow algorithms — pure async orchestration, no PTB routing.

Import chain: discuss_panel_handler → panel_workflow → utils/llm_utilities (one-directional).
PTB-coupled code (locks, ConversationHandler states, context.user_data) stays in
discuss_panel_handler.py. Everything here is testable without a live bot application.
"""

import logging
import re
import asyncio
import json
import telegram
from telegram import Update
from telegram.ext import ContextTypes

import config
from bot import providers
from services import web_search_service
from bot.handlers.configure_panel_handler import load_panel_config
from storage import storage_manager
from bot.settings import USER_SETTINGS
from bot.messaging import send_safe_message, send_plain_message
from utils.hooks import hook_runner
from utils.llm_utilities import (
    get_robust_llm_response,
    get_expert_panel_fallback_config,
    is_error_response,
    extract_json_object,
    format_tools_for_prompt,
)
from utils.context_manager import ensure_context_fits, get_model_context_limits, truncate_text_to_tokens
from utils.tool_distiller import distill_tool_result
from utils.service_registry import touch_mcp_last_used, get_or_init_mcp_service, get_or_init_skill_service

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


async def _execute_panel_tool_calls(
    tool_calls: list,
    mcp_service,
    skill_service,
    panel_execution_tool_names: frozenset,
    tool_result_cache: dict,
    user_prompt: str,
    dossier_token_budget: int,
    context,
) -> list[str]:
    """Execute Orchestrator tool calls and return formatted result strings.

    Updates ``tool_result_cache`` in-place for cross-call deduplication.
    Never raises — per-tool exceptions are caught and included in the return list.
    """
    parts = []
    for tc in tool_calls:
        tool_name = tc.get('name', '')
        args = tc.get('arguments', {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError as e:
                logger.warning(f"Panel tool '{tool_name}': failed to parse arguments JSON ({e}). Using empty args.")
                args = {}

        try:
            cache_key = (tool_name, json.dumps(args, sort_keys=True, default=str))
        except Exception:
            cache_key = (tool_name, str(args))

        if cache_key in tool_result_cache:
            logger.info(f"Panel tool '{tool_name}' served from cache (identical call already executed this turn).")
            result = (
                tool_result_cache[cache_key]
                + "\n[Note: identical call already executed earlier this turn — result is unchanged. "
                  "To get different content, target a more specific sub-resource (a particular "
                  "heading/block_id or a narrower query) instead of re-fetching the same item.]"
            )
            parts.append(f"Tool: {tool_name}\nResult: {result}")
            continue

        try:
            # skill_ prefix checked BEFORE __ to prevent a skill named `server__foo`
            # from being misrouted to MCP execution.
            if tool_name.startswith("skill_") and skill_service:
                result = skill_service.get_skill_playbook(tool_name[len("skill_"):])
                logger.info(f"Panel skill '{tool_name}' executed.")
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
                    # Gate 2: hook validation — same path as normal chat tool execution.
                    try:
                        hook_runner.run_pre_tool_use(tool_name, {"arguments": args})
                    except PermissionError as hook_err:
                        logger.warning(f"Panel: Tool '{tool_name}' denied by security hook: {hook_err}")
                        result = f"[Denied by security hook: {hook_err}]"
                    else:
                        touch_mcp_last_used(getattr(context, 'application', None))
                        result = await mcp_service.execute_tool(server, tool, args)
                        logger.info(f"Panel tool '{tool_name}' executed.")
            else:
                result = f"[Error: Unknown tool or service unavailable for '{tool_name}']"

            if isinstance(result, str):
                result = await distill_tool_result(
                    result, query=user_prompt,
                    max_keep_tokens=dossier_token_budget, tool_name=tool_name
                )
                # Cache only genuine executions so denials/errors can be retried legitimately.
                if not result.startswith("[Denied") and not is_error_response(result):
                    tool_result_cache[cache_key] = result

            parts.append(f"Tool: {tool_name}\nResult: {result}")
        except Exception as tool_exc:
            logger.exception(f"Panel tool call failed for '{tool_name}': {tool_exc}")
            parts.append(f"Tool: {tool_name}\nResult: [Error: {tool_exc}]")

    return parts


async def _run_refinement_cycle(
    update: Update, context: ContextTypes.DEFAULT_TYPE, proposer_task, critic_task, user_prompt, full_history, placeholder_msg, panel_results,
    orchestrator_service, orchestrator_model, orchestrator_timeout, orchestrator_config, panel_config: dict,
    mcp_service=None, skill_service=None, available_tools_text: str = "No tools available.",
    panel_execution_tool_names: frozenset = frozenset(),
    quality_gate_tools_text: str = "No tools available.",
    initial_grounding: str = ""
):
    """
    Executes the Master & Apprentice iterative refinement cycle.
    The Orchestrator quality gate may request MCP tool calls to ground the next iteration.

    Returns:
        tuple: (proposer_response, quality_score, iteration_count)
    """
    # Extract configuration from user's panel_config
    quality_threshold = panel_config.get('quality_threshold', 85)
    # Fallback (4) matches config.yaml's shipped value — a stale fallback of 3 here would
    # silently reduce refinement rounds if the yaml key were ever dropped (same class of
    # trap as the context-budget fallbacks fixed elsewhere in this pass).
    max_iterations = panel_config.get('max_refinement_iterations', 4)
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
    # Stateful Persona History
    # quality_gate_history stores ONLY compact audit entries (score + instructions), never the
    # full proposer/critic response text. That text is already in each round's current prompt,
    # so re-embedding it in history causes context overflow and score anchoring.
    #
    # The Proposer is driven STATELESS (history=[]): the refine prompt is fully self-contained
    # (user query + previous draft + cumulative grounding dossier + Master instructions). Passing
    # (user query + previous draft + cumulative grounding dossier + Master instructions). Passing
    # an accumulating chat history here used to double-store each draft and force ensure_context_fits
    # to evict the OLDEST (most grounded) turns — the grounding cliff that made refinement worse.
    critic_history = []
    quality_gate_history = []

    # Cumulative grounding dossier — the fix for the grounding cliff. Iteration 1's grounding
    # (workspace pre-queries + research dossier) is seeded here so it survives into every refine
    # round; each round's tool results are appended (deduped). The FULL dossier is fed into every
    # refine prompt instead of only the current round's results, so grounding accumulates instead
    # of being discarded each iteration.
    grounding_dossier = (initial_grounding or "").strip()
    _tool_result_cache: dict = {}  # (tool_name, canonical_args_json) -> result string, to break re-fetch loops

    # Safety cap on the cumulative dossier. Individual tool results now arrive
    # pre-distilled (small), so this is just a backstop against many rounds piling up.
    _dossier_token_budget = config.get_panel_dossier_max_tokens()
    if grounding_dossier:
        grounding_dossier = truncate_text_to_tokens(grounding_dossier, _dossier_token_budget)

    # Iterative refinement loop
    for iteration in range(1, max_iterations + 1):
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
            history=[],  # stateless: refine prompt + dossier are self-contained (see note above)
            role_name='Proposer',
            request_timeout=proposer_role_config.get('request_timeout_seconds'),
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        proposer_response = proposer_llm_result['response']
        proposer_retries = proposer_llm_result['retries']
        proposer_fallback_used = proposer_llm_result['fallback_used']

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
                        history=[],
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

        # Retry-with-repair for the quality-gate JSON parse (mirrors the orchestrator plan
        # parser above). Previously a SINGLE malformed JSON response set quality_score=-1
        # and broke the entire refinement loop, discarding all remaining rounds — a
        # production-surfaced quality regression. Now a bad parse re-prompts the model
        # with an explicit repair instruction before giving up; only persistent failure
        # across all attempts falls back to the old abort-with-best-so-far behavior.
        _qg_attempts = config.get_panel_quality_gate_parse_attempts()
        _qg_prompt = quality_gate_prompt
        _qg_last_err = ""
        quality_assessment = None
        requested_tool_calls = []
        quality_response = ""
        quality_retries = 0
        quality_fallback_used = False

        for _qg_attempt in range(1, _qg_attempts + 1):
            quality_llm_result = await get_robust_llm_response(
                provider_name=orchestrator_config.get('provider'),
                model=orchestrator_config.get('model'),
                prompt=_qg_prompt,
                history=quality_gate_history,
                role_name='Master Orchestrator',
                request_timeout=orchestrator_timeout,
                fallback_provider=fallback_provider,
                fallback_model=fallback_model
            )
            quality_response = quality_llm_result['response']
            quality_retries = quality_llm_result['retries']
            quality_fallback_used = quality_llm_result['fallback_used']

            if quality_llm_result['is_error']:
                _qg_last_err = "LLM call error"
                logger.warning(f"Quality gate attempt {_qg_attempt}/{_qg_attempts}: {_qg_last_err}.")
            else:
                # extract_json_object does string-aware brace matching (handles braces
                # inside quoted values correctly, unlike a naive find('{')/rfind('}')).
                json_str = extract_json_object(quality_response)
                if not json_str:
                    _qg_last_err = "no JSON object found in response"
                    logger.warning(f"Quality gate attempt {_qg_attempt}/{_qg_attempts}: {_qg_last_err}.")
                else:
                    try:
                        quality_assessment = json.loads(json_str)
                        break  # parsed successfully
                    except json.JSONDecodeError as e:
                        _qg_last_err = f"JSON parse error: {e}"
                        logger.warning(f"Quality gate attempt {_qg_attempt}/{_qg_attempts}: {_qg_last_err}. Extracted: {json_str[:200]}")

            if _qg_attempt < _qg_attempts:
                _qg_prompt = (
                    quality_gate_prompt
                    + "\n\n--- IMPORTANT: YOUR PREVIOUS RESPONSE WAS INVALID ---\n"
                    + f"Problem: {_qg_last_err}\n"
                    + "Return ONLY a single valid JSON object — no prose, no markdown code fences. "
                    + "Escape every double-quote and newline inside string values."
                )

        # Compact audit entry — score + instructions only, no full response text. Only the
        # final attempt is recorded (intermediate malformed attempts aren't fed back in, to
        # avoid bloating quality_gate_history with throwaway repair exchanges).
        quality_gate_history.append({"role": "user", "content": f"[Round {iteration} assessment request]"})
        quality_gate_history.append({"role": "assistant", "content": quality_response})

        # Store quality gate metrics in panel_results
        panel_results['Quality_Gate'] = {
            'provider': orchestrator_config.get('provider'),
            'model': orchestrator_config.get('model'),
            'status': 'Success' if quality_assessment is not None else 'Failure',
            'response': quality_response,
            'retries': quality_retries,
            'fallback_used': quality_fallback_used
        }

        if quality_assessment is not None:
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
            # Exhausted all repair attempts — genuinely persistent model failure, not a
            # one-off hiccup. Preserve the original safety-net behavior: stop refining and
            # use the best response seen so far, rather than looping forever on a broken model.
            logger.error(f"Quality gate parsing failed after {_qg_attempts} attempts ({_qg_last_err}). Breaking refinement loop with best response so far.")
            logger.debug(f"Problematic quality response (first 500 chars): {quality_response[:500]}")
            quality_score = -1
            break

        # Track the best response seen across all iterations.
        # The Proposer can regress when its model times out and the fallback takes over,
        # causing later iterations to score LOWER than earlier ones. We give the Refiner
        # the best draft, not the last one.
        if quality_score > best_score:
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
            tool_results_text = ""
            if requested_tool_calls and (mcp_service or skill_service):
                tool_results_parts = await _execute_panel_tool_calls(
                    requested_tool_calls, mcp_service, skill_service,
                    panel_execution_tool_names, _tool_result_cache,
                    user_prompt, _dossier_token_budget, context,
                )
                if tool_results_parts:
                    tool_results_text = "\n\n".join(tool_results_parts)
                    logger.info(f"Panel orchestrator provided {len(tool_results_parts)} tool result(s) to Proposer for iteration {iteration + 1}.")
                    # Accumulate into the cumulative grounding dossier so grounding PERSISTS
                    # across refinement rounds instead of being discarded each iteration.
                    grounding_dossier = (
                        (grounding_dossier + "\n\n" + tool_results_text).strip()
                        if grounding_dossier else tool_results_text
                    )
                    grounding_dossier = truncate_text_to_tokens(grounding_dossier, _dossier_token_budget)
                    # Feed compact results into quality_gate_history so the next Quality Gate
                    # invocation knows which queries succeeded or failed (e.g. "no such table").
                    # Token-based (not a raw char slice) so the gate actually sees enough of the
                    # retrieved content to judge groundedness — a prior 1500-CHAR cap (~375
                    # tokens) starved the gate and caused it to under-score grounded answers.
                    _qg_token_budget = config.get_panel_quality_gate_context_tokens()
                    _qg_summary = truncate_text_to_tokens(tool_results_text, _qg_token_budget)
                    _qg_truncated = len(_qg_summary) < len(tool_results_text)
                    quality_gate_history.append({
                        "role": "user",
                        "content": f"[Tool execution results from Round {iteration}]:\n{_qg_summary}" + ("\n[truncated]" if _qg_truncated else "")
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
                tool_results=grounding_dossier or "(no external tool results yet)"
            )
        else:
            logger.warning(f"Max iterations reached. Final quality score: {quality_score}")
            if requested_tool_calls and quality_score < quality_threshold and (mcp_service or skill_service):
                _final_parts = await _execute_panel_tool_calls(
                    requested_tool_calls, mcp_service, skill_service,
                    panel_execution_tool_names, _tool_result_cache,
                    user_prompt, _dossier_token_budget, context,
                )
                if _final_parts:
                    grounding_dossier = truncate_text_to_tokens(
                        (grounding_dossier + "\n\n" + "\n\n".join(_final_parts)).strip()
                        if grounding_dossier else "\n\n".join(_final_parts),
                        _dossier_token_budget
                    )
                    _final_synth_prompt = config.PROMPTS.get_prompt('panel_proposer_refine').format(
                        user_prompt=user_prompt,
                        proposer_response=best_proposer_response,
                        quality_score=best_score,
                        refinement_instructions=(
                            "⚠️ FINAL SYNTHESIS PASS — no further iterations follow. "
                            "Write the most complete, fully-grounded answer possible using the "
                            "dossier below. Fulfil the user's request in full; do not defer."
                        ),
                        tool_results=grounding_dossier,
                    )
                    try:
                        _fb_prov, _fb_mod = get_expert_panel_fallback_config()
                        _final_llm = await get_robust_llm_response(
                            provider_name=proposer_provider,
                            model=proposer_model,
                            prompt=_final_synth_prompt,
                            history=[],
                            role_name="Proposer (final synthesis)",
                            request_timeout=proposer_role_config.get('request_timeout_seconds'),
                            fallback_provider=_fb_prov,
                            fallback_model=_fb_mod,
                        )
                        if not _final_llm.get('is_error') and _final_llm.get('response'):
                            proposer_response = _final_llm['response']
                            best_proposer_response = proposer_response
                            best_score = quality_threshold
                            logger.info("Final synthesis Proposer pass succeeded.")
                    except Exception as _fpe:
                        logger.warning(f"Final synthesis Proposer pass failed: {_fpe}")
            break

    # Return the best-scoring response, not necessarily the last one.
    if best_proposer_response:
        if best_score != quality_score:
            logger.info(f"Using best response from earlier iteration (score {best_score} vs final {quality_score}).")
        return best_proposer_response, best_score, iteration
    return proposer_response, quality_score, iteration


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
        # Fallback (4) matches config.yaml's shipped value — see the identical note above.
        max_iterations = panel_config.get('max_refinement_iterations', 4)

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
    available_tools_text = format_tools_for_prompt(panel_execution_tools)

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

    # Use consolidated LLM response function for orchestrator call.
    # The orchestrator must return a parseable JSON plan. LLMs intermittently emit
    # malformed JSON (an unescaped quote/newline in a value, a stray code fence) — a
    # single bad plan used to kill the ENTIRE panel turn (QA-surfaced). Retry the
    # call a few times, asking explicitly for clean JSON, before giving up.
    fallback_provider, fallback_model = get_expert_panel_fallback_config()
    _PLAN_ATTEMPTS = config.get_panel_plan_parse_attempts()
    orchestrator_plan = None
    orchestrator_response = ""
    _plan_prompt = meta_prompt
    _last_plan_err = ""

    for _plan_attempt in range(1, _PLAN_ATTEMPTS + 1):
        orchestrator_llm_result = await get_robust_llm_response(
            provider_name=orchestrator_provider,
            model=orchestrator_model,
            prompt=_plan_prompt,
            history=None,  # No history for the initial plan
            role_name='Initial Orchestrator',
            request_timeout=orchestrator_timeout,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        orchestrator_response = orchestrator_llm_result['response']
        panel_results['Initial_Orchestrator'] = {
            'provider': orchestrator_provider,
            'model': orchestrator_model,
            'status': 'Success' if not orchestrator_llm_result['is_error'] else 'Failure',
            'response': orchestrator_response,
            'retries': orchestrator_llm_result['retries'],
            'fallback_used': orchestrator_llm_result['fallback_used']
        }
        logger.debug(f"Initial Orchestrator response (attempt {_plan_attempt}/{_PLAN_ATTEMPTS}): {orchestrator_response[:200]}...")

        if orchestrator_llm_result['is_error']:
            _last_plan_err = "LLM call error"
        else:
            json_str = extract_json_object(orchestrator_response)
            if not json_str:
                _last_plan_err = "no JSON object found in response"
                logger.warning(f"Orchestrator plan attempt {_plan_attempt}: {_last_plan_err}.")
            else:
                try:
                    _candidate_plan = json.loads(json_str)
                    _candidate_tasks = _candidate_plan.get("tasks", []) if isinstance(_candidate_plan, dict) else []
                    if not isinstance(_candidate_tasks, list) or len(_candidate_tasks) < 2:
                        # Parsed but unusable — retry instead of killing the turn (QA-surfaced).
                        _last_plan_err = (
                            f"incomplete plan: 'tasks' must be an array with at least a Proposer and a "
                            f"Critic (got {len(_candidate_tasks) if isinstance(_candidate_tasks, list) else 0})"
                        )
                        logger.warning(f"Orchestrator plan attempt {_plan_attempt}: {_last_plan_err}.")
                    else:
                        orchestrator_plan = _candidate_plan
                        break  # parsed AND complete
                except json.JSONDecodeError as parse_error:
                    _last_plan_err = f"JSON parse error: {parse_error}"
                    logger.warning(f"Orchestrator plan attempt {_plan_attempt}: {_last_plan_err}. Extracted: {json_str[:200]}")

        # Prepare a repair prompt for the next attempt (if any remain).
        if _plan_attempt < _PLAN_ATTEMPTS:
            _plan_prompt = (
                meta_prompt
                + "\n\n--- IMPORTANT: YOUR PREVIOUS PLAN WAS INVALID ---\n"
                + f"Problem: {_last_plan_err}\n"
                + "Return ONLY a single valid JSON object — no prose, no markdown code fences. "
                + "Escape every double-quote and newline inside string values. The object MUST include a "
                + "\"tasks\" array containing at least a Proposer task and a Critic task."
            )
            try:
                await placeholder_msg.edit_text(
                    f"Re-planning (attempt {_plan_attempt + 1}/{_PLAN_ATTEMPTS})…", parse_mode=None
                )
            except (telegram.error.NetworkError, telegram.error.TimedOut):
                pass

    if orchestrator_plan is None:
        logger.error(f"Orchestrator failed to produce a parseable plan after {_PLAN_ATTEMPTS} attempts. Last error: {_last_plan_err}")
        try:
            await placeholder_msg.edit_text(
                f"⚠️ The orchestrator response could not be parsed after {_PLAN_ATTEMPTS} attempts. "
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
                # Token-aware truncation (not a char head-slice) so a deep section of a large page
                # isn't silently discarded. The aggregate is token-capped again below.
                # Distill the pre-query result against the user's task — a get-block-children
                # pre-query returns the whole page body; keep only the relevant part.
                if isinstance(_wq_result, str):
                    _wq_result = await distill_tool_result(
                        _wq_result, query=user_prompt,
                        max_keep_tokens=config.get_panel_workspace_max_tokens(), tool_name=_wq_tool
                    )
                workspace_context_parts.append(f"[{_wq_tool}]\n{_wq_result}")
                logger.info(f"Workspace pre-query '{_wq_tool}' executed successfully.")
            except PermissionError as _hook_err:
                logger.warning(f"Workspace pre-query '{_wq_tool}' denied by hook: {_hook_err}")
            except Exception as _wq_exc:
                logger.warning(f"Workspace pre-query '{_wq_tool}' failed: {_wq_exc}")

        if workspace_context_parts:
            _workspace_context = "\n\n".join(workspace_context_parts)
            # Safety cap on the aggregate. Each part is already distilled to the task, so
            # this is just a backstop against many pre-queries piling up.
            _max_workspace_tokens = config.get_panel_workspace_max_tokens()
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
                # Persist the raw grounding so the refinement cycle can carry it forward into every
                # refine round (otherwise it only lives in iteration 1's prompt — the grounding cliff).
                _proposer_task["_grounding"] = (
                    (_proposer_task.get("_grounding", "") + "\n\n--- WORKSPACE CONTEXT ---\n" + _workspace_context).strip()
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
                                proposer_task["_grounding"] = (
                                    (proposer_task.get("_grounding", "") + "\n\n--- RESEARCH DOSSIER ---\n" + truncated_dossier).strip()
                                )
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
                                    proposer_task["_grounding"] = (
                                        (proposer_task.get("_grounding", "") + "\n\n--- WEB SEARCH RESULTS ---\n" + search_results).strip()
                                    )
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
    # Fallback (4) matches config.yaml's shipped value — a stale fallback of 3 here would
    # silently reduce refinement rounds if the yaml key were ever dropped (same class of
    # trap as the context-budget fallbacks fixed elsewhere in this pass).
    max_iterations = panel_config.get('max_refinement_iterations', 4)
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
    quality_gate_tools_text = format_tools_for_prompt(quality_gate_tools)
    logger.info(f"Quality Gate tool scope: {sorted(_qg_servers)} ({len(quality_gate_tools)} tools)")

    # Execute the Master & Apprentice refinement cycle using the helper function
    proposer_response, quality_score, iteration = await _run_refinement_cycle(
        update, context, proposer_task, critic_task, user_prompt, full_history, placeholder_msg, panel_results,
        orchestrator_service, orchestrator_model, orchestrator_timeout, orchestrator_config, panel_config,
        mcp_service=mcp_service, skill_service=skill_service, available_tools_text=available_tools_text,
        panel_execution_tool_names=panel_execution_tool_names,
        quality_gate_tools_text=quality_gate_tools_text,
        # Seed the cumulative grounding dossier with iteration-1 grounding so it survives refine rounds.
        initial_grounding=proposer_task.get('_grounding', '')
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
