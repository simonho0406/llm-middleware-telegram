# Deployment Runbook

Production servers **pull a prebuilt image** from GHCR and run it. They must **never build**
(the build OOM-locks a small VM) and must run **exactly one container per bot token**.

## First-time setup (per server)

```bash
git clone https://github.com/simonho0406/llm-middleware-telegram.git
cd llm-middleware-telegram
cp .env.example .env        # fill in real tokens/keys; each server needs its OWN bot token
```

Optional but recommended on a ≤1 GB box — a host swapfile so a spike degrades instead of freezing:
```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## Deploy / update (per server)

Always stop the old container first — this is the rule that prevents the duplicate-poller outage:

```bash
cd llm-middleware-telegram
docker compose down --remove-orphans     # REQUIRED: kills any stale/old container
git pull                                  # refresh compose + config
docker compose pull                       # fetch the latest prebuilt image (no build)
docker compose up -d
docker ps                                 # MUST show exactly ONE llm-middleware-telegram container
docker compose logs -f                    # verify health (below)
```

> If the local git history diverged (after a history rewrite), use
> `git fetch origin && git reset --hard origin/main` instead of `git pull`.
> Never run `docker compose up --build` on a server.

## Healthy startup looks like

- `Openrouter/Groq/Nvidia/Gemini connection check successful ... Online` (Ollama may be absent — fine).
- On the first user message: `Connected to MCP server 'sqlite-tools' / 'notion-workspace' / 'tavily-search' successfully` then `MCP supervisor: service ready.`
- `Application started`.

MCP connects **lazily** (on the first message) and shuts down after ~30 min idle to free RAM —
"MCP supervisor: service idle ... Will reconnect on next request" is normal, not a failure.

## Symptom → cause cheatsheet

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot unresponsive; "tools don't work"; log repeats `telegram.error.Conflict` | **Two pollers on one token** (stale container, or two servers sharing a token) | `docker compose down --remove-orphans && docker compose up -d`; `docker ps` shows one; ensure each server has its own `TELEGRAM_BOT_TOKEN` |
| Server freezes / loses SSH after deploy | Built on the server (`--build`), or `mem_limit` > host RAM | Use `pull` only; keep `mem_limit` (768m) under host RAM; add swapfile |
| Frequent `429 RESOURCE_EXHAUSTED` (Gemini) | Free-tier quota | More keys, smaller context, or another provider/paid tier |
| Frequent `503 UNAVAILABLE` | Upstream provider overload (transient) | Handled by failover/backoff; usually self-resolves |

## Two servers

Each server **must use a different `TELEGRAM_BOT_TOKEN`** (Telegram allows one poller per token).
Two pollers on the same token produce the Conflict loop above.
