#!/usr/bin/env bash
#
# trax_run_claude_demo.sh -- capture: `trax run claude` tails the CLI's own
# session log and syncs each turn to the server as an AgentSession event.
#
#   1. `trax run --as scientist claude -p ...` spawns the real claude binary
#      on a PTY the wrapper owns and drains its session JSONL.
#   2. Each parsed turn is POSTed to /api/sessions/<id>/events, so the
#      transcript is readable over HTTP *while the run is still going*.
#   3. This script polls that endpoint and prints every new event as it
#      lands -- kind, then whatever the typed message carries.
#   4. When claude exits the wrapper stamps `ended`; the poller drains the
#      tail and stops.
#
# Usage:  ./trax_run_claude_demo.sh          (exits green; data is wiped)
#         ./trax_run_claude_demo.sh --raw    (print events as raw JSON lines)

set -euo pipefail

RAW=0
[ "${1:-}" = "--raw" ] && RAW=1

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${TRACKINIZER_PORT:-8770}"
export TRACKINIZER_URL="http://127.0.0.1:${PORT}"
KINDS="$(mktemp -t trax-run-claude-demo.XXXXXX)"
PIDS=()

command -v claude >/dev/null || { echo "claude not on PATH" >&2; exit 1; }

# shared helpers: wait_for, session_live/_id/_ended, follow_session,
# assert_captured
source "${HERE}/start_server.sh"

trax() { uv --quiet run --frozen trax "$@"; }
say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

say "boot an ephemeral trackinizer"
TRACKINIZER_PORT="${PORT}" "${HERE}/start_server.sh" >/dev/null &
PIDS+=($!)
# Kill only processes this script started -- `kill 0` would signal the whole
# process group, including an invoking harness's siblings.
trap 'kill "${PIDS[@]}" 2>/dev/null || true' EXIT
wait_for curl -fsS "${TRACKINIZER_URL}/api/version"

say "scientist: wrap a real claude turn (one tool call, then a word)"
T0="$(date +%s.%N)"
# The prompt must sit immediately after `-p`: with a flag in between, claude
# stops treating it as the print prompt and exits "Input must be provided".
trax run --as scientist claude -- \
    -p 'Run the bash command: echo hello from claude. Then reply with just DONE.' \
    --output-format stream-json --verbose \
    --allowedTools 'Bash(echo:*)' \
    </dev/null >/dev/null 2>&1 &
PIDS+=($!)

say "wait for the session row, then stream its events as they land"
wait_for session_live scientist
SID="$(session_id scientist)"
echo "session ${SID}"
follow_session "${SID}"

# The prompt drives a tool call and a reply, so a healthy capture carries the
# whole round trip: prompt in, tool call out, tool output back, model's answer.
assert_captured UserMessage AssistantMessage ToolResult

say "done: the whole transcript arrived over HTTP while claude ran"
