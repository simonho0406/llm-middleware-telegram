# Ticket 022: Database Migration for Tool Calling

**Priority:** P1
**Component:** Storage / SQLite
**Status:** ✅ Implemented & Verified
**Prerequisites:** None

---

## 1. Description
Extend the SQLite `messages` table schema to store tool calls and tool execution outcomes. The updates must be backwards-compatible and use an dynamic migration step on bot initialization rather than manual database recreations or drops.

## 2. Architectural Pillars (Immutable)
*   **Pillar C (Robust State Management)**: Thread message history loaded from database must perfectly serialize tool calls and tool responses back into the format expected by the LLM providers (e.g. OpenAI Compatible / Gemini SDKs).

## 3. Proposed Changes

### 3.1 SQLite Schema Alteration
Modify [storage/database_storage.py](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/storage/database_storage.py):
*   Add columns `tool_calls` (TEXT, stores JSON array) and `tool_call_id` (TEXT) to the `messages` table if they do not exist.
*   Update database initialization in `initialize_database()`:
    ```python
    # Check current schema of messages table
    async with db.cursor() as cursor:
        await cursor.execute("PRAGMA table_info(messages)")
        columns = [col[1] for col in await cursor.fetchall()]
        
        if 'tool_calls' not in columns:
            logger.info("Migrating messages table: adding 'tool_calls' column.")
            await db.execute("ALTER TABLE messages ADD COLUMN tool_calls TEXT")
            
        if 'tool_call_id' not in columns:
            logger.info("Migrating messages table: adding 'tool_call_id' column.")
            await db.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")
    ```

### 3.2 Update Message Save & Load Functions
*   Update `save_message(chat_id, role, content, tool_calls=None, tool_call_id=None)`:
    -   Write `tool_calls` (serialized as JSON string if passed) and `tool_call_id` to the database.
*   Update `get_thread_history(chat_id, limit=100)`:
    -   Read `tool_calls` and `tool_call_id` from database.
    -   Return dictionaries containing `{ "role": ..., "content": ..., "tool_calls": ..., "tool_call_id": ... }`.
    -   If `tool_calls` is not null, deserialize it from JSON back into standard Python structures.

## 4. Verification & Testing
*   **Test Case 1 (Migration)**: Create a temporary database with the old schema (without the tool columns). Run database initialization. Assert that the migration runs without errors and the columns `tool_calls` and `tool_call_id` are successfully appended to the `messages` table.
*   **Test Case 2 (Roundtrip)**: Save a message containing tool calls (JSON list structure) and another containing a tool response (`tool_call_id` matching). Load the history. Assert the loaded dictionaries exactly match the saved structure and that JSON deserialization executes cleanly.
*   **Backward Compatibility Test**: Save standard messages using the pre-migration signature. Ensure no errors occur and they read back with `tool_calls` and `tool_call_id` set to `None`.
