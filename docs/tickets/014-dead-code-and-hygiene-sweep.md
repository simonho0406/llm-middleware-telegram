# Ticket 014: Dead Code & Hygiene Sweep (HIGH-03, HIGH-04, ARCH-05, ARCH-06, MINOR-*)

**Priority:** P1
**Source:** [comprehensive_code_review.md](../comprehensive_code_review.md) — HIGH-03, HIGH-04, ARCH-05, ARCH-06, MINOR-01 through MINOR-12

## Problem

The codebase has accumulated dead code, duplicate lines, and redundant imports that signal maintenance debt and create traps for future developers.

## Items to Clean

### Duplicate Lines (HIGH-03)
- `bot/response_generator.py` L215-216: duplicate `final_content = raw_full_llm_response.strip()`
- `main.py` L33-34: duplicate `service_names` list
- `discuss_panel_handler.py` L1051-1052: duplicate `pure_markdown_content` assignment
- `storage/__init__.py` L29: duplicate `self.get_user_setting` assignment

### Dead Code (HIGH-04, ARCH-05)
- `bot/handlers/chat.py` L16-22: Dead `count_tokens` function with missing `tiktoken` import → **Delete**
- `utils/llm_utilities.py` L179+: `format_text_for_telegram` uses a hardcoded LLM call to escape markdown → **Delete** (AST renderer handles this)
- `services/gemini_service.py` L162-206: `_test()` function with `prompt_single` used before definition (MINOR-01) → **Delete** or fix

### Hardcoded Providers (ARCH-06)
- `bot/agent_utils.py` L25-29: Hardcoded `'gemini'` + `'gemini-1.5-flash-latest'` → Make configurable via `config.yaml` (add `utility_model` section)

### Redundant/Misplaced Imports (MINOR-02 through MINOR-12)
- `config.py` L74: mid-body import of `prompt_manager` (circular dep risk)
- `openrouter_service.py` L186: `import json` inside function
- `openrouter_service.py` L330: `import asyncio` inside function (already at module level)
- `response_generator.py` L3,9: `BadRequest` imported twice
- `response_generator.py` L60: `import json` inside function body
- `response_generator.py` L133: redundant re-import of `USER_SETTINGS`
- `llm_utilities.py` L18: `from config import get_expert_panel_config` duplicated
- `discuss_panel_handler.py` L646: `import json as _json` (should use existing `json` import)
- `openrouter_service.py` L91-92: Dead `if True: pass` block

## Verification

- `pytest` suite must pass with zero regressions
- `grep -rn "import json" services/` should show only top-of-file imports
