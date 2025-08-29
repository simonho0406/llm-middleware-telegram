import logging
import re
import asyncio
import json
from telegram import Update, BotCommand
from telegram.error import BadRequest
from telegram.error import TimedOut
from httpx import ConnectTimeout
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from telegram import BotCommandScopeChat
from utils.text_processing import split_message_markdown_aware, escape_markdown_v2
from utils.llm_utilities import get_robust_llm_response, get_expert_panel_fallback_config
from telegram import constants
import config
from bot import providers
from services import web_search_service
from bot.menu_setup import setup_bot_commands_and_menu
from storage import storage_manager
from bot.settings import USER_SETTINGS  # Added for settings access
from .misc_commands import cancel_command
from bot.errors import ProviderUnavailableError

# Define conversation states
AWAITING_FOLLOW_UP, PANEL_IN_PROGRESS = range(2)

logger = logging.getLogger(__name__)


async def _run_refinement_cycle(
    proposer_task, critic_task, user_prompt, full_history, placeholder_msg, panel_results,
    orchestrator_service, orchestrator_model, orchestrator_timeout, orchestrator_config
):
    """
    Executes the Master & Apprentice iterative refinement cycle.
    
    Returns:
        tuple: (proposer_response, quality_score, iteration_count)
    """
    # Extract configuration
    quality_threshold = config.EXPERT_PANEL_CONFIG.get('quality_threshold', 85)
    max_iterations = config.EXPERT_PANEL_CONFIG.get('max_refinement_iterations', 3)
    role_configs = config.EXPERT_PANEL_CONFIG.get('roles', {})
    
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
    
    # Iterative refinement loop
    for iteration in range(1, max_iterations + 1):
        await placeholder_msg.edit_text(f"Round {iteration}/{max_iterations}: Proposer is working...", parse_mode=None)
        
        # Execute Proposer
        fallback_provider, fallback_model = get_expert_panel_fallback_config()
        proposer_response = await get_robust_llm_response(
            provider_name=proposer_provider,
            model=proposer_model,
            prompt=current_proposer_prompt,
            history=full_history,
            role_name='Proposer',
            request_timeout=proposer_role_config.get('request_timeout_seconds'),
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        
        # Update panel results with Proposer response
        proposer_fallback = "[Fallback by Orchestrator" in proposer_response
        panel_results['Proposer'] = {
            'provider': proposer_provider,
            'model': proposer_model,
            'status': 'Success' if "[Error:" not in proposer_response else 'Failure',
            'response': proposer_response,
            'fallback': proposer_fallback
        }
        
        if "[Error:" in proposer_response:
            logger.error(f"Proposer failed: {proposer_response}")
            return proposer_response, 0, iteration
        
        await placeholder_msg.edit_text(f"Round {iteration}/{max_iterations}: Critic is reviewing...", parse_mode=None)
        
        # Execute Critic
        enhanced_critic_prompt = f"""
        {critic_prompt_template}
        
        **Current Response to Evaluate:**
        --- PROPOSER'S RESPONSE ---
        {proposer_response}
        --- END RESPONSE ---
        
        **Original User Query:**
        {user_prompt}
        
        **Detailed Instructions:**
        • Evaluate the response's accuracy, completeness, and clarity
        • Check for any factual errors or logical inconsistencies  
        • Assess if the response fully addresses the user's question
        • Note if the response doesn't fully address the user's query
        • Be constructive but uncompromising in your standards
        
        **Critical Task:** Your goal is to find legitimate flaws and improvement opportunities. Be thorough and specific in your critique.
        """
        
        critic_response = await get_robust_llm_response(
            provider_name=critic_provider,
            model=critic_model,
            prompt=enhanced_critic_prompt,
            history=full_history,
            role_name='Critic',
            request_timeout=critic_role_config.get('request_timeout_seconds'),
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        critic_fallback = "[Fallback by Orchestrator" in critic_response
        panel_results['Critic'] = {
            'provider': critic_provider,
            'model': critic_model,
            'status': 'Success' if "[Error:" not in critic_response else 'Failure',
            'response': critic_response,
            'fallback': critic_fallback
        }
        
        # Handle Critic failure by proceeding to Quality Gate with modified prompt
        critic_failed = "[Error:" in critic_response
        if critic_failed:
            logger.error(f"Critic failed: {critic_response}")
            # Replace critic response with failure explanation for the Master
            critic_response = "[The Critic agent failed to provide a review. Please assess the Proposer's work directly.]"
        
        await placeholder_msg.edit_text(f"Round {iteration}/{max_iterations}: Master is assessing quality...", parse_mode=None)
        
        # Execute Quality Gate Assessment
        quality_gate_prompt = f"""
        **Role & High-Level Task:** You are the Master overseeing an apprentice's work. Evaluate the quality of their response and provide specific refinement instructions if needed.
        
        **Tone & Persona:** Be a discerning but constructive master. Provide specific, actionable feedback that will lead to measurable improvement.
        
        **Dynamic Content:**
        Original User Query:
        --- USER QUERY ---
        {user_prompt}
        --- END QUERY ---
        
        Apprentice's Current Response:
        --- APPRENTICE RESPONSE ---
        {proposer_response}
        --- END RESPONSE ---
        
        Expert Critique:
        --- EXPERT CRITIQUE ---
        {critic_response}
        --- END CRITIQUE ---
        
        **Detailed Instructions:**
        Evaluate the apprentice's response considering the expert critique (or lack thereof):
        • If the critique shows "[The Critic agent failed...]", scrutinize the response more carefully on your own
        • Score from 1-100 based on: accuracy, completeness, clarity, addressing user's question
        • If score < {quality_threshold}, provide specific refinement instructions
        • If score >= {quality_threshold}, the work meets standards
        
        Provide assessment in JSON format:
        {{
          "quality_score": integer_from_1_to_100,
          "refinement_instructions": "specific_instructions_for_apprentice_or_empty_if_sufficient"
        }}
        
        **Critical Output Requirement:** Your response MUST be ONLY a valid JSON object. No other text.
        """
        
        quality_response = await get_robust_llm_response(
            provider_name=orchestrator_config.get('provider'),
            model=orchestrator_config.get('model'),
            prompt=quality_gate_prompt,
            history=None,
            role_name='Master Orchestrator',
            request_timeout=orchestrator_timeout,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        
        # Parse quality assessment
        try:
            json_match = re.search(r'{[\s\S]*}', quality_response)
            if json_match:
                json_str = json_match.group(0)
                quality_assessment = json.loads(json_str)
                quality_score = quality_assessment.get('quality_score', 0)
                refinement_instructions = quality_assessment.get('refinement_instructions', '')
                
                logger.info(f"Master quality assessment - Score: {quality_score}, Threshold: {quality_threshold}")
            else:
                raise ValueError("No valid JSON found in quality gate response.")
                
        except (json.JSONDecodeError, ValueError, Exception) as e:
            logger.error(f"Quality gate parsing failed: {e}")
            logger.warning("Quality gate failed, using emergency fallback")
            quality_score = quality_threshold  # Set to threshold to break loop
            refinement_instructions = ""
        
        # Check if quality meets threshold
        if quality_score >= quality_threshold:
            logger.info(f"Quality threshold met (Score: {quality_score} >= {quality_threshold}). Finalizing response.")
            break
        elif iteration < max_iterations:
            # Prepare refined prompt for next iteration
            await placeholder_msg.edit_text(f"Quality score: {quality_score}/{quality_threshold}. Refining... (Round {iteration+1})", parse_mode=None)
            current_proposer_prompt = f"""
            **Role & High-Level Task:** You are the research apprentice receiving feedback from your Master. Improve your previous response based on specific instructions.
            
            **Tone & Persona:** Be receptive to feedback and meticulous in your improvements. Think step-by-step about addressing each point raised.
            
            **Dynamic Content:**
            Original User Query:
            --- USER QUERY ---
            {user_prompt}
            --- END QUERY ---
            
            Your Previous Response:
            --- PREVIOUS DRAFT ---
            {proposer_response}
            --- END DRAFT ---
            
            Master's Refinement Instructions (Quality Score: {quality_score}):
            --- MASTER FEEDBACK ---
            {refinement_instructions}
            --- END FEEDBACK ---
            
            **Detailed Instructions:**
            • Address each point in the Master's feedback systematically
            • Keep what works well from your previous response
            • Improve or add content where instructed
            • Ensure your response fully answers the original user query
            • Write clearly and comprehensively
            
            **Critical Task:** Provide an improved, comprehensive response that addresses the Master's specific feedback while maintaining quality of your previous good points.
            """
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
    """Formats the results of the panel execution into a markdown string with quality metrics."""
    from utils.text_processing import escape_markdown_v2
    summary_parts = ["*Panel Execution Summary:*"]
    
    # Extract quality metrics first (if available)
    quality_metrics = panel_results.get('Quality_Metrics', {})
    
    # Format agent execution results (skip Quality_Metrics entry)
    for role, result in panel_results.items():
        if role == 'Quality_Metrics':  # Skip the metrics entry in agent listing
            continue
            
        status_icon = "✅" if result.get('status') == 'Success' else "⚠️"
        # Escape only dynamic parts
        provider = escape_markdown_v2(result.get('provider', 'Unknown'))
        model = escape_markdown_v2(result.get('model', 'Unknown'))
        status = escape_markdown_v2(result.get('status', 'Unknown'))
        fallback_note = escape_markdown_v2(" (Fallback)") if result.get('fallback') else ""
        summary_parts.append(f"{status_icon} *{role}:* `{provider}/{model}` ({status}){fallback_note}")
    
    # Add quality metrics section if available
    if quality_metrics:
        final_score = quality_metrics.get('final_score', 'N/A')
        threshold = quality_metrics.get('threshold', 'N/A')
        iterations_used = quality_metrics.get('iterations_used', 'N/A')
        max_iterations = quality_metrics.get('max_iterations', 'N/A')
        
        # Determine quality status icon
        if isinstance(final_score, (int, float)) and isinstance(threshold, (int, float)):
            quality_icon = "🎯" if final_score >= threshold else "📈"
        else:
            quality_icon = "📊"
        
        summary_parts.append("")  # Empty line for separation
        summary_parts.append("*Quality Metrics:*")
        summary_parts.append(f"{quality_icon} Final Score: `{final_score}/{threshold}` \\(Achieved/Threshold\\)")
        summary_parts.append(f"🔄 Refinement Rounds: `{iterations_used}/{max_iterations}` \\(Used/Max\\)")
    
    return "\n".join(summary_parts)

async def _run_panel_workflow(context: ContextTypes.DEFAULT_TYPE, user_prompt: str, full_history: list, placeholder_msg, chat_id: int) -> tuple:
    """Runs the full panel workflow, updating a placeholder message, and returns a dictionary of results and the final answer."""
    panel_results = {}

    # --- 0. Configuration Validation ---
    await placeholder_msg.edit_text("Assembling panel... Validating configuration...", parse_mode=None)
    
    # Validate expert panel configuration
    try:
        # Load configuration needed for the workflow
        quality_threshold = config.EXPERT_PANEL_CONFIG.get('quality_threshold', 85)
        max_iterations = config.EXPERT_PANEL_CONFIG.get('max_refinement_iterations', 3)
        
        orchestrator_config = config.EXPERT_PANEL_CONFIG.get('orchestrator', {})
        orchestrator_provider = orchestrator_config.get('provider')
        orchestrator_model = orchestrator_config.get('model')
        orchestrator_timeout = orchestrator_config.get('request_timeout_seconds', 600)  # Default 10 minutes
        
        if not orchestrator_config:
            raise ValueError("Configuration Error: The 'orchestrator' section is missing from expert_panel in config.yaml.")
        if not orchestrator_provider:
            raise ValueError("Configuration Error: The 'provider' field is missing from orchestrator in expert_panel config.")
        if not orchestrator_model:
            raise ValueError("Configuration Error: The 'model' field is missing from orchestrator in expert_panel config.")
        
        # Validate role configurations
        role_configs = config.EXPERT_PANEL_CONFIG.get('roles', {})
        required_roles = ['Proposer', 'Critic']
        
        for role in required_roles:
            role_config = role_configs.get(role, {})
            if not role_config:
                raise ValueError(f"Configuration Error: The '{role}' role is missing from expert_panel roles in config.yaml.")
            if not role_config.get('provider'):
                raise ValueError(f"Configuration Error: The 'provider' field is missing for {role} role in expert_panel config.")
            if not role_config.get('model'):
                raise ValueError(f"Configuration Error: The 'model' field is missing for {role} role in expert_panel config.")
        
        # Validate Refiner role if present
        refiner_config = role_configs.get('Refiner', {})
        if refiner_config and (not refiner_config.get('provider') or not refiner_config.get('model')):
            raise ValueError("Configuration Error: The 'Refiner' role is incomplete - missing provider or model in expert_panel config.")
            
    except ValueError as config_error:
        # Return user-friendly configuration error
        await placeholder_msg.edit_text(
            f"⚠️ {str(config_error)} Please check your configuration and use /reroll to try again.",
            parse_mode=None
        )
        return {}, f"[{str(config_error)}]"

    # --- 1. Deconstruct Task ---
    await placeholder_msg.edit_text("Assembling panel... Decomposing task...", parse_mode=None)
    
    orchestrator_service = providers.get_service_for_provider(orchestrator_provider)
    if orchestrator_service is None:
        raise ValueError(f"Orchestrator service '{orchestrator_provider}' is not available.")

    # Master & Apprentice Architecture: Initial Orchestrator (Project Manager)
    meta_prompt = f"""
    **Role & High-Level Task:** You are a meticulous project manager for an expert panel. Your mission is to create a flawless execution plan that will guide your team to produce an exceptional response.
    
    **Tone & Persona:** Be systematic and thorough. Think step-by-step about what each agent needs to succeed. You are the strategic planner, not the executor.
    
    **Dynamic Content:**
    Conversation History:
    --- CONVERSATION HISTORY ---
    {json.dumps(full_history, indent=2)}
    --- END HISTORY ---
    
    Latest User Request:
    --- LATEST REQUEST ---
    {user_prompt}
    --- END REQUEST ---
    
    **Detailed Instructions:**
    Create a comprehensive execution plan by analyzing the user's request in context. Your output must be a valid JSON object with this structure:
    {{
      "requires_search": boolean,
      "search_query": "string (only if requires_search is true, otherwise empty)",
      "tasks": [
        {{"role": "Proposer", "prompt": "Detailed, self-contained prompt for the research apprentice..."}},
        {{"role": "Critic", "prompt": "Detailed, self-contained prompt for the rigorous fact-checker..."}},
        {{"role": "Refiner", "prompt": "Generic instruction to polish the final response for clarity and style..."}}
      ]
    }}
    
    Guidelines:
    • Set requires_search=true only if the query needs current events or real-time information
    • Proposer prompts should be comprehensive and include all necessary context
    • Critic prompts should emphasize finding flaws, gaps, and inaccuracies
    • Refiner prompts should focus on final polish and presentation
    • Do NOT answer the user's question - only create the execution plan
    
    **Critical Output Requirement:** Your response MUST be ONLY a valid JSON object. No other text.
    """

    # Use consolidated LLM response function for initial Orchestrator call with integrated JSON parsing retry
    logger.info("Invoking Initial Orchestrator (Project Manager) with retry logic...")
    
    tasks_list = None
    requires_search = False
    search_query = ""
    
    # Use consolidated LLM response function for orchestrator call
    fallback_provider, fallback_model = get_expert_panel_fallback_config()
    orchestrator_response = await get_robust_llm_response(
        provider_name=orchestrator_provider,
        model=orchestrator_model,
        prompt=meta_prompt,
        history=None,  # No history for the initial plan
        role_name='Initial Orchestrator',
        request_timeout=orchestrator_timeout,
        fallback_provider=fallback_provider,
        fallback_model=fallback_model
    )
    
    logger.debug(f"Initial Orchestrator response: {orchestrator_response[:200]}...")  # Log first 200 chars
    
    # Handle potential error responses from get_robust_llm_response
    if "[Error:" in orchestrator_response:
        await placeholder_msg.edit_text(
            f"⚠️ The orchestrator failed to create a valid plan. "
            f"Please use /reroll to try again or /cancel to exit.",
            parse_mode=None
        )
        return {}, f"[System Error: Orchestrator planning failed. Use /reroll to retry.]"
    
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
        await placeholder_msg.edit_text(
            f"⚠️ The orchestrator response was invalid. "
            f"Please use /reroll to try again or /cancel to exit.",
            parse_mode=None
        )
        return {}, f"[System Error: Invalid orchestrator response format. Use /reroll to retry.]"
    
    try:
        orchestrator_plan = json.loads(json_str)
    except json.JSONDecodeError as parse_error:
        logger.error(f"JSON parsing failed for extracted string: {json_str[:200]}...")
        await placeholder_msg.edit_text(
            f"⚠️ The orchestrator response could not be parsed. "
            f"Please use /reroll to try again or /cancel to exit.",
            parse_mode=None
        )
        return {}, f"[System Error: Orchestrator response parsing failed. Use /reroll to retry.]"
    
    # Extract search requirements and tasks from the plan
    requires_search = orchestrator_plan.get("requires_search", False)
    search_query = orchestrator_plan.get("search_query", "")
    tasks_list = orchestrator_plan.get("tasks", [])
    
    if not tasks_list or len(tasks_list) < 2:  # Need at least Proposer and Critic
        logger.error(f"Invalid orchestrator plan: insufficient tasks ({len(tasks_list) if tasks_list else 0})")
        await placeholder_msg.edit_text(
            f"⚠️ The orchestrator plan was incomplete. "
            f"Please use /reroll to try again or /cancel to exit.",
            parse_mode=None
        )
        return {}, f"[System Error: Incomplete orchestrator plan. Use /reroll to retry.]"
    
    logger.info(f"Successfully parsed orchestrator's plan. Search required: {requires_search}, Tasks: {len(tasks_list)}")
    
    # If search is required, check user setting and conditionally perform it
    if requires_search and search_query:
        # Check if auto-search is enabled for panel discussions
        autosearch_enabled = await storage_manager.get_user_setting(
            chat_id, 
            'autosearch_panel_discussion', 
            USER_SETTINGS['autosearch_panel_discussion']['default']
        )
        
        if autosearch_enabled:
            await placeholder_msg.edit_text(f"Orchestrator requested web search: \"{search_query}\"...", parse_mode=None)
            search_results = await web_search_service.perform_search(search_query)
            
            if search_results.startswith("Error:"):
                await placeholder_msg.edit_text(f"⚠️ Web search failed: {search_results}", parse_mode=None)
                await asyncio.sleep(2)
            else:
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
            await placeholder_msg.edit_text(f"Auto-search disabled. Proceeding without web search...", parse_mode=None)
            
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
            await asyncio.sleep(1)  # Brief pause for user feedback

    # The retry logic and error handling is now handled by get_robust_llm_response

    # --- 2. Master & Apprentice Architecture: Iterative Quality Loop ---
    quality_threshold = config.EXPERT_PANEL_CONFIG.get('quality_threshold', 85)
    max_iterations = config.EXPERT_PANEL_CONFIG.get('max_refinement_iterations', 3)
    iteration = 1
    quality_score = 0  # Initialize quality score
    proposer_response = ""
    critic_response = ""
    role_configs = config.EXPERT_PANEL_CONFIG.get('roles', {})
    
    # Find Proposer and Critic in tasks_list
    proposer_task = next((t for t in tasks_list if t.get('role') == 'Proposer'), None)
    critic_task = next((t for t in tasks_list if t.get('role') == 'Critic'), None)
    
    if not proposer_task or not critic_task:
        raise RuntimeError("Orchestrator's plan must include Proposer and Critic roles.")

    # Execute the Master & Apprentice refinement cycle using the helper function
    proposer_response, quality_score, iteration = await _run_refinement_cycle(
        proposer_task, critic_task, user_prompt, full_history, placeholder_msg, panel_results,
        orchestrator_service, orchestrator_model, orchestrator_timeout, orchestrator_config
    )
    if quality_score < quality_threshold and iteration == max_iterations:
        await placeholder_msg.edit_text(f"Reached maximum iterations. Final quality score: {quality_score}/{quality_threshold}", parse_mode=None)
    # --- 3. Synthesize Final Answer ---
    await placeholder_msg.edit_text("Synthesizing final answer...", parse_mode=None)
    # proposer_response is already available from the refinement cycle
    critic_response = panel_results.get("Critic", {}).get('response', 'No response from critic.')
    
    synthesis_prompt = f"""
    You are a lead editor. Your task is to synthesize the work of your expert panel into a final answer for the user, taking into account the entire conversation history.
    
    --- CONVERSATION HISTORY ---
    {json.dumps(full_history, indent=2)}

    --- LATEST USER QUERY ---
    {user_prompt}
    
    --- INITIAL PROPOSAL ---\n{proposer_response}\n\n
    --- EXPERT CRITIQUE ---\n{critic_response}\n\n
    --- YOUR TASK ---\n
    Synthesize the proposal and critique into a direct, comprehensive answer to the LATEST USER QUERY, using the history for context. Do not repeat old information unless necessary.
    """
    
    # Step 4: Optional Final Polish with Refiner
    # After quality gate loop completes, proposer_response holds the final approved draft
    refiner_task = next((t for t in tasks_list if t.get('role') == 'Refiner'), None)
    refiner_role_config = role_configs.get('Refiner', {})
    refiner_provider = refiner_role_config.get('provider') if refiner_task else None
    refiner_model = refiner_role_config.get('model') if refiner_task else None
    
    if refiner_task and refiner_provider and refiner_model:
        # Refiner is configured - polish the final proposer_response
        refiner_prompt = refiner_task.get('prompt', 'Polish and refine the following response for clarity and style.')
        full_refiner_prompt = f"{refiner_prompt}\n\n--- DOCUMENT TO REFINE ---\n{proposer_response}"
        
        final_answer = await get_robust_llm_response(
            provider_name=refiner_provider,
            model=refiner_model,
            prompt=full_refiner_prompt,
            history=full_history,
            role_name='Refiner',
            request_timeout=refiner_role_config.get('request_timeout_seconds'),
            fallback_provider=fallback_provider,
            fallback_model=fallback_model
        )
        refiner_fallback = "[Fallback by Orchestrator" in final_answer
        
        panel_results['Refiner'] = {
            'provider': refiner_provider,
            'model': refiner_model,
            'status': 'Success' if "[Error:" not in final_answer else 'Failure',
            'response': final_answer,
            'fallback': refiner_fallback
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
    
    return panel_results, final_answer

async def start_panel_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the /discuss_panel command."""
    chat_id = update.effective_chat.id
    user_prompt = " ".join(context.args).strip()
    if not user_prompt:
        await update.message.reply_text("Usage: /discuss_panel <topic>", parse_mode=None)
        return ConversationHandler.END

    assembling_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="Assembling an expert panel...",
        parse_mode=None
    )
    await set_panel_commands(context.application, chat_id)

    try:
        panel_task = asyncio.create_task(
            _run_panel_workflow(context, user_prompt, [], assembling_msg, chat_id)
        )
        context.user_data['panel_task'] = panel_task
        panel_results, final_answer = await panel_task
    except asyncio.CancelledError:
        logger.warning(f"Panel workflow in start_panel_discussion for chat {chat_id} was cancelled.")
        await _cleanup_discussion_state(context, chat_id, assembling_msg)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Panel workflow failed in start_panel_discussion: {e}", exc_info=True)
        await assembling_msg.edit_text(f"An error occurred: {str(e)}", parse_mode=None)
        await _cleanup_discussion_state(context, chat_id, assembling_msg)
        return ConversationHandler.END

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
    summary_text = _format_panel_summary(panel_results)
    final_text = f"{summary_text}\n\n---\n\n{final_answer}"
    message_parts = split_message_markdown_aware(final_text)
    for part in message_parts:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=part,
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
        except BadRequest:
            await context.bot.send_message(chat_id, text=part, parse_mode=None)

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
                    context, 
                    follow_up_prompt, 
                    panel_state['full_transcript'],
                    placeholder,
                    chat_id
                )
            )
            context.user_data['panel_task'] = panel_task
            new_panel_results, new_final_answer = await panel_task
        except asyncio.CancelledError:
            logger.warning(f"Panel workflow in handle_follow_up for chat {chat_id} was cancelled.")
            await _cleanup_discussion_state(context, chat_id, placeholder)
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Panel workflow failed in handle_follow_up: {e}", exc_info=True)
            await placeholder.edit_text(f"An error occurred: {str(e)}", parse_mode=None)
            await _cleanup_discussion_state(context, chat_id, placeholder)
            return ConversationHandler.END

        panel_state['full_transcript'].append({"role": "user", "content": follow_up_prompt})
        panel_state['full_transcript'].append({"role": "assistant", "content": new_final_answer})
        panel_state['panel_results'] = new_panel_results
        panel_state['final_answer'] = new_final_answer

        await placeholder.delete()
        summary_text = _format_panel_summary(new_panel_results)
        final_text = f"{summary_text}\n\n---\n\n{new_final_answer}"
        message_parts = split_message_markdown_aware(final_text)
        for part in message_parts:
            try:
                await context.bot.send_message(chat_id, text=part, parse_mode=constants.ParseMode.MARKDOWN_V2)
            except BadRequest:
                await context.bot.send_message(chat_id, text=part, parse_mode=None)

    return AWAITING_FOLLOW_UP

async def _cleanup_discussion_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, placeholder_msg=None) -> None:
    """Safely cancels any running panel task, clears user_data, and resets commands.
    
    Args:
        context: The callback context
        chat_id: The chat ID
        placeholder_msg: Optional message object to update with cancellation status
    """
    panel_task = context.user_data.get('panel_task')
    if panel_task and not panel_task.done():
        panel_task.cancel()
        logger.info(f"Cancelled in-flight panel task for chat {chat_id}.")
        
        # Update placeholder message if provided
        if placeholder_msg:
            try:
                await placeholder_msg.edit_text("Discussion cancelled.", parse_mode=None)
            except Exception as e:
                logger.warning(f"Could not update placeholder message during cleanup: {e}")
        
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
    
    # Reset the command menu back to the default
    await setup_bot_commands_and_menu(context.application, chat_id)
    logger.info(f"Cleaned up panel state and reset commands for chat {chat_id}.")

async def end_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End the panel discussion, save its final answer, and clear context."""
    chat_id = update.effective_chat.id
    panel_state = context.user_data.get('panel_state')

    if panel_state:
        # Lock is not needed here as the conversation is ending, no race conditions.
        final_answer = panel_state.get("final_answer", "No final answer was recorded.")
        await storage_manager.save_message(chat_id, 'assistant:panel', final_answer)
        await update.message.reply_text("✅ Panel discussion concluded and saved.", parse_mode=None)
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

        try:
            panel_task = asyncio.create_task(
                _run_panel_workflow(context, last_user_prompt, history_for_reroll, placeholder_msg, chat_id)
            )
            context.user_data['panel_task'] = panel_task
            panel_results, final_answer = await panel_task
        except asyncio.CancelledError:
            logger.warning(f"Panel workflow in reroll_discussion for chat {chat_id} was cancelled.")
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Panel workflow failed during reroll: {e}", exc_info=True)
            error_message = f"An error occurred during the reroll: {escape_markdown_v2(str(e))}"
            try:
                if placeholder_msg:
                    await placeholder_msg.edit_text(error_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
                else:
                    await context.bot.send_message(chat_id, error_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
            except Exception as send_e:
                logger.error(f"Failed to send error message to user after reroll failure: {send_e}")
            
            await _cleanup_discussion_state(context, chat_id)
            return ConversationHandler.END

        if panel_state['full_transcript'] and panel_state['full_transcript'][-1]['role'] == 'assistant':
            panel_state['full_transcript'].pop()
        
        panel_state['full_transcript'].append({"role": "assistant", "content": final_answer})
        panel_state['panel_results'] = panel_results
        panel_state['final_answer'] = final_answer

        await placeholder_msg.delete()
        summary_text = _format_panel_summary(panel_results)
        final_text = f"{summary_text}\n\n---\n\n{final_answer}"
        message_parts = split_message_markdown_aware(final_text)
        for part in message_parts:
            try:
                await context.bot.send_message(chat_id, text=part, parse_mode=constants.ParseMode.MARKDOWN_V2)
            except BadRequest as e:
                if "Can't parse entities" in str(e):
                    logger.warning(f"MarkdownV2 parsing failed for a message part. Sending as plain text. Error: {e}")
                    await context.bot.send_message(chat_id, text=part, parse_mode=None)
                else:
                    raise

    return AWAITING_FOLLOW_UP

async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles conversation timeout."""
    chat_id = context.job.chat_id
    logger.info(f"Panel discussion timed out for chat {chat_id}.")
    if 'panel_state' in context.user_data:
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
    fallbacks=[CommandHandler('end_discussion', end_discussion), CommandHandler('cancel', cancel_command), CommandHandler('timeout', timeout_handler)],
    per_user=True,
    per_chat=True,
    block=True,
    per_message=False
)