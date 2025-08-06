import logging
from telegram import BotCommand, MenuButtonCommands, BotCommandScopeDefault, BotCommandScopeChat
from telegram.ext import Application

logger = logging.getLogger(__name__)

async def setup_bot_commands_and_menu(application: Application, chat_id: int | None = None) -> None:
    """Sets the bot's command list and menu button for the specified scope."""
    commands = [
        # Core
        BotCommand("help", "Show available commands"),
        BotCommand("new", "Start a new conversation"),
        BotCommand("reroll", "Regenerate the last AI response"),
        BotCommand("cancel", "Cancel the current operation"),
        # Tools
        BotCommand("search", "Answer a query using web search"),
        BotCommand("ask_selected", "Query multiple models at once"),
        BotCommand("discuss", "Start a multi-model, multi-provider discussion"),
        BotCommand("discuss_panel", "Orchestrate an expert AI panel"),
        BotCommand("end_discussion", "Conclude an ongoing panel discussion"),
        # Provider & Model
        BotCommand("provider", "Switch AI provider"),
        BotCommand("model", "Show the current AI model"),
        BotCommand("list_models", "List available models for the provider"),
        BotCommand("set_model", "Set a new model for the provider"),
        # Thread Management
        BotCommand("threads", "List and manage conversation threads"),
        BotCommand("rename_thread", "Rename the current thread"),
        # Misc
        BotCommand("start", "Initialize the bot"),
    ]
    
    scope = BotCommandScopeChat(chat_id) if chat_id else BotCommandScopeDefault()
    
    try:
        await application.bot.set_my_commands(commands, scope=scope)
        
        if isinstance(scope, BotCommandScopeDefault):
            await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            logger.info("Successfully set bot command list and menu button for the default scope.")
        else:
            logger.info(f"Successfully set bot command list for chat {chat_id}.")

    except Exception as e:
        logger.error(f"Failed to set bot commands for scope {scope}: {e}")
