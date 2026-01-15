import logging
import re
import asyncio
import telegram
import json
from telegram import Update, BotCommand
from telegram.error import BadRequest
from telegram.error import TimedOut
from httpx import ConnectTimeout
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from telegram import BotCommandScopeChat

from utils import text_processing
from utils.llm_utilities import get_robust_llm_response, get_expert_panel_fallback_config
from telegram import constants
import config
from bot import providers
from services import web_search_service
from bot.menu_setup import setup_bot_commands_and_menu
from storage import storage_manager
from bot.settings import USER_SETTINGS  # Added for settings access
from bot.messaging import send_safe_message
from .misc_commands import cancel_command
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
        tavily_results=initial_results
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
    if "[Error:" in response_text:
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

async def _run_refinement_cycle(
    update: Update, context: ContextTypes.DEFAULT_TYPE, proposer_task, critic_task, user_prompt, full_history, placeholder_msg, panel_results,
    orchestrator_service, orchestrator_model, orchestrator_timeout, orchestrator_config, panel_config: dict
):
    """
    Executes the Master & Apprentice iterative refinement cycle.
    
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
    
    # Stateful Persona History
    proposer_history = []
    critic_history = []
    quality_gate_history = []

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
            'status': 'Success' if "[Error:" not in proposer_response else 'Failure',
            'response': proposer_response,
            'retries': proposer_retries,
            'fallback_used': proposer_fallback_used
        }
        
        if "[Error:" in proposer_response:
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

                    if "[Error:" not in fallback_response:
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
                    logger.error(f"Orchestrator's backup failed with exception: {fallback_error}")
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
            logger.warning(f"Failed to update status to 'Critic reviewing' in round {iteration}: {e}")
        
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
            'status': 'Success' if "[Error:" not in critic_response else 'Failure',
            'response': critic_response,
            'retries': critic_retries,
            'fallback_used': critic_fallback_used
        }
        
        # Handle Critic failure by proceeding to Quality Gate with modified prompt
        critic_failed = "[Error:" in critic_response
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
            quality_threshold=quality_threshold
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

        quality_gate_history.append({"role": "user", "content": quality_gate_prompt})
        quality_gate_history.append({"role": "assistant", "content": quality_response})

        # Store quality gate metrics in panel_results
        panel_results['Quality_Gate'] = {
            'provider': orchestrator_config.get('provider'),
            'model': orchestrator_config.get('model'),
            'status': 'Success' if "[Error:" not in quality_response else 'Failure',
            'response': quality_response,
            'retries': quality_retries,
            'fallback_used': quality_fallback_used
        }

        # Parse quality assessment using robust JSON extraction
        try:
            # Find the first '{' and the last '}' to extract the JSON block.
            # This is more robust against conversational text from the LLM.
            start_index = quality_response.find('{')
            end_index = quality_response.rfind('}')

            if start_index != -1 and end_index != -1 and end_index > start_index:
                json_str = quality_response[start_index:end_index+1]
                quality_assessment = json.loads(json_str)
                quality_score = quality_assessment.get('quality_score', 0)
                refinement_instructions = quality_assessment.get('refinement_instructions', '')
                
                logger.info(f"Master quality assessment - Score: {quality_score}, Threshold: {quality_threshold}")
            else:
                raise ValueError("No valid JSON object found in the quality gate response.")
                
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Quality gate parsing failed: {e}")
            logger.debug(f"Problematic quality response (first 500 chars): {quality_response[:500]}")
            logger.warning("Quality gate failed, using emergency fallback to break loop.")
            quality_score = quality_threshold  # Set to threshold to break loop
            refinement_instructions = ""
        
        # Check if quality meets threshold
        if quality_score >= quality_threshold:
            logger.info(f"Quality threshold met (Score: {quality_score} >= {quality_threshold}). Finalizing response.")
            break
        elif iteration < max_iterations:
            try:
                await placeholder_msg.edit_text(f"Quality score: {quality_score}/{quality_threshold}. Refining... (Round {iteration+1})", parse_mode=None)
            except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                logger.warning(f"Failed to update placeholder message (Refining): {e}")
            proposer_refine_template = config.PROMPTS.get_prompt('panel_proposer_refine')
            current_proposer_prompt = proposer_refine_template.format(
                user_prompt=user_prompt,
                proposer_response=proposer_response,
                quality_score=quality_score,
                refinement_instructions=refinement_instructions
            )
        else:
            logger.warning(f"Max iterations reached. Final quality score: {quality_score}")
            break
    
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
        logger.error(f"Failed to set panel-specific commands for chat {chat_id}: {e}")


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
        from bot.handlers.configure_panel_handler import load_panel_config

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

    plan_template = config.PROMPTS.get_prompt('panel_orchestrator_plan')
    meta_prompt = plan_template.format(
        user_prompt=user_prompt,
        full_history_json=json.dumps(full_history, indent=2)
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
        'status': 'Success' if "[Error:" not in orchestrator_response else 'Failure',
        'response': orchestrator_response,
        'retries': orchestrator_retries,
        'fallback_used': orchestrator_fallback_used
    }
    
    logger.debug(f"Initial Orchestrator response: {orchestrator_response[:200]}...")  # Log first 200 chars
    
    # Handle potential error responses from get_robust_llm_response
    if "[Error:" in orchestrator_response:
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
    
    logger.info(f"Successfully parsed orchestrator's plan. Search required: {requires_search}, Tasks: {len(tasks_list)}")
    
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
                                original_prompt = proposer_task.get("prompt", "")
                                augmented_prompt = (
                                    f"Based on the following comprehensive research dossier, please address the user's original query.\n\n"
                                    f"--- RESEARCH DOSSIER ---\n{research_dossier}\n\n"
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

    # Execute the Master & Apprentice refinement cycle using the helper function
    proposer_response, quality_score, iteration = await _run_refinement_cycle(
        update, context, proposer_task, critic_task, user_prompt, full_history, placeholder_msg, panel_results,
        orchestrator_service, orchestrator_model, orchestrator_timeout, orchestrator_config, panel_config
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
    synthesis_prompt = synthesis_template.format(
        full_history=json.dumps(full_history, indent=2),
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
            history=full_history,
            role_name='Refiner',
            request_timeout=refiner_role_config.get('request_timeout_seconds'),
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        refiner_response = refiner_llm_result['response']
        refiner_retries = refiner_llm_result['retries']
        refiner_fallback_used = refiner_llm_result['fallback_used']
        
        # Challenge C: Check if Refiner failed and gracefully fall back to proposer_response
        if refiner_response.startswith("[Error:") or not refiner_response.strip():
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
        panel_results, final_answer, proposer_response = await _run_panel_workflow(
            update, context, user_prompt, [], assembling_msg, chat_id
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
            "lock": asyncio.Lock()
        }

        await assembling_msg.delete()
        
        # AST-Based Architecture: Parse, Split, and Send
        pure_summary = _format_panel_summary(panel_results)
        pure_markdown_content = f"{pure_summary}\n\n---\n\n{final_answer}"

        # Use the centralized send_safe_message function
        if await send_safe_message(context, update, pure_markdown_content):
            # Incremental Archival: Save the panel's result immediately
            await storage_manager.save_message(chat_id, 'assistant:panel', final_answer)
        
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
                await context.bot.send_message(chat_id=chat_id, text=error_message, parse_mode=None)
        except Exception as send_error:
            logger.error(f"Failed to send error message: {send_error}")
            
        await _cleanup_discussion_state(context, chat_id, assembling_msg)

async def start_panel_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the /discuss_panel command."""
    chat_id = update.effective_chat.id
    user_prompt = " ".join(context.args).strip()
    if not user_prompt:
        await update.message.reply_text("Usage: /discuss_panel <topic>", parse_mode=None)
        return ConversationHandler.END

    try:
        assembling_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="Assembling an expert panel...",
            parse_mode=None
        )
    except telegram.error.NetworkError as e:
        logger.error(f"Network error while sending initial message in start_panel_discussion: {e}")
        try:
            await update.message.reply_text("A network error occurred, please try again.", parse_mode=None)
        except Exception as e_inner:
            logger.error(f"Failed to send network error message to user: {e_inner}")
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

    async with panel_state["lock"]:
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
                logger.warning(f"Failed to edit error message in follow_up: {edit_e}")
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
                logger.error(f"Unexpected error updating placeholder during cleanup: {e}")
        
        try:
            # Await the task to allow it to process the cancellation
            try:
                await panel_task
            except asyncio.CancelledError:
                logger.info(f"Panel task for chat {chat_id} was already cancelled.")
            except Exception as e:
                logger.error(f"Error awaiting cancelled panel task for chat {chat_id}: {e}")
        except asyncio.CancelledError:
            logger.info(f"Panel task for chat {chat_id} successfully processed cancellation.")
        except Exception as e:
            logger.error(f"Error awaiting cancelled panel task for chat {chat_id}: {e}")

    context.user_data.pop('panel_task', None)
    context.user_data.pop('panel_state', None)
    context.user_data.pop('panel_placeholder', None)  # Clear the placeholder reference
    
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

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the active panel discussion via a command."""
    chat_id = update.effective_chat.id
    await update.message.reply_text("Cancelling discussion...", parse_mode=None)
    await _cleanup_discussion_state(context, chat_id)
    await update.message.reply_text("Panel discussion cancelled.", parse_mode=None)
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

    search_results = await web_search_service.perform_search(query)

    if search_results.startswith("Error:"):
        await placeholder_msg.edit_text(search_results, parse_mode=None)
        return AWAITING_FOLLOW_UP

    panel_state = context.user_data.get('panel_state')
    if panel_state and panel_state.get('full_transcript'):
        async with panel_state["lock"]:
            panel_state['full_transcript'].append({'role': 'user', 'content': f"Search results for '{query}':\n{search_results}"})
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
                logger.warning(f"Failed to update summary: {e}")
                
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

    async with panel_state["lock"]:
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
                logger.error(f"Failed to remove last assistant message during panel reroll: {e}")

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
                    await placeholder_msg.edit_text(error_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
                else:
                    # Fallback to send_message if placeholder doesn't exist
                    await context.bot.send_message(chat_id, error_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
            except Exception as send_e:
                logger.error(f"Failed to send error message to user after reroll failure: {send_e}")
            
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

        await context.bot.send_message(chat_id, "Panel discussion has timed out due to inactivity.", parse_mode=None)
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

discuss_panel_conv_handler = ConversationHandler(
    entry_points=[CommandHandler('discuss_panel', start_panel_discussion)],
    states={
        AWAITING_FOLLOW_UP: [
            CommandHandler('reroll', reroll_discussion),
            CommandHandler('search', search_discussion),
            # Block common commands that shouldn't work during panel discussions
            CommandHandler(['config', 'set_model', 'set_ollama_model', 'set_gemini_model', 'providers', 'models'], blocked_command_handler),
            CommandHandler(['ask_all_gemini', 'discuss', 'help'], blocked_command_handler),
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