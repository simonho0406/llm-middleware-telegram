# File: storage/database_storage.py
# This is the canonical, correct implementation with connection passing.

import aiosqlite
import os
import logging
from typing import List, Dict, Any, Optional
import time

logger = logging.getLogger(__name__)
DB_PATH = "data/bot_sessions.db"
_DEFAULT_THREAD_ID = "default"

async def init_database(conn: aiosqlite.Connection):
    """Initializes the database and creates tables according to the specified schema, using a provided connection."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    await conn.execute("PRAGMA foreign_keys = ON;")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            current_thread_id TEXT NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            thread_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            thread_id TEXT NOT NULL,
            name TEXT,
            provider TEXT,
            model TEXT,
            last_user_prompt TEXT,
            FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE,
            UNIQUE (chat_id, thread_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            message_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_fk INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            FOREIGN KEY (thread_fk) REFERENCES threads(thread_pk) ON DELETE CASCADE
        )
    """)
    await conn.commit()
    logger.info("Database initialized successfully.")

async def _get_or_create_chat(conn: aiosqlite.Connection, chat_id: int) -> None:
    """Ensures a chat and its default thread exist, using a provided connection."""
    async with conn.cursor() as cursor:
        await cursor.execute("SELECT chat_id FROM chats WHERE chat_id = ?", (chat_id,))
        if await cursor.fetchone() is None:
            await conn.execute("BEGIN")  # Explicitly begin transaction
            try:
                await cursor.execute("INSERT INTO chats (chat_id, current_thread_id) VALUES (?, ?)", (chat_id, _DEFAULT_THREAD_ID))
                await cursor.execute(
                    "INSERT INTO threads (chat_id, thread_id) VALUES (?, ?)",
                    (chat_id, _DEFAULT_THREAD_ID)
                )
                await conn.commit()  # Commit transaction on success
            except Exception as e:
                await conn.rollback()  # Rollback on error
                raise e  # Re-raise the exception
            logger.info(f"Created new chat record and default thread for chat_id: {chat_id}")

async def _get_current_thread_pk(conn: aiosqlite.Connection, chat_id: int) -> Optional[int]:
    """Gets the primary key (thread_pk) of the current thread, using a provided connection."""
    await _get_or_create_chat(conn, chat_id)
    async with conn.cursor() as cursor:
        await cursor.execute("""
            SELECT T.thread_pk FROM threads T
            JOIN chats C ON T.chat_id = C.chat_id
            WHERE T.chat_id = ? AND T.thread_id = C.current_thread_id
        """, (chat_id,))
        row = await cursor.fetchone()
        return row[0] if row else None

# --- Public Interface ---

async def get_current_thread_id(conn: aiosqlite.Connection, chat_id: int) -> str:
    """Gets the current thread ID for a chat, using a provided connection."""
    await _get_or_create_chat(conn, chat_id)
    async with conn.cursor() as cursor:
        await cursor.execute("SELECT current_thread_id FROM chats WHERE chat_id = ?", (chat_id,))
        row = await cursor.fetchone()
        return row[0] if row else _DEFAULT_THREAD_ID

async def set_current_thread_id(conn: aiosqlite.Connection, chat_id: int, thread_id: str) -> None:
    """Sets the current thread ID for a chat, using a provided connection."""
    await _get_or_create_chat(conn, chat_id)
    await conn.execute("UPDATE chats SET current_thread_id = ? WHERE chat_id = ?", (thread_id, chat_id))
    await conn.commit()

async def get_thread_key(conn: aiosqlite.Connection, chat_id: int, key: str, default: Any = None) -> Any:
    """Gets a thread metadata value by key, using a provided connection."""
    valid_keys = {"name", "provider", "model", "last_user_prompt"}
    if key not in valid_keys:
        raise ValueError(f"Invalid key '{key}' for get_thread_key")
    
    thread_pk = await _get_current_thread_pk(conn, chat_id)
    if not thread_pk: 
        return default
        
    async with conn.cursor() as cursor:
        await cursor.execute(f"SELECT {key} FROM threads WHERE thread_pk = ?", (thread_pk,))
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else default

