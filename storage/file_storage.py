import json
import os
import logging
import asyncio
from typing import Dict, Any, Optional, List # Import List
import config

logger = logging.getLogger(__name__)

# Ensure the data directory exists
DATA_DIR = os.path.dirname(config.SESSION_FILE_PATH)
if DATA_DIR and not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR)
        logger.info(f"Created data directory: {DATA_DIR}")
    except OSError as e:
        logger.error(f"Failed to create data directory {DATA_DIR}: {e}")
        # Depending on severity, might want to raise error or disable sessions

# In-memory cache of sessions to reduce file I/O
# Structure: { chat_id: { "current_thread_id": "...", "threads": { "thread_id": { ...thread_data... } } } }
_sessions: Dict[int, Dict[str, Any]] = {}
_lock = asyncio.Lock() # Lock for asynchronous file access
_DEFAULT_THREAD_ID = "default"

def _load_sessions_from_file() -> None:
    """Loads all sessions from the JSON file into the memory cache."""
    global _sessions
    if not os.path.exists(config.SESSION_FILE_PATH):
        logger.info(f"Session file '{config.SESSION_FILE_PATH}' not found. Starting with empty sessions.")
        _sessions = {}
        return
    try:
        with open(config.SESSION_FILE_PATH, 'r') as f:
            content = f.read()
            if not content.strip(): # Handle empty file
                 _sessions = {}
                 logger.info(f"Session file '{config.SESSION_FILE_PATH}' is empty.")
            else:
                 loaded_data = json.loads(content)
                 # Ensure data is dict and keys are integers
                 _sessions = {int(k): v for k, v in loaded_data.items()}
                 logger.info(f"Loaded {_sessions.__len__()} sessions from '{config.SESSION_FILE_PATH}'.")
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from session file '{config.SESSION_FILE_PATH}': {e}. Starting with empty sessions.")
        _sessions = {} # Avoid corrupting data
    except Exception as e:
        logger.error(f"Failed to load sessions from '{config.SESSION_FILE_PATH}': {e}. Starting with empty sessions.")
        _sessions = {}

import aiofiles

async def _save_sessions_to_file() -> None:
    """Saves the current in-memory sessions to the JSON file."""
    try:
        # Copy sessions under lock to avoid race conditions, but do file I/O outside lock
        async with _lock:
            sessions_to_save = {str(k): v for k, v in _sessions.items()}
        async with aiofiles.open(config.SESSION_FILE_PATH, 'w') as f:
            await f.write(json.dumps(sessions_to_save, indent=4))
            await f.flush()  # Ensure data is flushed to disk
        logger.info(f"Saved sessions to '{config.SESSION_FILE_PATH}'.")
    except Exception as e:
        logger.error(f"Failed to save sessions to '{config.SESSION_FILE_PATH}': {e}")

async def get_session(chat_id: int) -> Dict[str, Any]:
    """
    Retrieves the full session data object for a given chat ID.
    Initializes session structure if it doesn't exist.
    """
    async with _lock:
        if chat_id not in _sessions:
            logger.debug(f"Initializing new session structure for chat_id {chat_id}")
            _sessions[chat_id] = {
                "current_thread_id": _DEFAULT_THREAD_ID,
                "threads": {_DEFAULT_THREAD_ID: {}}
            }
        elif "current_thread_id" not in _sessions[chat_id]:
             logger.debug(f"Initializing 'current_thread_id' for chat_id {chat_id}")
             _sessions[chat_id]["current_thread_id"] = _DEFAULT_THREAD_ID
        elif "threads" not in _sessions[chat_id]:
             logger.debug(f"Initializing 'threads' dict for chat_id {chat_id}")
             _sessions[chat_id]["threads"] = {_DEFAULT_THREAD_ID: {}}
        elif _DEFAULT_THREAD_ID not in _sessions[chat_id]["threads"]:
             logger.debug(f"Initializing 'default' thread for chat_id {chat_id}")
             _sessions[chat_id]["threads"][_DEFAULT_THREAD_ID] = {}

        # Return a deep copy to prevent modification of the cached dict directly
        # Note: json loads/dumps is a simple way to deep copy dicts/lists
        return json.loads(json.dumps(_sessions.get(chat_id, {})))

async def get_current_thread_id(chat_id: int) -> str:
    """Gets the current active thread ID for the chat."""
    logger.debug(f"Getting current thread ID for chat {chat_id}")
    session = await get_session(chat_id) # Ensures session exists
    current_id = session.get("current_thread_id", _DEFAULT_THREAD_ID)
    logger.debug(f"Current thread ID for chat {chat_id} is '{current_id}'")
    return current_id

