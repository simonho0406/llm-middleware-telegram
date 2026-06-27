import sqlite3
import json
import os

DB_PATH = "data/bot_sessions.db"
CHAT_ID = int(os.getenv("QA_CHAT_ID", "0"))  # set in .env

def manage_thread_history():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get Active Thread
    cursor.execute("SELECT current_thread_id FROM chats WHERE chat_id = ?", (CHAT_ID,))
    result = cursor.fetchone()
    if not result: return
    thread_id = result[0]

    # Get Thread PK
    cursor.execute("SELECT thread_pk FROM threads WHERE thread_id = ? AND chat_id = ?", (thread_id, CHAT_ID))
    thread_pk = cursor.fetchone()[0]
    
    # Search for the problematic message
    target_content_start = "Here's an update of my current ingredients"
    # 3. Get Messages
    cursor.execute(
        "SELECT message_pk, role, content FROM messages WHERE thread_fk = ? ORDER BY message_pk ASC",
        (thread_pk,)
    )
    # Search Global
    print("\n--- GLOBAL SEARCH ---")
    cursor.execute("SELECT message_pk, thread_fk, content FROM messages WHERE content LIKE '%Here''s an update%' OR content LIKE '%Maraschino%'")
    global_matches = cursor.fetchall()
    
    for match in global_matches:
        print(f"FOUND MATCH: PK={match[0]}, ThreadFK={match[1]}, Content={match[2][:50]}...")
    
    if not global_matches:
        print("No matches found in ENTIRE database.")

    conn.close()
            
    # if target_pk:
    #     # Prune everything >= target_pk
    #     print(f"Pruning all messages from PK {target_pk} onwards...")
    #     cursor.execute("DELETE FROM messages WHERE thread_fk = ? AND message_pk >= ?", (thread_pk, target_pk))
    #     conn.commit()
    #     print(f"✅ Pruned {cursor.rowcount} messages to reset state.")
    # else:
    #     print("Target message not found in history.")

    conn.close()

if __name__ == "__main__":
    manage_thread_history()
