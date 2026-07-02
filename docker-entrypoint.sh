#!/bin/sh
# Start as root only to fix ownership of the host-mounted data dir, then drop to the
# non-root 'appuser' for the actual application. This keeps the long-running process
# (the LLM tool loop + MCP subprocesses — the real RCE surface) unprivileged WITHOUT
# requiring the operator to chown ./data on the host.
set -e

DATA_DIR="/app/data"

# /app/data is a bind mount; its ownership comes from the host. Make appuser able to
# write the SQLite DB (WAL mode requires -wal/-shm sidecar writes even for reads).
#
# IMPORTANT: unlike a previous version of this script, we do NOT silently swallow a
# chown failure. A silenced failure here previously caused the app to boot successfully
# and then fail unpredictably per-command with "attempt to write a readonly database"
# (e.g. on /threads) — a confusing, hard-to-diagnose runtime crash instead of a clear
# startup error. We now log the outcome and verify writability explicitly.
if chown -R appuser:appuser "$DATA_DIR" 2>/tmp/chown_err.log; then
    echo "[entrypoint] chown $DATA_DIR -> appuser: OK"
else
    echo "[entrypoint] WARNING: chown $DATA_DIR -> appuser FAILED:"
    cat /tmp/chown_err.log >&2
fi
rm -f /tmp/chown_err.log
chmod -R u+rwX "$DATA_DIR" 2>/dev/null || echo "[entrypoint] WARNING: chmod $DATA_DIR failed"

# Verify appuser can actually write to DATA_DIR before handing off. If the chown above
# didn't take (e.g. the host directory is owned by a different uid and the bind mount
# doesn't allow chown, or the mount is genuinely read-only), fail LOUD and FAST here —
# rather than booting fine and crashing later on the first /threads or DB write.
PROBE_FILE="$DATA_DIR/.write_probe_$$"
if gosu appuser sh -c "touch '$PROBE_FILE' && rm -f '$PROBE_FILE'" 2>/tmp/probe_err.log; then
    echo "[entrypoint] Writability probe for $DATA_DIR as appuser: OK"
else
    echo "[entrypoint] FATAL: $DATA_DIR is NOT writable by appuser (uid 10001)." >&2
    echo "[entrypoint] This will break the SQLite database (readonly errors on nearly" >&2
    echo "[entrypoint] every command). Fix on the HOST with:" >&2
    echo "[entrypoint]     sudo chown -R 10001:10001 ./data" >&2
    echo "[entrypoint] Probe error was:" >&2
    cat /tmp/probe_err.log >&2
    rm -f /tmp/probe_err.log
    exit 1
fi
rm -f /tmp/probe_err.log

exec gosu appuser "$@"
