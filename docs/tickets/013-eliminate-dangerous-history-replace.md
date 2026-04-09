# Ticket 013: Eliminate replace_thread_history_dangerous (HIGH-02)

**Priority:** P1
**Source:** [comprehensive_code_review.md](../comprehensive_code_review.md) — HIGH-02
**Pillar Violated:** C (Robust State Management)

## Problem

`bot/handlers/chat.py` line 206 calls `storage_manager.set_thread_history()`, which is mapped to `replace_thread_history_dangerous` in `storage/__init__.py`. This function **deletes ALL messages** for the thread and re-inserts the modified array. If a concurrent task is saving a new message to the same thread, that message is silently lost.

The function is explicitly marked `DEPRECATED` in its own docstring, yet remains wired and actively called from the message edit flow.

## Root Cause

The edit handler (`handle_edited_message`) needs to:
1. Remove all messages after the last user message
2. Update the content of the last user message

It currently does this by loading the full history, modifying the list in Python, and bulk-replacing. This is inherently unsafe under concurrency.

## Proposed Fix

Replace the bulk-replace with targeted atomic DB operations:

1. `delete_messages_after(chat_id, message_pk)` — deletes all messages with `message_pk > target_pk` for the thread
2. `update_message_content(message_pk, new_content)` — updates a single message's content

## Changes Required

### `storage/database_storage.py`
- Add `delete_messages_after(chat_id, target_pk, thread_id=None)` using `DELETE FROM messages WHERE thread_fk = ? AND message_pk > ?`
- Add `update_message_content(message_pk, new_content)` using `UPDATE messages SET content = ? WHERE message_pk = ?`
- Mark `replace_thread_history_dangerous` as raising `DeprecationWarning`

### `storage/__init__.py`
- Wire the new methods into `StorageManager`
- Remove `set_thread_history` mapping (or point it to a stub that raises)

### `bot/handlers/chat.py`
- Refactor `handle_edited_message` to use `get_thread_history_with_pk` to find the target message PK, then call the two new atomic operations

## Verification

- Existing `pytest` suite must pass
- Test: edit a message while a concurrent response is being saved — no data loss
