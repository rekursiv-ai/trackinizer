#!/usr/bin/env bash
#
# Spin up ONE ephemeral trackinizer v2 and replay BOTH canonical stories
# side by side, so a single browse session shows the full vocabulary:
#
#   Part 1 -- ISSUE STORY (the TRM gap):
#     A team (Scientist / Analyst / Engineer / Reviewer) chasing the
#     10-point gap between their TRM reproduction (~77%) and the paper's
#     87.4% on ARC-AGI-1.
#       - Goal Belief + the Issue aimed at proving it.
#       - ACT-required hypothesis (Belief#2) is killed by paper Table 1
#         evidence (author marks Belief#2 disproven after citing it).
#       - x10-overfits finding (Belief#3) is established by two Experiments.
#       - x17 (batch=768) and x108 (halting head) remain in flight.
#       - q_halt cross-experiment finding (Belief#5).
#
#   Part 2 -- EPISTEMIC STORY (the two-axis research-elicitation tree):
#       - Root Issue ("Can we autonomously create knowledge?") -- the
#         standing program both bets decompose under.
#       - docs/trax_research_example_1.sh -- the SURVIVOR (B1): noising a
#         memory channel; the rich provenance path.
#       - docs/trax_research_example_2.sh -- the OPEN BET (B2): starving
#         retrieval; one search, one opposite-mechanism hit, an EMPTY
#         epistemic axis.
#     Both sub-scripts RESOLVE the root and question by search, so this
#     composite only creates the root first; the rest flows from id
#     resolution. The sub-scripts inherit the ephemeral server through the
#     ``TRACKINIZER_URL`` this script exports.
#
# Trax grammar notes:
#   * The kind is the verb: "trax issue title to ..." creates an Issue.
#   * Row-local edits use "trax belief 2 judgement to disproven".
#   * Edges are SVO and stored child -> parent (younger -> older).
#   * CITATIONS flow Artifact -> {Belief, Experiment}: the CITING artifact is
#     the subject, the CITED claim (a Belief or Experiment) is the object.
#     So "trax paper 1 proves belief 2" and "trax experiment 1 disproves
#     belief 2". Only a Belief or Experiment may be the cited target; any
#     Artifact (Paper / Experiment / Belief / WebSearch / CodeChange) may cite.
#     Each edge reads from either endpoint via a *_by reverse alias that
#     anchors at the OTHER side without changing the stored direction:
#     "belief 2 proved_by paper 1" anchors at the claim, same stored edge.
#   * VALENCE (in [-1, 1]) carries for-vs-against: it is the SIGN, not a
#     separate edge kind. proves/favors default +0.5; the dis* spellings
#     (disproves/disfavors) default -0.5 and NEGATE an explicit magnitude, so
#     write "disproves ... valence to 0.95" (positive; it is negated to -0.95).
#   * SEQUENCING is "requires": "A requires B" => B must be done first. The
#     reverse views "blocked_by" (X blocked_by Y => X requires Y) and "blocks"
#     (Y blocks X => X requires Y) name the same stored edge.
#   * PROVENANCE is produced_by (child -> parent); the producer-voice alias
#     "produced" reads "issue 3 produced experiment 1" (issue 3 made it).
#
# Usage:  ./example.sh
# Then open the URL printed at the end. Ctrl-C shuts down (data is wiped).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DOCS="$(cd "${HERE}/../../../docs" && pwd)"
PORT="${TRACKINIZER_PORT:-8766}"
HOST="127.0.0.1"
URL="http://${HOST}:${PORT}"
LOG="$(mktemp -t trackinizer-example.XXXXXX)"

export TRACKINIZER_URL="$URL"

command -v trax >/dev/null || {
    echo "trax not on PATH (install trackinizer so 'trax' is available)" >&2
    exit 1
}

echo "[example] starting trackinizer --ephemeral on ${URL}"
echo "[example] server log: ${LOG}"
# setsid isolates trackinizer from the controlling terminal so Ctrl-C
# doesn't reach uv/python through the tty; we steer shutdown via the
# cleanup trap below. We can NOT rely on a process-group SIGTERM to
# reap the whole tree: py_pglite spawns Node with preexec_fn=os.setsid
# (manager.py), which puts the PGlite child in its own session/pgid.
# So we walk the descendant tree by ppid instead.
setsid uv --quiet --project "${HERE}" run --frozen --no-sync python3 \
    -m trackinizer.server \
    --ephemeral --no-auth --host "${HOST}" --port "${PORT}" \
    >"${LOG}" 2>&1 &
