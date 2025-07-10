import logging
import re
import asyncio
import json
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from utils.text_processing import split_message_markdown_aware, escape_markdown_v2
from telegram import constants
import config
from bot import providers

# Define conversation states
AWAITING_FOLLOW_UP, PANEL_IN_PROGRESS = range(2)

logger = logging.getLogger(__name__)

def _format_panel_summary(panel_results: dict) -> str:
    """Formats the results of the panel execution into a markdown string."""
    summary_parts = ["*Panel Execution Summary:*"]
    for role, result in panel_results.items():
        status_icon = "✅" if result.get('status') == 'Success' else "⚠️"
        model_info = f"`{result.get('provider')}/{result.get('model')}`"
        summary_parts.append(f"{status_icon} *{role}:* {model_info} ({result.get('status')})")
    return "\n".join(summary_parts)

async def _run_panel_workflow(context: ContextTypes.DEFAULT_TYPE, user_prompt: str, full_history: list, placeholder_msg) -> tuple:
    """Runs the full panel workflow, updating a placeholder message, and returns a dictionary of results and the final answer."""
    panel_results = {}

    # --- 1. Deconstruct Task ---
    await placeholder_msg.edit_text("Assembling panel... Decomposing task...", parse_mode=None)
    orchestrator_config = config.EXPERT_PANEL_CONFIG.get('orchestrator', {})
    orchestrator_provider = orchestrator_config.get('provider')
    orchestrator_model = orchestrator_config.get('model')
    if not all([orchestrator_provider, orchestrator_model]):
        raise ValueError("Expert panel orchestrator is not configured correctly.")
    
    orchestrator_service = providers.get_service_for_provider(orchestrator_provider)
    if orchestrator_service is None:
        raise ValueError(f"Orchestrator service '{orchestrator_provider}' is not available.")

    meta_prompt = f"""
    You are a master expert panel coordinator. Your sole responsibility is to break down a user's request into a structured plan for your expert agents. You MUST ONLY output a valid JSON array.

    Here is the history of the conversation so far:
    --- CONVERSATION HISTORY ---
    {json.dumps(full_history, indent=2)}
    --- END HISTORY ---

    Here is the user's most recent request:
    --- LATEST REQUEST ---
    {user_prompt}
    --- END REQUEST ---

    YOUR TASK:
    Analyze the user's LATEST REQUEST in the context of the conversation history. Generate a JSON array with objects for the 'Proposer', 'Critic', and 'Refiner' roles.
    - The 'Proposer' and 'Critic' prompts should be self-contained and provide all necessary context for them to complete their tasks.
    - The 'Refiner' prompt should be a generic instruction to review and polish a final document for clarity, grammar, and style.
    - Do NOT answer the user's request yourself. Your only output is the JSON plan.
    """

    try:
        response_chunks = [chunk async for chunk in orchestrator_service.generate_response(model=orchestrator_model, prompt=meta_prompt, context_history=None)]
        orchestrator_response = "".join(response_chunks)
    except Exception as e:
        logger.error(f"Orchestrator call failed: {e}")
        raise RuntimeError("Failed to decompose the task. Please try again later.")

    logger.info(f"Orchestrator response: {orchestrator_response}")
    logger.info("Attempting to parse orchestrator's JSON plan...")

    try:
        json_match = re.search(r'(\[[\s\S]*\]|{[\s\S]*})', orchestrator_response)
        if json_match:
            json_str = json_match.group(0)
            tasks_list = json.loads(json_str)
        else:
            raise ValueError("No valid JSON found in the orchestrator's response.")

        if not isinstance(tasks_list, list):
            raise ValueError("Expected a JSON array of tasks.")

        logger.info(f"Successfully parsed orchestrator's plan. Found {len(tasks_list)} tasks.")
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse orchestrator response: {e}\nFull response:\n{orchestrator_response}")
        raise RuntimeError("The panel's plan was unclear. Please try again with a different prompt.")

    # --- 2. Execute Sub-Tasks in Parallel ---
    
    async def get_full_response(provider_name, model, prompt, history):
        service = providers.get_service_for_provider(provider_name)
        if service is None:
            logger.error(f"Service for provider '{provider_name}' is not available.")
            return f"Error: Service for '{provider_name}' not configured or available."
        try:
            response_chunks = [chunk async for chunk in service.generate_response(model=model, prompt=prompt, context_history=history)]
            return "".join(response_chunks)
        except Exception as e:
            logger.error(f"Sub-task for model {model} failed: {e}")
            return f"Error generating response from {model}: {e}"

    role_configs = config.EXPERT_PANEL_CONFIG.get('roles', {})
    tasks_to_run = []
    task_role_map = {}
    for task_spec in tasks_list:
        role = task_spec.get('role')
        prompt = task_spec.get('prompt') or task_spec.get('content')
        if role in role_configs:
            role_config = role_configs[role]
            provider = role_config.get('provider')
            model = role_config.get('model')
            if all([provider, model, prompt]):
                panel_results[role] = {'provider': provider, 'model': model} # Pre-populate
                task = asyncio.create_task(get_full_response(provider, model, prompt, full_history))
                tasks_to_run.append(task)
                task_role_map[task] = role

    if not tasks_to_run:
        raise RuntimeError("No valid expert roles could be assigned based on the plan.")

    await placeholder_msg.edit_text("Executing expert tasks in parallel...", parse_mode=None)
    results = await asyncio.gather(*tasks_to_run, return_exceptions=True)

    for i, task in enumerate(tasks_to_run):
        role = task_role_map[task]
        result_data = results[i]
        if isinstance(result_data, Exception):
            panel_results[role]['status'] = 'Failure'
            panel_results[role]['response'] = f"Error: {result_data}"
        else:
            panel_results[role]['status'] = 'Success'
            panel_results[role]['response'] = result_data

    # --- 3. Synthesize Final Answer ---
    await placeholder_msg.edit_text("Synthesizing final answer...", parse_mode=None)
    proposer_response = panel_results.get("Proposer", {}).get('response', 'No response from proposer.')
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
    
    try:
        response_chunks = [chunk async for chunk in orchestrator_service.generate_response(model=orchestrator_model, prompt=synthesis_prompt, context_history=None)]
        synthesized_response = "".join(response_chunks).strip()
    except Exception as e:
        synthesized_response = "Failed to synthesize the final answer."

    # --- 4. Refine Final Answer ---
    await placeholder_msg.edit_text("Polishing final answer...", parse_mode=None)
    logger.info("Invoking Refiner agent...")

    final_answer = synthesized_response # Default to synthesized response
    refiner_config = role_configs.get("Refiner")

    if refiner_config:
        refiner_provider = refiner_config.get('provider')
        refiner_model = refiner_config.get('model')
        refiner_prompt_template = next((task.get('prompt') for task in tasks_list if task.get('role') == 'Refiner'), "Refine the following text.")
        
        panel_results["Refiner"] = {'provider': refiner_provider, 'model': refiner_model}

        if not all([refiner_provider, refiner_model]):
            panel_results["Refiner"]['status'] = 'Skipped'
            panel_results["Refiner"]['response'] = 'Refiner not configured.'
        else:
            full_refiner_prompt = f"{refiner_prompt_template}\n\n--- DOCUMENT TO REFINE ---\n{synthesized_response}"
            try:
                refined_response = await get_full_response(refiner_provider, refiner_model, full_refiner_prompt, full_history)
                if refined_response and not refined_response.startswith("Error:"):
                    final_answer = refined_response.strip()
                    panel_results["Refiner"]['status'] = 'Success'
                    panel_results["Refiner"]['response'] = final_answer
                    logger.info("Refinement task completed successfully.")
                else:
                    raise ValueError(f"Refiner returned an empty or error response: {refined_response}")
            except Exception as e:
                logger.error(f"Refiner call failed: {e}")
                panel_results["Refiner"]['status'] = 'Failure'
                panel_results["Refiner"]['response'] = str(e)
                # Fallback to synthesized_response is already handled by default
    
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

    try:
        panel_results, final_answer = await _run_panel_workflow(context, user_prompt, [], assembling_msg)
    except Exception as e:
        logger.error(f"Panel workflow failed in start_panel_discussion: {e}")
        await assembling_msg.edit_text(str(e), parse_mode=None)
        return ConversationHandler.END

    # Save panel state for follow-up questions
    context.user_data['panel_state'] = {
        "original_prompt": user_prompt,
        "panel_results": panel_results,
        "full_transcript": [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": final_answer}
        ]
    }

    # Send the Final Synthesized Response
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
    logger.debug(f"[{chat_id}] Handling follow-up: '{follow_up_prompt}'")

    placeholder = await update.message.reply_text("Panel is reconvening...", parse_mode=None)

    panel_state = context.user_data.get('panel_state')
    if not panel_state:
        await placeholder.edit_text("Error: Discussion context was lost. Please start a new discussion with /discuss_panel.", parse_mode=None)
        return ConversationHandler.END

    try:
        new_panel_results, new_final_answer = await _run_panel_workflow(
            context, 
            follow_up_prompt, 
            panel_state['full_transcript'],
            placeholder
        )
    except Exception as e:
        logger.error(f"Panel workflow failed in handle_follow_up: {e}")
        await placeholder.edit_text(str(e), parse_mode=None)
        return AWAITING_FOLLOW_UP

    # Update the panel state with the new interaction
    panel_state['full_transcript'].append({"role": "user", "content": follow_up_prompt})
    panel_state['full_transcript'].append({"role": "assistant", "content": new_final_answer})
    panel_state['panel_results'] = new_panel_results # Update with the latest results

    # Send the response
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

async def end_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End the panel discussion and clear state"""
    context.user_data.pop('panel_state', None)
    await update.message.reply_text("Panel discussion concluded.", parse_mode=None)
    return ConversationHandler.END

# Create command handler for ending discussions
end_discussion_handler = CommandHandler('end_discussion', end_discussion)

# Create conversation handler
discuss_panel_conv_handler = ConversationHandler(
    entry_points=[CommandHandler('discuss_panel', start_panel_discussion)],
    states={
        AWAITING_FOLLOW_UP: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_follow_up),
            end_discussion_handler
        ],
    },
    fallbacks=[end_discussion_handler],
    per_user=True,
    per_chat=True
)
