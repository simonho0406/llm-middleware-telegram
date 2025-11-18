import sqlite3
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DB_PATH = 'data/bot_sessions.db'
STALE_CHAT_IDS = [12345, 509387009]
ORPHANED_SETTING_KEY = 'panel_config'

def cleanup_database():
    """
    Performs a cleanup of the bot's SQLite database.
    - Removes settings with a specific orphaned key.
    - Deletes stale chat records, relying on CASCADE to clean up related data.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        conn.execute("PRAGMA foreign_keys = ON;")

        # 1. Delete orphaned 'panel_config' settings
        logging.info(f"Searching for and removing orphaned setting key: '{ORPHANED_SETTING_KEY}'...")
        cursor.execute("DELETE FROM user_settings WHERE key = ?", (ORPHANED_SETTING_KEY,))
        if cursor.rowcount > 0:
            logging.info(f"Successfully deleted {cursor.rowcount} orphaned setting(s).")
        else:
            logging.info("No orphaned settings found.")

        # 2. Delete stale chat IDs. ON DELETE CASCADE will handle the rest.
        logging.info(f"Searching for and removing stale chat IDs: {STALE_CHAT_IDS}...")
        for chat_id in STALE_CHAT_IDS:
            cursor.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            if cursor.rowcount > 0:
                logging.info(f"  Deleted chat_id {chat_id}. The database will cascade deletes to threads, messages, and user_settings.")
            else:
                logging.info(f"  Chat_id {chat_id} not found.")

        conn.commit()
        logging.info("Database cleanup committed successfully.")

    except sqlite3.Error as e:
        logging.error(f"Database error during cleanup: {e}")
        if 'conn' in locals() and conn:
            conn.rollback()
    finally:
        if 'conn' in locals() and conn:
            conn.close()
            logging.info("Database connection closed.")

if __name__ == "__main__":
    logging.info("Starting database cleanup process...")
    cleanup_database()
    logging.info("Database cleanup process finished.")
