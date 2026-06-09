# Ticket 005: Dynamic Environment Context Injection

**Status:** ✅ Completed & Verified

## Problem
The system instructs the LLM to search for "today's news" or perform actions relative to the current time, but the LLM lacks explicit temporal or environmental grounding. It inherently does not know what day it is.

## Architecture Guidelines (Immutable)
- This is a prompt augmentation feature.
- We must not hardcode dates; the environment context must be generated dynamically at inference time.

## Required Changes
1. **`bot/prompt_loader.py` (or equivalent where prompts are loaded)**
   - Create a helper `get_environment_context()` that returns a string formatted as:
     ```markdown
     # Current Environment
     Date: [Current Date, e.g., 2026-04-03]
     Timezone: UTC
     ```
   - Dynamically append this block to the end of the `chat_system_prompt.md` text *after* loading but *before* passing it to the LLM.

2. **`bot/handlers/discuss_panel_handler.py`**
   - Inject the same environment block into the Orchestrator, Critic, and Refiner system prompts so the entire Expert Panel shares temporal awareness.

## Verification
- Run a local unit test on prompt loading to ensure the date renders correctly.
- Send a test message "What is the date today?" to see if the model successfully answers without needing to run `<search>`.
