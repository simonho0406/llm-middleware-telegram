import logging
import asyncio
import re
from telegram import Update, constants
from telegram.ext import MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter, TimedOut, BadRequest
import config
# Ensure all imported services are used or remove unused ones
from services import ollama_service, gemini_service, openrouter_service
from services.openai_compatible_service import OpenAICompatibleService
from config import CUSTOM_PROVIDERS_CONFIG
from storage import file_storage
from utils.text_processing import split_message_markdown_aware

logger = logging.getLogger(__name__)

def escape_markdown_v2(text: str) -> str:
    """
    Escapes all MarkdownV2 special characters in a string for literal display.
    Ensures that characters like _, *, [, ], etc., are displayed as themselves
    rather than being interpreted as Markdown formatting.
    """
    if not text:
        return ""
    # Characters to escape for MarkdownV2.
    # The `\` itself must be escaped to `\\`.
    # Other characters: _ * [ ] ( ) ~ ` > # + - = | { } . !
    # Using re.escape on the set of characters ensures they are treated literally in the regex pattern.
    # The pattern captures a single special character, and re.sub replaces it with a backslash + the character.
    mdv2_special_chars_pattern = r"([_*\[\]()~`>#+\-=|{}.!])"
    escaped = re.sub(mdv2_special_chars_pattern, r'\\\1', text)
    return escaped

def escape_meta_tags_for_markdown_attempt(text: str) -> str:
    """
    Prepares LLM text for a Markdown rendering attempt by removing or
    neutralizing known LLM-internal "meta-tags" (like <think>, <reflect>).
    The goal is to allow user-facing Markdown produced by the LLM to render,
    without these internal tags interfering or causing parsing errors.

    Default strategy is to REMOVE these tags and their content.
    """
    if not text:
        return ""

    # --- STRATEGY 1: Remove LLM-internal tags and their content (Recommended) ---
    # This is generally best if these tags are for the LLM's internal reasoning
    # and not intended for user display.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<reflect>.*?</reflect>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Add more similar tags here if your LLM uses them, e.g.:
    # text = re.sub(r"<reasoning_steps>.*?</reasoning_steps>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # text = re.sub(r"<search_query>.*?</search_query>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # --- STRATEGY 2: Replace tags with a user-friendly notice (Alternative) ---
    # Use this if you want to indicate the LLM was thinking, but not show raw tags.
    # Comment out Strategy 1 if using this.
    # text = re.sub(r"<think>.*?</think>", "(System processing...)", text, flags=re.DOTALL | re.IGNORECASE)
    # text = re.sub(r"<reflect>.*?</reflect>", "(System reflecting...)", text, flags=re.DOTALL | re.IGNORECASE)

    # --- STRATEGY 3: Make tags appear literally (Causes issues if followed by full escape_markdown_v2) ---
    # This was problematic because escape_markdown_v2 would then double-escape backslashes.
    # Avoid this strategy if a full escape fallback is used.
    # text = text.replace("<think>", "\\<think\\>") # Not recommended with current fallback
    # text = text.replace("</think>", "\\</think\\>")

    return text.strip()


