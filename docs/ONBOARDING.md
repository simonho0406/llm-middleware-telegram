# 🧭 Architecture & Onboarding

Contributor reference for how this bot is built. For setup and usage, see the
[README](../README.md). For per-feature history, see `docs/tickets/archive/`.

---

## 1. Architectural Pillars

All code should uphold these four principles:

- **Pillar A — Stateless, class-based services.** External services are classes
  instantiated with config; avoid module-level mutable state. (Two legacy
  modules — `services/ollama_service.py` and `services/openrouter_service.py` —
  predate this and use module-level state; prefer `gemini_service.py` /
  `openai_compatible_service.py` as the pattern. The main OpenRouter path now
  goes through `OpenAICompatibleService`, not `openrouter_service.py`.)
- **Pillar B — Centralized, safe rendering.** All user-facing output goes through
  `bot/messaging.py::send_safe_message` (AST-based Markdown→TelegramV2). Never
  hand-escape Markdown.
- **Pillar C — Robust state management.** Every cancellable flow tracks its
  `asyncio.Task` and cleans up on cancel/timeout. State that must not be shared
  across concurrent handlers (`concurrent_updates=True`) is attached to the task,
  not to `chat_data` (see `_pending_user_message_pk`, `_llm_bg_tasks`,
  `_active_draft_id`, `_expected_cancel` on the LLM task).
- **Pillar D — Configuration-driven.** No hardcoded model names or provider
  logic; behavior comes from `config.yaml` and the `user_settings` DB table.

---

## 2. Directory Layout

```
llm-middleware-telegram/
├── main.py                       # Entry point: polling loop (recreates event loop on
│                                 #   NetworkError), handler registration, global
│                                 #   error_handler (job-aware), post_init/shutdown hooks
├── config.py / config.yaml       # Config accessors and values
├── bot/
│   ├── application.py            # PTB Application builder (concurrent_updates=True)
│   ├── response_generator.py     # ★ Core LLM pipeline: agentic tool loop, streaming,
│   │                             #   harness (notify-on-failure), inactivity watchdog
│   ├── recovery.py               # Startup take-over of unanswered turns (DB-based)
│   ├── messaging.py              # send_safe_message / draft messages (Pillar B)
│   ├── providers.py              # Provider registry; get_provider_details(), shutdown
│   ├── settings.py               # USER_SETTINGS (enable_mcp, enable_skills, auto_retry…)
│   ├── middleware.py             # auth_middleware (allowed_chat_ids)
│   ├── prompt_loader.py          # Loads prompts/*.md into config.PROMPTS
│   ├── agent_utils.py            # search-need heuristics
│   └── handlers/                 # chat, misc_commands, ask_selected, discuss,
│                                 #   discuss_panel, configure_panel, config,
│                                 #   context_sidebar, flash
├── services/
│   ├── gemini_service.py         # GeminiService (per-key client cache, tools support)
│   ├── openai_compatible_service.py # NVIDIA/Groq/OpenRouter/custom (tools support)
│   ├── ollama_service.py         # Ollama (legacy module-level)
│   ├── openrouter_service.py     # legacy; main path uses OpenAICompatibleService
│   ├── mcp_service.py            # McpClientService: stdio MCP servers, per-server pass_env
│   ├── skill_service.py          # SkillRegistryService: loads skills/*/SKILL.md
│   └── web_search_service.py     # Tavily/Google search (optional MCP routing)
├── storage/
│   ├── database_storage.py       # SQLite via aiosqlite; schema + conversation_history view
│   ├── file_storage.py           # JSON backend (limited; no per-message PK/history view)
│   └── __init__.py               # StorageManager facade (storage_manager)
├── utils/
│   ├── service_registry.py       # MCP supervisor + lazy skill init (see §4)
│   ├── hooks.py                  # HookRunner: pre-tool-use security gate
│   ├── context_manager.py        # ensure_context_fits() token budgeting
│   ├── text_processing.py        # AST Markdown→TelegramV2 renderer
│   ├── search_agent.py / llm_utilities.py
├── hooks/                        # External hook scripts + security_policy.py
├── prompts/                      # *.md system/panel prompts (loaded at startup)
├── skills/                       # SKILL.md playbooks (+ README); empty by default
├── tests/                        # pytest suite
└── docs/                         # README lives at root; tickets archived here
```

