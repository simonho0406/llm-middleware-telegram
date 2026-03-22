# Ticket 003: Context Window Caps and Navigator UX

## 1. Description
Production logs reveal three unhandled edge cases in the current pipeline:
## 1. Description
Production logs reveal three unhandled edge cases in the current pipeline:
1. **Search Context Window Burnout (`/discuss_panel` & `/search`):** The Expert Panel's `_plan_deep_dive_searches` (and the normal chat's `/search` command) merge raw web scrapes into massive strings and stuff them into LLM prompts. The history truncation logic trims conversation history but does *not* trim the prompt string. This triggers `400 Token Limit` API refusals.
2. **Missing Navigator UX:** The `/context` history command only allows users to view or delete past messages. We need an `📤 Resend Assistant Reply` button to push an archived AI response directly back into chat.
3. **Table Failsafes:** Ensure that if `fix_collapsed_tables` encounters a hallucination structure too bizarre to geometrically parse, it simply bails out gracefully rather than crashing the text processing pipeline.

## 2. Execution Directives (`gemini-cli-dev`)
**Pillar C (State/Concurrency):** Truncation must be executed synchronously before appending to the AST to prevent hanging tasks. 
**Pillar B (Rendering):** Resend operations must explicitly pass the message fully through `bot.messaging.send_safe_message()`. Do not send raw markdown APIs.

### The Fix Plan:
#### 1. Context Truncation Utility
- In `utils/context_manager.py`, add `def truncate_text_to_tokens(text: str, max_tokens: int) -> str:`
    - Try to use `_TIKTOKEN_ENCODER`.
    - Fallback: `text[:max_tokens * 4]`
- In `bot/handlers/discuss_panel_handler.py`:
  Calculate how many tokens the `proposer_model` supports. Give the `research_dossier` a max budget (e.g., 50% of the maximum window). If `research_dossier` exceeds `budget`, truncate it using the new utility.
- In `bot/handlers/misc_commands.py`:
  Apply the exact same truncation logic to `search_results` inside `search_command()`.

#### 2. Resend Button
- In `bot/handlers/context_sidebar_handler.py`:
  Add `CTX_RESEND` constant.
  Include the button: `[InlineKeyboardButton("📤 Resend this Assistant Reply", callback_data=f"{CTX_RESEND}{page}_{start_pk}")]`
  Handle the callback: Find the assistant's message from the block matched by the PK/page index, then use `await bot.messaging.send_safe_message(chat_id, assistant_message, ...)`

#### 3. Table Preprocessor Bulletproofing
- Wrap the core loop of `fix_collapsed_tables` inside `utils/text_processing.py` with a simple exception trap that `return text` unmodified if the geometric index calculation throws an `IndexError`.

## 3. Definition of Done
1. **No 400s:** Passing a 5 MB web search string to the Proposer prompt must be aggressively cleanly truncated without crashing the middleware.
2. **Functional Resend:** Pressing "Resend" cleanly pastes the history back into chat.
3. **Tests:** All current `test_algorithmic*.py` files continue to pass unconditionally.