TELEGRAM_MAX_LEN = 4096 # Max message length for Telegram

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages, gets response from LLM, and sends it back."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message_text = update.message.text
    log_prefix = f"(Chat {chat_id}) " # For prepending to log messages

    logger.info(f"{log_prefix}Message from User {user_id}: '{message_text[:100]}{'...' if len(message_text)>100 else ''}'")

    # Authorization Check
    if config.ALLOWED_CHAT_IDS and chat_id not in config.ALLOWED_CHAT_IDS:
        logger.warning(f"{log_prefix}Unauthorized chat ID. User: {user_id}.")
        return

    # Get Thread and Provider Info
    try:
        current_thread_id = await file_storage.get_current_thread_id(chat_id)
        session_provider = await file_storage.get_thread_key(chat_id, 'provider', config.DEFAULT_PROVIDER)
        logger.info(f"{log_prefix}Current thread: {current_thread_id}, Provider: {session_provider}")
    except Exception as e:
        logger.error(f"{log_prefix}Error getting session data: {e}")
        await update.message.reply_text("Sorry, there was an issue retrieving session information.")
        return

    # Determine Service and Model
    custom_service_instances = {}
    for provider_conf in CUSTOM_PROVIDERS_CONFIG:
        try:
            service_instance = OpenAICompatibleService(provider_conf)
            if service_instance.client: # Ensure client initialized
                custom_service_instances[provider_conf['name']] = service_instance
        except ValueError as e: # Specific config errors
            logger.error(f"{log_prefix}Skipping custom provider due to config error: {e}")
        except Exception as e: # General init errors
             logger.error(f"{log_prefix}Failed to initialize service for custom provider {provider_conf.get('name', 'UNKNOWN')}: {e}")

    provider_details = {
        'ollama': (ollama_service, 'ollama_model', config.DEFAULT_OLLAMA_MODEL),
        'gemini': (gemini_service, 'gemini_model', config.DEFAULT_GEMINI_MODEL),
        'openrouter': (openrouter_service, 'openrouter_model', config.DEFAULT_OPENROUTER_MODEL)
    }
    for name, instance in custom_service_instances.items():
        provider_details[name] = (instance, f'{name}_model', instance.get_default_model())

    if session_provider not in provider_details:
        logger.error(f"{log_prefix}Invalid or disabled provider '{session_provider}' selected. Falling back to default: {config.DEFAULT_PROVIDER}.")
        session_provider = config.DEFAULT_PROVIDER
        if session_provider not in provider_details:
             available_providers = list(provider_details.keys())
             if not available_providers:
                 logger.critical(f"{log_prefix}No valid LLM providers configured or initialized!")
                 await update.message.reply_text("Error: No AI providers are currently available.")
                 return
             session_provider = available_providers[0] # Fallback to the first available one
             logger.warning(f"{log_prefix}Default provider '{config.DEFAULT_PROVIDER}' also invalid. Falling back to first available: '{session_provider}'")
        await file_storage.set_thread_key(chat_id, 'provider', session_provider) # Save the corrected provider

    service, model_key, default_model_name = provider_details[session_provider]
    model_to_use = await file_storage.get_thread_key(chat_id, model_key, default_model_name)
    provider_name_display = session_provider.capitalize()
    logger.info(f"{log_prefix}Using service: {service.__class__.__name__ if hasattr(service, '__class__') else service.__name__}, Model: {model_to_use}")

    # Send Placeholder Message
    placeholder_message = None
    try:
        placeholder_text_content = f"Thinking with {provider_name_display} ({model_to_use})..."
        escaped_placeholder_text = escape_markdown_v2(placeholder_text_content)
        placeholder_message = await update.message.reply_text(
            text=escaped_placeholder_text,
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            reply_to_message_id=update.message.message_id # Reply to user's message
        )
    except Exception as e:
        logger.error(f"{log_prefix}Failed to send placeholder message: {e}")
        # Continue without placeholder; messages will be sent as new replies.

    # Accumulate Full LLM Response (No Streaming Edits to User)
    raw_full_llm_response = ""
    llm_error_reported_by_model = False
    try:
        logger.info(f"{log_prefix}Starting LLM generation...")
        context_history = await file_storage.get_thread_key(chat_id, 'history', [])
        async for chunk in service.generate_response(model=model_to_use, prompt=message_text, context_history=context_history):
            if chunk.startswith("[Error:") or chunk.startswith("Error:"): # More flexible error check
                logger.error(f"{log_prefix}LLM service reported an error in a chunk: {chunk}")
                raw_full_llm_response = chunk # Store the error message from LLM
                llm_error_reported_by_model = True
                break # Stop processing further chunks
            raw_full_llm_response += chunk
        
        if not llm_error_reported_by_model:
            logger.info(f"{log_prefix}LLM generation complete. Full response length: {len(raw_full_llm_response)}")
        else:
            logger.warning(f"{log_prefix}LLM generation interrupted by model-reported error. Partial response/error: '{raw_full_llm_response[:100]}...'")

    except Exception as e:
        logger.exception(f"{log_prefix}Critical error during LLM response generation stream: {e}")
        error_msg_for_user = escape_markdown_v2("Sorry, an error occurred while communicating with the AI.")
        if placeholder_message:
            try: await context.bot.edit_message_text(chat_id, placeholder_message.message_id, error_msg_for_user, parse_mode=constants.ParseMode.MARKDOWN_V2)
            except: pass # Best effort
        else:
            try: await update.message.reply_text(error_msg_for_user, parse_mode=constants.ParseMode.MARKDOWN_V2)
            except: pass
        return # Cannot proceed

    # --- Final Message Sending Logic ---
    message_sent_or_edited_successfully = False
    final_content_to_send = raw_full_llm_response.strip()

    if llm_error_reported_by_model:
        # If LLM itself reported an error, display it (fully escaped, as it's an error message)
        text_to_send_on_llm_error = escape_markdown_v2(final_content_to_send)
        chunks_for_llm_error = split_message_markdown_aware(text_to_send_on_llm_error, TELEGRAM_MAX_LEN)
        parse_mode_for_llm_error = constants.ParseMode.MARKDOWN_V2
        logger.info(f"{log_prefix}Sending LLM-reported error to user (fully escaped). Chunks: {len(chunks_for_llm_error)}")
        try:
            current_msg_id_to_edit = placeholder_message.message_id if placeholder_message else None
            for i, chunk_content in enumerate(chunks_for_llm_error):
                page_header = ""
                if len(chunks_for_llm_error) > 1:
                    page_header = escape_markdown_v2(f"(Error part {i+1}/{len(chunks_for_llm_error)})") + "\n\n"
                final_chunk_text = f"{page_header}{chunk_content}"

                if i == 0 and current_msg_id_to_edit:
                    await context.bot.edit_message_text(chat_id, current_msg_id_to_edit, final_chunk_text[:TELEGRAM_MAX_LEN], parse_mode=parse_mode_for_llm_error)
                else:
                    reply_target_id = update.message.message_id if not current_msg_id_to_edit or i > 0 else None
                    new_msg = await update.message.reply_text(final_chunk_text[:TELEGRAM_MAX_LEN], parse_mode=parse_mode_for_llm_error, reply_to_message_id=reply_target_id)
                    if i == 0 and not current_msg_id_to_edit: current_msg_id_to_edit = new_msg.message_id # For multi-page error

            message_sent_or_edited_successfully = True
        except Exception as e:
            logger.error(f"{log_prefix}Failed to send/edit LLM-reported error message to user: {e}")
            # Minimal fallback if even sending the escaped LLM error fails
            error_msg_for_user = escape_markdown_v2("An error occurred with the AI model.")
            if placeholder_message:
                 try: await context.bot.edit_message_text(chat_id, placeholder_message.message_id, error_msg_for_user, parse_mode=constants.ParseMode.MARKDOWN_V2)
                 except: pass
            else:
                 try: await update.message.reply_text(error_msg_for_user, parse_mode=constants.ParseMode.MARKDOWN_V2)
                 except: pass


    elif not final_content_to_send: # Check if raw response (after strip) is empty
        logger.warning(f"{log_prefix}LLM returned an empty response.")
        empty_response_text = escape_markdown_v2("(AI returned an empty response)")
        if placeholder_message:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=placeholder_message.message_id,
                    text=empty_response_text, parse_mode=constants.ParseMode.MARKDOWN_V2)
                message_sent_or_edited_successfully = True
            except Exception as e:
                logger.error(f"{log_prefix}Failed to edit placeholder for empty AI response: {e}")
                try: # Fallback to sending new message if edit fails
                    await update.message.reply_text(empty_response_text, parse_mode=constants.ParseMode.MARKDOWN_V2, reply_to_message_id=update.message.message_id)
                    message_sent_or_edited_successfully = True
                except Exception as e2:
                    logger.error(f"{log_prefix}Also failed to send new message for empty AI response: {e2}")
        else: # No placeholder, send as new message
            try:
                await update.message.reply_text(empty_response_text, parse_mode=constants.ParseMode.MARKDOWN_V2, reply_to_message_id=update.message.message_id)
                message_sent_or_edited_successfully = True
            except Exception as e:
                logger.error(f"{log_prefix}Failed to send new message for empty AI response (no placeholder): {e}")

    else: # We have content from LLM, and it wasn't a model-reported error
        # Two-attempt sending: 1. Try rendering Markdown, 2. Fallback to fully escaped text
        for attempt_num, attempt_type in enumerate(["markdown_render", "full_escape"]):
            logger.info(f"{log_prefix}Message Sending Attempt #{attempt_num + 1} ({attempt_type})")
            
            text_for_current_attempt = ""
            parse_mode_to_use = constants.ParseMode.MARKDOWN_V2 # Always try MDv2, content changes

            if attempt_type == "markdown_render":
                # Prepare text for Markdown rendering: remove/neutralize LLM-internal meta-tags
                text_for_current_attempt = escape_meta_tags_for_markdown_attempt(final_content_to_send)
                if not text_for_current_attempt.strip():
                    logger.warning(f"{log_prefix}Content is empty after 'escape_meta_tags_for_markdown_attempt'. Skipping '{attempt_type}' attempt.")
                    continue # Move to next attempt type (full_escape)
            elif attempt_type == "full_escape":
                # Fallback: fully escape the ORIGINAL raw LLM response
                text_for_current_attempt = escape_markdown_v2(final_content_to_send)
            
            chunks_to_send_this_attempt = split_message_markdown_aware(text_for_current_attempt, TELEGRAM_MAX_LEN)
            if not chunks_to_send_this_attempt:
                logger.warning(f"{log_prefix}No chunks to send for attempt '{attempt_type}' (text was: '{text_for_current_attempt[:50]}...').")
                if attempt_type == "markdown_render" and placeholder_message:
                     try: # Edit placeholder to indicate processed empty response
                         await context.bot.edit_message_text(chat_id, placeholder_message.message_id, escape_markdown_v2("(Processed AI response is empty)"), parse_mode=constants.ParseMode.MARKDOWN_V2)
                     except: pass # Best effort
                continue # Move to next attempt type or finish

            # Try sending all chunks for this attempt
            current_msg_id_to_edit_this_attempt = placeholder_message.message_id if placeholder_message else None
            all_chunks_sent_this_attempt = True # Assume success for this attempt until a chunk fails

            for i, chunk_text_content in enumerate(chunks_to_send_this_attempt):
                page_header_text = ""
                if len(chunks_to_send_this_attempt) > 1:
                    # Page headers are always fully escaped as they are bot-generated content
                    page_header_text = escape_markdown_v2(f"(Page {i+1}/{len(chunks_to_send_this_attempt)})") + "\n\n"
                
                final_text_for_chunk = f"{page_header_text}{chunk_text_content}"

                try:
                    if i == 0 and current_msg_id_to_edit_this_attempt:
                        logger.debug(f"{log_prefix}Attempt '{attempt_type}': Editing msg {current_msg_id_to_edit_this_attempt} with P{i+1}/{len(chunks_to_send_this_attempt)}")
                        await context.bot.edit_message_text(
                            chat_id=chat_id, message_id=current_msg_id_to_edit_this_attempt,
                            text=final_text_for_chunk[:TELEGRAM_MAX_LEN], parse_mode=parse_mode_to_use)
                    else:
                        logger.debug(f"{log_prefix}Attempt '{attempt_type}': Replying with P{i+1}/{len(chunks_to_send_this_attempt)}")
                        # Reply to original user message or the first part if placeholder was edited
                        reply_target_id = update.message.message_id if not current_msg_id_to_edit_this_attempt or i > 0 else None

                        new_msg = await update.message.reply_text(
                            text=final_text_for_chunk[:TELEGRAM_MAX_LEN], parse_mode=parse_mode_to_use,
                            reply_to_message_id=reply_target_id)
                        if i == 0 and not current_msg_id_to_edit_this_attempt: # If placeholder failed and this is the first chunk sent as new
                            current_msg_id_to_edit_this_attempt = new_msg.message_id # For multi-page, subsequent "edits" target this new message (not really edits, but conceptual continuation)
                
                except BadRequest as e_br:
                    logger.error(f"{log_prefix}BadRequest on {attempt_type} (P{i+1}/{len(chunks_to_send_this_attempt)}): {e_br}. Text preview: '{final_text_for_chunk[:100]}...'")
                    all_chunks_sent_this_attempt = False
                    if "Can't parse entities" in str(e_br) and attempt_type == "markdown_render":
                        logger.warning(f"{log_prefix}Parsing error on '{attempt_type}'. Will proceed to '{'full_escape' if attempt_type == 'markdown_render' else 'next step/failure' }'.")
                        # No need to reset current_msg_id_to_edit_this_attempt, next outer loop iteration will handle it.
                    elif "message to edit not found" in str(e_br).lower() and current_msg_id_to_edit_this_attempt:
                        logger.warning(f"{log_prefix}Msg {current_msg_id_to_edit_this_attempt} not found for editing. Will try sending this chunk as new reply.")
                        current_msg_id_to_edit_this_attempt = None # Force reply for this chunk
                        # Retry this specific chunk as a new message immediately (within the same attempt type)
                        try:
                            await update.message.reply_text(
                                text=final_text_for_chunk[:TELEGRAM_MAX_LEN], parse_mode=parse_mode_to_use,
                                reply_to_message_id=update.message.message_id)
                            all_chunks_sent_this_attempt = True # This chunk now sent
                        except Exception as e_retry_send:
                            logger.error(f"{log_prefix}Retry-send as new message also failed for P{i+1}: {e_retry_send}")
                            all_chunks_sent_this_attempt = False # Mark as failed again
                    # For other BadRequests or if parse error on full_escape, this attempt for this chunk failed.
                    break # Break from chunk loop for this attempt type

                except RetryAfter as e_ra:
                    logger.warning(f"{log_prefix}RetryAfter on {attempt_type} (P{i+1}): {e_ra.retry_after}s. Sleeping.")
                    all_chunks_sent_this_attempt = False # Mark as failed for now
                    await asyncio.sleep(e_ra.retry_after)
                    # Simple retry for this specific chunk (could be more sophisticated with max retries)
                    # Re-attempting the same operation that failed:
                    try:
                        if i == 0 and current_msg_id_to_edit_this_attempt:
                            await context.bot.edit_message_text(chat_id, current_msg_id_to_edit_this_attempt, final_text_for_chunk[:TELEGRAM_MAX_LEN], parse_mode=parse_mode_to_use)
                        else:
                            reply_target_id = update.message.message_id if not current_msg_id_to_edit_this_attempt or i > 0 else None
                            await update.message.reply_text(final_text_for_chunk[:TELEGRAM_MAX_LEN], parse_mode=parse_mode_to_use, reply_to_message_id=reply_target_id)
                        all_chunks_sent_this_attempt = True # If retry succeeds
                    except Exception as e_after_retry:
                        logger.error(f"{log_prefix}Send/edit for P{i+1} failed even after RetryAfter: {e_after_retry}")
                        all_chunks_sent_this_attempt = False
                    if not all_chunks_sent_this_attempt: break # Break from chunk loop if retry fails

                except Exception as e_gen_chunk:
                    logger.error(f"{log_prefix}Generic error sending/editing {attempt_type} (P{i+1}): {e_gen_chunk}")
                    all_chunks_sent_this_attempt = False
                    break # Break from chunk loop for this attempt type
            # End of chunk sending loop for this attempt type

            if all_chunks_sent_this_attempt:
                message_sent_or_edited_successfully = True
                logger.info(f"{log_prefix}All chunks successfully sent/edited with attempt '{attempt_type}'.")
                break # Break from the attempt_type loop (e.g., "markdown_render", "full_escape")
        # End of attempt_type loop

    if not message_sent_or_edited_successfully:
        logger.error(f"{log_prefix}All message sending strategies failed for the user.")
        final_user_error_message = escape_markdown_v2("Sorry, I encountered an issue and couldn't display the full response.")
        if placeholder_message:
            try: await context.bot.edit_message_text(chat_id, placeholder_message.message_id, final_user_error_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
            except Exception as e_last_ditch: logger.error(f"{log_prefix}Last-ditch edit of placeholder failed: {e_last_ditch}")
        else: # No placeholder, or editing it failed
            try: await update.message.reply_text(final_user_error_message, parse_mode=constants.ParseMode.MARKDOWN_V2, reply_to_message_id=update.message.message_id)
            except Exception as e_last_ditch_reply: logger.error(f"{log_prefix}Last-ditch reply failed: {e_last_ditch_reply}")

    # Update History
    # Store the raw LLM response if no LLM-reported error and we have content.
    # This is most faithful to what the LLM produced, before any client-side processing for display.
    if not llm_error_reported_by_model and raw_full_llm_response.strip():
        logger.debug(f"{log_prefix}Updating conversation history.")
        try:
            # It's safer to re-fetch history here in case of long operations or concurrent access (though less likely with asyncio)
            current_history = await file_storage.get_thread_key(chat_id, 'history', [])
            current_history = current_history[-19:] # Keep last N interactions (e.g., 10 user + 10 assistant = 20 messages)
            current_history.extend([
                {'role': 'user', 'content': message_text},
                {'role': 'assistant', 'content': raw_full_llm_response.strip()}
            ])
            await file_storage.set_thread_key(chat_id, 'history', current_history)
            logger.debug(f"{log_prefix}History updated successfully.")
        except Exception as e_hist:
            logger.error(f"{log_prefix}Failed to update history: {e_hist}")

    logger.info(f"{log_prefix}Finished processing message for User {user_id}.")


# Handler export
chat_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)