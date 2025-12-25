import logging
import config

logger = logging.getLogger(__name__)

# Conditionally import the backend based on the configuration
if config.get_storage_backend() == 'database':
    logger.info("Using database storage backend.")
    from . import database_storage as backend
elif config.get_storage_backend() == 'file':
    logger.info("Using file storage backend.")
    from . import file_storage as backend
else:
    logger.error(f"Invalid STORAGE_BACKEND '{config.get_storage_backend()}'. Defaulting to database.")
    from . import database_storage as backend

# Expose the functions from the chosen backend
init = backend.init_database
save_message = backend.save_message
get_thread_history = backend.get_thread_history
get_all_chat_ids = backend.get_all_chat_ids
get_user_setting = backend.get_user_setting
set_user_setting = backend.set_user_setting
get_current_thread_id = backend.get_current_thread_id
get_thread_key = backend.get_thread_key
set_thread_key = backend.set_thread_key