SERVER_PID=$!

# Recursively emit every descendant pid (children-first) of $1.
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
    echo
    echo "[example] shutting down trackinizer (pid ${SERVER_PID})"
    # SIGTERM uvicorn only; its lifespan teardown reaps the PGlite Node
    # child via substrate.__aexit__ -> py_pglite manager.stop(). Sending
    # SIGTERM to Node directly races against asyncpg's 2s close_timeout
    # in substrate._shutdown and adds latency every Ctrl+C for no gain.
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    # substrate.py allows PGlite up to 5s to stop gracefully; give the
    # full uvicorn->substrate->PGlite chain ~15s before we SIGKILL.
    for _ in $(seq 1 150); do
        kill -0 "$SERVER_PID" 2>/dev/null || break
        sleep 0.1
    done
    # Backstop: if lifespan stalled, walk the descendant tree (PGlite
    # Node sits in its own session via py_pglite's preexec_fn=os.setsid,
    # so pgid signaling from the parent won't reach it -- pgrep -P does).
    local kids
    kids="$(_descendants "$SERVER_PID") $SERVER_PID"
    kill -KILL $kids 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
}
trap 'cleanup; exit 130' INT TERM
trap cleanup EXIT

# Wait for the API to come up.
for _ in $(seq 1 60); do
    curl -fsS "${URL}/openapi.json" >/dev/null 2>&1 && break
    sleep 0.5
done
if ! curl -fsS "${URL}/openapi.json" >/dev/null 2>&1; then
    echo "[example] server failed to come up; tail of ${LOG}:" >&2
    cat "${LOG}" >&2
    exit 1
fi

trax() { uv --quiet run --frozen trax "$@"; }
say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

cat <<EOF

############################################################
#  PART 1 -- ISSUE STORY: closing the 10-point TRM gap
############################################################
EOF

say "Goal Belief + Issue aimed at proving it"
trax belief title to "TRM reproduction reaches >=87% on ARC-AGI-1 eval" \
    description to "Paper claims 87.4%. Our best baseline (x10) peaks at 76.46% then overfits down. 10-point gap to close." \
    label to goal --as user                                                    # Belief#1
trax belief 1 label add arc-agi --as user

trax issue title to "Close the 10-point TRM gap on ARC-AGI-1" \
    description to "Match the 87.4% reported in 'Tiny Recursive Models' Table 1." \
    label to trm --as user                                                     # Issue#1
trax issue 1 label add reproduction --as user

say "Shared knowledge artifacts (Paper + free-form Artifacts)"
trax paper title to "TRM paper -- arXiv:2511.08653" \
    description to "Pinto et al., 2025. Reference numbers, especially the Table 1 ablations." \
    source to "arXiv:2511.08653" --as librarian                                     # Paper#1

trax artifact title to "FINDINGS.md" \
    description to "Running summary of what we know about TRM training dynamics on ARC-AGI-1." \
    label to doc --as librarian                                                # Artifact#1

trax artifact title to "ERROR_PROP.md" \
    description to "Notes on why TRM commits to early-step errors; H_cos trajectory analysis." \
    label to doc --as librarian                                                # Artifact#2

say "Initial hypothesis: ACT is required (Belief#2)"
trax belief title to "ACT (Adaptive Computation Time) is required to reach 87% on ARC-AGI-1" \
    description to "x10's 76->73 degradation looks like fixed-depth inability to adapt per-puzzle. TRM ablation says 'removing ACT hurts generalization'." \
    label to hypothesis --as scientist                                      # Belief#2
trax paper 1 proves belief 2 --as scientist   # Paper#1 cites Belief#2 (motivating evidence)

trax issue title to "Implement ACT halting mechanism on top of x10 baseline" \
    description to "Halting probability head, ponder cost in loss, up to 16 iterations matching TRM." \
    priority to high label to act narrows issue 1 --as scientist               # Issue#2
trax issue 2 label add engineering --as scientist

trax issue title to "Find the quantitative ACT ablation in TRM paper" \
    description to "Analyst flagged that 'hurts generalization' without a magnitude is not a real number. Find Table 1." \
    priority to high label to research narrows issue 1 --as analyst            # Issue#3

