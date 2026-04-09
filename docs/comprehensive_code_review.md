# Comprehensive Code Review: llm-middleware-telegram

**Reviewer**: External Principal Software Engineer  
**Date**: 2026-04-05  
**Scope**: Full codebase audit — architecture, concurrency, security, and maintainability  
**Commit Basis**: Current `main` working tree

---

## Executive Summary

The `llm-middleware-telegram` project demonstrates a thoughtful, iterative architecture that has evolved from a simple chatbot into a genuinely sophisticated multi-agent LLM orchestration platform. The codebase shows strong evidence of deliberate hardening — centralized safe rendering (`send_safe_message`), an AST-based Markdown pipeline, archival database storage with append-only semantics, and a robust auth middleware layer.

**Overall Health: B+**

The project punches well above its weight for a personal middleware tool. The "Expert Panel" (Master & Apprentice) architecture is ambitious and largely well-implemented. The centralized rendering pipeline and configuration-driven design demonstrate mature engineering principles.

However, several systemic issues remain that could cause production crashes, data corruption under concurrency, or silent security failures. These fall into three critical categories:

1. **Gemini API concurrency unsafety** — a global `genai.configure()` call creates a race condition
2. **Database connection-per-call pattern** — creates high overhead and atomicity gaps
3. **Residual rendering bypasses** — `reply_text` calls in handlers break the centralized rendering contract

The sections below detail every finding, ranked by severity.

---

## 1. Critical Vulnerabilities & Bugs

These issues can cause crashes, data corruption, or silent failures in production.

### 1.1 🔴 Gemini Service: Global State Race Condition

**File**: [`services/gemini_service.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/services/gemini_service.py#L38)  
**Severity**: CRITICAL  
**Category**: Concurrency / Data Corruption

The `genai.configure(api_key=key)` call on line 38 mutates **global module-level state** in the `google.generativeai` library. Because the application runs with `concurrent_updates=True` (see `application.py` line 40), multiple users sending messages simultaneously will race on this global API key:

```python
# services/gemini_service.py:38
genai.configure(api_key=key)  # ← GLOBAL MUTATION
gemini_model = genai.GenerativeModel(model)  # ← uses the globally-set key
```

**Impact**: User A's request could silently use User B's API key (or vice versa). Under key rotation for rate-limit recovery, one user's response could consume another user's quota, or worse, a rate-limited key could be swapped in mid-stream.

**This same pattern appears 3 times** (lines 38, 84, 113).

**Recommendation**: Refactor `gemini_service.py` to be a class-based service (aligned with Pillar A of the constitution). Each call should instantiate its own `genai.GenerativeModel` with a per-request client, or use the new `google-genai` v1 SDK which supports per-client API keys.

---

### 1.2 🔴 Database: Connection-Per-Call Creates Atomicity Gaps

**File**: [`storage/database_storage.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/storage/database_storage.py)  
**Severity**: HIGH  
**Category**: Data Integrity / Performance

Every single database function opens a **new** `aiosqlite.connect()`, performs its operation, and closes. This means:

1. **No connection pooling**: For a high-frequency chatbot, this creates significant I/O overhead.
2. **Cross-function atomicity is impossible**: Consider `_generate_and_send_response_task` in `response_generator.py`:
   - Line 258: `save_message(chat_id, 'user', prompt)` → opens connection, saves, closes
   - Line 268: `_generate_llm_response(...)` → internally calls `get_thread_history()` → opens new connection
   - Line 307: `save_message(chat_id, 'assistant', ...)` → opens yet another connection

   If the process crashes between step 1 and step 3, the user's message is orphaned in the database with no corresponding assistant response. While the `pending_user_message_pk` cleanup on cancel is a good mitigation, it doesn't cover crashes.

3. **`_get_or_create_chat` TOCTOU race**: Line 13-26 performs a SELECT then an INSERT. Under `concurrent_updates=True`, two simultaneous first-messages from a new user can both see `fetchone() is None` and race to INSERT, though the UNIQUE constraint will catch one.

