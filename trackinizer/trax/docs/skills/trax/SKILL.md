---
name: trax
description: >
  ALWAYS invoke this skill for Trackinizer/trax work tracking and inquiry records: issues, beliefs, papers, experiments, codechanges, web results/searches, sessions, costs, board. Do not hand-write trax records directly -- invoke this skill first.
argument-hint: "[help|next|blocked|board|graph|search|recent|cost|profile|<kind>] ..."
user-invocable: true
tools: Bash, Read, Write, Edit, Glob, Grep
---

# trax -- Trackinizer CLI Skill

Use `trax` as the task and inquiry tracker for Trackinizer-backed work.

Core rule: `trackinizer/trax/docs/{grammar.lark,GRAMMAR.md}` and `trax help` are
the source of truth. If this skill disagrees with live help, trust live help.
The CLI is deterministic left-to-right prefix parsing: existing refs end at
`seq`/UUID, inline creates consume fields until a non-field token, edge metadata
ends at the first non-metadata token, and `del`/`help` are terminal. Do not guess
ownership from prose; apply these boundaries.

## Rules

1. Run commands as `trax ...`; assume it is on `PATH`.
2. Run `trax <kind> help` before the first write to a kind each session.
   Field cheat-sheet: Issue takes `title/description/status/priority/owner`;
   Paper takes `source` (not url); `note`/`valence` are EDGE metadata, not
   row fields -- there is no bare `note` verb on a row.
3. Use trax for task/inquiry tracking, not git operations.
4. Take ownership of substantial multi-agent work before editing:
   `trax issue 7 owner to Agent`. An `active` row with no owner is open;
   with an owner it is in progress.
5. Every trax write must include `--as ACTOR`, where `ACTOR` is your current
   agent self name (for this session, `Agent`). Add `--reason TEXT` when the
   audit log needs context; `--reason` is optional.
6. Record useful verification on tracked work: `trax issue 7 validation to "..."`.
   Long-lived issues: append dated progress sections to the description.
   Append recipe (no append primitive exists): read the current body
   (`trax issue 7 description`), add a dated section locally, write it
   back with `description to @file` or `to -`.
7. Link durable outputs as first-class artifact rows with `produced`; do not bury
   commit SHAs, experiment results, papers, web pages, searches, or beliefs in an
   issue description.
8. Record `agent-cost` as marginal spend on the most granular issue that directly
   consumed the work. Prefer leaf issues; do not add parent totals when children
   already carry costs because `trax cost ... --deep` aggregates them.
9. Reference row ids in commit messages only when the user asks you to write one.
10. Triage server errors by class: connection-refused/5xx = outage --
    probe `trax profile`, tell the user, keep working; HTTP 422 = your
    payload is wrong -- fix the command, do not retry verbatim; auth =
    ask the user how the deployment is configured.

## Ownership, actors, and priorities

`owner` means the person or agent currently responsible for the row. For
multi-agent work, set each child issue owner to the subagent actually doing that
leaf; the coordinator owns only the coordination issue.

Use `--as <name>` on every write so the audit log reflects who did the work.
Use your current agent self name for your own writes. Use another actor only
when recording work on behalf of that actor. Add `--reason TEXT` only when the
changelog needs extra audit context. `--reason` is stored on the change log;
it is not row state and is not part of normal trax queries. Example:

```bash
trax issue 64 owner to issue63-runtime-agent --as Agent
trax issue 64 validation to "pytest ... passed" --as issue63-runtime-agent
trax issue 64 status to abandoned --as Agent --reason "superseded by Issue#72"
```

Issue `priority` is an integer (lower = more urgent). The CLI also accepts a
named alias per band, which is what examples use (`priority to high`):

| Band | Alias      | Value |
|------|------------|------:|
| P0   | `critical` | 0     |
| P1   | `high`     | 10    |
| P2   | `medium`   | 20    |
| P3   | `low`      | 30    |
| P4   | `backlog`  | 40    |

Intermediate integers exist for rare tie-breaking inside a band, but avoid them
by default so priority retains a clear dynamic range.

## Status model

