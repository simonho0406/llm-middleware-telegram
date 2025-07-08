import logging
import re
import asyncio
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler

logger = logging.getLogger(__name__)

async def start_panel_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts a panel discussion with the provided prompt."""
    chat_id = update.effective_chat.id
    user_prompt = " ".join(context.args).strip()
    if not user_prompt:
        await update.message.reply_text("Usage: /discuss_panel <topic>")
        return ConversationHandler.END

    # Update the initial message
    assembling_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="Assembling an expert panel... Decomposing task...",
        parse_mode=None
    )

    # Select an Orchestrator Model from config
    from bot import providers
    import config
    orchestrator_config = config.EXPERT_PANEL_CONFIG.get('orchestrator', {})
    orchestrator_provider = orchestrator_config.get('provider')
    orchestrator_model = orchestrator_config.get('model')
    if not all([orchestrator_provider, orchestrator_model]):
        await assembling_msg.edit_text("Error: Expert panel orchestrator is not configured correctly.", parse_mode=None)
        return ConversationHandler.END
    orchestrator_service = providers.get_service_for_provider(orchestrator_provider)
    if orchestrator_service is None:
        await assembling_msg.edit_text("Error: Orchestrator service (Groq) is not available.", parse_mode=None)
        return ConversationHandler.END

    # Construct the Orchestrator's Meta-Prompt
    meta_prompt = f"""
    You are a helpful assistant that deconstructs a user's prompt into a series of sub-tasks for a panel of AI experts. The available expert roles are: 'Proposer' (provides the main answer), 'Critic' (finds flaws and suggests alternatives), and 'Refiner' (improves grammar, style, and structure).

    Based on the user's query, provide a JSON array of objects, where each object contains a 'role' and a 'prompt'. The 'prompt' for the Refiner should be a generic instruction to await the outputs of the others.

    User Query: '{user_prompt}'
    """

    # Invoke the Orchestrator
    try:
        response_chunks = []
        async for chunk in orchestrator_service.generate_response(
            model=orchestrator_model,
            prompt=meta_prompt,
            context_history=None
        ):
            response_chunks.append(chunk)
        orchestrator_response = "".join(response_chunks)
    except Exception as e:
        logger.error(f"Orchestrator call failed: {e}")
        await assembling_msg.edit_text("Failed to decompose the task. Please try again later.", parse_mode=None)
        return ConversationHandler.END

    # Log the raw response
    logger.info(f"Orchestrator response: {orchestrator_response}")
    logger.info("Attempting to parse orchestrator's JSON plan...")

    # Parse the JSON response
    import json
    import re
    try:
        # Use regex to find the JSON block, allowing for surrounding text
        json_match = re.search(r'```json\s*([\s\S]*?)\s*```', orchestrator_response)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Fallback for raw JSON without backticks
            json_str = orchestrator_response

        tasks_list = json.loads(json_str)
        if not isinstance(tasks_list, list):
            raise ValueError("Expected a JSON array")
        logger.info(f"Successfully parsed orchestrator's plan. Found {len(tasks_list)} tasks.")
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse orchestrator response: {e}")
        await assembling_msg.edit_text("The panel's plan was unclear. Please try again with a different prompt.", parse_mode=None)
        return ConversationHandler.END

    # Get role configurations from expert_panel config
    role_configs = config.EXPERT_PANEL_CONFIG.get('roles', {})
    tasks_to_run = []
    task_role_map = {}  # To map asyncio task back to its role and response

    # Define helper function to get full response from a service
    async def get_full_response(provider_name, model, prompt):
        """Helper to consume an async generator from a service and return the full string."""
        service = providers.get_service_for_provider(provider_name)
        if service is None:
            logger.error(f"Service for provider '{provider_name}' is not available.")
            return f"Error: Service for '{provider_name}' not configured or available."
        try:
            response_chunks = [chunk async for chunk in service.generate_response(model=model, prompt=prompt, context_history=[])]
            return "".join(response_chunks)
        except Exception as e:
            logger.error(f"Sub-task for model {model} failed: {e}")
            return f"Error generating response from {model}: {e}"

    for task_spec in tasks_list:
        role = task_spec.get('role')
        prompt = task_spec.get('prompt')
        
        if role in role_configs:
            role_config = role_configs[role]
            provider = role_config.get('provider')
            model = role_config.get('model')
            
            if all([provider, model, prompt]):
                task = asyncio.create_task(get_full_response(provider, model, prompt))
                tasks_to_run.append(task)
                task_role_map[task] = role  # Map the task object to its role name

    if not tasks_to_run:
        await assembling_msg.edit_text("Error: No valid expert roles could be assigned based on the plan.", parse_mode=None)
        return ConversationHandler.END

    # Run tasks concurrently
    logger.info(f"Executing {len(tasks_to_run)} expert tasks concurrently...")
    results = await asyncio.gather(*tasks_to_run, return_exceptions=True)
    logger.info("Expert tasks completed.")

    # Process results using the map
    panel_results = {}
    for i, task in enumerate(tasks_to_run):
        role = task_role_map[task]
        result_data = results[i]
        response = result_data if not isinstance(result_data, Exception) else f"Error: {result_data}"
        panel_results[role] = response
    logger.info("Expert responses processed. Preparing synthesis prompt.")

    # Get responses for specific roles
    proposer_response = panel_results.get("Proposer", "No response from proposer.")
    critic_response = panel_results.get("Critic", "No response from critic.")

    # Construct the Final Synthesis Prompt
    synthesis_prompt = (
        f"You are a lead editor responsible for creating a final, comprehensive answer for a user.\n\n"
        f"The original user query was: '{user_prompt}'\n\n"
        f"--- INITIAL PROPOSAL ---\n{proposer_response}\n\n"
        f"--- EXPERT CRITIQUE ---\n{critic_response}\n\n"
        f"--- YOUR TASK ---\n"
        f"Synthesize the initial proposal and the expert critique into a single, high-quality, and well-structured response for the user. "
        f"Address the points from the critique, integrate the strengths of the proposal, and deliver a final, polished answer. "
        f"Do not act as a commentator; produce the final answer directly."
    )

    # Update the assembling message
    await assembling_msg.edit_text("Synthesizing final answer...", parse_mode=None)

    # Invoke the Synthesis Agent
    try:
        response_chunks = []
        async for chunk in orchestrator_service.generate_response(
            model=orchestrator_model,
            prompt=synthesis_prompt,
            context_history=None
        ):
            response_chunks.append(chunk)
        synthesized_response = "".join(response_chunks)
        logger.info("Synthesis task completed. Sending final response to user.")
    except Exception as e:
        logger.error(f"Synthesis call failed: {e}")
        synthesized_response = "Failed to synthesize the final answer. Please try again later."

    # Send the Final Synthesized Response
    from utils.text_processing import split_message_markdown_aware, escape_markdown_v2
    from telegram import constants
    
    escaped_response = escape_markdown_v2(synthesized_response)
    final_text = f"*Final Synthesized Answer:*\n\n{escaped_response}"
    
    message_parts = split_message_markdown_aware(final_text)
    for part in message_parts:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=part,
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
        except BadRequest:
            # Fallback to plain text if markdown fails
            await context.bot.send_message(chat_id, text=part, parse_mode=None)
    
    # Delete the assembling message
    await assembling_msg.delete()

    return ConversationHandler.END

# Create conversation handler
discuss_panel_conv_handler = CommandHandler('discuss_panel', start_panel_discussion)
