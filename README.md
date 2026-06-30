# llm-middleware-telegram — Multi-Backend LLM Bot

A Telegram bot that fronts multiple Large Language Model backends (Ollama, Google
Gemini, OpenRouter, and any OpenAI-compatible API) and layers an agentic tool
system, a multi-agent expert panel, and a robustness harness on top.

> For architecture, directory layout, and contributor conventions, see
> [`docs/ONBOARDING.md`](docs/ONBOARDING.md). This README is the user/operator guide.

## Features

* **Multiple LLM backends** — Ollama (local/remote), Google Gemini, OpenRouter,
  and any OpenAI-compatible API (e.g. Groq, NVIDIA) added via `config.yaml`.
* **Provider & model switching** — `/provider`, `/model`, `/list_models`, `/set_model`.
* **Persistent conversation threads** — separate histories per thread, stored in
  SQLite; `/new`, `/threads`, `/rename_thread`, `/delete_thread`.
* **Streaming responses** — answers stream into the chat as they generate.
* **Agentic tool use** — a multi-turn loop where the model can call tools and
  feed results back to itself (capped at 5 turns):
  * **MCP servers** (Model Context Protocol) — configured in `config.yaml`:
    `sqlite-tools` (read-only access to the bot's own conversation history),
    `tavily-search` (web), `notion-workspace` (Notion). Toggle per chat with the
    `enable_mcp` setting.
  * **Skills** — markdown "playbooks" in `skills/` exposed as `skill_*` tools
    (toggle with `enable_skills`). See [`skills/README.md`](skills/README.md).
* **Web search** — automatic (`<search>` is emitted by the model when current
  info is needed) and manual via `/search`, with a retry button on failure.
* **Multi-model tools**
  * `/ask_selected` — query several models concurrently with one prompt.
  * `/discuss` — sequential "Round Table" across models/providers.
  * `/discuss_panel` + `/configure_panel` — an orchestrated expert panel
    (Orchestrator → Proposer/Critic → Quality Gate → Synthesis).
* **Privacy & control** — `/flash` (one-shot, not saved), `/context` (prune
  history), `/cancel` (stop generation), `/status` (provider/MCP/skill health).
* **Robustness harness** — every turn ends in an answer or a visible error (never
  silent): unexpected cancels/exceptions are surfaced, JobQueue errors reach the
  user, an inactivity watchdog catches stalled streams, and on startup the bot
  takes over the most recent message a prior session failed to answer.
* **Dockerized** — tuned to run on a small (≈2 CPU / 2 GB) shared host.

## Prerequisites

* **Docker & Docker Compose** (recommended), or **Python 3.11+** for bare-metal.
* **API keys/tokens** — a Telegram Bot Token (required) plus keys for whichever
  providers/MCP servers you enable.
* **Ollama** — a running instance only if you use the Ollama provider.
* **`npx` and `uvx`** — required for the MCP subprocesses (provided in the Docker
  image; install Node.js + uv if running bare-metal with MCP enabled).

## Configuration

Two files: `.env` for secrets, `config.yaml` for behavior.

### 1. Secrets — `.env`

```bash
cp .env.example .env
```

Fill in the values. `TELEGRAM_BOT_TOKEN` is **required**; add provider keys
(`GEMINI_API_KEYS`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `NVIDIA_API_KEY`, …)
and MCP keys (`TAVILY_API_KEY`, `NOTION_TOKEN`) only for what you enable. `.env`
is gitignored and must never be committed. See `.env.example` for the full list
and notes (it is the single source of truth for which variables exist).

### 2. Behavior — `config.yaml`

* `storage_backend` — `database` (SQLite, default) or `file`.
* `default_provider` and per-provider default models.
* `custom_openai_providers` — define OpenAI-compatible providers (Groq, NVIDIA,
  …). Each provider's `api_key` resolves from a matching env var.
* `mcp_servers` — the MCP subprocesses to run. Each entry has a `pass_env`
  allowlist controlling which env vars are forwarded to that subprocess (secrets
  are not broadcast to all servers).
* `expert_panel` — panel agents, models, and quality thresholds.
* `generation_idle_timeout_seconds` — inactivity budget for a streaming turn
  (default 180; resets on every token, so slow-but-progressing generations are
  never cut off; `0` disables).
* `recovery` — startup take-over of unanswered messages: `enabled` (default true)
  and `window_seconds` (default 3600).

> **Docker + host services:** to reach a service on the host (e.g. Ollama) from
> inside the container, use `host.docker.internal`, not `localhost` —
> e.g. `OLLAMA_HOST=http://host.docker.internal:11434`.

## Setup (build from source — local / development)

For deploying to a server, pull the prebuilt image instead — see
**[Deployment](#deployment-production)** below.

```bash
git clone https://github.com/simonho0406/llm-middleware-telegram.git
cd llm-middleware-telegram
cp .env.example .env          # then edit .env
docker compose up --build -d  # build locally & run
docker compose logs -f        # view logs
docker compose down           # stop
```

## Deployment (production)

Production hosts **pull a prebuilt image** from GHCR — no building on the server.
GitHub Actions builds and publishes `ghcr.io/simonho0406/llm-middleware-telegram:latest`
(`linux/amd64`) on every push to `main`, with all MCP servers baked in, so nothing is
fetched at runtime.

```bash
# 1. Get the deploy files (compose + config) onto the host
git clone https://github.com/simonho0406/llm-middleware-telegram.git
cd llm-middleware-telegram

# 2. Provide your secrets
cp .env.example .env          # then edit .env with your real tokens/keys

# 3. Pull the prebuilt image and start
docker compose pull
docker compose up -d
docker compose logs -f        # expect providers "Online" + "Application started"
```

**Updating to a new release:**

```bash
docker compose down --remove-orphans   # stop the old container (avoids duplicate pollers)
git pull            # refresh docker-compose.yml / config.yaml
docker compose pull # fetch the latest image
docker compose up -d
docker ps           # confirm exactly ONE container
```

See **[`docs/DEPLOY.md`](docs/DEPLOY.md)** for the full runbook, a symptom→cause cheatsheet,
and the one-token-per-server rule.

Notes:
- The GHCR image is **public**, so no `docker login` is needed. (If you make it private,
  run `docker login ghcr.io` once with a `read:packages` token.)
- `data/` (the SQLite DB) is a mounted volume and **persists** across updates.
- **Do not** use `--build` on servers — `build:` in the compose file is for local dev only.
- For a pinned/rollback deploy, override the tag with an immutable one, e.g.
  `image: ghcr.io/simonho0406/llm-middleware-telegram:sha-<commit>`.

## Usage

| Command | Description |
|---|---|
| `/help` | Show the command menu |
| `/new` | Start a new conversation thread |
| `/reroll` | Regenerate the last AI response |
| `/cancel` | Cancel the current generation |
| `/config` | Manage settings (auto-search, MCP, skills, auto-retry, …) |
| `/search <query>` | Answer using live web search (auto-search also triggers on its own) |
| `/ask_selected <prompt>` | Query several models concurrently |
| `/discuss <prompt>` | Sequential multi-model "Round Table" |
| `/discuss_panel` · `/configure_panel` · `/end_discussion` | Orchestrated expert panel |
| `/context` | View/prune conversation history |
| `/flash <query>` | One-shot query, not saved to history |
| `/provider` · `/model` · `/list_models` · `/set_model` | Provider/model management |
| `/status` | Provider, MCP, and skill health |
| `/threads` · `/rename_thread <name>` · `/delete_thread <id>` | Thread management |

**Panel/edit behavior:** editing a follow-up during a panel discussion cancels the
in-flight generation and restarts the round with your new input; `/cancel` stops it.

## Storage

SQLite at `data/bot_sessions.db` (tables: `chats`, `threads`, `messages`,
`user_settings`, `panel_tasks`). A read-only `conversation_history` view (flat:
`id, chat_id, thread_id, thread_name, role, content, timestamp`) is exposed to the
`sqlite-tools` MCP server so the model can query its own history, scoped to the
current chat and thread.

## Troubleshooting

* **Telegram `NetworkError` / `Bad Gateway`** — transient; the polling loop
  auto-restarts and recreates the connection (on a fresh event loop).
* **`Message is not modified`** — benign; swallowed by the safe-send pipeline.
* **A provider/tool fails mid-turn** — surfaced to the user as a clear error (and
  logged with a traceback); auto-retry can be enabled per chat in `/config`.
* **Stalled generation** — if a stream produces no output for
  `generation_idle_timeout_seconds`, the turn is aborted with a timeout notice.
* **MCP server won't connect** — the MCP servers are baked into the image; ensure the
  relevant `pass_env` keys are set in `.env` (e.g. `NOTION_TOKEN`, `TAVILY_API_KEY`);
  `/status` reports per-server health.

## Contributing

See [`docs/ONBOARDING.md`](docs/ONBOARDING.md) for the architectural pillars and
conventions. Contributions are welcome via issues and pull requests.
