# Ticket 004: Search Context Overflow & Error Leakage Fix

## Problem

The `/search` command (both user-provoked and auto-search) was hitting 400 errors from LLM providers when conversation history was large.

**Root Cause:** `search_command` sent the **full un-truncated conversation history** (~106K tokens) alongside search results to the LLM, without calling `ensure_context_fits()`. Normal chat correctly used this function, but search bypassed it entirely.

**Compounding Bug:** When the overflow caused an API error, the `[Error: ...]` string was being saved to the database as an assistant message, permanently polluting the conversation history.

**Additional Bug:** `openai_compatible_service` treated ALL first-attempt 400 errors as "reasoning parameter rejection," masking genuine token overflow errors.

## Changes

### `bot/handlers/misc_commands.py`
- Added `ensure_context_fits()` call after building the augmented prompt, before sending to the LLM
- Kept existing `truncate_text_to_tokens` as a first-pass safety net for oversized web scrapes
- Added `[Error:` prefix guard on the assistant response save to prevent error string pollution

### `services/openai_compatible_service.py`
- Replaced blanket 400 error handling with error body inspection
- Token overflow (keywords: "token", "context length", "too long", "maximum") → immediate fail with clear user message
- Reasoning rejection → only when no token-related keywords found, attempt 0 only
- Cleaned up stale comments

## Verification

- Full `pytest` suite: **79/79 passed**
- Audited all other save paths (`response_generator.py`, `discuss_panel_handler.py`) — already properly guarded