say "Scientist chases the paper, produces hard counter-evidence"
trax issue 3 owner to scientist --as scientist

trax experiment title to "TRM Table 1: without ACT 87.4%, with ACT 86.1%" \
    description to "From the paper's published ablation. ACT actually HURTS by 1.3 points. Paper notes 'no significant difference in generalization' and simpler halting works better." \
    outcome to "ablation reads ACT off >= ACT on" \
    label to counter-hypothesis --as scientist                                 # Experiment#1
trax issue 3 produced experiment 1 --as scientist
trax experiment 1 status to complete --as scientist
trax issue 3 status to complete --as scientist

say "Cite Experiment#1 AGAINST Belief#2 and mark the hypothesis disproven"
trax experiment 1 disproves belief 2 --as scientist
trax belief 2 judgement to disproven \
    --reason "Paper Table 1 shows ACT off >= ACT on." \
    --as scientist

say "ACT implementation issue is no longer needed -- mark abandoned"
trax issue 2 status to abandoned \
    --reason "Paper ablation shows ACT hurts by 1.3 points; not the gap." \
    --as scientist

say "Pivot: diagnose x10 overfit + try batch=768"
trax issue title to "Pull x10 and x15 log diagnostics" \
    description to "Compare x10 (Muon, batch 128) vs x15 (PDC curriculum) at matched step counts and eval depth." \
    priority to medium label to diagnostics narrows issue 1 --as scientist     # Issue#4

trax issue 4 owner to analyst --as analyst

trax experiment title to "x10 peaks at 76.46% test acc at step 12.5k" \
    description to "Eval at full depth H=3, L=6 (D_eff=42). Train loss = 1.2188. Best checkpoint to date." \
    outcome to "best=76.46% at step 12.5k" \
    label to x10 --as analyst                                                  # Experiment#2
trax experiment 2 label add peak --as analyst
trax issue 4 produced experiment 2 --as analyst
trax experiment 2 status to complete --as analyst

trax experiment title to "x10 degrades 76.46% -> 72.89% by step 26k while train loss falls to 0.97" \
    description to "Classic overfit pattern: train loss monotonically down, test acc monotonically down post-12.5k." \
    outcome to "test 72.89% / train loss 0.97 at step 26k" \
    label to x10 --as analyst                                                  # Experiment#3
trax experiment 3 label add overfit --as analyst
trax issue 4 produced experiment 3 --as analyst
trax experiment 3 status to complete --as analyst

trax experiment title to "x15 PDC Stage 1 at step 4k = 66.19% at curriculum depth (H=1, L=2)" \
    description to "NOT comparable to x10 at step 4k (68.55% at full depth H=3, L=6). x15 must reach Stage 3 before fair comparison." \
    outcome to "66.19% at curriculum depth; not apples to apples" \
    label to x15 --as analyst                                                  # Experiment#4
trax experiment 4 label add pdc --as analyst
trax issue 4 produced experiment 4 --as analyst
trax experiment 4 status to complete --as analyst
trax issue 4 status to complete --as analyst

say "Diagnostics establish Belief#3 (x10 overfits) as 'proven'"
trax belief title to "x10 overfits past step 12.5k under Muon + batch 128" \
    description to "Train loss falls monotonically while test acc declines -- textbook overfit signature." \
    label to finding --as analyst                                           # Belief#3
trax experiment 2 proves belief 3 --as analyst
trax experiment 3 proves belief 3 --as analyst
trax experiment 3 proves belief 3 note to "This edge carries the degradation half of the overfit argument." \
    valence to 0.95 label add degradation --as analyst
trax belief 3 judgement to proven \
    --reason "Experiments #2 and #3 jointly establish peak then degradation." \
    --as analyst

say "Working hypothesis: batch=768 closes the gap (Belief#4, unproven)"
trax belief title to "Batch size 768 closes the gap by reducing Muon gradient noise" \
    description to "Reference uses batch 768; we use 128. 6x less gradient noise. Untested. Muon LR may need rescaling." \
    label to hypothesis --as analyst                                        # Belief#4
trax paper 1 proves belief 4 --as analyst   # Paper#1 cites Belief#4 (motivating evidence)
trax belief 4 label add x17 --as analyst

