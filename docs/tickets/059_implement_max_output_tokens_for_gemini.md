# TICKET-059: Implement `max_output_tokens` for Gemini Provider

**Status:** Open
**Priority:** Critical
**User Story:** As a developer, I want to be able to configure the maximum number of output tokens for the Gemini provider, so that I can prevent `MAX_TOKENS` errors that cause silent failures in the panel discussion workflow.

## Root Cause

The `/reroll` command is failing silently because the Proposer agent, using the Gemini model, is generating a response that exceeds the default output token limit of the API. This causes the API to return an empty response, which our system correctly interprets as a failure, triggering a fallback. This masks the underlying issue from the user.

The codebase currently has no mechanism to set `max_output_tokens` for Gemini API calls.

## Acceptance Criteria

1.  **Update `config.yaml`:**
    *   Add a new key under the `gemini` section: `max_output_tokens: 8192`.

2.  **Update `config.py`:**
    *   Add a new function `get_gemini_max_output_tokens()` that reads the `gemini.max_output_tokens` value from the configuration.

3.  **Update `services/gemini_service.py`:**
    *   In the `generate_response` function, modify the call to `gemini_model.generate_content_async`.
    *   Create a `generation_config` dictionary.
    *   This dictionary must contain the key `max_output_tokens` with the value retrieved from the new `get_gemini_max_output_tokens()` function.
    *   Pass this `generation_config` object to the `generate_content_async` call.

4.  **Verification:**
    *   After implementation, manually trigger the same long-running `initial question -> follow-up -> /reroll` sequence that previously failed.
    *   Confirm that the Proposer agent now successfully generates a response without triggering the Gemini `MAX_TOKENS` error and without needing to use the fallback provider.