async def set_thread_key(conn: aiosqlite.Connection, chat_id: int, key: str, value: Any) -> None:
    """Sets a thread metadata value by key, using a provided connection."""
    valid_keys = {"name", "provider", "model", "last_user_prompt"}
    if key not in valid_keys:
        raise ValueError(f"Invalid key '{key}' for set_thread_key")

    thread_pk = await _get_current_thread_pk(conn, chat_id)
    if not thread_pk: 
        return

    await conn.execute(f"UPDATE threads SET {key} = ? WHERE thread_pk = ?", (value, thread_pk))
    await conn.commit()

async def get_thread_history(conn: aiosqlite.Connection, chat_id: int) -> List[Dict[str, str]]:
    """Gets the message history for the current thread, using a provided connection."""
    thread_pk = await _get_current_thread_pk(conn, chat_id)
    if not thread_pk: 
        return []

    async with conn.cursor() as cursor:
        await cursor.execute(
            "SELECT role, content FROM messages WHERE thread_fk = ? ORDER BY timestamp ASC",
            (thread_pk,)
        )
        rows = await cursor.fetchall()
        return [{"role": row[0], "content": row[1]} for row in rows]

async def set_thread_history(conn: aiosqlite.Connection, chat_id: int, history: List[Dict[str, str]]) -> None:
    """Sets the message history for the current thread, using a provided connection."""
    thread_pk = await _get_current_thread_pk(conn, chat_id)
    if not thread_pk: 
        return

    await conn.execute("BEGIN")  # Explicitly begin transaction
    try:
        await conn.execute("DELETE FROM messages WHERE thread_fk = ?", (thread_pk,))
        if history:
            ts = int(time.time())
            messages_to_insert = [
                (thread_pk, msg['role'], msg['content'], ts + i)
                for i, msg in enumerate(history)
            ]
            await conn.executemany(
                "INSERT INTO messages (thread_fk, role, content, timestamp) VALUES (?, ?, ?, ?)",
                messages_to_insert
            )
        await conn.commit()  # Commit transaction on success
    except Exception as e:
        await conn.rollback()  # Rollback on error
        raise e  # Re-raise the exception

async def create_thread(conn: aiosqlite.Connection, chat_id: int, thread_id: str) -> bool:
    """Creates a new thread for a chat, using a provided connection."""
    try:
        await _get_or_create_chat(conn, chat_id)
        await conn.execute("INSERT INTO threads (chat_id, thread_id) VALUES (?, ?)", (chat_id, thread_id))
        await conn.commit()
        return True
    except aiosqlite.IntegrityError:
        logger.warning(f"Attempted to create existing thread '{thread_id}' for chat {chat_id}")
        return False

async def delete_thread(conn: aiosqlite.Connection, chat_id: int, thread_id: str) -> bool:
    """Deletes a thread for a chat, using a provided connection."""
    if thread_id == _DEFAULT_THREAD_ID:
        logger.warning(f"Attempt to delete default thread for chat {chat_id} denied.")
        return False
        
    current_id = await get_current_thread_id(conn, chat_id)
    if current_id == thread_id:
        await set_current_thread_id(conn, chat_id, _DEFAULT_THREAD_ID)

    cursor = await conn.execute("DELETE FROM threads WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id))
    await conn.commit()
    return cursor.rowcount > 0

async def list_threads(conn: aiosqlite.Connection, chat_id: int) -> List[Dict[str, Any]]:
    """Lists all threads for a chat, using a provided connection."""
    await _get_or_create_chat(conn, chat_id)
    async with conn.cursor() as cursor:
        await cursor.execute(
            "SELECT thread_id, name FROM threads WHERE chat_id = ?", (chat_id,)
        )
        rows = await cursor.fetchall()
        return [{"id": row[0], "name": row[1]} for row in rows]

async def rename_thread(conn: aiosqlite.Connection, chat_id: int, new_name: str) -> bool:
    """Renames the current thread, using a provided connection."""
    return await set_thread_key(conn, chat_id, "name", new_name)
