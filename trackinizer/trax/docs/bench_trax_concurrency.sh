#!/bin/bash
# Latency deciles for N concurrent `trax` invocations, attributed by tier.
#
# Two paths are timed against the same server:
#
#   CLI    -- full `trax ...`: process start, traxd hop, server round trip.
#   HTTP   -- one authenticated GET via curl: server round trip only.
#
# Timing only the CLI conflates three tiers and sends you optimizing the
# wrong one. The split says directly whether the server or the client path
# is the ceiling. Server-side auth used to dominate: scrypt ran per request
# and capped throughput near 110 req/s regardless of core count; a verified
# -bearer cache moved that into the thousands. A regression shows up as HTTP
# deciles spreading while the serial floor stays flat.
#
# Usage:
#   ./bench_trax_concurrency.sh [-n RUNS] [-p PARALLEL] [--no-http] [-- TRAX_ARGS...]
#
# Examples:
#   ./bench_trax_concurrency.sh
#   ./bench_trax_concurrency.sh -n 1000 -p 250
#   ./bench_trax_concurrency.sh -- --profile origin issue status is active
#
# Example Output
# $ ./bench_trax_concurrency.sh
# warming daemon + connection pool...
# unloaded floor:  CLI=81.0 ms   HTTP=1.5 ms   CLI overhead=79.6 ms
#
# running 500 invocations at concurrency 500
#
#   CLI  (trax: process start + traxd + server)
#     min        123.0 ms
#     D1         563.0 ms
#     D2         694.0 ms
#     D3         764.0 ms
#     D4         828.0 ms
#     D5         896.0 ms
#     D6         982.0 ms
#     D7        1091.0 ms
#     D8        1196.0 ms
#     D9        1326.0 ms
#     max       1456.0 ms
#     mean       913.1 ms
#
#     floor W0=81.0 ms   queue wait=832.1 ms (91% of W)
#     λ=303.8/s   W=913.1 ms   L=λW=277.4 in flight   failures=0/500
#
#   HTTP (server round trip only)
#     min          0.9 ms
#     D1           1.2 ms
#     D2           1.3 ms
#     D3           1.4 ms
#     D4           1.4 ms
#     D5           1.5 ms
#     D6           1.6 ms
#     D7           1.7 ms
#     D8           1.8 ms
#     D9           2.0 ms
#     max         17.0 ms
#     mean         1.6 ms
#
#     floor W0=1.4 ms   queue wait=0.1 ms (9% of W)
#     λ=1773.0/s   W=1.6 ms   L=λW=2.8 in flight   failures=0/500
#
# LITTLE'S LAW   L = λW   (an identity, not a score)
#   L  items in flight     λ  throughput req/s     W  latency s/req
#
#   CLI    λ=   303.8/s
#   HTTP   λ=  1773.0/s
#
#   BOTTLENECK: the CLIENT path. The server sustains 1773/s but trax
#               delivers 304/s -- 5.8x less. The 79.6 ms unloaded gap
#               (CLI 81.0 ms vs HTTP 1.4 ms) is process start plus the
#               traxd hop; that is where the next win is, not the server.
#
#   TARGETS for 500 concurrent callers:
#     1. λ_max >= peak arrival rate. Server is at 1773/s; 500 callers
#        each issuing one request every T seconds need 500/T per second,
#        so T > 0.28s keeps you inside capacity with no queueing.
#     2. A latency budget at a decile you name (say D9), alerted on.
#        Not the mean -- the mean hides the tail that users feel.
#     3. NOT L/P. It falls as the system gets faster.

set -euo pipefail

RUNS=500
PARALLEL=500
WITH_HTTP=1
PROFILE=localhost
TRAX_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) RUNS="$2"; shift 2 ;;
    -p) PARALLEL="$2"; shift 2 ;;
    --no-http) WITH_HTTP=0; shift ;;
    --) shift; TRAX_ARGS=("$@"); break ;;
    -h|--help) sed -n '2,23p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ ${#TRAX_ARGS[@]} -eq 0 ]]; then
  TRAX_ARGS=(--profile "$PROFILE" issue account is "$(whoami)@rekursiv.ai" status is active)
fi

command -v trax >/dev/null || { echo "trax not on PATH" >&2; exit 1; }

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

# Resolve the profile the run targets so the HTTP tier hits the same server.
for i in "${!TRAX_ARGS[@]}"; do
  [[ ${TRAX_ARGS[$i]} == "--profile" ]] && PROFILE="${TRAX_ARGS[$((i + 1))]}"
