# Ticket 008: User-Defined Hooks & Agentic Scratchpad Architecture

**Status:** ✅ Completed & Verified

## Problem
1. **Hooks**: Tool validation and access controls are hardcoded in Python. There is no pipeline for a user to block specific search domains dynamically without writing core Python.
2. **Scratchpad**: The Expert Panel Orchestrator creates a rigid plan. The Proposer/Critic/Refiner don't share a persistent state to track what failed and what is completed.

## Architecture Guidelines (Immutable)
- **Hooks**: Use a pluggable hook architecture. Do not execute untrusted shell scripts without a strict config whitelist.
- **Scratchpad**: All Stateful operations must survive crash loops (stored in SQLite database, not just memory).

## Required Changes
1. **Hooks Pipeline (`utils/hooks.py`)**
   - Create a `HookRunner` class that looks in `config/hooks/`.
   - Before `search_command` executes, pause and pass the payload to `pre_tool_use` hooks. 
   - If a hook raises an exception, return a Permission Denied block to the LLM.

2. **Agentic Task Tracker (`storage/database_storage.py` & `bot/handlers/discuss_panel_handler.py`)**
   - Create a `panel_tasks` table: `id`, `session_id`, `role`, `status` (pending, in_progress, completed).
   - The Orchestrator writes its JSON array to this table.
   - Injections: Each agent reads `SELECT * FROM panel_tasks` into its system prompt so it knows the global state of the panel.

## Verification
- Write a dummy pre-hook that blocks search queries containing "blockedkeyword", verifying the system rejects it safely without crashing.
- Verify the Expert Panel accurately reflects completed stages in its sub-agent logs.
