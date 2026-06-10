# 028 — MCP supervisor's request_event has a tiny edge-loss window

## Severity: Medium (narrow but real)

## Problem

`utils/service_registry.py:_mcp_supervisor` Phase 1 wakes when
`request_event.is_set()`, immediately calls `request_event.clear()`,
and then enters Phase 2 (connect_all). If `connect_all` fails
(line 86-92), the supervisor:
1. Sets `ready_event` to release waiters
2. Sleeps 5s
3. Clears `ready_event`
4. Loops back to wait on `request_event`

If a third caller called `request_event.set()` *during* step 1
(after the clear but before connect failed), and another caller is
between `ready_event.set()` and `ready_event.clear()`, the lost
edge means the third caller blocks until the 30-min idle timer or
external shutdown.

## Failure mode

Cold start with two near-simultaneous `/discuss_panel` invocations
while the first MCP connect fails (e.g. Notion API key revoked).
Second user's panel hangs for 30 minutes.

## Fix direction

Use a counting signal instead of an Event: a
`asyncio.Queue`-like "wakeup count" the supervisor reads in a loop.
Alternatively, don't clear `request_event` until *after*
`ready_event.set()` succeeds — so any caller arriving in the
window observes a still-set event.

Simplest patch: in the supervisor's failure path, do *not* clear
`ready_event` — keep it briefly set as a "connection attempt
finished" signal; instead reset by checking
`bot_data['mcp_service'] is None` at the call site.
