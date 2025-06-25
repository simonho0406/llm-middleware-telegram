import logging
import asyncio
from telegram import Update, BotCommand, MenuButtonCommands
from telegram.ext import CommandHandler, Application, ContextTypes

# Import the function to create the application
from bot.application import create_application
import config # To access config variables if needed directly
# Import provider initialization function
from bot.providers import get_provider_details

# Configure logging
logger = logging.getLogger(__name__)

# --- Basic Command Handlers ---

# --- Bot Commands and Menu Setup Function ---
async def setup_bot_commands_and_menu(application: Application) -> None:
    """Sets the bot's command list and menu button for all scopes."""
    commands = [
        # Core
        BotCommand("help", "Show available commands"),
        BotCommand("new", "Start a new conversation"),
        BotCommand("reroll", "Regenerate the last AI response"),
        # Tools
        BotCommand("search", "Answer a query using web search"),
        BotCommand("ask_selected", "Query multiple models at once"),
        BotCommand("discuss", "Start a multi-model discussion"),
        # Provider & Model
        BotCommand("provider", "Switch AI provider (e.g., Ollama, Gemini)"),
        BotCommand("model", "Show the current AI model"),
        BotCommand("list_models", "List available models for the provider"),
        BotCommand("set_model", "Set a new model for the provider"),
        # Thread Management
        BotCommand("threads", "List and manage conversation threads"),
        BotCommand("rename_thread", "Rename the current thread"),
        # Misc
        BotCommand("start", "Initialize the bot"),
    ]
    
    try:
        # 1. Set commands for the default scope (for all users)
        await application.bot.set_my_commands(commands)
        
        # 2. Set the menu button to show commands for the default scope
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        
        logger.info("Successfully set bot command list and menu button for the default scope.")


    except Exception as e:
        logger.error(f"Failed to set bot commands or menu button: {e}")

# --- Startup Checks Function ---
async def run_startup_checks(application: Application) -> None:
    """Runs asynchronous checks for AI services after initialization."""
    logger.info("Performing startup checks for AI services...")
    from services import ollama_service, gemini_service, openrouter_service

    results = await asyncio.gather(
        ollama_service.check_connection(),
        gemini_service.check_connection(),
        openrouter_service.check_connection(),
        return_exceptions=True
    )
    
    service_names = ["Ollama", "Gemini", "OpenRouter"]
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
    from bot.handlers.chat import chat_handler
    from bot.handlers.discuss_handler import discuss_conv_handler

    app.add_handlers(misc_handlers)
    app.add_handlers(ask_selected_handlers)
    app.add_handler(discuss_conv_handler)
    logger.info("Registered command and conversation handlers.")
    app.add_handler(chat_handler)
    logger.info("Registered main chat handler.")

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception while handling an update:", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "Sorry, an internal error occurred. The developers have been notified.",
                    parse_mode=None
                )
            except Exception as e:
                logger.error(f"Failed to send error notification to user: {e}")

    app.add_error_handler(error_handler)
    logger.info("Registered global error handler.")

    logger.info("Starting bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot stopped.")


if __name__ == "__main__":
    if not hasattr(config, 'TELEGRAM_BOT_TOKEN') or not config.TELEGRAM_BOT_TOKEN:
        logger.critical("CRITICAL: TELEGRAM_BOT_TOKEN not found in config. Exiting.")
        exit(1)
    main()
