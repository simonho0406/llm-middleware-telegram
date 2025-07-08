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


async def _run_panel_workflow(context: ContextTypes.DEFAULT_TYPE, user_prompt: str, full_history: list, placeholder_msg) -> dict:
    """Runs the full panel workflow, updating a placeholder message, and returns a dictionary of results."""
    
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
    Analyze the user's LATEST REQUEST in the context of the conversation history. Generate a JSON array with two objects, one for a "Proposer" role and one for a "Critic" role. The prompts for these roles must be self-contained and provide all necessary context for them to complete their tasks based on the latest request.
    - The 'Proposer' prompt should ask for a comprehensive answer to the user's latest request.
    - The 'Critic' prompt should ask for a critical review of the Proposer's potential answer in light of the user's latest request and the history.
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
        # Use regex to find a JSON block, which might be wrapped in markdown
        json_match = re.search(r'(\[[\s\S]*\]|{[\s\S]*})', orchestrator_response)
        if json_match:
            json_str = json_match.group(0)
            tasks_list = json.loads(json_str)
        else:
            # If no JSON structure is found at all
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
        prompt = task_spec.get('prompt')
        if role in role_configs:
            role_config = role_configs[role]
            provider = role_config.get('provider')
            model = role_config.get('model')
            if all([provider, model, prompt]):
                task = asyncio.create_task(get_full_response(provider, model, prompt, full_history))
                tasks_to_run.append(task)
                task_role_map[task] = role

    if not tasks_to_run:
        raise RuntimeError("No valid expert roles could be assigned based on the plan.")

    await placeholder_msg.edit_text("Executing expert tasks in parallel...", parse_mode=None)
    logger.info(f"Executing {len(tasks_to_run)} expert tasks concurrently...")
    results = await asyncio.gather(*tasks_to_run, return_exceptions=True)
    logger.info("Expert tasks completed.")

    panel_results = {}
    for i, task in enumerate(tasks_to_run):
        role = task_role_map[task]
        result_data = results[i]
        response = result_data if not isinstance(result_data, Exception) else f"Error: {result_data}"
        panel_results[role] = response
    
    # --- 3. Synthesize Final Answer ---
    await placeholder_msg.edit_text("Synthesizing final answer...", parse_mode=None)
    logger.info("Expert responses processed. Preparing synthesis prompt.")

    proposer_response = panel_results.get("Proposer", "No response from proposer.")
    critic_response = panel_results.get("Critic", "No response from critic.")

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
        logger.info("Synthesis task completed.")
    except Exception as e:
        logger.error(f"Synthesis call failed: {e}")
        synthesized_response = "Failed to synthesize the final answer. Please try again later."

    return {
        "proposer_response": proposer_response,
        "critic_response": critic_response,
        "synthesized_answer": synthesized_response
    }


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
        panel_results = await _run_panel_workflow(context, user_prompt, [], assembling_msg)
    except Exception as e:
        logger.error(f"Panel workflow failed in start_panel_discussion: {e}")
        await assembling_msg.edit_text(str(e), parse_mode=None)
        return ConversationHandler.END

    # Save panel state for follow-up questions
    context.user_data['panel_state'] = {
        "original_prompt": user_prompt,
        "proposer_response": panel_results["proposer_response"],
        "critic_response": panel_results["critic_response"],
        "synthesized_answer": panel_results["synthesized_answer"],
        "full_transcript": [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": panel_results["synthesized_answer"]}
        ]
    }

    # Send the Final Synthesized Response
    await assembling_msg.delete()
    message_parts = split_message_markdown_aware(escape_markdown_v2(panel_results["synthesized_answer"]))
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
        new_panel_results = await _run_panel_workflow(
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
    panel_state['full_transcript'].append({"role": "assistant", "content": new_panel_results["synthesized_answer"]})
    panel_state['synthesized_answer'] = new_panel_results["synthesized_answer"] # Update latest answer

    # Send the response
    await placeholder.delete()
    message_parts = split_message_markdown_aware(escape_markdown_v2(new_panel_results["synthesized_answer"]))
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
