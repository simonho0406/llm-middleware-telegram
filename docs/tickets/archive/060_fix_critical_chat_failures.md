# TICKET-060: Fix Critical Failures in Normal Chat

**Status:** Open
**Priority:** Blocker

## User Story
As a user, I want the normal chat function to work reliably without crashing, so that I can have a basic conversation with the bot.

## Root Cause Analysis
Two critical bugs were identified in the logs, causing a total failure of the normal chat handler.

1.  **`KeyError: 'autosearch_normal_chat'`:** The primary bug is in `bot/response_generator.py`. The code attempts to access a user setting with the key `autosearch_normal_chat`, but the correct key in `bot/settings.py` is `autosearch_chat`. This typo causes a `KeyError` that crashes the response generation process.

2.  **`NameError: name 'telegram' is not defined`:** The secondary bug is in the error handling block of `bot/handlers/chat.py`. When the `KeyError` occurs, the `except` block tries to catch `telegram.error.NetworkError`, but the `telegram` module was never imported. This causes a `NameError`, preventing the bot from even reporting the original error gracefully.

## Acceptance Criteria

1.  **Fix the `KeyError` in `response_generator.py`:**
    *   In `bot/response_generator.py`, find the line that references `USER_SETTINGS['autosearch_normal_chat']`.
    *   Change it to use the correct key: `USER_SETTINGS['autosearch_chat']`.

2.  **Fix the `NameError` in `chat.py`:**
    *   At the top of the file `bot/handlers/chat.py`, add the following import statement:
        ```python
        from telegram import error
        ```
    *   In the `handle_message` function, modify the `except` block to catch `error.NetworkError` instead of `telegram.error.NetworkError`.

3.  **Verification:**
    *   After applying both fixes, restart the bot.
    *   Send a normal chat message (not a command).
    *   The bot must now respond correctly without any errors in the logs.