done
PROFILE_FILE="$(python3 -c 'from trackinizer.lib.userdirs import config_dir; print(config_dir())' \
  2>/dev/null || echo "$HOME/.config")/rekursiv-ai/trax/profiles/$PROFILE"
BASE_URL=$(awk -F= '/^url=/{print $2}' "$PROFILE_FILE" 2>/dev/null || true)
API_KEY=$(awk -F= '/^api_key=/{print $2}' "$PROFILE_FILE" 2>/dev/null || true)
if [[ -z $BASE_URL || -z $API_KEY ]]; then
  [[ $WITH_HTTP -eq 1 ]] && echo "note: no url/api_key in $PROFILE_FILE; skipping HTTP tier"
  WITH_HTTP=0
fi

# One timed invocation per tier. Written to files rather than inlined because
# xargs needs a command it can exec per input line.
cat > "$WORKDIR/cli.sh" <<'INNER'
#!/bin/bash
TIMEFORMAT=%R
seconds=$({ time trax "$@" >/dev/null 2>&1; } 2>&1)
echo "$seconds $?"
INNER

cat > "$WORKDIR/http.sh" <<'INNER'
#!/bin/bash
# %{time_total} is curl's own measurement, so process start is excluded --
# which is the point: this tier is meant to isolate the server.
code=$(curl -s -o /dev/null -w "%{time_total} %{http_code}" \
  -H "Authorization: Bearer $2" "$1/api/me/profile")
read -r seconds status <<<"$code"
[[ $status == 200 ]] && echo "$seconds 0" || echo "$seconds 1"
INNER
chmod +x "$WORKDIR/cli.sh" "$WORKDIR/http.sh"

# Report deciles plus the derived quantities for one tier's samples.
# $1 label, $2 samples file ("seconds exit" per line), $3 wall, $4 floor.
report_tier() {
  local label=$1 file=$2 wall=$3 floor=$4
  local failures
  failures=$(awk '$2 != 0' "$file" | wc -l)
  awk '{print $1}' "$file" | sort -n > "$file.sorted"
  echo "  $label"
  awk -v runs="$RUNS" -v wall="$wall" -v w0="$floor" -v p="$PARALLEL" -v fail="$failures" '
    { v[NR] = $1 }
    END {
      n = NR
      total = 0
      for (i = 1; i <= n; i++) total += v[i]
      mean = total / n
      lambda = runs / wall
      # Nearest-rank on a 1-based sorted array; int(x+0.5) rounds half up,
      # which awk lacks a builtin for.
      d5 = v[int(0.5 * (n - 1) + 0.5) + 1]
      d9 = v[int(0.9 * (n - 1) + 0.5) + 1]
      printf "    %-6s %9.1f ms\n", "min", v[1] * 1000
      for (d = 1; d <= 9; d++)
        printf "    %-6s %9.1f ms\n", "D" d, v[int(d / 10 * (n - 1) + 0.5) + 1] * 1000
      printf "    %-6s %9.1f ms\n", "max", v[n] * 1000
      printf "    %-6s %9.1f ms\n", "mean", mean * 1000
      queued = mean - w0
      if (queued < 0) queued = 0
      printf "\n    floor W\x30=%.1f ms   queue wait=%.1f ms (%.0f%% of W)\n", \
             w0 * 1000, queued * 1000, 100 * queued / mean
      printf "    \xce\xbb=%.1f/s   W=%.1f ms   L=\xce\xbbW=%.1f in flight   failures=%d/%d\n", \
             lambda, mean * 1000, lambda * mean, fail, runs
    }
  ' "$file.sorted"
}

echo "warming daemon + connection pool..."
"$WORKDIR/cli.sh" "${TRAX_ARGS[@]}" >/dev/null
[[ $WITH_HTTP -eq 1 ]] && "$WORKDIR/http.sh" "$BASE_URL" "$API_KEY" >/dev/null

# Latency floor per tier: the FASTEST unloaded run, not the median. A single
# sample can be inflated by an unrelated hiccup, and an inflated floor
# subtracted from the loaded mean yields a nonsense negative queue wait.
for _ in 1 2 3 4 5 6 7; do
  "$WORKDIR/cli.sh" "${TRAX_ARGS[@]}"
done > "$WORKDIR/cli_serial.txt"
CLI_FLOOR=$(sort -n "$WORKDIR/cli_serial.txt" | awk 'NR == 1 {print $1}')
HTTP_FLOOR=0
if [[ $WITH_HTTP -eq 1 ]]; then
  for _ in 1 2 3 4 5 6 7; do
    "$WORKDIR/http.sh" "$BASE_URL" "$API_KEY"
  done > "$WORKDIR/http_serial.txt"
  HTTP_FLOOR=$(sort -n "$WORKDIR/http_serial.txt" | awk 'NR == 1 {print $1}')
