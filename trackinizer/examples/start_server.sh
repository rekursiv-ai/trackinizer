#!/usr/bin/env bash
#
# Boot ONE ephemeral trackinizer (pglite, --no-auth) and print its URL when
# ready. Foreground; Ctrl-C shuts down and wipes the data. Same boot/teardown
# shape as example.sh / docs/trax_research_example_3.sh.
#
# Usage:
#   ./start_server.sh &                # prints TRACKINIZER_URL=... when ready
#   TRACKINIZER_PORT=8768 ./start_server.sh &
#
# Or SOURCE it for the shared demo helpers (no server started):
#   source "${HERE}/start_server.sh"   # wait_for, session_live

# Shared helpers for the demo scripts (available when sourced).
#
# wait_for CMD...: retry a condition ~60s, else dump ${EVENTS} (if the caller
# set it) and fail. An unbounded `until` would hang a demo forever on a
# regression.
wait_for() {
    for _ in $(seq 1 120); do "$@" >/dev/null 2>&1 && return 0; sleep 0.5; done
    echo "TIMEOUT waiting for: $*" >&2
    tail -10 "${EVENTS:-/dev/null}" >&2 || true
    exit 1
}

# session_live NAME: whether a live AgentSession owned by NAME exists.
session_live() {
    curl -fsS "${TRACKINIZER_URL}/api/inquiries?kind=AgentSession&limit=10" \
        | grep -q "\"owner\": *\"$1\""
}



# Sourced for the helpers above: stop before the server-boot half.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
    return 0
fi

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRV="$(cd "${HERE}/.." && pwd)"
PORT="${TRACKINIZER_PORT:-8769}"
HOST="127.0.0.1"
URL="http://${HOST}:${PORT}"
# The .XXXXXX template is required by GNU mktemp; macOS appends its own
# randomness after it (a literal XXXXXX in the name), which is harmless.
LOG="$(mktemp -t trackinizer-examples.XXXXXX)"

# Refuse to start if the port is already bound: a leftover server from a
# crashed run would pass the readiness probe below and then 500 on the first
# write. Fail loudly instead of talking to someone else's server.
if curl -fsS "${URL}/openapi.json" >/dev/null 2>&1; then
    echo "[server] ERROR: ${URL} is already serving (a stale trackinizer?)." >&2
    echo "[server] Kill it or set TRACKINIZER_PORT to a free port, then retry." >&2
    exit 1
fi

echo "[server] starting trackinizer --ephemeral on ${URL} (log: ${LOG})" >&2
# setsid detaches the server from the terminal so Ctrl-C is steered through
# the cleanup trap (Linux-only; plain launch on macOS). py_pglite spawns Node
# in its own session, so teardown walks the descendant tree by ppid.
SETSID="$(command -v setsid || true)"
${SETSID} uv --quiet --project "${SRV}" run --frozen --no-sync python3 \
    -m trackinizer.server \
    --ephemeral --no-auth --log-level INFO --host "${HOST}" --port "${PORT}" \
    >"${LOG}" 2>&1 &
SERVER_PID=$!

_descendants() {
    local pid=$1 kid
    for kid in $(pgrep -P "$pid" 2>/dev/null); do
        _descendants "$kid"
        echo "$kid"
    done
}

cleanup() {
    trap - EXIT
    trap '' INT TERM
    echo "[server] shutting down (pid ${SERVER_PID}); data is wiped" >&2
    # Snapshot descendants BEFORE killing the parent: PGlite's Node runs in
    # its own session, so once the parent dies it reparents to init and
    # pgrep -P can no longer find it (it would squat the port next run).
    local kids
    kids="$(_descendants "$SERVER_PID") $SERVER_PID"
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    for _ in $(seq 1 30); do
        kill -0 "$SERVER_PID" 2>/dev/null || break
        sleep 0.1
    done
    kill -KILL $kids 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
}
trap 'cleanup; exit 130' INT TERM
trap cleanup EXIT

for _ in $(seq 1 60); do
    kill -0 "${SERVER_PID}" 2>/dev/null || break
    curl -fsS "${URL}/openapi.json" >/dev/null 2>&1 && break
    sleep 0.5
done
if ! curl -fsS "${URL}/openapi.json" >/dev/null 2>&1; then
    echo "[server] failed to come up; tail of ${LOG}:" >&2
    tail -30 "${LOG}" >&2
    exit 1
fi

echo "TRACKINIZER_URL=${URL}"
wait "${SERVER_PID}"
