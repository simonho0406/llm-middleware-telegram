# File: storage/__init__.py

import logging
import config
from . import database_storage
from . import file_storage

logger = logging.getLogger(__name__)

class StorageManager:
    def __init__(self, backend: str):
        logger.info(f"Initializing StorageManager with backend: {backend}")
        if backend == "database":
            self.init = database_storage.init_database
            self.get_current_thread_id = database_storage.get_current_thread_id
            self.set_current_thread_id = database_storage.set_current_thread_id
            self.get_thread_key = database_storage.get_thread_key
            self.set_thread_key = database_storage.set_thread_key
            self.get_thread_history = database_storage.get_thread_history
            self.set_thread_history = database_storage.set_thread_history
            self.create_thread = database_storage.create_thread
            self.delete_thread = database_storage.delete_thread
            self.list_threads = database_storage.list_threads
            self.rename_thread = database_storage.rename_thread
            self.save_message = database_storage.save_message
        elif backend == "file":
            self.init = file_storage.init_file_storage
            self.get_current_thread_id = file_storage.get_current_thread_id
            self.set_current_thread_id = file_storage.set_current_thread_id
            self.get_thread_key = file_storage.get_thread_key
            self.set_thread_key = file_storage.set_thread_key
            self.get_thread_history = file_storage.get_thread_history
            self.set_thread_history = file_storage.set_thread_history
            self.create_thread = file_storage.create_thread
            self.delete_thread = file_storage.delete_thread
            self.list_threads = file_storage.list_threads
            self.rename_thread = file_storage.rename_thread
            self.save_message = file_storage.save_message
        else:
            raise ValueError(f"Invalid STORAGE_BACKEND: {backend}")

# Create a single, globally accessible instance of the StorageManager
storage_manager = StorageManager(backend=config.STORAGE_BACKEND)
