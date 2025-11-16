
# TICKET-054: Enhance Orchestrator for Multi-Step Research Planning

**Status:** Open
**Priority:** High

## Goal

To enable the Orchestrator agent to perform the "Analyze & Plan Deep Dive" step of the new Conversational Research workflow. This involves creating a new prompt and ensuring the bot can correctly parse the expected list of follow-up queries from the LLM's response.

## Key Insights & Context

- This is the core "brain" of the new feature. The Orchestrator needs to act like a skilled researcher who, after reading a summary, can identify the most important areas for a deep dive.
- The new prompt must be explicit, instructing the LLM to return a JSON list of strings (the Google queries) and to return an empty list if no further research is needed.

## Acceptance Criteria (TDD Plan)

1.  A new prompt file, `prompts/panel_orchestrator_analyze.md`, will be created.
2.  A new unit test will be added. This test will mock a call to the Orchestrator, providing it with a sample user prompt and mock Tavily results.
3.  The test will assert that the function correctly parses the LLM's JSON response (a list of strings) from within potentially conversational text.
