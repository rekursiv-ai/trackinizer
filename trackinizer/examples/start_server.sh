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

# session_id NAME: the AgentSession id owned by NAME, empty until it opens.
session_id() {
    curl -fsS "${TRACKINIZER_URL}/api/inquiries?kind=AgentSession&limit=10" \
        | jq -r --arg owner "$1" 'map(select(.owner == $owner)) | .[0].id // empty'
}

# session_ended ID: whether the wrapper has closed the session out.
session_ended() {
    curl -fsS "${TRACKINIZER_URL}/api/inquiries/${1}" | jq -e '.ended != null' >/dev/null
}

# render_events: turn one page of events (JSON on stdin) into readable lines.
# Parsing is the CLIENT's job -- the server hands back the typed message
# verbatim -- and the shape is the same for every adapter, so one renderer
# covers claude, codex, and anything else `trax run` wraps. ${RAW}=1 prints
# raw JSON instead; ${T0} (a `date +%s.%N` stamp) makes each line report
# ARRIVAL time, which is what shows a turn is readable mid-run.
render_events() {
    if [ "${RAW:-0}" = "1" ]; then
        jq -r '.events[] | "[event] \(tojson)"'
        return
    fi
    jq -r --arg t "$(printf '%.1f' "$(echo "$(date +%s.%N) - ${T0}" | bc)")" '
        def clip: gsub("\\s+"; " ") | if length > 160 then .[0:160] + "..." else . end;
        .events[]
        | "[+\($t)s] [\(.seq)] \(.kind)"
          + (if (.message.thinking // "") != ""
             then "\n      thinking: " + (.message.thinking | clip) else "" end)
          + (if (.message.text // "") != ""
             then "\n      text: " + (.message.text | clip) else "" end)
          + (if (.message.tool_calls // []) | length > 0
             then "\n      calls: "
                  + (.message.tool_calls
                     | map("\(.name)(\(.args | tojson | .[0:80]))") | join(", "))
             else "" end)
          + (if (.message.content // "") != ""
             then "\n      result: " + (.message.content | clip) else "" end)
          + (if (.message.raw.type // "") != ""
             then "\n      raw type: " + .message.raw.type else "" end)'
}

# follow_session ID: page the event feed from the start, printing each new
# event, until the session ends AND a page comes back empty. Offset-paging
# (not a tail) is what makes this exactly-once: the server orders by `seq`.
# Renders through `render_events` and records every kind seen in ${KINDS}
# for `assert_captured`.
#
# Bounded by an IDLE deadline, not a total one: a model turn legitimately
# takes minutes, so a wall-clock cap would kill a healthy run, but a wrapped
# CLI that dies without closing its session (or a server that never stamps
# `ended`) leaves the feed silent forever. Every arriving event resets the
# clock; ${FOLLOW_IDLE_SEC} seconds with no event AND no `ended` is a hang,
# reported and failed rather than waited on. Same stance as `wait_for`.
follow_session() {
    local sid="$1" offset=0 page count ended=0
    local idle_limit="${FOLLOW_IDLE_SEC:-180}" idle=0
    while :; do
        # The deadline is checked FIRST, so every path through the loop is
        # bounded by it -- an empty page and an unreachable endpoint alike.
        # A check placed after a branch that ``continue``s is not a deadline:
        # the failing path skips it on every retry and polls forever.
        if [ "$((idle / 2))" -ge "${idle_limit}" ]; then
            echo "FAIL: no events for ${idle_limit}s and session ${sid} never" \
                "ended; captured: $(sort -u "${KINDS}" | tr '\n' ' ')" >&2
            exit 1
        fi
        page="$(curl -fsS \
            "${TRACKINIZER_URL}/api/sessions/${sid}/events?limit=100&offset=${offset}" \
            2>/dev/null)" || { sleep 0.5; idle=$((idle + 1)); continue; }
        count="$(jq '.events | length' <<<"${page}")"
        if [ "${count}" -gt 0 ]; then
            render_events <<<"${page}"
            jq -r '.events[].kind' <<<"${page}" >>"${KINDS}"
            offset=$((offset + count))
            idle=0
            continue  # a full page may mean more is already waiting
        fi
        [ "${ended}" = "1" ] && return 0
        session_ended "${sid}" && ended=1  # one more empty page, then stop
        sleep 0.5
        idle=$((idle + 1))
    done
}

# assert_captured KIND...: fail unless every KIND reached the server.
# A capture demo that captures nothing must exit RED: without this, a run
# whose CLI died before its first turn printed a session id, zero events,
# and still exited 0 -- green on a total capture failure.
assert_captured() {
    local kind
    for kind in "$@"; do
        grep -qx "${kind}" "${KINDS}" && continue
        echo "FAIL: no ${kind} captured; got: $(sort -u "${KINDS}" | tr '\n' ' ')" >&2
        exit 1
    done
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
