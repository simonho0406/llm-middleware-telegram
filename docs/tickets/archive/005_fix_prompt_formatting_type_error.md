# TICKET-005: Fix `TypeError` in Prompt Formatting

**Status:** CLOSED (Superseded)

**Problem:** The application crashes with a `TypeError` because `config.PROMPTS.get_prompt()` is being called with keyword arguments, but it only accepts a single `name` argument.

**Evidence:**
```
TypeError: ... unexpected keyword argument 'user_prompt'
```

**Definition of Done:**
1. In `bot/handlers/discuss_panel_handler.py`, find all calls to `config.PROMPTS.get_prompt()`.
2. Refactor them to follow the correct two-step pattern:
   - **Step 1:** Get the template string: `template = config.PROMPTS.get_prompt('PROMPT_NAME')`
   - **Step 2:** Format the template: `prompt = template.format(user_prompt=user_prompt, ...)`
3. Apply this fix to all places where `get_prompt` is called with extra arguments in the file.