Statuses are `active|complete|abandoned|invalid`. `active` with no `owner` is
open; with an `owner` it is in progress. `trax next`, `board`, and `blocked`
operate over `active` rows. `abandoned` = work stopped before completion;
`invalid` = should not have been opened. Prefer either over `del` when history
matters.

## Kinds and artifacts

`issue` is the only schedulable kind; every other kind is an artifact (evidence
or output) that issues produce and beliefs cite. `grammar.lark` (and
`GRAMMAR.md`) owns the current kind list, and `types/inquiries.py` owns the
kind meanings and fields; do not hand-maintain them in this skill. When an
issue produces an output, create/link the most specific artifact row with
`produced`; do not append the evidence as prose to the issue body.

Two artifact boundaries are easy to get wrong:

- A `codechange` row must use the full 40-character SHA from `git rev-parse`,
  never an abbreviated SHA.
- An `agentsession` row is the captured session envelope; its turn log lives in
  `agent_session_events`, outside normal inquiry edges and change logs.

## Editing Trackinizer/trax

When changing Trackinizer, keep the skill aligned with the owning contracts:

- Keep the CLI grammar minimal. `trax/docs/{grammar.lark,GRAMMAR.md}` and
  `trax/grammar.py` define
  accepted forms; prefer one deterministic production or deleting a form over
  adding a special case.
- Do not hand-maintain kind, token, field, or route lists in this skill. Point
  to the owning source instead.
- Treat `types/inquiries.py`, `types/edges.py`, `types/change_log.py`, and
  `types/agent_session_events.py` as the DB/domain source of truth. API docs
  explain the wire surface; types win on names and structure.
- Follow `docs/api.md` and `docs/api_agent_session_events.md` for API naming.
  Do not invent aliases or parallel names.
- Preserve the `agent_session_events` asymmetry: it is deliberately outside
  `inquiries` and `change_log`; simplifying trax must not collapse that
  boundary.

## Minimal grammar

Refs are `kind seq`, a bare `seq` under a leading kind, or a UUID:

```bash
trax issue 7
trax issue 5 6 12
trax issue 1 belief 3
```

Common row forms:

```bash
trax                              # list all kinds
trax issue                        # list issues
trax issue status is active       # filter; ops: is ne re nre lt le gt ge isnull notnull
trax issue 7                      # show row
trax issue 7 --changes            # show row with audit history
trax issue title to "Title"      # create row
trax issue 7 title              # show field
trax issue 7 priority to high     # replace field
trax issue 7 label to backend     # overwrite list field
trax issue 7 label add urgent     # append list value
trax issue 7 label del old        # remove list value
trax issue 7 agent-cost add 1.25  # signed cost delta
trax issue 1..50 owner to jvd --makeitso  # bulk edit every matched row
```

A `field to value` mutation on a range/filter is a BULK edit applied to every
matched row. When it matches more than one row it requires `--makeitso`;
without it the matches are previewed and nothing is written (a zero- or one-row
match applies directly).

**A value that starts with `-` needs a `--` end-of-options marker**, else
argparse reads it as an unknown flag and fails with "unrecognized arguments"
(the write silently no-ops). This bites Google Scholar cluster_ids, which are
base64-ish and ~1/64 lead with `-` (e.g. `-n5NzXb4b5sJ`). Put `--` after the
last real flag / after the seq, before the positional SVO:

```bash
trax paper 176 -- google_scholar_cluster_id to -n5NzXb4b5sJ   # value leads with -
trax paper --format json -- 5 title to -weird                  # flags stay before --
```

Harmless on safe values, so `trax <kind> [--flags] -- ...` is a fine default.

Use `FIELD to -` to read one field value from stdin. Use `FIELD to @path`
to read any number of field values from files or shell process substitutions:

```bash
trax issue title to "Clear title" description to - <<'EOF'
## Problem
The retry path drops timeout context.

## Validation
Run the retry regression test.
EOF
```

```bash
trax issue title to "Clear title" description to @body.md
trax issue title to "Clear title" description to @<(printf '%s\n' '# Body')
```

