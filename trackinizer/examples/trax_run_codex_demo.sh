#!/usr/bin/env bash
#
# trax_run_codex_demo.sh -- capture: `trax run codex` tails the CLI's own
# rollout log and syncs each turn to the server as an AgentSession event.
#
# Same shape as trax_run_claude_demo.sh, against a different CLI -- which is
# the point: the adapter absorbs the format difference (claude writes one
# JSONL per session under ~/.claude/projects; codex writes a Y/M/D-sharded
# rollout under ~/.codex/sessions) and the server sees identical typed
# events either way. The rendering and assertions here are the SAME shared
# helpers the claude demo uses; only the spawn line differs.
#
#   1. `trax run --as scientist codex exec ...` spawns the real codex binary
#      on a PTY the wrapper owns and drains its rollout JSONL.
#   2. Each parsed turn is POSTed to /api/sessions/<id>/events, so the
#      transcript is readable over HTTP *while the run is still going*.
#   3. This script pages that endpoint and prints every new event as it
#      lands, stamped with its ARRIVAL time.
#   4. When codex exits the wrapper stamps `ended`; the poller drains the
#      tail and stops.
#
# Usage:  ./trax_run_codex_demo.sh          (exits green; data is wiped)
#         ./trax_run_codex_demo.sh --raw    (print events as raw JSON lines)

set -euo pipefail

RAW=0
[ "${1:-}" = "--raw" ] && RAW=1
export RAW

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${TRACKINIZER_PORT:-8771}"
export TRACKINIZER_URL="http://127.0.0.1:${PORT}"
KINDS="$(mktemp -t trax-run-codex-demo.XXXXXX)"
PIDS=()

command -v codex >/dev/null || { echo "codex not on PATH" >&2; exit 1; }

# shared helpers: wait_for, session_live/_id/_ended, render_events,
# follow_session, assert_captured
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

say "scientist: wrap a real codex turn (one shell call, then a word)"
T0="$(date +%s.%N)"
# ``exec`` is codex's non-interactive mode. ``model_reasoning_summary=detailed``
# is what populates a reasoning item's ``summary[].text``; without it the
# adapter captures the turn but its thinking is empty (see adapters/codex.py).
# ``--skip-git-repo-check`` keeps the run working from any cwd.
trax run --as scientist codex -- \
    exec --skip-git-repo-check \
    -c model_reasoning_summary=detailed \
    'Run the shell command: echo hello from codex. Then reply with just DONE.' \
    </dev/null >/dev/null 2>&1 &
PIDS+=($!)

say "wait for the session row, then stream its events as they land"
wait_for session_live scientist
SID="$(session_id scientist)"
echo "session ${SID}"
follow_session "${SID}"

# Codex opens its session with a SystemMessage (the ``session_meta`` line's
# base instructions) before any turn, so a healthy capture carries that plus
# the prompt and the model's reply.
assert_captured SystemMessage UserMessage AssistantMessage

say "done: the whole transcript arrived over HTTP while codex ran"
