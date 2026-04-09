# Ticket 006: Context Compression Transparency & Defensiveness Prompts

## Problem
Following Ticket 004, the system aggressively truncates context histories using `ensure_context_fits()`. However, the LLM is unaware of this, which can cause hallucinated references to missing history. Additionally, the Expert Panel sub-agents (like the Critic) can hallucinate that they verified data.

## Architecture Guidelines (Immutable)
- We do not change Python logic for this ticket, only markdown prompt text.
- Must preserve the existing JSON JSON-mode instructions where applicable.

## Required Changes
1. **`prompts/chat_system_prompt.md` & `prompts/panel_synthesis.md`**
   - Add a `Context Warning` section stating: 
     > The system may automatically compress or hide prior messages as conversational context grows. Do not hallucinate missing information if the user references something omitted.

2. **`prompts/panel_critic.md` & `prompts/panel_orchestrator_quality.md`**
   - Add a `Defensive Reporting` constraint:
     > Report outcomes faithfully. If verification fails or was not run due to lack of search results, say so explicitly. Do not invent a passing grade.

## Verification
- Ensure the prompt modifications do not break the strict output structural requirements of the Orchestrator/Critic.
