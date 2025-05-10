import logging
import asyncio
from telegram import Update, BotCommand, MenuButtonCommands # Import MenuButtonCommands
from telegram.ext import CommandHandler, Application, ContextTypes

# Import the function to create the application
from bot.application import create_application
import config # To access config variables if needed directly
# Import provider initialization function
from bot.providers import get_provider_details

# Configure logging (ensure it's configured before other imports if needed)
# Basic config is already done in config.py, but you can customize further
logger = logging.getLogger(__name__)

# --- Basic Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    # Escape Markdown V2 characters in the user's name
    safe_user_name = user.mention_markdown_v2()
    await update.message.reply_markdown_v2(
        rf'Hi {safe_user_name}\! I am your friendly LLM bot\. How can I help you today\?'
    )

# --- Startup Checks Function ---
async def run_startup_checks(application: Application) -> None:
    """
    Runs asynchronous checks for AI services after initialization
    AND sets the default bot commands and menu button.
    """
    logger.info("Performing startup checks for AI services...")
    # Import services here to avoid potential circular imports during module load
    from services import ollama_service, gemini_service, openrouter_service

    # Run checks concurrently
    ollama_ok, gemini_ok, openrouter_ok = await asyncio.gather(
        ollama_service.check_connection(),
        gemini_service.check_connection(),
        openrouter_service.check_connection(),
        return_exceptions=True
    )

    if isinstance(ollama_ok, Exception) or not ollama_ok:
        logger.warning(f"Ollama connection check failed or service unavailable. Ollama features may not work. Error (if any): {ollama_ok}")
    else:
        logger.info("Ollama connection check successful.")

    if not config.GEMINI_API_KEYS or all(key == "YOUR_GOOGLE_API_KEY" for key in config.GEMINI_API_KEYS):
         logger.warning("No valid Gemini API keys configured. Gemini features disabled.")
    elif isinstance(gemini_ok, Exception) or not gemini_ok:
        logger.warning(f"Gemini connection check failed or service unavailable (tried key(s)). Gemini features may not work. Error (if any): {gemini_ok}")
    else:
        logger.info("Gemini connection check successful.")

    if not config.OPENROUTER_API_KEY or config.OPENROUTER_API_KEY == "YOUR_OPENROUTER_API_KEY":
        logger.warning("OpenRouter API Key not configured. OpenRouter features disabled.")
    elif isinstance(openrouter_ok, Exception) or not openrouter_ok:
        logger.warning(f"OpenRouter connection check failed or service unavailable. OpenRouter features may not work. Error (if any): {openrouter_ok}")
    else:
        logger.info("OpenRouter connection check successful.")

    # --- Set Bot Command Menu and Menu Button ---
    # Import necessary scope classes if setting for specific scopes
    from telegram import BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats # Uncommented

    # Define the commands to appear in the menu
    # Note: Keep this list consistent with the handlers you actually register in main()
    commands = [
        BotCommand("start", "Start the bot and show welcome message"),
        BotCommand("help", "Show help and available commands"),
        BotCommand("new", "Start a new conversation thread"),
        BotCommand("threads", "Manage and switch conversation threads"),
        BotCommand("provider", "Show/switch LLM provider"),
        BotCommand("model", "Show current model for active provider"),
        BotCommand("list_models", "List available models for active provider"),
        BotCommand("set_model", "Set model for active provider"),
        # Add more commands as needed
    ]
    try:
        await application.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        await application.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
        logger.info("Bot command list set successfully for all scopes.")

        # Step 2: Explicitly set the menu button to show the command list
        # This is crucial for the menu button to work reliably.
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Default menu button set to show commands.")

    except Exception as e:
        logger.error(f"Failed to set bot commands: {e}") # Simplified error message

# --- Main Execution ---

# Removed first, unused main() function definition

# --- Main Execution ---

def main() -> None:
    """Create the application, register handlers, and start the bot."""
    logger.info("Starting bot initialization...")

    # --- Post Initialization Hook ---
    # Combined hook for startup checks and setting commands
    async def post_init_with_commands(application: Application):
        # Initialize providers first (moved from the duplicate definition)
        logger.info("Initializing provider details...")
        get_provider_details() # Call the function to populate the cache
        logger.info("Provider details initialization complete.")

        # Run connection checks AND set commands/menu button
        await run_startup_checks(application) # run_startup_checks now handles commands/menu

        # Log bot username and token (masked) for verification
        try:
            bot_info = await application.bot.get_me()
            token_masked = f"{config.TELEGRAM_BOT_TOKEN[:5]}...{config.TELEGRAM_BOT_TOKEN[-4:]}" if config.TELEGRAM_BOT_TOKEN else "Not Set"
            logger.info(f"Bot initialized: Username='{bot_info.username}', Token='{token_masked}'")
        except Exception as e:
            logger.error(f"Failed to get bot info: {e}")

    # --- Create Application ---
    # We need to run this within an async context after the app is initialized
    # A simple way is to add it to the post_init hook
    # Removed duplicate post_init_with_commands definition

    try:
        # Pass the combined post_init hook to your application creation function
        app = create_application(post_init_hook=post_init_with_commands)
    except ValueError as e:
        logger.critical(f"Failed to create Telegram application: {e}. Exiting.")
        return
    except Exception as e:
        logger.critical(f"An unexpected error occurred during application creation: {e}. Exiting.")
        return

    # --- Register Handlers ---
    # Import handlers here (avoids potential circular imports if handlers import config/providers)
    from bot.handlers.ollama_commands import ollama_handlers
    from bot.handlers.misc_commands import misc_handlers # Contains generic /provider, /model, etc.
    from bot.handlers.gemini_commands import gemini_handlers
    from bot.handlers.openrouter_commands import openrouter_handlers
    from bot.handlers.ask_selected_handler import ask_selected_handlers
    from bot.handlers.chat import chat_handler

    # Register all handlers
    # Note: misc_handlers now includes the ConversationHandler for /set_model
    app.add_handlers(misc_handlers) # Add misc first as it contains /start, /help, /provider, /model...
    app.add_handlers(ollama_handlers) # Keep provider-specific for now
    app.add_handlers(gemini_handlers)
    app.add_handlers(openrouter_handlers)
    app.add_handlers(ask_selected_handlers)
    logger.info("Registered command and conversation handlers.")

    # Register the main chat handler (must be added *after* command/conversation handlers)
    app.add_handler(chat_handler)
    logger.info("Registered main chat handler.")

    # --- Global Error Handler ---
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log Errors caused by Updates."""
        logger.error("Exception while handling an update:", exc_info=context.error)
        # Optionally, notify the user in the chat where the error occurred
        if isinstance(update, Update) and update.effective_message:
            try:
                # Use parse_mode=None for plain text error message
                await update.effective_message.reply_text(
                    "Sorry, an internal error occurred. The developers have been notified.",
                    parse_mode=None
                )
            except Exception as e:
                logger.error(f"Failed to send error notification to user: {e}")

    app.add_error_handler(error_handler)
    logger.info("Registered global error handler.")

    # --- Start Bot ---
    logger.info("Starting bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES) # Consider specifying updates if known

    # The bot runs until you press Ctrl-C
    logger.info("Bot stopped.")


if __name__ == "__main__":
    # Add any necessary checks before starting main() if needed
    if not hasattr(config, 'TELEGRAM_BOT_TOKEN') or not config.TELEGRAM_BOT_TOKEN:
        logger.critical("CRITICAL: TELEGRAM_BOT_TOKEN not found in config. Exiting.")
        exit(1)
    main()
