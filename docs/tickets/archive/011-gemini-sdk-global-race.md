# Ticket 011: Gemini SDK Global Race Condition (CRITICAL-01)

**Priority:** P0
**Source:** [comprehensive_code_review.md](../comprehensive_code_review.md) — CRITICAL-01
**Pillar Violated:** A (Stateless, Class-Based Services)

## Problem

`genai.configure(api_key=key)` in `services/gemini_service.py` sets the API key **globally** on the `google.generativeai` module singleton. With `concurrent_updates=True` enabled in the Telegram application, two concurrent requests will pollute each other's keys mid-stream.

```python
# Line 38 — sets global state
genai.configure(api_key=key)
```

The same issue exists on lines 84 (`list_models`) and 113 (`generate_concurrent_responses`).

## Root Cause

The `google.generativeai` v1 SDK uses a single global configuration. There is no per-request or per-client key isolation.

## Impact

- Silent key pollution across concurrent requests
- Incorrect billing attribution
- Potential `PERMISSION_DENIED` errors when a stream switches keys mid-flight

## Proposed Fix

**Option A (Recommended — Full migration):** Migrate to `google-genai` v2 SDK (`google.genai.Client(api_key=...)`) which provides per-instance configuration. This also resolves the `FutureWarning` already visible in startup logs.

**Option B (Temporary guard):** Wrap key rotation + streaming in an `asyncio.Lock` to serialize all Gemini calls. This is safe but kills concurrency.

## Changes Required

### `services/gemini_service.py`
- Replace `import google.generativeai as genai` with `from google import genai`
- Refactor into a class `GeminiService` with `__init__(self, api_keys: list)`
- Each call creates a `genai.Client(api_key=key)` instance — no global mutation
- Update `generate_response`, `list_models`, `generate_concurrent_responses` as instance methods

### `bot/providers.py`
- Instantiate `GeminiService(keys=config.GEMINI_API_KEYS)` instead of storing the module reference

### `requirements.txt` / `Dockerfile`
- Replace `google-generativeai` with `google-genai`

## Verification

- Existing `pytest` suite must pass
- Manual test: send two concurrent requests using different Gemini keys and confirm no cross-contamination in logs
