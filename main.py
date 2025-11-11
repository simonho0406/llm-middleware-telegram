import logging
import asyncio
from telegram import Update
from telegram.ext import CommandHandler, Application, ContextTypes
from bot.menu_setup import setup_bot_commands_and_menu

# Import the function to create the application
from bot.application import create_application
import config # To access config variables if needed directly
# Import provider initialization function
from bot.providers import get_provider_details
from storage import storage_manager

# Configure logging
logger = logging.getLogger(__name__)

# --- Basic Command Handlers ---

# --- Basic Command Handlers ---

# --- Startup Checks Function ---
async def run_startup_checks(application: Application) -> None:
    """Runs asynchronous checks for AI services after initialization."""
    logger.info("Performing startup checks for AI services...")
    from services import ollama_service, gemini_service, openrouter_service

    results = await asyncio.gather(
        ollama_service.check_connection(),
        openrouter_service.check_connection(),
        return_exceptions=True
    )
    
    service_names = ["Ollama", "OpenRouter"]
    for name, result in zip(service_names, results):
        if isinstance(result, Exception) or not result:
            logger.warning(f"{name} connection check failed. Features may be limited. Error: {result}")
        else:
            logger.info(f"{name} connection check successful.")

# --- Main Execution ---

def main() -> None:
    """Create the application, register handlers, and start the bot."""
    logger.info("Starting bot initialization...")

    async def post_init_with_commands(application: Application):
        logger.info("Initializing provider details...")
        get_provider_details()
        logger.info("Provider details initialization complete.")

        # Initialize storage
        logger.info("Initializing storage...")
        await storage_manager.init()
        logger.info("Storage initialization complete.")
        
        # Clean up any lingering conversation states that might interfere with ConversationHandlers
        logger.info("Cleaning up persistent conversation states...")
        try:
            # PTB stores conversation states in memory but can have stale states after restarts
            # Clear all user_data that might contain persistent panel_state or other conversation remnants
            # This is critical for resolving callback query routing failures
            
            # Get all active chats to clean their user_data
            all_chat_ids = await storage_manager.get_all_chat_ids()
            if all_chat_ids:
                for chat_id in all_chat_ids:
                    # Clear any persistent user_data that might interfere with ConversationHandlers
                    # This includes panel_state, conversation states, and other cached data
                    if chat_id in application.user_data:
                        old_data = application.user_data[chat_id].copy()
                        application.user_data[chat_id].clear()
                        if old_data:
                            logger.info(f"Cleared persistent user_data for chat {chat_id}: {list(old_data.keys())}")
                    
                    # Also clear any chat_data that might contain stale states
                    if chat_id in application.chat_data:
                        old_chat_data = application.chat_data[chat_id].copy()
                        application.chat_data[chat_id].clear()
                        if old_chat_data:
                            logger.info(f"Cleared persistent chat_data for chat {chat_id}: {list(old_chat_data.keys())}")
                
                logger.info(f"Conversation state cleanup completed for {len(all_chat_ids)} chats.")
            else:
                logger.info("No existing chats found for conversation state cleanup.")
        except Exception as e:
            logger.warning(f"Conversation state cleanup failed: {e}")

        # --- Failsafe: Reset command scopes for all known chats ---
        logger.info("Running startup failsafe: resetting command scopes for all known chats...")
        try:
            all_chat_ids = await storage_manager.get_all_chat_ids()
            if all_chat_ids:
                for chat_id in all_chat_ids:
                    try:
                        await setup_bot_commands_and_menu(application, chat_id)
                    except Exception as e:
                        logger.error(f"Failed to reset command scope for chat_id {chat_id} on startup: {e}")
                logger.info(f"Completed command scope reset for {len(all_chat_ids)} chats.")
            else:
                logger.info("No existing chats found to reset command scopes.")
        except Exception as e:
            logger.error(f"An error occurred during the startup command scope reset: {e}")

        # Run connection checks and set up the new global commands/menu
        await run_startup_checks(application)
        await setup_bot_commands_and_menu(application) # This now includes the temporary fix

        try:
            bot_info = await application.bot.get_me()
            token_masked = f"{config.TELEGRAM_BOT_TOKEN[:5]}...{config.TELEGRAM_BOT_TOKEN[-4:]}" if config.TELEGRAM_BOT_TOKEN else "Not Set"
            logger.info(f"Bot initialized: Username='{bot_info.username}', Token='{token_masked}'")
        except Exception as e:
            logger.error(f"Failed to get bot info: {e}")

    try:
        app = create_application(post_init_hook=post_init_with_commands)

    except ValueError as e:
        logger.critical(f"Failed to create Telegram application: {e}. Exiting.")
        return
    except Exception as e:
        logger.critical(f"An unexpected error occurred during application creation: {e}. Exiting.")
        return

    # --- Register Handlers ---
    from bot.handlers.misc_commands import misc_handlers
    from bot.handlers.ask_selected_handler import ask_selected_handlers
    from bot.handlers.chat import chat_handler, edited_message_handler
    from bot.handlers.discuss_handler import discuss_conv_handler
    from bot.handlers.discuss_panel_handler import discuss_panel_conv_handler
    from bot.handlers.config_handler import config_conv_handler
    from bot.handlers.configure_panel_handler import configure_panel_conv_handler

    # High-priority group for conversation handlers (group=0)
    app.add_handler(config_conv_handler, group=0)
    app.add_handler(discuss_conv_handler, group=0)
    app.add_handler(discuss_panel_conv_handler, group=0)
    app.add_handler(configure_panel_conv_handler, group=0)
    app.add_handler(edited_message_handler, group=0)
    app.add_handler(chat_handler, group=0)
    for handler in ask_selected_handlers:
        app.add_handler(handler, group=0)
    
    # Lower-priority group for command handlers (group=1)
    for handler in misc_handlers:
        app.add_handler(handler, group=1)

    
    logger.info("Registered handlers with priority groups")

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception while handling an update:", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            error_msg = "Sorry, an internal error occurred. The developers have been notified."
            try:
                # Try with timeout protection
                await asyncio.wait_for(
                    update.effective_message.reply_text(error_msg, parse_mode=None),
                    timeout=10.0
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.error(f"Failed to send error notification to user: {e}")
                try:
                    # Fallback: direct send if reply fails
                    chat_id = update.effective_chat.id if update.effective_chat else None
                    if chat_id:
                        await asyncio.wait_for(
                            context.bot.send_message(chat_id=chat_id, text=error_msg, parse_mode=None),
                            timeout=10.0
                        )
                except (asyncio.TimeoutError, Exception) as fallback_error:
                    logger.error(f"Fallback error notification also failed: {fallback_error}")

    app.add_error_handler(error_handler)
    logger.info("Registered global error handler.")

    import time
    from telegram.error import NetworkError

    logger.info("Starting bot polling...")
    while True:
        try:
            app.run_polling(allowed_updates=Update.ALL_TYPES)
            logger.info("Bot stopped normally.")
            break  # Exit loop if stopped normally
        except NetworkError as e:
            logger.error(f"Telegram NetworkError: {e}. Reconnecting in 10 seconds...")
            time.sleep(10)
        except Exception as e:
            logger.critical(f"Unexpected error in polling loop: {e}", exc_info=True)
            logger.info("Restarting in 30 seconds...")
            time.sleep(30)


if __name__ == "__main__":
    if not hasattr(config, 'TELEGRAM_BOT_TOKEN') or not config.TELEGRAM_BOT_TOKEN:
        logger.critical("CRITICAL: TELEGRAM_BOT_TOKEN not found in config. Exiting.")
        exit(1)
    main()
