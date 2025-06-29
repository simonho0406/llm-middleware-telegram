import asyncio
import logging
from storage import file_storage, database_storage
import aiosqlite # Import aiosqlite

# Configure basic logging for the script
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def main():
    """
    Main migration function to transfer data from sessions.json to bot_sessions.db.
    """
    logging.info("Starting migration from JSON to SQLite...")

    # Open a single database connection for the entire migration process
    async with aiosqlite.connect(database_storage.DB_PATH) as db:
        # 1. Initialize database schema
        await database_storage.init_database(db) # Pass the connection
        logging.info("Database schema initialized.")

        # 2. Load all data from the JSON file
        await file_storage.init_file_storage()
        sessions = file_storage._sessions
        if not sessions:
            logging.warning("No sessions found in file storage. No data to migrate.")
            return

        logging.info(f"Loaded data for {len(sessions)} chats from JSON file.")

        # 3. Iterate through each chat and migrate its data
        for chat_id_str, chat_data in sessions.items():
            try:
                chat_id = int(chat_id_str)
                logging.info(f"--- Migrating chat {chat_id} ---")

                # Ensure chat and its default thread exist in the database
                await database_storage._get_or_create_chat(db, chat_id) # Pass the connection

                threads_to_migrate = chat_data.get("threads", {})
                if not threads_to_migrate:
                    logging.warning(f"Chat {chat_id} has no threads to migrate.")
                    continue

                for thread_id, thread_data in threads_to_migrate.items():
                    logging.info(f"  Migrating thread: {thread_id}...")

                    # Create the thread in the new database
                    await database_storage.create_thread(db, chat_id, thread_id) # Pass the connection

                    # Set the new thread as current to update its data
                    await database_storage.set_current_thread_id(db, chat_id, thread_id) # Pass the connection

                    # Migrate all keys (name, provider, model, etc.)
                    if "name" in thread_data:
                        await database_storage.set_thread_key(db, chat_id, "name", thread_data["name"]) # Pass the connection
                    if "provider" in thread_data:
                        await database_storage.set_thread_key(db, chat_id, "provider", thread_data["provider"]) # Pass the connection
                    if "model" in thread_data:
                        await database_storage.set_thread_key(db, chat_id, "model", thread_data["model"]) # Pass the connection
                    if "last_user_prompt" in thread_data:
                        await database_storage.set_thread_key(db, chat_id, "last_user_prompt", thread_data["last_user_prompt"]) # Pass the connection

                    # Migrate the conversation history
                    history = thread_data.get("history", [])
                    if history:
                        await database_storage.set_thread_history(db, chat_id, history) # Pass the connection

                    logging.info(f"  Successfully migrated thread '{thread_id}' with {len(history)} messages.")

                # Restore the original current_thread_id from the JSON file
                original_current_thread = chat_data.get("current_thread_id", "default")
                await database_storage.set_current_thread_id(db, chat_id, original_current_thread) # Pass the connection
                logging.info(f"  Restored original current thread to '{original_current_thread}'.")

            except Exception as e:
                logging.error(f"Failed to migrate chat {chat_id_str}: {e}", exc_info=True)

    logging.info("--- Migration completed successfully! ---")

if __name__ == "__main__":
    asyncio.run(main())