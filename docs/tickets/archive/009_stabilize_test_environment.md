
# TICKET-009: Stabilize Test Environment by Installing `mistletoe-ebp`

**Status:** CLOSED (Superseded)

**Phase:** A

**Problem:** The entire test suite for the rendering engine is failing because the `mistletoe-ebp` library is not installed in the test environment, causing the system to fall back to a simple, incorrect escaping function.

**Evidence:**
```
WARNING  utils.text_processing:text_processing.py:216 mistletoe-ebp not found, falling back to basic markdown escaping.
```

**Definition of Done:**
1. Add `mistletoe-ebp` as a new line in the `requirements.txt` file.
2. Run `docker compose build` to rebuild the container with the new dependency.
3. Run `docker compose exec llm-middleware-telegram pytest` and confirm that all 25 tests now pass.
