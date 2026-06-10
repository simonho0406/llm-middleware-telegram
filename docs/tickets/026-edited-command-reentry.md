# 026 — Edited command messages re-enter active conversation handler

## Severity: Medium

## Problem

`bot/handlers/chat.py:handle_edited_message` synthesizes a new Update
with `copy.copy(update); new_update.message = update.edited_message`
and calls `context.application.process_update(new_update)`. PTB then
re-runs the *entire* handler dispatch (auth middleware, TypeHandler,
conversation handlers) for the synthetic update.

## Failure mode

User edits a `/discuss_panel` command while a panel discussion is
already in `AWAITING_FOLLOW_UP` state. `discuss_panel_conv_handler`
re-enters with `allow_reentry=True`, spawning a second
`_run_panel_task_background`. Both tasks share `user_data['panel_task']`
— only the latest wins. The orphaned task keeps `touch_mcp_last_used`
firing, defeating the supervisor's idle timer and keeping MCP
subprocesses alive forever. Worst case: the orphan also tries to
write to the same DB thread, producing message ordering corruption.

## Fix direction

Drop the synthetic-update path. If a user edits a command, send a
new user-facing message: "Please send the command as a new message
rather than editing." Alternatively use
`application.update_queue.put_nowait` to queue the update normally
through PTB's machinery rather than re-entering dispatch.
