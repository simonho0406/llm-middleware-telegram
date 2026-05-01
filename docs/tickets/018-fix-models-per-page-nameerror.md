# Ticket 018: Fix `MODELS_PER_PAGE` NameError in `ask_selected_handler.py`

**Priority:** P0 — Runtime Crash  
**Type:** Bug Fix  
**Branch:** `feature/tech-debt-refactor`  
**Status:** Open  
**Created:** 2026-05-01  
**Discovered By:** Ticket 017 Post-Refactor Code Review  

---

## Context

During the comprehensive code review (Ticket 017), a latent `NameError` was discovered in `bot/handlers/ask_selected_handler.py`. The pagination calculation on **line 133** references `MODELS_PER_PAGE`, a constant that is **never defined** in the file.

The file defines `ITEMS_PER_PAGE = 8` on line 84, but the pagination logic on line 133 uses the wrong name:

```python
# Line 84 — correct definition
ITEMS_PER_PAGE = 8

# Line 133 — WRONG reference
total_pages = (total_models - 1) // MODELS_PER_PAGE + 1  # ← NameError!
```

`MODELS_PER_PAGE` exists in `configure_panel_handler.py` (line 29), suggesting this was a copy-paste oversight during the handler's creation.

## Impact

- **When**: Any time a user selects a provider in `/ask_selected` that has more than 8 models
- **Result**: `NameError: name 'MODELS_PER_PAGE' is not defined` — crashes the conversation handler
- **Affected Feature**: Model pagination in the `/ask_selected` council flow
- **NOT Affected**: Core chat, panel, search, config — all other features work correctly

## Fix

**Option A** (minimal): Replace `MODELS_PER_PAGE` with `ITEMS_PER_PAGE` on line 133:
```python
total_pages = (total_models - 1) // ITEMS_PER_PAGE + 1
```

**Option B** (consistent naming): Define `MODELS_PER_PAGE = 8` at module level and update line 84 to use it:
```python
MODELS_PER_PAGE = 8
# ... later on line 84:
# ITEMS_PER_PAGE = MODELS_PER_PAGE  (or just use MODELS_PER_PAGE everywhere)
```

## Verification

```bash
# Grep to confirm no other references to MODELS_PER_PAGE in ask_selected_handler
grep -n 'MODELS_PER_PAGE\|ITEMS_PER_PAGE' bot/handlers/ask_selected_handler.py

# Run tests
pytest tests/ -v
```

## Test Gap

No existing test covers `ask_selected_handler` pagination. Consider adding a unit test that mocks `get_models_for_provider` to return > 8 models and verifies the keyboard is correctly paginated.
