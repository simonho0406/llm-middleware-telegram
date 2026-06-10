# 030 — `asyncio.Lock()` stored in `user_data['panel_state']` is brittle

## Severity: Low (no clear bug today)

## Problem

`bot/handlers/discuss_panel_handler.py` constructs `asyncio.Lock()`
instances and stores them inside `user_data['panel_state']`. asyncio
primitives are bound to the event loop they were created in. If the
polling loop restarts (main.py creates a new event loop), any surviving
panel state would carry a Lock bound to the dead loop.

## Why no clear bug today

PTB's default `user_data` is in-memory only, so a process restart
drops the state. `post_init_with_commands` clears `chat_data` for
all known chats (main.py line 91) but does NOT clear `user_data`. If
PTB persistence were ever enabled, this would surface immediately.

## Failure mode (future-facing)

Enable PicklePersistence or equivalent → after restart, calls into
`handle_follow_up` try to acquire a Lock object whose event loop has
been closed → `RuntimeError: <Lock ...> is bound to a different
event loop` or hang.

## Fix direction

Either:
- Never store asyncio primitives in PTB persisted dicts; lazily
  create a Lock keyed by chat_id in a module-level dict that's reset
  on each polling-loop restart, OR
- When persistence is enabled, also clear panel_state in
  `post_init_with_commands` for all known users.
