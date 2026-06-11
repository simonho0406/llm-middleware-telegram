# 025 — Cancel-mid-save can lose assistant message

## Severity: High (upgraded from Medium)

A validation review noted that this is a silent corruption bug — the user
sees the AI's reply but next /reroll shows the previous prompt because
history has no record of this turn. No telemetry surfaces the loss.

Also note the fix-direction must use `current_task().cancelling() > 0`
(Python 3.11+) or `asyncio.shield(save)`. The `current_task().cancelled()`
check that's currently in the code is dead — it always returns False from
inside the task's own coroutine.

## Problem

`bot/response_generator.py:_generate_and_send_response_task` checks
`asyncio.current_task().cancelled()` between `send_safe_message()` and
`storage_manager.save_message('assistant', ...)`. **`Task.cancelled()`
returns True only after the task has finished**; from inside its own
coroutine it always returns False. The intended cancellation gate is
inert.

## Failure mode

User calls `/cancel` between `send_safe_message` returning and the
history-save block:
1. The cancel sets the task's cancelling flag.
2. Our guard `if asyncio.current_task().cancelled()` is False.
3. We proceed to `await storage_manager.save_message(...)`.
4. The await raises `CancelledError` — assistant content was already
   sent to the user but never persisted.
5. `/reroll` shows the prior prompt because history has no record of
   this turn.

## Fix direction

Wrap the save in `try/except CancelledError` and explicitly proceed
even on cancellation (use `asyncio.shield` if needed). Or use
`asyncio.current_task().cancelling() > 0` on Python ≥3.11.