async def set_current_thread_id(chat_id: int, thread_id: str) -> bool:
    """Sets the current active thread ID, returns True if successful."""
    logger.debug(f"Attempting to set current thread for chat {chat_id} to '{thread_id}'")
    # Ensure session structure exists before acquiring lock
    session = await get_session(chat_id)
    async with _lock:
        if thread_id not in _sessions[chat_id].get("threads", {}):
            logger.error(f"Attempted to set non-existent thread '{thread_id}' as current for chat {chat_id}")
            return False
        _sessions[chat_id]["current_thread_id"] = thread_id
        logger.debug(f"Current thread ID set to '{thread_id}' in memory for chat {chat_id}")
    await _save_sessions_to_file()
    logger.info(f"Successfully set and saved current thread for chat {chat_id} to '{thread_id}'")
    return True

async def get_thread_data(chat_id: int, thread_id: Optional[str] = None) -> Dict[str, Any]:
    """Gets the data for a specific thread, or the current thread if thread_id is None."""
    session = await get_session(chat_id) # Ensures session exists
    target_thread_id = thread_id if thread_id is not None else session.get("current_thread_id", _DEFAULT_THREAD_ID)
    logger.debug(f"Getting thread data for chat {chat_id}, thread '{target_thread_id}'")
    # Return a copy
    data = session.get("threads", {}).get(target_thread_id, {}).copy()
    logger.debug(f"Returning data for thread '{target_thread_id}': {list(data.keys())}")
    return data

async def update_thread_data(chat_id: int, data: Dict[str, Any], thread_id: Optional[str] = None) -> None:
    """Updates data for a specific thread, or the current thread if thread_id is None."""
    async with _lock:
        # Ensure session structure exists using get_session logic implicitly via _sessions access
        if chat_id not in _sessions:
             logger.debug(f"Initializing session for chat {chat_id} during update_thread_data")
             _sessions[chat_id] = {
                 "current_thread_id": _DEFAULT_THREAD_ID,
                 "threads": {_DEFAULT_THREAD_ID: {}}
             }
        session = _sessions[chat_id]
        target_thread_id = thread_id if thread_id is not None else session.get("current_thread_id", _DEFAULT_THREAD_ID)
        logger.debug(f"Updating thread data for chat {chat_id}, thread '{target_thread_id}' with keys: {list(data.keys())}")

        if "threads" not in session:
             logger.debug(f"Initializing 'threads' dict for chat {chat_id} during update")
             session["threads"] = {}
        if target_thread_id not in session["threads"]:
             logger.debug(f"Initializing thread '{target_thread_id}' for chat {chat_id} during update")
             session["threads"][target_thread_id] = {}

        session["threads"][target_thread_id].update(data)
        logger.debug(f"Thread '{target_thread_id}' updated in memory for chat {chat_id}")

    await _save_sessions_to_file()

async def set_thread_key(chat_id: int, key: str, value: Any, thread_id: Optional[str] = None) -> None:
    """Sets a specific key-value pair for a thread (defaults to current thread)."""
    target_thread_id_str = f"thread '{thread_id}'" if thread_id else "current thread"
    logger.debug(f"Setting key '{key}' for chat {chat_id}, {target_thread_id_str}")
    await update_thread_data(chat_id, {key: value}, thread_id)
    logger.debug(f"Key '{key}' set successfully for chat {chat_id}, {target_thread_id_str}")

async def get_thread_key(chat_id: int, key: str, default: Optional[Any] = None, thread_id: Optional[str] = None) -> Any:
    """Gets a specific key's value for a thread (defaults to current thread)."""
    target_thread_id_str = f"thread '{thread_id}'" if thread_id else "current thread"
    logger.debug(f"Getting key '{key}' for chat {chat_id}, {target_thread_id_str}")
    thread_data = await get_thread_data(chat_id, thread_id)
    value = thread_data.get(key, default)
    logger.debug(f"Value for key '{key}' in chat {chat_id}, {target_thread_id_str}: {'<default>' if value is default else '<found>'}")
    return value

async def list_threads(chat_id: int) -> List[str]:
     """Lists the IDs of all threads for a chat."""
     logger.debug(f"Listing threads for chat {chat_id}")
     session = await get_session(chat_id)
     thread_ids = list(session.get("threads", {}).keys())
     logger.debug(f"Found threads for chat {chat_id}: {thread_ids}")
     return thread_ids

async def create_thread(chat_id: int, thread_id: str) -> bool:
     """Creates a new empty thread, returns True if successful."""
     logger.debug(f"Attempting to create thread '{thread_id}' for chat {chat_id}")
     async with _lock:
         # Use internal _sessions directly after ensuring chat exists
         if chat_id not in _sessions:
             logger.debug(f"Initializing session for chat {chat_id} during create_thread")
             _sessions[chat_id] = {
                 "current_thread_id": _DEFAULT_THREAD_ID,
                 "threads": {_DEFAULT_THREAD_ID: {}}
             }

         if thread_id in _sessions[chat_id].get("threads", {}):
             logger.warning(f"Attempted to create existing thread '{thread_id}' for chat {chat_id}")
             return False # Thread already exists

         if "threads" not in _sessions[chat_id]: # Should be created by get_session, but double check
              _sessions[chat_id]["threads"] = {}
         _sessions[chat_id]["threads"][thread_id] = {} # Initialize with empty dict
         logger.debug(f"Thread '{thread_id}' created in memory for chat {chat_id}")
     await _save_sessions_to_file()
     logger.info(f"Successfully created and saved thread '{thread_id}' for chat {chat_id}")
     return True

