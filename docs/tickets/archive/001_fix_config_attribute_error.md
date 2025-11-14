
# TICKET-001: Fix Critical `AttributeError` in Panel Handler

**Status:** CLOSED

**Problem:** The application crashes when the `/discuss_panel` command is run because `bot/handlers/discuss_panel_handler.py` tries to access `config.EXPERT_PANEL_CONFIG` directly. This violates our core architectural rule of using accessor functions.

**Evidence:**
```
AttributeError: module 'config' has no attribute 'EXPERT_PANEL_CONFIG'
```

**Definition of Done:**
1. In `bot/handlers/discuss_panel_handler.py`, find the line `if panel_config != config.EXPERT_PANEL_CONFIG:`.
2. Replace the direct access `config.EXPERT_PANEL_CONFIG` with a call to the correct accessor function: `config.get_expert_panel_config()`.