Every edge is stored child -> parent (`from` = younger/dependent, `to` = older
parent), and there are exactly seven stored kinds: `narrows`, `requires`,
`produced_by`, `supersedes`, `proves`, `favors`, `cites_paper`. The CLI offers
reverse-voice aliases so either endpoint can anchor the same stored edge.

Use `requires` / `required_by` for Issue prerequisites -- they are unambiguous:

- `A requires B` -- B is a prerequisite of A; B must be done before A.
- `A required_by B` -- the inverse; A must be done before B.

The two spellings are the same stored edge from opposite ends (`A requires B`
== `B required_by A`). The `blocks` / `blocked_by` aliases name the same edge
but read backwards -- `A blocks B` == `A required_by B`, and `A blocked_by B`
== `A requires B` -- so they are easy to misread; prefer requires/required_by.
`A produced B` means A spawned B (stored `produced_by`, B -> A); `A narrows B`
/ `A broadens B` is taxonomy.

Citations store `Artifact -> {Belief, Experiment}` (the citing artifact is the
child pointing up to the claim): `paper Q proves belief P` stores the edge with
the Artifact on the from-side, meaning Q is load-bearing evidence for P. When
reading the claim, the same edge appears as `proved_by`; the reverse alias
`belief P proved_by paper Q` stores the identical edge. `favors` is the same
shape but contextual (does not vote in the proof predicate).

For-vs-against is the SIGN of the edge's `valence` (`[-1, 1]`: positive
supports, negative argues against, default `0.5`), not a separate edge kind.
The CLI `disproves` / `disfavors` spellings are aliases for `proves` / `favors`
that negate the valence (default `-0.5`), so they store the same two kinds with
a negative sign.

```bash
trax issue 7 requires issue 8 --as Agent
trax issue 7 required_by issue 9 --as Agent
trax issue 7 produced codechange sha to $(git rev-parse HEAD) \
  title to "Implement retry policy" --as Agent
trax paper 5 proves belief 3 valence to 0.9 --as Agent
trax issue 7 requires             # list related rows; no --as for reads
trax issue 7 produced             # list produced artifacts; no --as for reads
trax issue 7 requires 1           # inspect one related edge/row; no --as for reads
```

Create commands mix row fields, list actions, and edge actions. Two
composition rules cover most needs (full rules: `GRAMMAR.md` section 4):

- **Descent:** an existing-ref edge target (`requires issue 125`) does not
  move the cursor -- later fields bind to the leading row. An INLINE-create
  edge target (`requires issue title to ...`) becomes the cursor -- later
  fields and edges bind to the NEW node, so consecutive inline creates chain.
- **Siblings:** wrap each child in `begin ... end` to pop the cursor back to
  the anchor.

```bash
# CHAIN (cursor descends): root requires repro, repro requires ship.
trax issue title to "Evaluate retry jitter" priority to high kind to task \
  requires issue title to "Reproduce timeout failure" kind to bug \
  requires issue title to "Ship retry policy" kind to feature

# SIBLINGS under one anchor: begin..end re-binds the next edge to the anchor.
trax issue title to "Evaluate retry jitter" \
  requires begin issue title to "Reproduce timeout failure" kind to bug end \
  requires begin issue title to "Ship retry policy" kind to feature end
```

Edge metadata: `note`/`valence` annotate the edge bare; other fields need the
`edge` marker (`requires issue 125 edge label add urgent`). The `edge` marker
is always allowed and never wrong. Details and traps: `GRAMMAR.md`.

Field values may read from stdin (`to -`), a file (`to @path`), or a process
substitution (`to @<(...)`) -- the value form is independent of the tree shape.

### Linking code changes (commit SHAs)

Attach commits as `codechange` artifacts via the `produced` edge. Two traps:

1. The inline edge form **creates** a new row, so it **requires `title to`**
   alongside `sha to`; omitting it returns HTTP 422 `title Field required`.
2. Use the **full 40-char SHA** from `git rev-parse <ref>`, never abbreviated.

```bash
# Create + link a new codechange in one edge (title is mandatory):
trax issue 316 produced codechange \
  sha to "$(git rev-parse 8c5658d2a)" \
  title to "PR1+PR2: retry gate + NoticeMessage" --as Agent

# Link an ALREADY-existing codechange row by seq (no create, no title):
trax issue 316 produced codechange 73 --as Agent
```

