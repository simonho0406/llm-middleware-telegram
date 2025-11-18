import sqlite3
import json

DB_PATH = 'data/bot_sessions.db'

def inspect_panel_configs():
    """Connects to the database and prints any stored expert panel configurations."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # The key for panel configs is 'expert_panel_config'
        query = "SELECT chat_id, value FROM user_settings WHERE key = 'expert_panel_config'"
        
        cursor.execute(query)
        rows = cursor.fetchall()

        if not rows:
            print("No custom expert panel configurations found in the database.")
            return

        print("Found the following custom expert panel configurations:")
        for chat_id, value in rows:
            try:
                config_data = json.loads(value)
                print(f"  - Chat ID: {chat_id}")
                print(f"    Orchestrator: {config_data.get('orchestrator')}")
                print(f"    Proposer: {config_data.get('Proposer')}")
                print(f"    Critic: {config_data.get('Critic')}")
                print(f"    Refiner: {config_data.get('Refiner')}")
                print("-" * 20)
            except json.JSONDecodeError:
                print(f"  - Chat ID: {chat_id} has invalid JSON data: {value}")

    except sqlite3.Error as e:
        print(f"Database error: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

if __name__ == "__main__":
    inspect_panel_configs()
