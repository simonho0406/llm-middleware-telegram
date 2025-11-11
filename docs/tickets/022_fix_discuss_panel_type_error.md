
# TICKET-022: Fix Critical TypeError in /discuss_panel Handler

**Status:** CLOSED

**Epic:** Bugfix

**Problem:** The `/discuss_panel` command is crashing with a `TypeError` because the handler is calling `PromptManager.get_prompt()` with unexpected keyword arguments. The `get_prompt` method is designed to fetch a raw template, and the formatting should be done separately.

**Definition of Done:**
1.  In `bot/handlers/discuss_panel_handler.py`, locate all calls to `config.PROMPTS.get_prompt()`.
2.  Modify these calls to follow the correct, two-step `get -> format` pattern.
3.  Specifically, change the code to first fetch the template string, and then call the `.format()` method on that string.

**Example Fix (for `_run_panel_workflow`):**

```python
# --- OLD, BROKEN CODE ---
# meta_prompt = config.PROMPTS.get_prompt(
#     'panel_orchestrator_plan',
#     user_prompt=user_prompt,
#     full_history=json.dumps(full_history, indent=2)
# )

# --- NEW, CORRECT CODE ---
plan_template = config.PROMPTS.get_prompt('panel_orchestrator_plan')
meta_prompt = plan_template.format(
    user_prompt=user_prompt,
    full_history=json.dumps(full_history, indent=2)
)
```

4.  Apply this same pattern to all other calls to `get_prompt` within `discuss_panel_handler.py`.