fi

printf "unloaded floor:  CLI=%.1f ms" "$(awk -v a="$CLI_FLOOR" 'BEGIN{print a * 1000}')"
[[ $WITH_HTTP -eq 1 ]] && printf "   HTTP=%.1f ms   CLI overhead=%.1f ms" \
  "$(awk -v b="$HTTP_FLOOR" 'BEGIN{print b * 1000}')" \
  "$(awk -v a="$CLI_FLOOR" -v b="$HTTP_FLOOR" 'BEGIN{print (a - b) * 1000}')"
echo
echo
echo "running $RUNS invocations at concurrency $PARALLEL"
echo

TIMEFORMAT=%R
CLI_WALL=$( { time seq "$RUNS" \
  | xargs -P "$PARALLEL" -I{} "$WORKDIR/cli.sh" "${TRAX_ARGS[@]}" \
    > "$WORKDIR/cli.txt"; } 2>&1 )
report_tier "CLI  (trax: process start + traxd + server)" \
  "$WORKDIR/cli.txt" "$CLI_WALL" "$CLI_FLOOR"

HTTP_WALL=0
if [[ $WITH_HTTP -eq 1 ]]; then
  echo
  HTTP_WALL=$( { time seq "$RUNS" \
    | xargs -P "$PARALLEL" -I{} "$WORKDIR/http.sh" "$BASE_URL" "$API_KEY" \
      > "$WORKDIR/http.txt"; } 2>&1 )
  report_tier "HTTP (server round trip only)" \
    "$WORKDIR/http.txt" "$HTTP_WALL" "$HTTP_FLOOR"
fi

# Little's Law (L = lambda*W) is an IDENTITY: it holds in any stable system
# and cannot be violated, so "are we above or below Little" has no answer.
# It is a lens, not a score. In particular L/P is NOT a health metric --
# because L = lambda*W, a system that answers instantly has a tiny L no
# matter how much concurrency you offer it. Low L is what fast looks like.
# The two numbers that DO carry a target are throughput headroom
# (lambda_max vs peak arrival rate) and the latency decile you promise.
awk -v cli_wall="$CLI_WALL" -v http_wall="$HTTP_WALL" -v runs="$RUNS" \
    -v p="$PARALLEL" -v with_http="$WITH_HTTP" \
    -v cli_floor="$CLI_FLOOR" -v http_floor="$HTTP_FLOOR" '
  BEGIN {
    printf "\nLITTLE\x27S LAW   L = \xce\xbbW   (an identity, not a score)\n"
    printf "  L  items in flight     \xce\xbb  throughput req/s     W  latency s/req\n\n"
    cli_lambda = runs / cli_wall
    printf "  CLI    \xce\xbb=%8.1f/s\n", cli_lambda
    if (with_http) {
      http_lambda = runs / http_wall
      printf "  HTTP   \xce\xbb=%8.1f/s\n", http_lambda
      printf "\n  BOTTLENECK: "
      if (http_lambda > 2 * cli_lambda)
        printf "the CLIENT path. The server sustains %.0f/s but trax\n" \
               "              delivers %.0f/s -- %.1fx less. The %.1f ms unloaded gap\n" \
               "              (CLI %.1f ms vs HTTP %.1f ms) is process start plus the\n" \
               "              traxd hop; that is where the next win is, not the server.\n", \
               http_lambda, cli_lambda, http_lambda / cli_lambda, \
               (cli_floor - http_floor) * 1000, cli_floor * 1000, http_floor * 1000
      else
        printf "the SERVER. trax tracks it within %.1fx, so client\n" \
               "              overhead is not what is limiting throughput.\n", \
               http_lambda / cli_lambda
      printf "\n  TARGETS for %d concurrent callers:\n", p
      printf "    1. \xce\xbb_max >= peak arrival rate. Server is at %.0f/s; %d callers\n", http_lambda, p
      printf "       each issuing one request every T seconds need %d/T per second,\n", p
      printf "       so T > %.2fs keeps you inside capacity with no queueing.\n", p / http_lambda
      printf "    2. A latency budget at a decile you name (say D9), alerted on.\n"
      printf "       Not the mean -- the mean hides the tail that users feel.\n"
      printf "    3. NOT L/P. It falls as the system gets faster.\n"
    } else {
      printf "\n  (HTTP tier skipped; no server-only baseline to attribute against.)\n"
    }
  }'
