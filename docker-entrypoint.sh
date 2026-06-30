#!/bin/sh
# Start as root only to fix ownership of the host-mounted data dir, then drop to the
# non-root 'appuser' for the actual application. This keeps the long-running process
# (the LLM tool loop + MCP subprocesses — the real RCE surface) unprivileged WITHOUT
# requiring the operator to chown ./data on the host.
set -e

# /app/data is a bind mount; its ownership comes from the host. Make appuser able to
# write the SQLite DB. Best-effort: if chown fails (e.g. read-only mount), continue.
chown -R appuser:appuser /app/data 2>/dev/null || true

exec gosu appuser "$@"
