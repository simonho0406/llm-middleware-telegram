# Deployment Runbook

Production servers **pull a prebuilt image** from GHCR and run it. They must **never build**
(the build OOM-locks a small VM) and must run **exactly one container per bot token**.

## Pre-flight (run on your dev box BEFORE pushing)

```bash
./scripts/preflight.sh
```

CI only builds the image — it runs no tests and can't hold API keys, so it cannot catch the
failures that actually bite in production (they live in the gap between the dev and prod
*environments*: uid/permissions, the container's env-var parser, seeded state, load/OOM,
adversarial model output). Pre-flight closes that gap by running the **real production image
under production conditions** on your machine — non-root, `768m`/`1.5cpu`, mounted config,
**real API keys** — and prints a `GO` / `NO-GO` verdict. It:

- **1a** mounts `./data` read-only → asserts the container fails **loud** at boot (not a
  silent per-command crash) — the readonly-DB class.
- **1b** feeds a quoted/CRLF `.env` → asserts keys still authenticate — the env-parsing class.
- **2** runs `e2e_qa` + `panel_qa` with real providers inside the image — **you judge answer
  quality** from the printed output.
- **3** fires concurrent generations under `--memory=768m` and checks `OOMKilled` + crashes —
  the "don't spike load and break the VM" check.
- **0** (optional) boots live on a **spare** token (set `PREFLIGHT_BOT_TOKEN`) to exercise the
  real polling/auth/startup path. Never use Azure/Oracle's token here — it would Conflict.
- **4** sweeps all captured logs for known error signatures and prints the verdict.

Everything except check 0 runs as one-off `docker run` (uses provider keys, **never polls
Telegram**), so it can't conflict with the live bots. `NO-GO` (non-zero exit) means don't push.
Fast pass for iteration: `PREFLIGHT_SKIP_PANEL=1 ./scripts/preflight.sh` (skips the slow panel).

## First-time setup (per server)

```bash
git clone https://github.com/simonho0406/llm-middleware-telegram.git
cd llm-middleware-telegram
cp .env.example .env        # fill in real tokens/keys; each server needs its OWN bot token
mkdir -p data
sudo chown -R 10001:10001 data   # the container runs as non-root uid 10001; ./data must be
                                   # writable by that uid or the SQLite DB (WAL mode) fails
                                   # with "attempt to write a readonly database"
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
| `sqlite3.OperationalError: attempt to write a readonly database`, `/threads` (or anything) crashes; entrypoint logs `FATAL: ... not writable by appuser` | The container's non-root user (uid 10001) doesn't own the bind-mounted `./data` on this host | `sudo chown -R 10001:10001 ./data` then `docker compose up -d`. The container refuses to start until this is fixed (fails loud at boot instead of crashing per-command). |

## Two servers

Each server **must use a different `TELEGRAM_BOT_TOKEN`** (Telegram allows one poller per token).
Two pollers on the same token produce the Conflict loop above.
