# TICKET-065: Improve Cleanup of Stale Chat Data

**Issue:**
On startup, the application attempts to reset command scopes for all chat IDs stored in the database. If the bot has been blocked or removed from a chat, this results in a "Chat not found" error in the logs.

```
ERROR - Failed to set bot commands for scope BotCommandScopeChat(chat_id=12345): Chat not found
```

While these errors are not critical and do not crash the bot, they add noise to the logs and indicate that stale chat data is not being properly cleaned up.

**Proposed Solution:**
1.  Modify the `cleanup_chat_data` function in `main.py` (or a similar startup function) to gracefully handle the `telegram.error.BadRequest: Chat not found` exception.
2.  When this specific exception is caught for a given `chat_id`, the application should log a message indicating that it is removing the stale chat data.
3.  The function should then proceed to delete all records associated with that `chat_id` from the database. This includes session data, user settings, and any other related information.
4.  This will ensure that the startup process is cleaner and that the database does not retain data for chats where the bot is no longer a member.
