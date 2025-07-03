# File: storage/database_storage.py
# This is the canonical, correct implementation.

import aiosqlite
import os
import logging
from typing import List, Dict, Any, Optional
import time
import config

logger = logging.getLogger(__name__)
_DEFAULT_THREAD_ID = "default"

# --- Internal Helper Functions (still require a passed connection) ---

async def _get_or_create_chat(conn: aiosqlite.Connection, chat_id: int) -> None:
    """Ensures a chat and its default thread exist, using a provided connection."""
    async with conn.cursor() as cursor:
        await cursor.execute("SELECT chat_id FROM chats WHERE chat_id = ?", (chat_id,))
        if await cursor.fetchone() is None:
            # Use a transaction for multi-statement write
            async with conn.transaction():
                await cursor.execute("INSERT INTO chats (chat_id, current_thread_id) VALUES (?, ?)", (chat_id, _DEFAULT_THREAD_ID))
                await cursor.execute("INSERT INTO threads (chat_id, thread_id) VALUES (?, ?)", (chat_id, _DEFAULT_THREAD_ID))
            logger.info(f"Created new chat record and default thread for chat_id: {chat_id}")

async def _get_thread_pk(conn: aiosqlite.Connection, chat_id: int, thread_id: Optional[str] = None) -> Optional[int]:
    """Gets the primary key (thread_pk) of a specific thread, or the current thread if thread_id is None."""
    await _get_or_create_chat(conn, chat_id)
    async with conn.cursor() as cursor:
        if thread_id:
            await cursor.execute("SELECT thread_pk FROM threads WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id))
        else: # Get current thread's pk
            await cursor.execute("SELECT T.thread_pk FROM threads T JOIN chats C ON T.chat_id = C.chat_id WHERE T.chat_id = ? AND T.thread_id = C.current_thread_id", (chat_id,))
        row = await cursor.fetchone()
        return row[0] if row else None

# --- Public Interface ---

async def init_database():
    """Initializes the database and creates tables, managing its own connection."""
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, current_thread_id TEXT NOT NULL)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                thread_pk INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, thread_id TEXT NOT NULL,
                name TEXT, provider TEXT, model TEXT, last_user_prompt TEXT,
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE, UNIQUE (chat_id, thread_id)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                message_pk INTEGER PRIMARY KEY AUTOINCREMENT, thread_fk INTEGER NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, timestamp INTEGER NOT NULL,
                FOREIGN KEY (thread_fk) REFERENCES threads(thread_pk) ON DELETE CASCADE
            )""")
        await db.commit()
        logger.info("Database initialized successfully.")

async def get_current_thread_id(chat_id: int) -> str:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await _get_or_create_chat(db, chat_id)
        async with db.cursor() as cursor:
            await cursor.execute("SELECT current_thread_id FROM chats WHERE chat_id = ?", (chat_id,))
            row = await cursor.fetchone()
            return row[0] if row else _DEFAULT_THREAD_ID

async def set_current_thread_id(chat_id: int, thread_id: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await _get_or_create_chat(db, chat_id)
        await db.execute("UPDATE chats SET current_thread_id = ? WHERE chat_id = ?", (thread_id, chat_id))
        await db.commit()

async def get_thread_key(chat_id: int, key: str, default: Any = None, thread_id: Optional[str] = None) -> Any:
    valid_keys = {"name", "provider", "model", "last_user_prompt"}
    if key not in valid_keys:
        # This is the abstraction break. History is not a simple key.
        if key == 'history':
            return await get_thread_history(chat_id, thread_id)
        raise ValueError(f"Invalid key '{key}' for get_thread_key")
    
    async with aiosqlite.connect(config.DB_PATH) as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return default
        
        async with db.cursor() as cursor:
            await cursor.execute(f"SELECT {key} FROM threads WHERE thread_pk = ?", (thread_pk,))
            row = await cursor.fetchone()
            return row[0] if row and row[0] is not None else default

async def set_thread_key(chat_id: int, key: str, value: Any, thread_id: Optional[str] = None) -> None:
    valid_keys = {"name", "provider", "model", "last_user_prompt"}
    if key not in valid_keys:
        # This is the abstraction break. History is not a simple key.
        if key == 'history':
            return await set_thread_history(chat_id, value, thread_id)
        raise ValueError(f"Invalid key '{key}' for set_thread_key")

    async with aiosqlite.connect(config.DB_PATH) as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return

        await db.execute(f"UPDATE threads SET {key} = ? WHERE thread_pk = ?", (value, thread_pk))
        await db.commit()

async def get_thread_history(chat_id: int, thread_id: Optional[str] = None) -> List[Dict[str, str]]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return []

        async with db.cursor() as cursor:
            await cursor.execute(
                "SELECT role, content FROM messages WHERE thread_fk = ? ORDER BY timestamp ASC",
                (thread_pk,)
            )
            rows = await cursor.fetchall()
            return [{"role": row[0], "content": row[1]} for row in rows]

async def set_thread_history(chat_id: int, history: List[Dict[str, str]], thread_id: Optional[str] = None) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        thread_pk = await _get_thread_pk(db, chat_id, thread_id)
        if not thread_pk: return

        async with db.transaction():
            await db.execute("DELETE FROM messages WHERE thread_fk = ?", (thread_pk,))
            if history:
                ts = int(time.time())
                messages_to_insert = [
                    (thread_pk, msg['role'], msg['content'], ts + i)
                    for i, msg in enumerate(history)
                ]
                await db.executemany(
                    "INSERT INTO messages (thread_fk, role, content, timestamp) VALUES (?, ?, ?, ?)",
                    messages_to_insert
                )

async def create_thread(chat_id: int, thread_id: str) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        try:
            await _get_or_create_chat(db, chat_id)
            await db.execute("INSERT INTO threads (chat_id, thread_id) VALUES (?, ?)", (chat_id, thread_id))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            logger.warning(f"Attempted to create existing thread '{thread_id}' for chat {chat_id}")
            return False

async def delete_thread(chat_id: int, thread_id: str) -> bool:
    if thread_id == _DEFAULT_THREAD_ID:
        logger.warning(f"Attempt to delete default thread for chat {chat_id} denied.")
        return False
        
    async with aiosqlite.connect(config.DB_PATH) as db:
        current_id = await get_current_thread_id(chat_id)
        if current_id == thread_id:
            await set_current_thread_id(chat_id, _DEFAULT_THREAD_ID)

        cursor = await db.execute("DELETE FROM threads WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id))
        await db.commit()
        return cursor.rowcount > 0

async def list_threads(chat_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await _get_or_create_chat(db, chat_id)
        async with db.cursor() as cursor:
            await cursor.execute(
                "SELECT thread_id, name FROM threads WHERE chat_id = ?", (chat_id,)
            )
            rows = await cursor.fetchall()
            return [{"id": row[0], "name": row[1]} for row in rows]

async def rename_thread(chat_id: int, new_name: str) -> bool:
    # This now correctly uses the modified set_thread_key
    return await set_thread_key(chat_id, "name", new_name)