#!/usr/bin/env bash
#
# Local pre-flight gate. Runs the REAL production image under production-shaped conditions
# (linux/amd64, non-root, 768m/1.5cpu, mounted config, real API keys) on your dev box,
# BEFORE pushing — to catch the environment / performance / observability bugs that unit
# tests and a build-only CI structurally cannot see.
#
# Why local (not CI): only your machine has the real API keys + a real Docker engine + a
# human to judge output. This is the thing CI can't be.
#
# Usage:   ./scripts/preflight.sh
# Requires: docker, a populated .env. Optional: PREFLIGHT_BOT_TOKEN in env for the live-boot
#           check (a SPARE Telegram bot token — never Azure/Oracle's, to avoid a poller Conflict).
#
# Almost every check runs as a one-off `docker run` (uses provider keys, does NOT poll
# Telegram) so it can't conflict with the live bots. Only the optional live-boot check polls,
# and only with the spare token.
set -uo pipefail
cd "$(dirname "$0")/.."

IMAGE="llm-mw-preflight:local"
PLATFORM="linux/amd64"
DATA_DIR="/tmp/preflight_data"
LOGDIR="/tmp/preflight_logs"
FAILURES=0
WARNINGS=0

c_bold=$'\033[1m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_rst=$'\033[0m'
step(){ printf '\n%s[preflight] %s%s\n' "$c_bold" "$*" "$c_rst"; }
pass(){ printf '%s  PASS%s %s\n' "$c_grn" "$c_rst" "$*"; }
fail(){ printf '%s  FAIL%s %s\n' "$c_red" "$c_rst" "$*"; FAILURES=$((FAILURES+1)); }
warn(){ printf '%s  WARN%s %s\n' "$c_yel" "$c_rst" "$*"; WARNINGS=$((WARNINGS+1)); }

PROD_RUN=(docker run --rm --platform "$PLATFORM" --memory=768m --memory-swap=768m --cpus=1.5
          --security-opt no-new-privileges:true)
CFG_MOUNT=(-v "$PWD/config.yaml:/app/config.yaml:ro")

# ── 0. Preconditions + build ────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || { echo "docker not found"; exit 2; }
[ -f .env ] || { echo ".env missing — copy .env.example and fill in real keys"; exit 2; }
mkdir -p "$LOGDIR"; rm -f "$LOGDIR"/*.log

step "Building production image (linux/amd64)…"
# Clear __pycache__ first: stale .pyc under a cloud-synced dir can hang docker's build-context
# transfer (learned the hard way).
find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} + 2>/dev/null
if ! docker build --platform "$PLATFORM" --build-arg CACHEBUST="$(date +%s)" -t "$IMAGE" . > "$LOGDIR/build.log" 2>&1; then
    echo "Image build FAILED — see $LOGDIR/build.log"; tail -15 "$LOGDIR/build.log"; exit 2
fi
pass "image built"

# Fresh, writable data dir (also exercises the brand-new-server 'empty ./data' path).
rm -rf "$DATA_DIR"; mkdir -p "$DATA_DIR"

# ── 1. Adversarial environment parity ───────────────────────────────────────────
step "Check 1a — read-only data dir must fail LOUD at boot (Fix A)"
if "${PROD_RUN[@]}" "${CFG_MOUNT[@]}" -v "$DATA_DIR:/app/data:ro" --env-file .env \
     "$IMAGE" python -c "print('should not reach')" > "$LOGDIR/ro.log" 2>&1; then
    fail "container booted on a read-only data dir (should have exited 1)"
elif grep -q "NOT writable by appuser" "$LOGDIR/ro.log"; then
    pass "read-only data dir → loud, actionable failure + non-zero exit"
else
    fail "failed, but without the actionable message — see $LOGDIR/ro.log"
fi

step "Check 1b — quoted/CRLF .env values must still authenticate (get_env strips them)"
# Fabricate the quoted+CRLF .env from a CLEAN baseline (dotenv strips whatever quoting the
# source file already has), so we apply EXACTLY one quote layer regardless of how the local
# .env happens to be formatted — this is the true production 'env_file with quoted values' case.
python3 - <<'PY'
from dotenv import dotenv_values
v = dotenv_values('.env')
with open('/tmp/preflight_quoted.env', 'w', newline='') as f:
    for k, val in v.items():
        if val is not None:
            f.write(f'{k}="{val}"\r\n')   # one quote layer + CRLF, the exact Docker-passes-literally case
PY
if "${PROD_RUN[@]}" "${CFG_MOUNT[@]}" -v "$DATA_DIR:/app/data" --env-file /tmp/preflight_quoted.env \
     "$IMAGE" python -c "import config,sys; t=config.TELEGRAM_BOT_TOKEN or ''; sys.exit(0 if t and t[:1] not in (chr(34), chr(39)) else 1)" > "$LOGDIR/quoted.log" 2>&1; then
    pass "quoted .env values stripped correctly (no literal quotes leak into keys)"
else
    fail "quoted .env not handled — keys would be corrupted on a quoted-.env host"
fi
rm -f /tmp/preflight_quoted.env

# ── 2. Functional smoke with real providers (human judges quality) ──────────────
step "Check 2 — functional smoke (e2e + panel) with real keys, inside the prod image"
"${PROD_RUN[@]}" "${CFG_MOUNT[@]}" -v "$DATA_DIR:/app/data" --env-file .env \
     "$IMAGE" python scripts/e2e_qa.py 2>&1 | tee "$LOGDIR/e2e.log"
if grep -qE "[1-9][0-9]*/[0-9]+ cases passed" "$LOGDIR/e2e.log" && ! grep -q "✗ FAIL" "$LOGDIR/e2e.log"; then
    pass "e2e_qa: all cases passed"
else
    fail "e2e_qa reported a failing case — review $LOGDIR/e2e.log"
fi
if [ "${PREFLIGHT_SKIP_PANEL:-0}" != "1" ]; then
    step "Check 2b — panel pipeline (slow: multi-round, real MCP)."
    "${PROD_RUN[@]}" "${CFG_MOUNT[@]}" -v "$DATA_DIR:/app/data" --env-file .env \
         "$IMAGE" python scripts/panel_qa.py 2>&1 | tee "$LOGDIR/panel.log"
    if grep -q "quality gate parsing failed persistently" "$LOGDIR/panel.log"; then
        fail "panel quality gate aborted (Fix C regression) — review $LOGDIR/panel.log"
    else
        pass "panel completed without a quality-gate abort (human: review answer quality above)"
    fi
else
    warn "panel check skipped (PREFLIGHT_SKIP_PANEL=1)"
fi

# ── 3. Load / OOM soak under the production memory limit ─────────────────────────
step "Check 3 — concurrent load soak under --memory=768m (the 'don't break the VM' check)"
SOAK_NAME="preflight_soak_$$"
docker run --name "$SOAK_NAME" --platform "$PLATFORM" --memory=768m --memory-swap=768m --cpus=1.5 \
    --security-opt no-new-privileges:true "${CFG_MOUNT[@]}" -v "$DATA_DIR:/app/data" --env-file .env \
    "$IMAGE" python scripts/load_soak.py > "$LOGDIR/soak.log" 2>&1
SOAK_RC=$?
OOM=$(docker inspect --format '{{.State.OOMKilled}}' "$SOAK_NAME" 2>/dev/null || echo "unknown")
docker rm -f "$SOAK_NAME" >/dev/null 2>&1
grep -E "SOAK_SUMMARY|CRASHED|OOM" "$LOGDIR/soak.log" | tail -5 || true
if [ "$OOM" = "true" ]; then
    fail "container was OOM-KILLED under 768m — would crash the VM under this load"
elif [ "$SOAK_RC" -ne 0 ]; then
    fail "load soak exited $SOAK_RC (a generation crashed) — review $LOGDIR/soak.log"
else
    pass "survived concurrent load under 768m, no OOM, no crash (OOMKilled=$OOM)"
fi

# ── 0b. Optional live-boot parity (polls; SPARE token only) ─────────────────────
if [ -n "${PREFLIGHT_BOT_TOKEN:-}" ]; then
    step "Check 0 — live boot with the SPARE token (real polling/auth/startup path)"
    BOOT_NAME="preflight_boot_$$"
    docker run -d --name "$BOOT_NAME" --platform "$PLATFORM" --memory=768m --memory-swap=768m --cpus=1.5 \
        --security-opt no-new-privileges:true "${CFG_MOUNT[@]}" -v "$DATA_DIR:/app/data" \
        --env-file .env -e "TELEGRAM_BOT_TOKEN=$PREFLIGHT_BOT_TOKEN" "$IMAGE" >/dev/null 2>&1
    sleep 40
    docker logs "$BOOT_NAME" > "$LOGDIR/boot.log" 2>&1
    docker rm -f "$BOOT_NAME" >/dev/null 2>&1
    if grep -q "Application started" "$LOGDIR/boot.log" \
       && ! grep -qE "readonly database|Access denied for ALL|telegram.error.Conflict|Traceback" "$LOGDIR/boot.log"; then
        pass "live boot healthy (Application started, no readonly/Conflict/denied/traceback)"
    else
        fail "live boot unhealthy — review $LOGDIR/boot.log"
    fi
else
    warn "Check 0 (live boot) skipped — set PREFLIGHT_BOT_TOKEN (a SPARE token) to enable"
fi

# ── 4. Observability summary + go/no-go ─────────────────────────────────────────
step "Observability sweep — error signatures across all captured logs"
SIGS=$(grep -rhoE "readonly database|Access denied for ALL|telegram.error.Conflict|RESOURCE_EXHAUSTED|Traceback \(most recent call last\)" "$LOGDIR"/*.log 2>/dev/null | sort | uniq -c | sort -rn)
[ -n "$SIGS" ] && printf '%s\n' "$SIGS" || echo "  (none)"

echo
printf '%s══════════ PRE-FLIGHT VERDICT ══════════%s\n' "$c_bold" "$c_rst"
printf 'Logs: %s\n' "$LOGDIR"
if [ "$FAILURES" -eq 0 ]; then
    printf '%sGO%s — 0 failures, %d warning(s). (Still eyeball the panel/e2e answers above for quality.)\n' "$c_grn" "$c_rst" "$WARNINGS"
    # cleanup image + data on success
    docker rmi "$IMAGE" >/dev/null 2>&1; rm -rf "$DATA_DIR"
    exit 0
else
    printf '%sNO-GO%s — %d failure(s), %d warning(s). Do NOT push. See %s. (image %s kept for debugging)\n' "$c_red" "$c_rst" "$FAILURES" "$WARNINGS" "$LOGDIR" "$IMAGE"
    exit 1
fi