**Recommendation**: Introduce a connection pool (e.g., a single shared `aiosqlite` connection guarded by an `asyncio.Lock`, or migrate to `aiosqlite` with WAL mode + a connection pool wrapper). Critical multi-step operations should use a single connection with explicit transactions.

---

### 1.3 🔴 SQL Column Name Interpolation (Controlled but Fragile)

**File**: [`storage/database_storage.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/storage/database_storage.py#L200)  
**Severity**: MEDIUM (currently mitigated, but architecturally fragile)  
**Category**: Security

Lines 200 and 216 use f-string interpolation for column names:

```python
await cursor.execute(f"SELECT {key} FROM threads WHERE thread_pk = ?", (thread_pk,))
await db.execute(f"UPDATE threads SET {key} = ? WHERE thread_pk = ?", (value, thread_pk))
```

While the `valid_keys` whitelist on lines 188 and 205 gates the input, this is a defense-in-depth gap. If a future developer adds a new key to the whitelist without proper validation, or if the redirect logic on line 191 is modified, the f-string becomes a SQL injection vector.

**Recommendation**: Use a dict-based approach mapping keys to hardcoded queries, eliminating f-string SQL entirely.

---

### 1.4 🟡 httpx.AsyncClient Leak in Draft Messages

**File**: [`bot/messaging.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/bot/messaging.py#L54)  
**Severity**: MEDIUM  
**Category**: Resource Leak

`send_draft_message` and `finalize_draft` create a **new** `httpx.AsyncClient()` for every call:

```python
async with httpx.AsyncClient() as client:
    resp = await client.post(url, json=payload, timeout=2.0)
```

During streaming, `send_draft_message` is called every 0.5 seconds per active chat. Each call creates a new TCP connection, performs TLS handshake, sends one request, and tears down. Under load with multiple simultaneous chats, this creates connection churn and could exhaust file descriptors.

**Recommendation**: Create a single shared `httpx.AsyncClient` for draft messages (similar to how `OpenAICompatibleService` manages its client) and close it during `shutdown_providers()`.

---

### 1.5 🟡 Provider API Key Resolution Bug

**File**: [`bot/providers.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/bot/providers.py#L78)  
**Severity**: MEDIUM  
**Category**: Configuration Bug

Line 78 uses a hardcoded pattern to derive the environment variable name:

```python
provider_conf['api_key'] = os.getenv(f"{provider_conf['name'].upper()}_API_KEY")
```

But the `config.yaml` specifies the env var name explicitly (e.g., `api_key: "CUSTOM_PROVIDER_API_KEY_GROQ"`). The code **ignores the configured env var name** and constructs its own. For the `groq` provider:
- Config says to look for `CUSTOM_PROVIDER_API_KEY_GROQ`
- Code actually looks for `GROQ_API_KEY`

This means custom providers will silently fail if the user follows the documented `.env.example` convention.

**Recommendation**: Replace line 78 with:
```python
provider_conf['api_key'] = os.getenv(provider_conf.get('api_key', f"{name.upper()}_API_KEY"))
```

---

## 2. Architectural Violations

### 2.1 Frontend Leakage into Core Logic

The project aspires to be "frontend-agnostic," but several violations exist:

| Location | Violation | Severity |
|---|---|---|
| `response_generator.py:7` | Imports `telegram.Update`, `constants`, `InlineKeyboardButton` | HIGH |
| `response_generator.py:156` | `from bot.messaging import send_draft_message` — hardcodes Telegram draft API | HIGH |
| `utils/context_manager.py` | Clean — no Telegram imports ✅ | — |
| `storage/database_storage.py` | Clean — no Telegram imports ✅ | — |
| `services/*.py` | Clean — no Telegram imports ✅ | — |

**Key Violation**: `response_generator.py` is supposed to be the core "response generation engine," but it directly imports Telegram types and sends Telegram-specific draft messages. If you wanted to swap Telegram for a Discord or Web frontend, this file would need a full rewrite.

**Recommendation**: Split `response_generator.py` into:
1. A pure `core/llm_engine.py` that returns response data (no Telegram imports)
2. A Telegram-specific `bot/telegram_response_handler.py` that handles drafts, placeholders, and message sending

---

### 2.2 Dual Storage Manager Pattern

There are **two** storage manager modules:
- [`storage/__init__.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/storage/__init__.py) — Class-based `StorageManager`
- [`storage/storage_manager.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/storage/storage_manager.py) — Module-level function aliasing

Both exist simultaneously and are imported by different parts of the codebase (`from storage import storage_manager` resolves to the `StorageManager` class in `__init__.py`). The `storage_manager.py` file appears to be a vestige of an earlier approach.

**Impact**: Confusion for contributors. The `storage_manager.py` module does redundant work (conditional import + re-export), while `__init__.py` already handles this via the `StorageManager` class. Dead code that increases cognitive load.

**Recommendation**: Delete `storage/storage_manager.py` and verify all imports resolve through `storage/__init__.py`.

---

### 2.3 Service Architecture Inconsistency (Pillar A Violations)

| Service | Architecture | Status |
|---|---|---|
| `openai_compatible_service.py` | Class-based ✅ | Compliant |
| `gemini_service.py` | Module-level functions, global `genai.configure()` | ❌ Violates Pillar A |
| `openrouter_service.py` | Module-level functions, creates new `httpx` client per call | ❌ Violates Pillar A |
| `ollama_service.py` | Module-level functions with singleton-cached client | ⚠️ Partial compliance |
| `web_search_service.py` | Module-level functions, new `httpx` client per call | ❌ Violates Pillar A |

Only `OpenAICompatibleService` follows the constitutional mandate of "stateless, class-based services." The other three LLM services and the search service are module-level function collections with no encapsulation.

---

### 2.4 Rendering Bypass: ~20 `reply_text` Calls Circumvent Centralized Rendering

**Constitutional Pillar B** mandates all user-facing output passes through `send_safe_message`. However, grep reveals **~20 instances** of `update.message.reply_text(...)` in handlers — primarily in `discuss_panel_handler.py`, `ask_selected_handler.py`, and `context_sidebar_handler.py`.

Most of these use `parse_mode=None` (plain text), which is safe from Telegram parse errors but still violates the centralized rendering contract. Some, like `context_sidebar_handler.py:268`, use `parse_mode=constants.ParseMode.HTML` — an entirely different rendering mode that bypasses the AST pipeline and could cause `BadRequest` crashes if the content contains unescaped HTML entities.

**Recommendation**: Create a tracking ticket to systematically migrate all `reply_text` calls to either `send_safe_message` or `send_plain_message`.

---

## 3. Concurrency & Async Safety Analysis

### 3.1 Expert Panel State: asyncio.Lock Stored in user_data

**File**: [`discuss_panel_handler.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/bot/handlers/discuss_panel_handler.py#L926)  
**Severity**: MEDIUM

The panel stores an `asyncio.Lock()` inside `context.user_data['panel_state']['lock']` (line 926). This lock is used correctly in `handle_follow_up` and `reroll_discussion` to serialize panel operations. However:

1. **Lock survives restarts poorly**: If the bot restarts, the lock object is lost but `user_data` may be persisted (PTB persistence), causing `KeyError` when accessing `panel_state['lock']`.
2. **Lock is not created in all code paths**: `handle_panel_edit` (line 1170) accesses `panel_state` without acquiring the lock, creating a potential race with `handle_follow_up`.

**Recommendation**: Acquire the lock in `handle_panel_edit` before modifying the transcript. Consider using a factory pattern that lazily creates the lock if missing.

---

### 3.2 Task Lifecycle: Background Task Tracking is Solid ✅

The `_generate_and_send_response` function (lines 26-43) implements defensive zombie-task cancellation before creating new tasks. The `_bg_tasks` set with `add_done_callback(discard)` pattern (lines 167-174) is a clean approach to tracked fire-and-forget tasks. The cancellation cleanup in `_cleanup_discussion_state` is thorough.

This is one of the strongest aspects of the codebase.

---

### 3.3 Message Debounce: Stale Update Object Reference

**File**: [`bot/handlers/chat.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/bot/handlers/chat.py#L111-L114)  
**Severity**: LOW-MEDIUM

The debounce mechanism stores the `update` object from the **first** message in the buffer (line 114):

```python
data={'chat_id': chat_id, 'user_id': user_id, 'update': update}
```

When the debounce fires and calls `_generate_and_send_response`, it uses this stored `update` for `send_safe_message`. If the user sent 3 messages that were combined, the reply will be attached to the **first** message, not the latest. This is a minor UX issue but could confuse users.

---

### 3.4 Edited Message Handler: Dangerous `replace_thread_history_dangerous`

**File**: [`bot/handlers/chat.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/bot/handlers/chat.py#L194)

When a user edits a message, the handler fetches the full history, mutates it in-memory, and calls `set_thread_history` (which maps to `replace_thread_history_dangerous`). This performs a full DELETE + bulk INSERT of the entire history. For conversations with hundreds of messages, this is:

1. **Expensive**: Deletes and re-inserts all messages
2. **Race-prone**: If another message arrives during the DELETE → INSERT window, it will be lost
3. **Correctly named**: The `_dangerous` suffix is apt warning, but the code path is still live

**Recommendation**: Use targeted `UPDATE` and `DELETE` operations based on `message_pk` instead of full history replacement.

---

## 4. Error Handling & API Boundaries

### 4.1 Rate Limit Handling: Good but Incomplete

| Service | 429 Handling | Retry Logic | Backoff |
|---|---|---|---|
| `openai_compatible_service.py` | `RateLimitError` caught, retries with exponential backoff ✅ | 3 retries ✅ | 2x exponential ✅ |
| `openrouter_service.py` | 429/403 caught with user-facing error ✅ | No automatic retry ⚠️ | N/A |
| `gemini_service.py` | `ResourceExhausted` triggers key rotation ✅ | Tries next key ✅ | No delay between keys ⚠️ |
| `ollama_service.py` | No rate limiting expected (local) | N/A | N/A |

**Gap**: `openrouter_service.py` does not retry on 429. It returns immediately with an error message. Since OpenRouter free tier has tight limits (20 RPM), this is a common failure mode.

**Gap**: Gemini key rotation has no delay between keys. If all keys are rate-limited simultaneously (common on free tier at 5 RPM), the rotation loop will exhaust all keys within milliseconds.

---

### 4.2 Timeout Handling: Inconsistent Timeout Configuration

The codebase has at least 4 different timeout configurations:

1. `config.yaml`: `REQUEST_TIMEOUT_SECONDS: 180`
2. `config.yaml`: `OLLAMA_REQUEST_TIMEOUT_SECONDS: 1200`
3. `config.yaml`: Expert panel `request_timeout_seconds: 600` per role
4. `application.py`: Telegram HTTP request timeouts at 120s each
5. `web_search_service.py`: Hardcoded `timeout=20.0` on client, `timeout=30.0` on request (line 27-28 — note: the per-request timeout overrides the client timeout, so the client timeout is misleading)

The `openrouter_service.py` has a hardcoded `timeout_config = request_timeout if request_timeout is not None else 30.0` (line 57) which ignores the global `REQUEST_TIMEOUT_SECONDS` config when `request_timeout` is not passed. This means regular chat through OpenRouter uses a 30s timeout vs the configured 180s.

---

### 4.3 Empty Response Handling: Recursive Retry Risk

**File**: [`bot/response_generator.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/bot/response_generator.py#L229-L231)

When the LLM returns an empty response, the code recursively calls itself with `force_truncate=True`:

```python
if not force_truncate and not llm_error_reported_by_model:
    return await _generate_llm_response(context, chat_id, prompt, is_reroll, force_truncate=True, ...)
```

The recursion is bounded (one level deep via the `force_truncate` guard), which is correct. However, this pattern is fragile — a future refactor could inadvertently remove the guard and create infinite recursion.

---

## 5. Security & Validation

### 5.1 Auth Middleware: Correct but Permissive by Default

**File**: [`bot/middleware.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/bot/middleware.py)

The auth middleware correctly uses `ApplicationHandlerStop` to block unauthorized chats. However, `allowed_chat_ids` is `None` by default (commented out in `config.yaml`), which means **all chats are allowed**. This is a conscious design choice, but it means:

- The bot is open to anyone who finds the bot username
- No logging of authorized access attempts (only unauthorized ones are logged)

**Note**: The auth check uses `chat_id`, not `user_id`. In group chats, this means all members of an authorized group can use the bot, which is likely intentional.

---

### 5.2 Bot Token Exposure in Draft API Calls

**File**: [`bot/messaging.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/bot/messaging.py#L43)

The draft message API constructs URLs with the bot token directly:

```python
url = f"https://api.telegram.org/bot{context.bot.token}/sendMessageDraft"
```

While this is the standard Telegram API pattern, the use of raw `httpx` instead of PTB's built-in request methods means:
- The token appears in `httpx` debug logs if logging level is lowered
- Any failed request logged at DEBUG level (line 58) could expose the token in error details

**Recommendation**: Use `context.bot._request` or at minimum ensure the URL is not logged.

---

### 5.3 Hook System: Subprocess Execution Risk

**File**: [`utils/hooks.py`](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/utils/hooks.py#L33)

The `HookRunner` executes arbitrary Python scripts from the `hooks/` directory via `subprocess.run`. While this is a plugin mechanism, it:

1. Runs synchronously (blocking the event loop for up to 10s)
2. Has no sandboxing — the hook script has full filesystem and network access
3. Accepts JSON input via stdin, which could be manipulated if the hook path is predictable

**Recommendation**: Consider running hooks via `asyncio.create_subprocess_exec` to avoid blocking the event loop. Document the security implications clearly.

---

### 5.4 Markdown AST Pipeline: Robust ✅

The `text_processing.py` AST-based rendering pipeline is well-designed:
- Uses `markdown-it-py` for parsing (proper AST, not regex)
- Uses `telegram.helpers.escape_markdown` for text escaping
- Code blocks are rendered unescaped (correct — code blocks are monospace in Telegram)
- Tables are rendered inside code blocks (clever workaround for Telegram's lack of table support)
- The `split_document_ast_aware` function correctly maintains block context across splits (reopening/closing tags)

The `replace_html_tags` function (lines 352-361) only handles `<br>` tags. Other HTML tags emitted by some LLMs (e.g., `<b>`, `<i>`, `<a>`) may cause issues in the AST parser, though `markdown-it` is configured with `html: False` (line 9), which should handle this.

**Minor issue**: The `fix_collapsed_tables` function uses regex that could be slow on very long strings with many pipe characters. In practice, this is unlikely to be a bottleneck.

---

## 6. Refactoring Opportunities

### 6.1 `discuss_panel_handler.py`: 1,428 Lines — God Module

This single file contains the **entire** Expert Panel workflow:
- Orchestrator planning
- Proposer/Critic/Refiner execution
- Quality gate assessment
- JSON parsing
- Search integration (basic + advanced)
- State management
- Follow-up handling
- Reroll logic
- Edit handling
- Timeout handling
- Command blocking

**Cyclomatic complexity of `_run_panel_workflow`** is estimated at 30+, with deeply nested `if/elif/else` chains (the search section at lines 656-797 is particularly complex with 4 levels of nesting).

**Recommendation**: Break into:
1. `panel_workflow.py` — pure orchestration logic
2. `panel_search.py` — RAG/search integration
3. `panel_state.py` — state management and persistence
4. `discuss_panel_handler.py` — Telegram handler glue only

---

### 6.2 Duplicated Role Mapping Logic

The pattern of mapping `assistant:panel` → `assistant` appears in **4 separate files**:
- `openai_compatible_service.py:99-100`
- `openrouter_service.py:39-40`
- `ollama_service.py:96-97`
- `openrouter_service.py:201` (in `_generate_single_model_non_streaming`)

This violates DRY and means a new internal role would need to be mapped in 4+ places.

**Recommendation**: Create a `sanitize_message_roles(messages)` utility in `utils/` that all services call before processing.

---

### 6.3 Dead/Vestigial Code

| File | Issue |
|---|---|
| `storage/storage_manager.py` | Entire file is redundant with `storage/__init__.py` |
| `bot/handlers/gemini_commands.py` | 426 bytes — likely superseded by generic provider commands |
| `bot/handlers/ollama_commands.py` | 489 bytes — likely superseded by generic provider commands |
| `bot/handlers/openrouter_commands.py` | 510 bytes — likely superseded by generic provider commands |
| `main.py:18-19` | Duplicate comment: `# --- Basic Command Handlers ---` appears twice |
| `gemini_service.py:176` | `prompt_single` used before assignment in test code |
| `storage/file_storage.py` | 16KB — appears to be an alternative backend, but lacks feature parity (no `delete_messages`, no `get_thread_history_with_pk`) |

---

### 6.4 Configuration: Rate Limits Defined but Never Enforced

`config.yaml` defines detailed rate limits (lines 127-142):

```yaml
rate_limits:
  openrouter:
    requests_per_minute: 15
    daily_requests: 800
```

But **no code in the application reads or enforces these limits**. There is no rate limiter, token bucket, or request counter. The config exists purely as documentation.

**Recommendation**: Either implement a simple in-memory rate limiter (e.g., using `asyncio` + sliding window) or remove the config to avoid giving users a false sense of protection.

---

## 7. Test Coverage Assessment

The project has **31 test files** covering:
- ✅ Markdown rendering and text processing
- ✅ Reroll logic
- ✅ Cancel logic  
- ✅ Search command deduplication
- ✅ Panel orchestrator
- ✅ Panel edit handling
- ✅ Context window management
- ✅ Flash handler
- ✅ Provider shutdown
- ✅ Reasoning fallback
- ✅ Service-level tests

**Gaps**:
- ❌ No integration test for the full Telegram → Response → Storage flow with a real (mocked) database
- ❌ No test for concurrent message handling (race conditions)
- ❌ No test for auth middleware
- ❌ No test for the debounce mechanism under rapid-fire messages
- ❌ No test for Gemini key rotation under `ResourceExhausted`

---

## 8. Positive Highlights

These aspects of the codebase demonstrate strong engineering:

1. **AST-Based Markdown Pipeline** (`text_processing.py`): Using `markdown-it-py` for proper AST parsing instead of regex-based escaping is a rare and correct approach. The document-aware splitting that maintains block context is particularly impressive.

2. **Archival Database Design**: The append-only `save_message` pattern with `pending_user_message_pk` tracking for cancellation cleanup is a well-thought-out approach to data integrity.

3. **Expert Panel Architecture**: The Master & Apprentice quality-gate loop with configurable thresholds, role-based fallbacks, and refinement iterations is sophisticated and well-structured.

4. **Configuration-Driven Design**: The `config.yaml` + accessor function pattern provides clean separation. The `custom_openai_providers` system is extensible and powerful.

5. **`get_robust_llm_response`** (`utils/llm_utilities.py`): Centralized retry + fallback logic with consistent return format. This is exactly the right abstraction.

6. **Defensive Task Management**: The zombie-task cancellation pattern in `_generate_and_send_response` and the `_bg_tasks` tracking set are production-quality patterns.

---

## 9. Prioritized Remediation Roadmap

| Priority | Issue | Effort | Impact |
|---|---|---|---|
| **P0** | 1.1 — Gemini `genai.configure()` race condition | Medium | Data integrity |
| **P0** | 1.5 — Provider API key resolution bug | Low | Functionality |
| **P1** | 1.2 — Database connection pooling | High | Performance, integrity |
| **P1** | 2.1 — Decouple `response_generator.py` from Telegram | High | Architecture |
| **P1** | 6.1 — Break up `discuss_panel_handler.py` | High | Maintainability |
| **P2** | 2.4 — Migrate `reply_text` calls to centralized rendering | Medium | Consistency |
| **P2** | 1.4 — httpx client leak in drafts | Low | Performance |
| **P2** | 6.2 — Extract role mapping utility | Low | DRY |
| **P2** | 4.2 — Inconsistent timeout configuration | Low | Reliability |
| **P3** | 6.4 — Implement or remove rate limits | Medium | Correctness |
| **P3** | 2.2 — Remove duplicate storage manager | Low | Cleanup |
| **P3** | 6.3 — Remove dead code | Low | Clarity |

---

*End of review. All findings are based on static analysis of the current working tree. No code was executed during this review.*