---

## 3. The Response Pipeline (`bot/response_generator.py`)

1. A message is debounced (`handlers/chat.py`) then `_generate_and_send_response`
   wraps generation in a tracked, cancellable task — **the harness choke point**.
2. `_generate_llm_response` builds history, injects `CHAT_SYSTEM_PROMPT` + a live
   **tool catalog** (connected MCP servers, skills, and a `conversation_history`
   cheat-sheet scoped to the current chat_id + thread_id), then runs an **agentic
   loop** (max 5 turns): model emits text or a `{"tool_calls": …}` request →
   tools execute (`skill_*` before `server__tool` routing) via the hook security
   gate → results feed back → repeat until a plain answer.
3. **Auto-search:** if the model emits `<search>…</search>` and auto-search is on,
   the turn delegates to `search_command` (web search → synthesis).
4. **Delivery & archival:** the answer is sent via `send_safe_message`, then saved
   with `asyncio.shield` so a late `/cancel` can't drop a shown reply.

### The harness (never fail silently)
- **Layer 1** (`_generate_and_send_response`): unexpected cancels or exceptions →
  `_notify_user_failure` (the turn always ends visibly). *Expected* cancels
  (user `/cancel`, edit-supersede, deliberate zombie-cancel) are flagged
  `task._expected_cancel = True` and stay silent.
- **Layer 2** (`main.py::error_handler`): resolves a chat_id even for JobQueue
  errors (`context.job`), so debounced-job failures reach the user.
- **Layer 3** (inactivity watchdog): aborts a stream with no token for
  `generation_idle_timeout_seconds`; resets on every token so healthy long
  generations are never cut.
- **Recovery** (`bot/recovery.py`): on startup, resumes the most recent
  unanswered user message per chat within `recovery.window_seconds` (in place,
  `save_input=False` — no delete, no data-loss window). DB-based, since Telegram
  can't expose chat history.

---

## 4. MCP Supervisor Pattern (`utils/service_registry.py`)

The MCP SDK's `stdio_client()` uses anyio cancel scopes that **must be entered
and exited from the same asyncio task**. Violating that leaks subprocess zombies
and corrupts the loop. So the entire MCP lifecycle (connect → keep-alive → idle
shutdown → cleanup) is owned by a single long-lived **supervisor task** spawned on
first use; callers talk to it via `asyncio.Event`s. It idle-shuts-down the
subprocesses after ~30 min and reconnects transparently on the next request.
`shutdown_mcp_supervisor()` is called from the shutdown hook.

`get_or_init_mcp_service` / `get_or_init_skill_service` are the race-free
accessors (double-checked locking; honor `enable_mcp` / `enable_skills`).

---

## 5. Security: tool hooks

`utils/hooks.py::HookRunner.run_pre_tool_use` runs before every tool execution
and can deny it (e.g. write/DDL SQL, dangerous shell). The blocklists live in one
place (`hooks/security_policy.py`) shared by the in-process runner and the
external `hooks/pre_tool_use.py` script. A hook *script* failure raises
`HookScriptError` (a `PermissionError` subclass) so config errors are
distinguishable from genuine security denials. MCP secrets are confined per server
via each server's `pass_env` allowlist in `config.yaml`.

---

## 6. Conventions

- **generate_response signature:** `async def generate_response(self, model, prompt,
  context_history=None, tools=None, ...) -> AsyncGenerator[str, None]`.
- **History format:** `[{"role": "user"|"assistant"|"system"|"tool", "content": …}]`.
- **DB access:** connection-per-call via `async with aiosqlite.connect(config.DB_PATH)`.
- **Event-loop-bound singletons** (provider httpx pools, MCP service, panel locks)
  are reset on shutdown because the polling loop builds a fresh event loop on
  restart — see `shutdown_providers`, `shutdown_mcp_supervisor`, `reset_panel_locks`.
- **Tests:** `python -m pytest -q`. Prefer writing/adjusting a test alongside any
  behavior change.

---

## 7. Running & verifying

```bash
python -m pytest -q                         # full suite
docker compose up --build -d                # build & run
docker logs -f llm-middleware-telegram      # live logs
docker exec llm-middleware-telegram python scripts/panel_qa.py   # panel E2E (needs keys)
```
