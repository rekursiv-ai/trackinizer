#!/usr/bin/env bash
#
# trax_run_sh_demo.sh -- session IO with `trax run sh`: any program becomes
# addressable through its own stdin/stdout.
#
#   1. alice and bob each hold a live session (`trax run --as NAME sh`):
#      a session row makes them @alice/@bob; pollers feed their stdin.
#   2. bob sends `trax send @alice "hello alice"`; alice's poller types
#      it into her script's stdin (~1s).
#   3. alice's script reacts to the line by replying: `trax send @bob
#      "hi bob, got your message"`.
#   4. The reply crosses the same path back and bob's printer prints it.
#
# Usage:  ./trax_run_sh_demo.sh        (exits green; data is wiped)

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${TRACKINIZER_PORT:-8769}"
export TRACKINIZER_URL="http://127.0.0.1:${PORT}"
ALICE_OUT="$(mktemp -t trax-run-sh-demo-alice.XXXXXX)"
BOB_OUT="$(mktemp -t trax-run-sh-demo-bob.XXXXXX)"
PIDS=()

# The PTY child inherits PATH, not shell functions, so `trax` must be a
# real executable for alice's inner script to call it.
export PATH="$(cd "${HERE}/../../.." && pwd)/bin:${PATH}"

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

# The child a session runs: print each stdin line tagged with PREFIX, then
# run the given command (any trax verb -- a reply, an edit, a submit) once
# per message. No command = a plain printer.
echo_and_run() {
    local prefix="$1"
    shift
    while IFS= read -r line; do
        printf '%s %s\n' "${prefix}" "${line}"
        if [ "$#" -gt 0 ]; then "$@"; fi
    done
}

say "alice: a live session that echoes its stdin AND replies to @bob"
# ``declare -f`` ships the function's source into the PTY child (a plain
# function cannot cross exec).
trax run --as alice sh -- bash -c \
    "$(declare -f echo_and_run); echo_and_run '[alice got]' trax send @bob 'hi, got your message'" \
    </dev/null >"${ALICE_OUT}" 2>/dev/null &
PIDS+=($!)

say "bob: a live session around a plain stdin printer"
trax run --as bob sh -- bash -c \
    "$(declare -f echo_and_run); echo_and_run '[bob got]'" \
    </dev/null >"${BOB_OUT}" 2>/dev/null &
PIDS+=($!)

# Wait for both sessions to be live before sending (drop-if-absent).
wait_for session_live alice
wait_for session_live bob

say "kick off: send one message to @alice"
trax send @alice "hello alice"

say "wait for the round trip: to alice, her reply back to bob"
wait_for grep -q "got your message" "${BOB_OUT}"
grep '\[alice got\]' "${ALICE_OUT}"
grep '\[bob got\]' "${BOB_OUT}"

say "done: message delivered, reply received"