async def delete_thread(chat_id: int, thread_id: str) -> bool:
     """Deletes a thread, returns True if successful. Cannot delete default."""
     logger.debug(f"Attempting to delete thread '{thread_id}' for chat {chat_id}")
     if thread_id == _DEFAULT_THREAD_ID:
         logger.warning(f"Attempted to delete default thread for chat {chat_id}")
         return False

     async with _lock:
         # Use internal _sessions directly after ensuring chat exists
         if chat_id not in _sessions:
             logger.warning(f"Attempted to delete thread '{thread_id}' for non-existent chat {chat_id}")
             return False
         session = _sessions[chat_id] # Use direct reference under lock

         if thread_id not in session.get("threads", {}):
             logger.warning(f"Attempted to delete non-existent thread '{thread_id}' for chat {chat_id}")
             return False # Thread doesn't exist

         # If deleting the current thread, switch back to default
         if session.get("current_thread_id") == thread_id:
             logger.debug(f"Deleting current thread '{thread_id}', switching chat {chat_id} to default")
             session["current_thread_id"] = _DEFAULT_THREAD_ID

         del session["threads"][thread_id]
         logger.debug(f"Thread '{thread_id}' deleted in memory for chat {chat_id}")

     await _save_sessions_to_file()
     logger.info(f"Successfully deleted and saved thread '{thread_id}' for chat {chat_id}")
     return True

async def rename_thread(chat_id: int, new_name: str) -> bool:
    """Rename the current thread's name (stored in 'name' key)."""
    current_thread_id = await get_current_thread_id(chat_id)
    if not current_thread_id:
        return False
    try:
        await set_thread_key(chat_id, 'name', new_name, thread_id=current_thread_id)
        return True
    except Exception as e:
        logger.error(f"Failed to rename thread for chat {chat_id}: {e}")
        return False

# Load sessions when the module is imported
_load_sessions_from_file()

# Example usage (for testing purposes) - Needs update for new structure
async def _test():
    print("Testing File Storage (Threaded)...")
    chat_id_1 = 123
    chat_id_2 = 456

    # Initial state
    print(f"Initial session for {chat_id_1}: {await get_session(chat_id_1)}")
    print(f"Initial current thread for {chat_id_1}: {await get_current_thread_id(chat_id_1)}")
    print(f"Initial default thread data for {chat_id_1}: {await get_thread_data(chat_id_1)}")

    # Set data in default thread
    await set_thread_key(chat_id_1, "provider", "ollama")
    await set_thread_key(chat_id_1, "ollama_model", "llama3:latest")
    print(f"Default thread data for {chat_id_1} after set: {await get_thread_data(chat_id_1)}")

    # Create and switch to a new thread
    new_thread_name = "work_project"
    await create_thread(chat_id_1, new_thread_name)
    await set_current_thread_id(chat_id_1, new_thread_name)
    print(f"Threads for {chat_id_1}: {await list_threads(chat_id_1)}")
    print(f"Current thread for {chat_id_1}: {await get_current_thread_id(chat_id_1)}")

    # Set data in the new thread
    await set_thread_key(chat_id_1, "provider", "gemini")
    await set_thread_key(chat_id_1, "history", [{"role": "user", "content": "hello"}])
    print(f"'{new_thread_name}' thread data for {chat_id_1}: {await get_thread_data(chat_id_1)}")
    print(f"Provider for current thread ({new_thread_name}): {await get_thread_key(chat_id_1, 'provider')}")

    # Switch back to default
    await set_current_thread_id(chat_id_1, _DEFAULT_THREAD_ID)
    print(f"Current thread for {chat_id_1} after switch back: {await get_current_thread_id(chat_id_1)}")
    print(f"Provider for current thread (default): {await get_thread_key(chat_id_1, 'provider')}")

    # Delete the new thread
    await delete_thread(chat_id_1, new_thread_name)
    print(f"Threads for {chat_id_1} after delete: {await list_threads(chat_id_1)}")

    # Simulate reload
    print("\nSimulating reload...")
    global _sessions
    _sessions = {}
    _load_sessions_from_file()
    print(f"Reloaded session for {chat_id_1}: {await get_session(chat_id_1)}")
    print(f"Reloaded default thread data for {chat_id_1}: {await get_thread_data(chat_id_1)}")


if __name__ == "__main__":
    import asyncio
    # Ensure data dir exists for test
    DATA_DIR = os.path.dirname(config.SESSION_FILE_PATH)
    if DATA_DIR and not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    # Clear session file for clean test run? Optional.
    # if os.path.exists(config.SESSION_FILE_PATH):
    #     os.remove(config.SESSION_FILE_PATH)
    asyncio.run(_test())
