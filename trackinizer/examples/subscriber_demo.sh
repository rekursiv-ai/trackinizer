#!/usr/bin/env bash
#
# subscriber_demo.sh -- subscription push: committed changes arrive as JSON
# envelopes on a subscriber's stdin; the client owns the rendering.
#
#   1. alice holds a live session (`trax run --as alice sh`) around a small
#      stdin loop that parses each envelope into a readable line; being
#      subscribed + live is all it takes.
#   2. bob mutates with plain trax verbs: create an Issue (alice
#      subscribed at birth), link a CodeChange edge, complete it.
#   3. Each commit, the server's push task (server/subscriber.py) drops the
#      change envelope -- metadata plus the `row`/`delta` follow-up commands,
#      never the field values -- into alice's session; her poller types it
#      into the loop's stdin.
#   4. alice renders three events: created, edge_added, status.
#
# Usage:  ./subscriber_demo.sh          (exits green; data is wiped)
#         ./subscriber_demo.sh --raw    (print envelopes as raw JSON lines)

set -euo pipefail

RAW=0
[ "${1:-}" = "--raw" ] && RAW=1

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${TRACKINIZER_PORT:-8768}"
export TRACKINIZER_URL="http://127.0.0.1:${PORT}"
EVENTS="$(mktemp -t subscriber-demo.XXXXXX)"

PIDS=()

source "${HERE}/start_server.sh"  # shared helpers: wait_for, session_live

trax() { uv --quiet run --frozen trax "$@"; }
say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

say "boot an ephemeral trackinizer"
TRACKINIZER_PORT="${PORT}" "${HERE}/start_server.sh" >/dev/null &
PIDS+=($!)
# Kill only processes this script started -- `kill 0` would signal the whole
# process group, including an invoking harness's siblings.
trap 'kill "${PIDS[@]}" 2>/dev/null || true' EXIT
wait_for curl -fsS "${TRACKINIZER_URL}/api/version"

# The child alice's session runs: render each pushed change envelope (JSON
# on stdin) as a readable block -- one header line, then every field the
# envelope carries. Parsing is the CLIENT's job; this is the simplest
# client. Non-envelope lines are dropped (`fromjson?`). With --raw, the
# envelope prints as one raw JSON line instead (RENDER_RAW=1 crosses into
# the PTY child via the environment; shell vars do not survive exec).
render_events() {
    if [ "${RENDER_RAW:-0}" = "1" ]; then
        jq --unbuffered -Rr '
            sub(".*trackinizer: "; "") | fromjson? | "[event] \(tojson)"'
    else
        jq --unbuffered -Rr '
            sub(".*trackinizer: "; "") | fromjson?
            | "[event] \(.actor) \(.kind) \(.subject_kind)",
              (to_entries[] | "  \(.key): \(.value)"),
              ""'
    fi
}

say "alice: subscribe by holding a live session around a stdin renderer"
# ``declare -f`` ships the function's source into the PTY child (a plain
# function cannot cross exec).
RENDER_RAW="${RAW}" \
    trax run --as alice sh -- bash -c "$(declare -f render_events); render_events" \
    </dev/null >"${EVENTS}" 2>/dev/null &
PIDS+=($!)

# Wait for alice's session to be live before mutating (delivery is live-only).
wait_for session_live alice

say "bob: create an Issue (alice subscribed at birth)"
trax issue title to "Retry bug" priority to high subscriber to alice --as bob

say "bob: link a CodeChange to the Issue with a provenance edge"
trax codechange title to "Fix retry backoff" \
    sha to deadbeefdeadbeefdeadbeefdeadbeefdeadbeef --as bob
trax codechange 1 produced_by issue 1 --as bob

say "bob: complete the Issue"
trax issue 1 status to complete --as bob

say "wait for the pushed change events to reach alice"
wait_for grep -Eq 'bob status Issue|"kind":"status"' "${EVENTS}"
grep -A 10 '^\[event\]' "${EVENTS}"

say "done: created, edge_added, and status all pushed"
