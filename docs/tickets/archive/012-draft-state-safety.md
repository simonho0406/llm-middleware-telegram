# Ticket 012: Draft State Concurrency Safety (CRITICAL-03)

**Priority:** P0
**Source:** [comprehensive_code_review.md](../comprehensive_code_review.md) — CRITICAL-03
**Pillar Violated:** C (Robust State Management)

## Problem

`_active_drafts: dict[int, int] = {}` in `bot/response_generator.py` (line 23) is a module-level mutable dict accessed from multiple concurrent `asyncio.Task`s. The check-then-act pattern across `await` points is non-atomic:

```python
# Lines 166-170 — Race window between get() and assignment
if _active_drafts.get(chat_id):
    asyncio.create_task(finalize_draft(...))  # fire-and-forget!
_active_drafts[chat_id] = draft_id

# Lines 186-188 — Race window between compare and delete
if _active_drafts.get(chat_id) == draft_id:
    del _active_drafts[chat_id]
```

Additionally, `finalize_draft` is dispatched via fire-and-forget `asyncio.create_task` (lines 169, 187) with no tracking — orphan tasks can outlive the parent.

## Impact

- Ghost drafts that never finalize (user sees permanent "typing..." indicator)
- Stale draft eviction races where new draft is immediately finalized by the old cleanup task

## Proposed Fix

Move draft tracking into `context.chat_data` (PTB's per-chat concurrent-safe storage). This eliminates the global dict entirely.

## Changes Required

### `bot/response_generator.py`
- Remove `_active_drafts: dict[int, int] = {}`
- Replace `_active_drafts[chat_id]` reads/writes with `context.chat_data.get('active_draft_id')` / `context.chat_data['active_draft_id']`
- Track fire-and-forget tasks using the pattern:
  ```python
  bg_tasks = context.chat_data.setdefault('_bg_tasks', set())
  task = asyncio.create_task(finalize_draft(...))
  bg_tasks.add(task)
  task.add_done_callback(bg_tasks.discard)
  ```
- On cancellation cleanup, await all remaining tasks in `_bg_tasks`

## Verification

- Existing `pytest` suite must pass
- Manual test: rapid-fire messages to ensure no ghost typing indicators persist