`trax issue 7 codechange to 73` is **wrong** -- that is a field-set, not an
edge, and silently projects instead of linking. The edge verb is `produced`.
Verify links with `trax issue 7 produced` (lists produced artifacts).

Slash args forward verbatim to `trax`. A bare number routes to `trax issue <seq>`.

## Workflow

1. Pick: `trax next` / `trax blocked` / `trax board`.
2. Take ownership: `trax issue 7 owner to Agent`.
3. Implement.
4. Verify: `trax issue 7 validation to "pytest ... passed"`.
5. Close when the user requests it or closing is clearly part of the task:
   `trax issue 7 status to complete`.

Re-running `description to @body.md` (or `to -`) overwrites; that is the
recovery path when a body lands too thin.

## Per-kind authoring expectations (child docs)

This skill teaches the GRAMMAR (how to write a row). WHAT a complete, well-formed
row of each kind must CONTAIN lives in a per-kind **child doc** — the content SoT
for that kind (not field lists — `types/inquiries.py` owns fields). Each is its
own invocable skill, named `trax-<kind>`; invoke it before authoring a
substantial row of that kind:

| Invoke | Before authoring a… |
|---|---|
| `trax-issue` | Issue / bug report (context, success criteria, verification) |
| `trax-paper` | Paper (bib cascade, DOI, companion WebResults, `cites` vs `proves`) |
| `trax-belief` | Belief (falsifiable claim, judgement/confidence, signed citations) |
| `trax-experiment` | Experiment (codechanges, outcome, config, proved_by) |
| `trax-codechange` | CodeChange (full SHA, produced-link) |
| `trax-webresult` | WebResult (url, companion-of-paper) |
| `trax-websearch` | WebSearch (query, provider, findings as edges) |
| `trax-agentsession` | AgentSession (envelope vs events asymmetry) |

The bodies live at `trackinizer/trax/docs/skills/trax-<kind>/SKILL.md`.

For live trackinizer DB schema/shape changes (migrations, constraints,
new Inquiry kinds), read
`trackinizer/docs/db_schema_migration.md` before
touching the deployed database.

Inspection: `trax graph --open-only`, `trax search <terms> --kind issue`,
`trax recent`, `trax cost issue 7 --deep`, `trax issue 7 --changes`.

## Decomposing a large task

When asked to plan a feature, create one row per discrete unit and wire
dependencies with `requires` / `required_by` edges. Use the inline-edge syntax
above to create root + leaves + edges in one command.

- Each row completable in a single focused session.
- Each row independently verifiable; capture the check in `validation to`.
- Encode ordering with edges, not seq numbering.
- Avoid "part 1 of X" rows; each should deliver value on its own.
- Promote evidence-bearing work to artifact rows instead of burying it in an
  issue body. Use `codechange` for commits, `experiment` for empirical runs,
  `paper` for external papers, `webresult` for pages, `websearch` for searches,
  `belief` for propositions, and generic `artifact` only when no specific kind
  fits.

Multi-level trees can be built in one command: an inline-created node becomes
the cursor, so an edge written after it grows FROM that node (descent). Plain
juxtaposition descends (a chain); `begin ... end` around a child pops the cursor
back to the anchor for siblings.

```bash
# Chain by descent: Step 1 -> Step 2 -> Step 3 (each requires the next).
trax issue title to "Step 1" \
  required_by issue title to "Step 2" \
  required_by issue title to "Step 3"

# Two siblings under Step 1: wrap each in begin..end.
trax issue title to "Step 1" \
  required_by begin issue title to "Step 2" end \
  required_by begin issue title to "Step 3" end
```

When a tree is too large or its node seqs must be referenced later, split across
commands instead -- capture each new seq from the output, then attach its
children by ref.

## Recovery

- Server unreachable or auth error: `trax profile`, then `trax profile url to
  ...` / `trax profile token to ...` / `trax profile current <name>`. Ask the
  user before changing profile settings on a shared host.
- Abandoning work without losing history:
  `trax issue 7 status to abandoned --reason "..."`. Use `del` only when the
  row should disappear entirely.
