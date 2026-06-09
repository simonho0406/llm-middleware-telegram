# 🧭 Agent Onboarding: MCP & Skill Integration Sprint

> **Last Updated:** 2026-05-24 by Tech Lead session (conversation `423ba8f4-2d21-4194-b05f-804c169c9cae`)
> **Branch:** `main` (clean, at commit `b80059f`)
> **Tests:** 87 collected, all passing
> **Unstaged files:** 5 new ticket files (020–024) ready to commit

---

## 1. READ THIS FIRST: The Project Constitution

This project operates under a strict **Brain vs. Hands** protocol defined in the user's global rules. Before doing anything:

1. **You are the `gemini-cli-dev` (Developer / Hands)** unless told otherwise.
2. **You execute Tickets.** You do NOT make architectural decisions. If a ticket violates the architecture, you halt and report.
3. **TDD Mandate:** You write the test *first* (or ensure one exists), then write the code to pass the test.
4. **4 Immutable Architectural Pillars** govern all code:
   - **Pillar A — Stateless, Class-Based Services:** All external services must be Classes instantiated with config. No global module-level variables for state.
   - **Pillar B — Centralized, Safe Rendering:** ALL user-facing output goes through `messaging.send_safe_message`. Use the AST-based renderer. Never manually escape Markdown.
   - **Pillar C — Robust State Management:** `ConversationHandler`s in `group=0`. Every stateful flow needs an `asyncio.Task` tracker and cleanup to kill zombies on cancel/timeout.
   - **Pillar D — Configuration-Driven:** No hardcoded model names or provider logic. Behavior from `config.yaml` and `user_settings` DB table.

---

## 2. What Was Accomplished (Prior Sessions)

### Session 1: Tech Debt Refactor (Complete ✅)
- Full 38-file codebase review → `docs/comprehensive_code_review.md`
- Branch `feature/tech-debt-refactor` merged to `main`
- Created Tickets 018 (NameError bug) and 019 (WAL mode)
- All 79→87 tests passing

### Session 2: MCP/Skill Architecture Design (Complete ✅)
- **Thorough industry research** of MCP (Anthropic), OpenClaw Skills, Claude Code skill registry
- **Architecture decision: Hybrid approach** — MCP for external APIs/data, Skill Playbooks for procedural workflows
- **Implementation plan created** at: `<appDataDir>/brain/423ba8f4-2d21-4194-b05f-804c169c9cae/implementation_plan.md`
- **5 development tickets created** (020–024), currently unstaged in git

---

## 3. The Active Work: MCP & Skill Integration

### What We're Building
A hybrid extensibility layer that gives the LLM two new capabilities:
1. **MCP Client** — Spawns local subprocess servers (SQLite, GitHub CLI, etc.) via the `mcp` Python SDK, queries their tools, and lets the LLM call them natively.
2. **Skill Registry** — Loads markdown-based "playbooks" from a `skills/` directory. Each skill is a `SKILL.md` with YAML frontmatter (parameters, description) and a natural-language procedure body. Skills are registered as lightweight tool stubs; the full body is loaded only when invoked (deferred activation pattern from Claude Code).
3. **Agentic Loop** — The response generator becomes a multi-turn recursive loop: LLM generates → if tool call, execute it → feed result back → LLM synthesizes. Capped at 5 turns.

### Execution Order (Dependency Graph)

```
Tickets 020, 021, 022, 024  ← Can be done in parallel (no interdependencies)
         ↓
      Ticket 023             ← Depends on all four above
```

| Ticket | File | Summary | Status |
|--------|------|---------|--------|
| **020** | `docs/tickets/020-mcp-client-subsystem.md` | `services/mcp_service.py` — McpClientService class | **Ready** |
| **021** | `docs/tickets/021-skill-registry-subsystem.md` | `services/skill_service.py` — SkillRegistryService class | **Ready** |
| **022** | `docs/tickets/022-database-migration-for-tool-calling.md` | SQLite migration: add `tool_calls`, `tool_call_id` columns | **Ready** |
| **024** | `docs/tickets/024-unified-tool-security-hooks.md` | Expand `utils/hooks.py` for all tool types | **Ready** |
| **023** | `docs/tickets/023-agentic-response-loop.md` | Rewrite `response_generator.py` + provider tool support | **Ready** (depends on 020-022, 024) |

### User Approval Status
> **⚠️ The implementation plan has been presented to the user but explicit "go ahead" approval has NOT yet been recorded in conversation.** The user's last message was asking for this onboarding document. **Ask the user for approval before starting implementation.**

---

## 4. Codebase Quick Reference

