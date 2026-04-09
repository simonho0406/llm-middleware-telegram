# Ticket 016: OpenRouter Double-Request Elimination (ERR-02)

**Priority:** P1
**Source:** [comprehensive_code_review.md](../comprehensive_code_review.md) — ERR-02

## Problem

`services/openrouter_service.py` makes **every request twice**:

1. **Lines 66-89:** A full non-streaming `POST` request to check if the model rejects reasoning parameters (status 400).
2. **Lines 117-148:** The actual streaming `POST` request with the same payload.

This means every OpenRouter call:
- Costs 2x the API credits
- Adds 2x the latency
- Consumes 2x the rate limit quota

Additionally, lines 91-92 contain dead code:
```python
if True:
    pass
```

## Root Cause

The initial non-streaming request was added as a "probe" to detect 400 errors from reasoning parameters. However, the streaming path (lines 117-148) already handles 400 status codes via the `ValueError("fallback")` pattern.

## Proposed Fix

Delete the non-streaming probe entirely (lines 66-92). The streaming-with-fallback pattern already works correctly:

```python
# ATTEMPT 1: Reasoning (streaming)
async with client.stream(..., json=reasoning_data) as response:
    if response.status_code == 400:
        raise ValueError("fallback")
    # process stream...

# FALLBACK: Standard (streaming)  
async with client.stream(..., json=data) as response:
    # process stream...
```

## Changes Required

### `services/openrouter_service.py`
- Delete lines 66-92 (non-streaming probe + dead `if True: pass` block)
- Delete lines 94-114 (stale comments about the refactored approach)
- The remaining streaming fallback logic (lines 116-148) already handles the 400 case correctly

## Verification

- `pytest` suite must pass
- Manual test: send a request to a model that rejects reasoning params — should gracefully fall back to standard payload in a single round-trip
