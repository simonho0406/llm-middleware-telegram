import logging
import re
import asyncio
from asyncio import Lock
import json
from telegram import Update, BotCommand
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from telegram import BotCommandScopeChat
from utils.text_processing import split_message_markdown_aware, escape_markdown_v2
from telegram import constants
import config
from bot import providers
from services import web_search_service
from bot.menu_setup import setup_bot_commands_and_menu

# Define conversation states
AWAITING_FOLLOW_UP, PANEL_IN_PROGRESS = range(2)

logger = logging.getLogger(__name__)

async def set_panel_commands(application, chat_id: int) -> None:
    """Sets the bot's command list to panel-specific commands."""
    panel_commands = [
        BotCommand("reroll", "Rerun the last panel turn"),
        BotCommand("search", "Inject web search results into the discussion"),
        BotCommand("end_discussion", "End the current panel discussion"),
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
    """Formats the results of the panel execution into a markdown string."""
    summary_parts = ["*Panel Execution Summary:*"]
    for role, result in panel_results.items():
        status_icon = "✅" if result.get('status') == 'Success' else "⚠️"
        model_info = f"`{result.get('provider')}/{result.get('model')}`"
        fallback_note = escape_markdown_v2(" (Fallback)") if result.get('fallback') else ""
        summary_parts.append(f"{status_icon} *{role}:* {model_info} ({result.get('status')}){fallback_note}")
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

    # Get timeout from config
    orchestrator_timeout = orchestrator_config.get('request_timeout_seconds')
    
    max_retries = 3
    tasks_list = None
    for attempt in range(max_retries):
        try:
            response_chunks = [chunk async for chunk in orchestrator_service.generate_response(
                model=orchestrator_model,
                prompt=meta_prompt,
                context_history=None,
                request_timeout=orchestrator_timeout
            )]
            orchestrator_response = "".join(response_chunks)
            
            logger.debug(f"Orchestrator response (Attempt {attempt+1}/{max_retries}): {orchestrator_response}")
            
            # Attempt to parse JSON
            json_match = re.search(r'(\[[\s\S]*\]|{[\s\S]*})', orchestrator_response)
            if json_match:
                json_str = json_match.group(0)
                tasks_list = json.loads(json_str)
            else:
                raise ValueError("No valid JSON found in the orchestrator's response.")
            
            if not isinstance(tasks_list, list):
                raise ValueError("Expected a JSON array of tasks.")
            
            logger.info(f"Successfully parsed orchestrator's plan. Found {len(tasks_list)} tasks.")
            break # Break out of retry loop on success
        except (json.JSONDecodeError, ValueError, Exception) as e:
            logger.error(f"Orchestrator attempt {attempt+1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2) # Wait before retrying
            else:
                raise RuntimeError("Failed to get a valid plan from the orchestrator after 3 attempts.")

    # --- 2. Execute Sub-Tasks Sequentially ---
    
    async def get_full_response(provider_name, model, prompt, history, role_config, role_name):
        service = providers.get_service_for_provider(provider_name)
        if service is None:
            logger.error(f"Service for provider '{provider_name}' is not available.")
            return f"[Error: Service for '{provider_name}' not configured or available.]"
        
        request_timeout = role_config.get('request_timeout_seconds')
        
        try:
            response_chunks = [chunk async for chunk in service.generate_response(
                model=model,
                prompt=prompt,
                context_history=history,
                request_timeout=request_timeout
            )]
            response = "".join(response_chunks)

            if "[Error:" in response:
                raise ValueError(f"Initial response from {role_name} contained an error: {response}")

            return response

        except Exception as e:
            logger.warning(f"Sub-task for {role_name} ({provider_name}/{model}) failed: {e}. Falling back to orchestrator.")
            
            fallback_prompt = (
                f"You must now take on the role of the '{role_name}'. The original agent failed. "
                f"Analyze the following original prompt and provide a comprehensive response that fulfills the role's task.\n\n"
                f"--- ORIGINAL PROMPT FOR {role_name.upper()} ---\n{prompt}"
            )
            
            try:
                orchestrator_service = providers.get_service_for_provider(orchestrator_provider)
                fallback_chunks = [chunk async for chunk in orchestrator_service.generate_response(
                    model=orchestrator_model,
                    prompt=fallback_prompt,
                    context_history=history,
                    request_timeout=orchestrator_timeout
                )]
                fallback_response = "".join(fallback_chunks)
                
                # Prefix the fallback response to make it clear it's a fallback
                return f"[Fallback by Orchestrator as {role_name}]: {fallback_response}"

            except Exception as fallback_e:
                logger.error(f"Orchestrator fallback for {role_name} also failed: {fallback_e}")
                return f"[Error: {role_name} failed, and Orchestrator fallback also failed.]"

    # --- 2. Dynamic Iteration with Quality Gate ---
    max_iterations = 3
    iteration = 1
    sufficient_quality = False
    proposer_response = ""
    critic_response = ""
    role_configs = config.EXPERT_PANEL_CONFIG.get('roles', {})
    
    # Find Proposer and Critic in tasks_list
    proposer_task = next((t for t in tasks_list if t.get('role') == 'Proposer'), None)
    critic_task = next((t for t in tasks_list if t.get('role') == 'Critic'), None)
    
    if not proposer_task or not critic_task:
        raise RuntimeError("Orchestrator's plan must include Proposer and Critic roles.")

    # Get role configs
    proposer_role_config = role_configs.get('Proposer', {})
    critic_role_config = role_configs.get('Critic', {})
    proposer_provider = proposer_role_config.get('provider')
    proposer_model = proposer_role_config.get('model')
    critic_provider = critic_role_config.get('provider')
    critic_model = critic_role_config.get('model')
    
    if not all([proposer_provider, proposer_model, critic_provider, critic_model]):
        raise RuntimeError("Proposer or Critic configuration is incomplete.")

    # Initial Proposer prompt
    current_proposer_prompt = proposer_task.get('prompt') or proposer_task.get('content')
    critic_prompt_template = critic_task.get('prompt') or critic_task.get('content')
    
    for iteration in range(1, max_iterations + 1):
        await placeholder_msg.edit_text(f"Round {iteration}/{max_iterations}: Proposer is working...", parse_mode=None)
        
        # Execute Proposer
        proposer_response = await get_full_response(proposer_provider, proposer_model, current_proposer_prompt, full_history, proposer_role_config, 'Proposer')
        proposer_fallback = "[Fallback by Orchestrator" in proposer_response
        panel_results['Proposer'] = {
            'provider': proposer_provider,
            'model': proposer_model,
            'status': 'Success' if "[Error:" not in proposer_response else 'Failure',
            'response': proposer_response,
            'fallback': proposer_fallback
        }

        # Execute Critic
        await placeholder_msg.edit_text(f"Round {iteration}/{max_iterations}: Critic is reviewing...", parse_mode=None)
        full_critic_prompt = (
            f"{critic_prompt_template}\n\n"
            f"--- USER'S ORIGINAL QUERY ---\n{user_prompt}\n\n"
            f"--- PROPOSER'S DRAFT ANSWER ---\n{proposer_response}"
        )
        
        critic_response = await get_full_response(critic_provider, critic_model, full_critic_prompt, full_history, critic_role_config, 'Critic')
        critic_fallback = "[Fallback by Orchestrator" in critic_response
        panel_results['Critic'] = {
            'provider': critic_provider,
            'model': critic_model,
            'status': 'Success' if "[Error:" not in critic_response else 'Failure',
            'response': critic_response,
            'fallback': critic_fallback
        }

        # Quality check
        await placeholder_msg.edit_text(f"Round {iteration}/{max_iterations}: Assessing quality...", parse_mode=None)
        quality_check_prompt = (
            f"You are a quality assurance manager. Given the following proposal and critique, is the quality sufficient for a final answer? "
            f"Answer only with the single word 'SUFFICIENT' or the single word 'ITERATE'.\n\n"
            f"--- PROPOSAL ---\n{proposer_response}\n\n"
            f"--- CRITIQUE ---\n{critic_response}"
        )
        
        quality_decision = await get_full_response(orchestrator_provider, orchestrator_model, quality_check_prompt, None, orchestrator_config, 'Orchestrator')
        quality_decision = quality_decision.strip().upper()
        
        if "SUFFICIENT" in quality_decision:
            sufficient_quality = True
            break
        elif "ITERATE" in quality_decision and iteration < max_iterations:
            # Prepare for next iteration
            current_proposer_prompt = (
                f"Please refine your previous answer based on the following critique. This is round {iteration+1}.\n\n"
                f"--- PREVIOUS ANSWER ---\n{proposer_response}\n\n"
                f"--- CRITIQUE ---\n{critic_response}\n\n"
                f"--- YOUR TASK ---\n"
                f"Provide a new, improved, and comprehensive answer that addresses the critique."
            )
            await placeholder_msg.edit_text(f"Quality check failed. Starting round {iteration+1}...", parse_mode=None)
        else:
            break

    if not sufficient_quality and iteration == max_iterations:
        await placeholder_msg.edit_text("Reached maximum iterations without achieving sufficient quality.", parse_mode=None)
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
        response_chunks = [chunk async for chunk in orchestrator_service.generate_response(
            model=orchestrator_model,
            prompt=synthesis_prompt,
            context_history=None,
            request_timeout=orchestrator_timeout
        )]
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
            full_refiner_prompt = f"{refiner_prompt_template}\n\n--- DOCUMENT TO REFINE ---\n{json.dumps(synthesized_response)}"
            refined_response = await get_full_response(refiner_provider, refiner_model, full_refiner_prompt, full_history, refiner_config, 'Refiner')

            if refined_response and "[Error:" not in refined_response:
                final_answer = refined_response.strip()
                panel_results["Refiner"]['status'] = 'Success'
                panel_results["Refiner"]['response'] = final_answer
                logger.info("Refinement task completed successfully.")
            else:
                logger.error(f"Refiner call failed or returned error: {refined_response}")
                panel_results["Refiner"]['status'] = 'Failure'
                panel_results["Refiner"]['response'] = refined_response if refined_response else "[Empty Response]"
                panel_results["Refiner"]['fallback'] = refiner_fallback
                # Fallback to synthesized_response is already handled by default as final_answer is not updated
    
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
    # Update commands to panel-specific set
    await set_panel_commands(context.application, chat_id)

    try:
        panel_task = asyncio.create_task(
            _run_panel_workflow(context, user_prompt, [], assembling_msg)
        )
        context.user_data['panel_task'] = panel_task
        panel_results, final_answer = await panel_task
    except asyncio.CancelledError:
        logger.warning(f"Panel workflow in start_panel_discussion for chat {chat_id} was cancelled.")
        # The cleanup is handled by the command that initiated the cancellation.
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Panel workflow failed in start_panel_discussion: {e}", exc_info=True)
        await assembling_msg.edit_text(f"An error occurred: {str(e)}", parse_mode=None)
        await _cleanup_discussion_state(context, chat_id)
        return ConversationHandler.END

    # Save panel state for follow-up questions
    context.user_data['panel_state'] = {
        "lock": Lock(),
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
    logger.info(f"[{chat_id}] Handling follow-up: '{follow_up_prompt}'")

    placeholder = await update.message.reply_text("Panel is reconvening...", parse_mode=None)

    panel_state = context.user_data.get('panel_state')
    if not panel_state:
        await placeholder.edit_text("Error: Discussion context was lost. Please start a new discussion with /discuss_panel.", parse_mode=None)
        return ConversationHandler.END

    async with panel_state["lock"]:
        try:
            panel_task = asyncio.create_task(
                _run_panel_workflow(
                    context,
                    follow_up_prompt,
                    panel_state['full_transcript'],
                    placeholder
                )
            )
            context.user_data['panel_task'] = panel_task
            new_panel_results, new_final_answer = await panel_task
        except asyncio.CancelledError:
            logger.warning(f"Panel workflow in handle_follow_up for chat {chat_id} was cancelled.")
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Panel workflow failed in handle_follow_up: {e}", exc_info=True)
            await placeholder.edit_text(f"An error occurred: {str(e)}", parse_mode=None)
            await _cleanup_discussion_state(context, chat_id)
            return ConversationHandler.END

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

async def _cleanup_discussion_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Cancels any running panel task, resets commands, and clears user data."""
    # Cancel any in-flight panel workflow task
    panel_task = context.user_data.get('panel_task')
    if panel_task and not panel_task.done():
        panel_task.cancel()
        logger.info(f"Cancelled in-flight panel task for chat {chat_id}.")

    context.user_data.pop('panel_task', None)
    context.user_data.pop('panel_state', None)

    # Reset commands to the default set for the specific chat
    await setup_bot_commands_and_menu(context.application, chat_id)
    logger.info(f"Cleaned up panel state and reset commands for chat {chat_id}.")

async def end_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End the panel discussion, save its state as a single message, and clear context."""
    from storage import database_storage
    import json

    chat_id = update.effective_chat.id
    panel_state = context.user_data.get('panel_state')

    if panel_state:
        async with panel_state["lock"]:
            # Extract the final answer from the last assistant message in transcript
            final_answer = next(
                (msg['content'] for msg in reversed(panel_state['full_transcript'])
                 if msg['role'] == 'assistant'),
                "No final answer recorded"
            )

            # Create and save panel summary
            await database_storage.save_message(
                chat_id,
                'panel_discussion',
                json.dumps({
                    "original_prompt": panel_state["original_prompt"],
                    "participants": list(panel_state["panel_results"].keys()),
                    "final_answer": final_answer
                }, indent=2)
            )

            await update.message.reply_text("✅ Panel discussion saved to thread history.", parse_mode=None)
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

    search_results = await web_search_service.perform_search(query)

    if search_results.startswith("Error:"):
        await placeholder_msg.edit_text(search_results, parse_mode=None)
        return AWAITING_FOLLOW_UP

    # Add the search results to the panel's transcript
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

    # Find the last user message in the transcript
    last_user_prompt = next((msg['content'] for msg in reversed(panel_state['full_transcript']) if msg['role'] == 'user'), None)

    if not last_user_prompt:
        await update.message.reply_text("⚠️ Could not find the last user prompt to reroll.", parse_mode=None)
        return AWAITING_FOLLOW_UP

    placeholder_msg = await update.message.reply_text(f'Re-running panel for: \"{last_user_prompt[:50]}...\"', parse_mode=None)

    # Make a copy of the history to be passed to the workflow
    history_for_reroll = list(panel_state['full_transcript'])

    # Remove the last assistant response from the transcript before rerunning
    if history_for_reroll and history_for_reroll[-1]['role'] == 'assistant':
        history_for_reroll.pop()

    async with panel_state["lock"]:
        try:
            panel_task = asyncio.create_task(
                _run_panel_workflow(context, last_user_prompt, history_for_reroll, placeholder_msg)
            )
            context.user_data['panel_task'] = panel_task
            panel_results, final_answer = await panel_task
            
            # --- Correctly update the transcript ---
            # 1. Pop the old assistant message if it exists
            if panel_state['full_transcript'] and panel_state['full_transcript'][-1]['role'] == 'assistant':
                panel_state['full_transcript'].pop()
            
            # 2. Append the new, successful response
            panel_state['full_transcript'].append({"role": "assistant", "content": final_answer})
            panel_state['panel_results'] = panel_results

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

async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles conversation timeout."""
    chat_id = context.job.chat_id
    logger.info(f"Panel discussion timed out for chat {chat_id}.")
    panel_state = context.user_data.get('panel_state')
    if panel_state:
        async with panel_state["lock"]:
            await context.bot.send_message(chat_id, "Panel discussion has timed out due to inactivity.", parse_mode=None)
            await _cleanup_discussion_state(context, chat_id)
    return ConversationHandler.END

async def already_in_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Informs the user that they are already in a panel discussion."""
    await update.message.reply_text(
        "A panel discussion is already in progress. Please use /end_discussion to conclude it before starting a new one.",
        parse_mode=None
    )

async def discussion_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Informs the user that they must end the current discussion first."""
    await update.message.reply_text(
        "That command is not available during a panel discussion. Please use /end_discussion to conclude the current panel first.",
        parse_mode=None
    )

# Create command handler for ending discussions
end_discussion_handler = CommandHandler('end_discussion', end_discussion)

# Create conversation handler
discuss_panel_conv_handler = ConversationHandler(
    entry_points=[CommandHandler('discuss_panel', start_panel_discussion)],
    states={
        AWAITING_FOLLOW_UP: [
            CommandHandler('discuss_panel', already_in_discussion), # Add this line
            CommandHandler('reroll', reroll_discussion),
            CommandHandler('search', search_discussion),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_follow_up),
        ],
    },
    fallbacks=[
        CommandHandler('end_discussion', end_discussion),
        CommandHandler('timeout', timeout_handler),
        CommandHandler(['start', 'new', 'provider', 'model', 'threads', 'help'], discussion_fallback), # Add this line
    ],
    per_user=True,
    per_chat=True,
    conversation_timeout=1800,  # 30 minutes
)