trax issue title to "Run x17 -- batch_size=768 from x10 baseline" \
    description to "Re-run x10 config with batch 768 and Muon LR rescaled. Decision rule: 15k steps. Reject if peak <= 76.46% AND degradation pattern identical." \
    priority to high label to x17 narrows issue 1 blocked_by issue 4 --as scientist # Issue#5
trax issue 5 label add engineering --as scientist
trax issue 5 owner to engineer --as engineer

say "Second story thread: q_halt is not learning (Belief#5)"
trax belief title to "q_halt loss does not differentiate x85 from x86 -- halting signal is not learning" \
    description to "Across 5 step checkpoints (500, 1k, 1.5k, 2k, 2.5k) q_halt_loss was bit-identical: 0.2389, 0.1945, 0.0748, 0.0291, 0.0881." \
    label to finding --as scientist                                            # Belief#5
trax belief 5 label add q-halt --as scientist

trax issue title to "x86 -- cross-experiment q_halt comparison" \
    description to "Side-by-side x85/x86 to confirm whether q_halt is actually learning anything per-puzzle." \
    priority to medium label to x86 narrows issue 1 --as scientist             # Issue#6
trax issue 6 label add q-halt --as scientist
trax issue 6 owner to reviewer --as reviewer

trax experiment title to "x86 vs x85 q_halt_loss identical at steps 500/1k/1.5k/2k/2.5k" \
    description to "Exact match: 0.2389, 0.1945, 0.0748, 0.0291, 0.0881. q_halt is not learning useful per-puzzle signal." \
    outcome to "bit-identical across 5 checkpoints" \
    label to q-halt --as reviewer                                              # Experiment#5
trax issue 6 produced experiment 5 --as reviewer
trax experiment 5 status to complete --as reviewer
trax issue 6 status to complete --as reviewer
trax experiment 5 proves belief 5 --as reviewer
trax belief 5 judgement to proven \
    --reason "Experiment#5 reproduces the identical q_halt loss trace." \
    --as reviewer

trax issue title to "x108 -- halting head with scalar confidence features" \
    description to "Replace concat(z_H, softmax(logits)) with two scalars: max_prob, entropy. q_halt input = [z_H_pooled, max_prob, entropy] -> linear -> halt_logit." \
    priority to medium label to x108 narrows issue 1 blocked_by issue 6 --as scientist # Issue#7
trax issue 7 label add halting --as scientist

say "Explicit watches"
trax belief 1 subscriber add user --as user
trax issue 1 subscriber add user --as user
trax belief 4 subscriber add analyst --as analyst

cat <<EOF

############################################################
#  PART 2 -- EPISTEMIC STORY: two-axis research tree
############################################################
EOF

say "Root: the standing program both bets decompose under"
# The two sub-scripts resolve this root by search; create it first so their
# `title re "autonomously create knowledge"` guard finds exactly one.
trax issue \
    issue_kind to question \
    title to "Can we autonomously create knowledge?" \
    description to "Root inquiry. Can an autonomous system generate genuinely novel, non-trivial research knowledge -- not retrieve or recombine the already-known? Each child Issue is a concrete stuck problem; beliefs under them are seed-bets, killed or survived by adversarial novelty/importance gates. The kill-graph accumulates across runs." \
    label to knowop \
    --as Scientist

say "B1 -- the SURVIVOR (docs/trax_research_example_1.sh)"
bash "${DOCS}/trax_research_example_1.sh"

say "B2 -- the OPEN BET (docs/trax_research_example_2.sh)"
bash "${DOCS}/trax_research_example_2.sh"

cat <<EOF

============================================================
  Final state
============================================================

EOF

say "board -- Issues grouped by status"
trax board || true

say "belief -- author-owned proven/disproven judgements, both stories"
trax belief || true

say "show Belief#2 -- the killed ACT hypothesis"
trax belief 2 || true

say "graph -- the epistemic two-axis tree"
trax graph || true

say "recent -- last 20 events"
trax recent --limit 20 || true

say "search 'overfit' -- ILIKE over title+description"
trax search overfit || true

cat <<EOF

============================================================
  Web UI:      ${URL}/
  Server PID:  ${SERVER_PID}   log: ${LOG}
  TRACKINIZER_URL=${URL} (exported for this shell)

  Ctrl-C to shut down (data is ephemeral -- wiped on exit).
============================================================
EOF

# Keep the server alive for browsing.
wait "${SERVER_PID}"
