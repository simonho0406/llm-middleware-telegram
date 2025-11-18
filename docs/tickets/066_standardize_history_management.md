# TICKET-066: Standardize Conversation History Management

**Issue:**
The application uses two different and inconsistent methods for saving conversation history to the `messages` table.

1.  **Get/Set Pattern:** Most handlers (`chat.py`, etc.) use `storage_manager.get_thread_history()` to fetch the full message list, append new messages to it in memory, and then use `storage_manager.set_thread_history()` to completely overwrite the old history.
2.  **Append Pattern:** The `/discuss_panel` handler uses `storage_manager.save_message()` to append a single message directly to the database.

This dual approach is a design flaw that can lead to race conditions, data loss, and makes the codebase harder to reason about and maintain.

**Proposed Solution:**
1.  **Choose a Single Strategy:** The Get/Set pattern is generally safer as it's less prone to race conditions when handled within a single request, although it can be less performant for very long conversations. The Append pattern is more direct but requires careful management of state. The Get/Set pattern is the more dominant one and should be the standard.
2.  **Refactor `discuss_panel_handler.py`:** Modify the `/discuss_panel` handler to stop using `save_message`. It should be updated to use the standard `get_thread_history` and `set_thread_history` pattern, consistent with the rest of the application.
3.  **Deprecate `save_message` (Optional):** Once the refactoring is complete, consider deprecating the `storage_manager.save_message` function if it is no longer used anywhere else, to enforce the single, consistent pattern.
