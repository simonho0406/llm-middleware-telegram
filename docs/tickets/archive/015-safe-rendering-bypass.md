# Ticket 015: Safe Rendering Bypass & Auth Middleware (ARCH-04, SEC-03, ERR-03)

**Status:** ✅ Completed & Verified

**Priority:** P1
**Source:** [comprehensive_code_review.md](../comprehensive_code_review.md) — ARCH-04, SEC-03, ERR-03
**Pillar Violated:** B (Centralized, Safe Rendering)

## Problem A: Direct `send_message` Calls Bypass Safe Rendering (ARCH-04)

11+ locations call `context.bot.send_message()` directly instead of routing through `send_safe_message`. Most use `parse_mode=None` (safe), but `discuss_panel_handler.py` L1336 sends **MarkdownV2 directly**, which can crash with `BadRequest` on unescaped special characters.

Key offenders:
- `discuss_panel_handler.py` L953, L968, L1336, L1378
- `flash_handler.py` L47
- `misc_commands.py` L120
- `ask_selected_handler.py` L326, L576
- `main.py` L148

### Proposed Fix
Create a `send_plain_message(context, chat_id, text)` helper that enforces `parse_mode=None` for system/status messages. Audit and replace all direct `send_message` calls.

## Problem B: Authorization Check Only on `handle_message` (SEC-03)

`allowed_chat_ids` is only checked in `chat.py:handle_message`. All command handlers (`/discuss_panel`, `/search`, `/config`, `/flash`) have **no authorization check**. An unauthorized user can invoke commands freely.

### Proposed Fix
Create a `@require_auth` decorator or PTB `TypeHandler` middleware that wraps all handlers and enforces `allowed_chat_ids` at the application level.

## Problem C: `send_safe_message` BadRequest Fallback Gap (ERR-03)

When a non-"message is not modified" `BadRequest` occurs in `bot/messaging.py`, the code logs a warning but falls through without triggering the plaintext fallback logic (which lives in a separate `except Exception` block).

### Proposed Fix
Restructure the exception handling so that `BadRequest` (excluding "not modified") falls through to the same plaintext fallback path as generic `Exception`.

## Verification

- `grep -rn "context.bot.send_message\|\.send_message(" bot/ main.py` should return zero results outside of `messaging.py`
- `pytest` suite must pass
