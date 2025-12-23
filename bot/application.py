import logging
from telegram.ext import Application, ApplicationBuilder, Defaults
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from typing import Callable, Awaitable
import config

logger = logging.getLogger(__name__)

# Define the type hint for the async post_init function
PostInitFunc = Callable[[Application], Awaitable[None]]

def create_application(post_init_hook: PostInitFunc | None = None) -> Application:
    """Creates and configures the Telegram Bot Application."""
    if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.error("Telegram Bot Token is not configured. Exiting.")
        raise ValueError("TELEGRAM_BOT_TOKEN is missing or invalid.")

    # Set default settings for the bot (e.g., parse mode)
    defaults = Defaults(parse_mode=ParseMode.MARKDOWN_V2)

    # Configure HTTP request settings with improved timeout handling
    request = HTTPXRequest(
        connection_pool_size=8,  # Increase connection pool size
        pool_timeout=120.0,       # Pool timeout
        read_timeout=120.0,       # Read timeout for responses
        write_timeout=120.0,      # Write timeout for requests
        connect_timeout=120.0     # Connection timeout (includes socket timeout)
    )

    # Create the ApplicationBuilder instance with improved request configuration
    builder = (Application.builder()
               .token(config.TELEGRAM_BOT_TOKEN)
               .defaults(defaults)
               .request(request)
               .concurrent_updates(True))

    # Add the post_init hook if provided
    if post_init_hook:
        builder.post_init(post_init_hook)

    # Build the Application
    application = builder.build()

    logger.info("Telegram Application created with improved timeout configuration.")
    return application

# Note: We no longer create the app instance here as a singleton,
# because the post_init hook needs to be passed during creation in main.py.
# The 'app' instance will be created and managed within main.py.
