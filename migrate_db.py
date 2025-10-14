import sqlite3
import os

def clean_database():
    """
    Connects to the bot_sessions.db and removes any consecutive duplicate messages
    (based on role and content) within each conversation thread.
    """
    db_path = os.path.join('data', 'bot_sessions.db')
    
    if not os.path.exists(db_path):
        print(f"Error: Database file not found at '{db_path}'")
        return

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # SQL query to find the primary keys of consecutive duplicate messages
        find_duplicates_query = """
        WITH RankedMessages AS (
            SELECT
                message_pk,
                role,
                content,
                LAG(role, 1) OVER (PARTITION BY thread_fk ORDER BY timestamp) AS prev_role,
                LAG(content, 1) OVER (PARTITION BY thread_fk ORDER BY timestamp) AS prev_content
            FROM messages
        )
        SELECT message_pk FROM RankedMessages
        WHERE role = prev_role AND content = prev_content;
        """

        print("Identifying duplicate messages...")
        cursor.execute(find_duplicates_query)
        duplicate_pks = cursor.fetchall()

        if not duplicate_pks:
            print("No duplicate messages found. Database is clean.")
            return

        # Flatten the list of tuples
        pks_to_delete = [pk[0] for pk in duplicate_pks]
        num_duplicates = len(pks_to_delete)

        print(f"Found {num_duplicates} duplicate messages. Proceeding with deletion...")

        # Create a placeholder string for the IN clause
        placeholders = ', '.join('?' for _ in pks_to_delete)
        delete_query = f"DELETE FROM messages WHERE message_pk IN ({placeholders})"

        cursor.execute(delete_query, pks_to_delete)
        conn.commit()

        print(f"Database cleanup complete. Removed {num_duplicates} duplicate messages.")

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    clean_database()
