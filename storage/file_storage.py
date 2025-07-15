import json
import os
import logging
import traceback
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional, List
import config

logger = logging.getLogger(__name__)

async def init_file_storage():
    """Initializes the file-based storage by loading sessions from the JSON file."""
    logger.info("Initializing file storage...")
    # Call the synchronous _load_sessions_from_file in a separate thread
    await asyncio.to_thread(_load_sessions_from_file)
    logger.info("File storage initialized.")

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
    file_path = config.SESSION_FILE_PATH
    
    if not os.path.exists(file_path):
        logger.info(f"Session file '{file_path}' not found. Starting with empty sessions.")
        _sessions = {}
        return

    logger.debug(f"Attempting to load sessions from {file_path}")
    file_size = os.path.getsize(file_path)
    modified_time = datetime.fromtimestamp(os.path.getmtime(file_path)).isoformat()
    logger.debug(f"Session file metadata - Size: {file_size} bytes, Modified: {modified_time}")

    try:
        with open(file_path, 'r') as f:
            content = f.read()
            if not content.strip():
                logger.warning("Session file is empty")
                _sessions = {}
                return

            loaded_data = json.loads(content)
            _sessions = {int(k): v for k, v in loaded_data.items()}
            
            # Log structural integrity checks
            logger.info(f"Loaded {len(_sessions)} chats from session file")
            for chat_id in list(_sessions.keys())[:3]:  # Sample first 3 chats
                chat_data = _sessions[chat_id]
                logger.debug(f"Chat {chat_id} structure: current_thread_id={chat_data.get('current_thread_id')}, "
                            f"threads={len(chat_data.get('threads', {}))} threads")
                
                # Verify current_thread_id exists in threads
                current_id = chat_data.get('current_thread_id')
                if current_id and current_id not in chat_data.get('threads', {}):
                    logger.error(f"Invalid current_thread_id '{current_id}' in chat {chat_id} - not found in threads")

    except json.JSONDecodeError as e:
        logger.error(f"JSON decode failed: {e}\n{traceback.format_exc()}")
        _sessions = {}
    except Exception as e:
        logger.error(f"Critical load failure: {e}\n{traceback.format_exc()}")
        _sessions = {}
import aiofiles

async def _save_sessions_to_file() -> None:
    """Saves the current in-memory sessions to the JSON file atomically."""
    temp_path = f"{config.SESSION_FILE_PATH}.tmp"
    try:
        # Copy sessions under lock to avoid race conditions
        async with _lock:
            sessions_to_save = {str(k): v for k, v in _sessions.items()}
            total_chats = len(sessions_to_save)
            total_threads = sum(len(chat.get('threads', {})) for chat in sessions_to_save.values())

        # Write to temporary file first
        async with aiofiles.open(temp_path, 'w') as f:
            content = json.dumps(sessions_to_save, indent=4)
            await f.write(content)
            await f.flush()
            
        # Atomically replace the old file
        await asyncio.to_thread(os.replace, temp_path, config.SESSION_FILE_PATH)
        logger.info(
            f"Saved {total_chats} chats with {total_threads} total threads to '{config.SESSION_FILE_PATH}'"
            f" ({len(content)} bytes)"
        )
    except Exception as e:
        logger.error(f"Session save failed: {e}\n{traceback.format_exc()}")
        # Clean up temporary file if it exists
        if await asyncio.to_thread(os.path.exists, temp_path):
            await asyncio.to_thread(os.remove, temp_path)

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

async def list_threads(chat_id: int) -> List[Dict[str, Any]]:
    """Lists all threads for a chat, returning a list of dicts with id and name."""
    logger.debug(f"Listing threads for chat {chat_id}")
    session = await get_session(chat_id)
    threads = session.get("threads", {})
    thread_list = []
    for thread_id, thread_data in threads.items():
        thread_list.append({"id": thread_id, "name": thread_data.get("name")})
    logger.debug(f"Found {len(thread_list)} threads for chat {chat_id}")
    return thread_list

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

async def get_thread_history(chat_id: int, thread_id: Optional[str] = None) -> List[Dict[str, str]]:
    """Gets the message history for a thread."""
    thread_data = await get_thread_data(chat_id, thread_id)
    return thread_data.get('history', [])

async def set_thread_history(chat_id: int, history: List[Dict[str, str]], thread_id: Optional[str] = None) -> None:
    """Sets the message history for a thread."""
    await update_thread_data(chat_id, {'history': history}, thread_id)

async def save_message(chat_id: int, role: str, content: str, thread_id: Optional[str] = None) -> None:
    """Saves a single message to the history of a specific or current thread for the file backend."""
    async with _lock:
        session = _sessions.get(chat_id)
        if not session:
            logger.warning(f"No session found for chat_id {chat_id} in save_message.")
            return

        target_thread_id = thread_id if thread_id is not None else session.get("current_thread_id", _DEFAULT_THREAD_ID)
        
        if target_thread_id not in session.get("threads", {}):
            logger.error(f"Attempted to save message to non-existent thread '{target_thread_id}' for chat {chat_id}")
            return

        if "history" not in session["threads"][target_thread_id]:
            session["threads"][target_thread_id]["history"] = []

        session["threads"][target_thread_id]["history"].append({"role": role, "content": content})

    await _save_sessions_to_file()
# Load sessions when the module is imported

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
