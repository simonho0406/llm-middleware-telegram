# Ticket: Migrate Gemini Service to `google-genai` SDK

## Context
The `google.generativeai` package is now deprecated and Enf-of-Life (EOL). Google has released a new SDK `google-genai` (v1.0+) which is incompatible with the old API.
Startup logs currently show a `FutureWarning` advising immediate migration.

## Objectives
1.  **Replace Dependency**: Remove `google-generativeai` from `requirements.txt` and add `google-genai`.
2.  **Refactor Service**: Rewrite `services/gemini_service.py` to use the new `Client` pattern.
    -   Old: `genai.configure(api_key=...)` -> `genai.GenerativeModel(...)`
    -   New: `client = genai.Client(api_key=...)` -> `client.models.generate_content(...)`
3.  **Verify Feature Parity**:
    -   Streaming (`generate_content_stream`).
    -   Async support.
    -   Key switching/rotation logic (ensure `Client` can be instantiated per request or keys swapped).

## Risk vs Value
-   **Risk**: High (Rewrite of core service).
-   **Value**: High (Future proofing, access to new models like Gemini 2.0).
-   **Timing**: Post-Launch (Technical Debt).

## Implementation Details
-   Consult [Google GenAI Python SDK Migration Guide](https://github.com/googleapis/python-genai).
