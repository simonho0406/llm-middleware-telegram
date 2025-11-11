
# TICKET-021: Report External API Failures to User

**Status:** CLOSED

**Epic:** Robustness

**Problem:** When an external API call (like Tavily for web search) fails, the error is logged but not reported to the user, leaving them confused.

**Definition of Done:**
1.  In `services/web_search_service.py`, modify `perform_search` to return a structured dictionary (e.g., `{'status': 'error', 'message': '...'}`) instead of a simple error string.
2.  In `bot/handlers/misc_commands.py`, update the `search_command` to check the status of the result from `perform_search`.
3.  If the status is 'error', edit the placeholder message to show the user the error message (e.g., `await placeholder_message.edit_text(search_results['message'])`) and halt execution.