### Directory Layout
```
llm-middleware-telegram/
├── main.py                          # Entry point, PTB handler registration
├── config.py / config.yaml          # All configuration accessors and values
├── bot/
│   ├── response_generator.py        # ★ Core LLM pipeline (will be modified in T023)
│   ├── messaging.py                 # send_safe_message, send_draft_message
│   ├── providers.py                 # initialize_providers(), get_service_for_provider()
│   ├── agent_utils.py               # is_search_required() classifier
│   ├── settings.py                  # USER_SETTINGS dict
│   └── handlers/
│       ├── misc_commands.py         # /search, /help, etc.
│       ├── discuss_panel_handler.py  # Expert Panel orchestration (1,497 lines)
│       └── ...                      # config, model selection, thread mgmt
├── services/
│   ├── gemini_service.py            # GeminiService class (v2 SDK)
│   ├── openai_compatible_service.py # OpenAICompatibleService class (NVIDIA, Groq)
│   ├── openrouter_service.py        # Module-level (Pillar A violation — legacy)
│   ├── ollama_service.py            # Module-level singleton (Pillar A violation — legacy)
│   └── web_search_service.py        # Tavily/Google search
├── storage/
│   ├── database_storage.py          # SQLite via aiosqlite (will be modified in T022)
│   └── storage_manager.py           # High-level storage API
├── utils/
│   ├── hooks.py                     # HookRunner class (will be modified in T024)
│   ├── search_agent.py              # Iterative search agent loop
│   ├── text_processing.py           # AST-based Markdown→TelegramV2 renderer
│   ├── context_manager.py           # ensure_context_fits()
│   └── llm_utilities.py             # get_robust_llm_response()
├── hooks/                           # Directory for user hook scripts
├── skills/                          # NEW — Will contain SKILL.md playbooks
├── tests/                           # 87 tests, all passing
└── docs/tickets/                    # Development tickets (002–024)
```

### Key Patterns to Follow
- **Service instantiation**: See `GeminiService.__init__` in `services/gemini_service.py` — takes config, no globals.
- **generate_response signature**: `async def generate_response(self, model, prompt, context_history=None, request_timeout=None)` — returns `AsyncGenerator[str, None]`.
- **Safe message sending**: Always use `bot.messaging.send_safe_message(context, update, text)`.
- **DB pattern**: Connection-per-call via `async with aiosqlite.connect(config.DB_PATH) as db`.
- **History format**: List of `{"role": "user"|"assistant"|"system", "content": "..."}` dicts.

### Existing Extension Points (Already in Codebase)
- `utils/hooks.py` → `HookRunner` class with `run_pre_tool_use()` — currently only used for search in `misc_commands.py:117`.
- `.roo/mcp.json` → An existing MCP config file (Docker-based SQLite server) from a prior experiment. Not wired into the bot.
- `docs/tickets/007-mcp-integration-specification.md` → The original MCP spec ticket (predecessor to our new 020–024 series). Useful for historical context but **superseded** by the new tickets.

---

## 5. Pre-Flight Checklist Before Starting

1. **Ask user for explicit approval** on the implementation plan.
2. **Commit the 5 new ticket files** (020–024) to a new feature branch:
   ```bash
   git checkout -b feature/mcp-skill-integration
   git add docs/tickets/02*.md
   git commit -m "docs: add tickets 020-024 for MCP and Skill integration"
   ```
3. **Install the `mcp` SDK** and verify it imports:
   ```bash
   pip install mcp
   python -c "from mcp import ClientSession; print('MCP SDK OK')"
   ```
4. **Run the existing test suite** to confirm green baseline:
   ```bash
   python -m pytest -q
   ```

---

## 6. Critical Gotchas & Landmines

| Issue | Details |
|-------|---------|
| **`openrouter_service.py` is module-level** | It violates Pillar A. Do NOT follow its patterns. Follow `gemini_service.py` or `openai_compatible_service.py` instead. |
| **`ollama_service.py` has a global singleton** | Same as above — legacy pattern. |
| **`hooks.py` line 52 has a module-level instance** | `hook_runner = HookRunner()` — this is a Pillar A violation but is currently depended on by `misc_commands.py:30`. When modifying hooks, keep backward compatibility or refactor the import site. |
| **`response_generator.py` uses `<search>` tag extraction** | Lines 108–122: The current auto-search system works by instructing the LLM to emit `<search>query</search>` tags, then regex-extracting them. The new agentic loop should eventually **replace** this with proper tool calling, but keep it working during the transition. |
| **Expert Panel** (`discuss_panel_handler.py`) | 1,497 lines of complex multi-agent orchestration. **Do NOT touch this file** in this sprint. It has its own provider/model routing and is independent of the main response pipeline. |
| **Database migrations must be backwards-compatible** | Use `ALTER TABLE ... ADD COLUMN` with `PRAGMA table_info` checks. Never `DROP TABLE` on the messages table. |

---

## 7. Definition of Done

For the sprint to be considered complete:
- [ ] `McpClientService` class exists, connects to a configured stdio server, lists tools, executes a tool, and cleans up (Ticket 020)
- [ ] `SkillRegistryService` class exists, parses `SKILL.md` files, exposes tool schemas (Ticket 021)
- [ ] `messages` table has `tool_calls` and `tool_call_id` columns with migration (Ticket 022)
- [ ] `HookRunner` validates all tool types, sample hook exists (Ticket 024)
- [ ] `response_generator.py` runs recursive agentic loop with tool calling (Ticket 023)
- [ ] `openai_compatible_service.py` and `gemini_service.py` accept `tools` parameter (Ticket 023)
- [ ] All new code has tests written FIRST (TDD mandate)
- [ ] All 87+ existing tests still pass
- [ ] No Pillar violations introduced
