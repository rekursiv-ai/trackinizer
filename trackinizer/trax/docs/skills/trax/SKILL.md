---
name: trax
description: >
  ALWAYS invoke this skill for Trackinizer/trax work tracking, inquiry records, and visual canvases: issues, beliefs, papers, experiments, codechanges, web results/searches, sessions, costs, board, canvas. Do not hand-write trax records directly -- invoke this skill first.
argument-hint: "[help|next|blocked|board|graph|search|recent|cost|profile|env|canvas|<kind>] ..."
user-invocable: true
tools: Bash, Read, Write, Edit, Glob, Grep
---

# trax -- Trackinizer CLI Skill

Use `trax` as the task and inquiry tracker for Trackinizer-backed work.

Core rule: `grammar.lark`, `GRAMMAR.md` and `trax help` are
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
4. Take ownership of substantial multi-agent work before editing. Prefer
   `trax next owner to Agent --as Agent`, which selects and claims in one
   atomic step -- safe under concurrent agents, since no two callers can
   land on the same Issue. Only use `trax issue 7 owner to Agent` when you
   already know the specific Issue you want (e.g. one the user pointed
   you at); doing that on an Issue found via `trax next`/`board` recreates
   the race the atomic form exists to close. An `active` row with no owner
   is open; with an owner it is in progress.
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

Statuses are `active|complete|stopped|abandoned|invalid`. `active` with no
`owner` is open; with an `owner` it is in progress. `trax next`, `board`, and
`blocked` operate over `active` rows. `complete` = the work finished;
`stopped` = a budget or usage limit ended the work before it finished;
`abandoned` = a person gave the work up before completion; `invalid` = should
not have been opened. Prefer any of these over `del` when history matters.

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
- An `agentsession` row is the captured session envelope; its transcript lives
  in `session_records`, outside normal inquiry edges and change logs.

Every kind also has `app`: one JSON object where an app layered on trackinizer
keeps its own fields about the row, under one key named for the app
(`trax issue 7 app to '{"<app>": {...}}'`). Trackinizer never reads it, and it
is not a filter field: an app finds its rows by labels and edges, and `app`
holds only what the app knows about each one. Write the whole object; a write
that must keep another writer's key compares and sets through the API.

## Editing Trackinizer/trax

When changing Trackinizer, keep the skill aligned with the owning contracts:

- Keep the CLI grammar minimal. `trax/docs/{grammar.lark,GRAMMAR.md}` and
  `trax/grammar.py` define
  accepted forms; prefer one deterministic production or deleting a form over
  adding a special case.
- Do not hand-maintain kind, token, field, or route lists in this skill. Point
  to the owning source instead.
- Treat `types/inquiries.py`, `types/edges.py`, `types/change_log.py`, and
  `types/session_records.py` as the DB/domain source of truth. API docs
  explain the wire surface; types win on names and structure. The record
  VOCABULARY is not trackinizer's: it is `trackinizer.sagent.session.types`.
- Follow `docs/api.md` and `docs/api_session_records.md` for API naming.
  Do not invent aliases or parallel names.
- Preserve the `session_records` asymmetry: it is deliberately outside
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

### Backfilling historical rows

Use `--backfill TIMESTAMP` when creating rows for historical facts. Supply a
timezone-aware ISO-8601 timestamp in the past. It sets `created` on every new
row in that command, including inline creates; an existing anchor keeps its
creation date. Put the flag before row arguments or at the command's end.
Split commands when the new rows need different dates.

```bash
trax issue --backfill 2025-02-14T08:30:00Z \
  title to "Historical timeout" --as Agent
trax issue --backfill 2025-02-14T08:30:00Z 7 \
  produced artifact title to "Historical result" --as Agent
```

The flag requires at least one new row; reads, edit-only commands, links
between existing rows, deletes, and bulk edits reject it. `created` has no
generic setter. `modified` and the creation audit event use the current
server time. Later edits preserve `created`; retries and deduplication that
return an existing row preserve its timestamps.

Import historical sessions under their original CLI session IDs. Reserved
science-chat identities (`chat:` IDs) cannot be backfilled: their server-owned
creation order establishes chat ownership.

When importing a session summary, use the original occurrence date for an
existing historical fact. Use the synthesis date for a newly inferred
Belief: old evidence does not make a new inference historical. Omit the flag
for normal creation at the current server time.

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

1. Claim: `trax next owner to Agent --as Agent` -- selects and claims the
   next unblocked, unowned Issue in one atomic step. If you need to survey
   what's open first, `trax blocked` / `trax board` are read-only and fine
   to run before claiming, but do not follow them with a separate
   `trax issue N owner to Agent` on a row you found that way -- that
   reintroduces the two-step race the atomic form exists to close.
2. Implement.
3. Verify: `trax issue 7 validation to "pytest ... passed"`.
4. Close when the user requests it or closing is clearly part of the task:
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

The bodies live at `<kind>/SKILL.md`, relative to this skill. Installed copies
include `grammar.lark` and `GRAMMAR.md` beside this file.

For live trackinizer DB schema/shape changes (migrations, constraints,
new Inquiry kinds), read
`trackinizer/docs/db_schema_migration.md` before
touching the deployed database.

Inspection: `trax graph --open-only`, `trax issue title re "<terms>"`,
`trax recent`, `trax cost issue 7 --deep`, `trax issue 7 --changes`.

## Agent-controlled canvas

Open `DESIGN.md` and `LAW.md` before you compose, every time you compose. Not once
a session: in the turn where you build the answer. They sit beside this file.

This is first because skipping it has one predictable outcome. The catalog is
twenty-eight nodes — Vega-Lite charts, uPlot series, KaTeX equations, measured
tables, your own SVG, a React Flow graph of the store, tiers of cards, annotated
code, and a whole HTML page you write and mount. Nobody holds that in their head.
An answer composed from memory of having read the brief reaches for the two or
three nodes anyone can recall — a heading, some prose, a row — and ships a
transcript with the canvas doing none of the work. The failure does not announce
itself: the composition validates, the page renders, and the reader gets an essay
where a chart was the answer.

Reading it is also how the choice stays yours. The brief gives each node the one
rule saying when to take it over its neighbour, so what a question needs is
something you decide against the catalog in front of you, not something anyone can
enforce afterwards. A gate can reject a malformed figure; it cannot know that this
sweep wanted a chart and that one wanted a table. That judgement is the job, and it
is only possible with the catalog open.

Two levers, kept apart. `trax workspace` moves attention: which visual shows,
on which record, where. What a visual draws is rows: its record is the app's
anchor row, and each row's `app.<app>` holds what the app knows about it
(`trax experiment 12 app to '{"myapp": {...}}'`). To change what an app's visual
shows inside, write its rows; the visual redraws on its next read.

In a `trax run` session your canvas is `$TRAX_WORKSPACE` (your workstream is
`$TRAX_WORKSTREAM`): show your work there, and leave the id off:
`trax workspace show trax.knowledge --record RECORD_UUID`. Unset means the
session was not placed; use a paired chat message's `workspace_id` instead.

When a paired chat message includes a `workspace_id`, use that ID to inspect or
change the user's canvas. The agent's trax profile must point at the same server
as the browser workspace, and the session using its API key must be paired to
that workspace. Read the state before choosing an instance ID:

```bash
trax workspace WORKSPACE_UUID
trax workspace WORKSPACE_UUID show trax.knowledge --record RECORD_UUID \
  --param hops=2 --param claim=RECORD_UUID
trax workspace WORKSPACE_UUID show trax.subgraph --record RECORD_UUID \
  --param hops=2 --param highlight=UUID,UUID
trax workspace WORKSPACE_UUID show trax.timeline --record RECORD_UUID --placement side
trax workspace WORKSPACE_UUID show trax.files --record RECORD_UUID \
  --param path='src/**/*.py' --param changed=true --param file=train.py
trax workspace WORKSPACE_UUID focus INSTANCE_UUID
trax workspace WORKSPACE_UUID place INSTANCE_UUID floating
trax workspace WORKSPACE_UUID hide INSTANCE_UUID
trax workspace WORKSPACE_UUID highlight RECORD_UUID,RECORD_UUID
trax workspace WORKSPACE_UUID mark 0.1,0.2 0.4,0.5 --record RECORD_UUID
trax workspace WORKSPACE_UUID mark remove MARK_UUID
trax workspace WORKSPACE_UUID mark clear
trax workspace WORKSPACE_UUID mark clear --page '#/graph'
trax workspace WORKSPACE_UUID hide chat
trax workspace WORKSPACE_UUID show chat
trax workspace WORKSPACE_UUID theme to light
trax workspace WORKSPACE_UUID chat_side to null
```

Boxes are red rectangles on the canvas, drawn by people and agents: `mark X1,Y1
X2,Y2` takes two corners as fractions (0 to 1) of the stage, and the box shows on
the page the station is on until someone clicks it away. `--record` names the
records it points at. `trax workspace WORKSPACE_UUID` lists every box with its id,
page and the records and visuals under it, and a Chat line carries the boxes drawn
since the last one. A Claude session started with `trax run` in a station hears
of each new box with the person's next prompt; any other agent in a station runs
`trax workspace $TRAX_WORKSPACE` to see the boxes when the person mentions the
canvas. `mark remove` takes one away; `mark clear` takes every box,
or one page's with `--page`.

The catalog registers `trax.browse`, `trax.subgraph`, `trax.knowledge`,
`trax.timeline`, `trax.artifact`, `trax.summary`, `trax.files`,
`trax.terminal` and `trax.blocks`. `trax.knowledge` (What we know) is the fold: it takes one record
and shows the claims within reach of it as blocks, the near and best-wired first,
each block naming the rows and the stored edges that formed it. Ask for it first: it
is the answer a record gets by default, and a fold of a Belief reads as what is known
where a subgraph of it reads as what is linked. It opens as one window over the page,
`hops` (1 to 3, default 2) wide and `claim` set to one record's uuid to open its
formation panel at once, and it scrolls, so a large distillation shows its whole flow
instead of being cut to one screen. The fold reads the store, so the records you write
are the picture: a card's body is that record's own `description`, and one with none
falls back to printing the edges it rests on. An arrow up into a block is drawn only by
an edge pointing INTO that record from one standing a tier below it — an edge you point
outward prints as that card's `→ cites K#4` line and never as a run. So claims that
only cite papers fold into a row of cards joined by nothing: give the claims edges to
each other, and each one a body, for a fold that reads as a picture.
When a prompt asks a knowledge question that no single record answers — or you
have distilled several records into one picture — write the flow JSON and show
the blocks page. The flow is a fetched artifact, not a store row: write your
answer as JSON to `blocks-latest.json` in the static dir the server serves
(`dist/` on a checkout server; a rebuild replaces it, so re-write it after
deploying), then `trax workspace $TRAX_WORKSPACE navigate '#/blocks'` — or
`show trax.blocks`, which mounts that same answer page as a canvas pane.
UI dynamism is mandatory: an answer is a rich graphical instrument, never a chat transcript or plain text prose.
Never dump the answer into Chat: Chat is an executive ticker (1–2 sentences maximum pointing to the canvas). Every table, sweep, quantitative comparison, hypothesis card, and knowledge graph belongs ON THE CANVAS.
Optimize space aggressively: pack horizontal density, eliminate dead voids, and never stack lone single lines down a mile of empty space.
Proactively use custom design components without waiting to be asked:
- Use `figure` -> `table` (`NumTable`) for all metrics, hyperparameter sweeps, benchmark comparisons, and numbers (never markdown tables in Chat or monospace ASCII grids).
- Use a `tier` of `card`s (`lane: 36–45ch`) to present competing hypotheses, leads, or ablation branches side-by-side.
- Proactively embed the `graph` node (`{ node: "graph", record: UUID, hops: 2 }`) inside the canvas flow whenever claims, issues, beliefs, or causal evidence are discussed, rendering the interactive React Flow knowledge graph with claim cards and formation wires directly within the canvas stream (never as an isolated full page).
- Proactively author a `drawing` (inline SVG) or interactive 3D/simulation `embed` (published to `/api/artifacts/content` as HTML) whenever a mechanism has spatial, geometric, or dynamic properties.
- Provide deep, exhaustive technical substance with exact distributions, parameters, and attributions, not superficial summaries.
- Use `marks` with glyphs (`+`, `−`, `·`, `→`) for verdicts and demands, and `footnote` for provenance.
When no node in the catalog holds the shape — a solid, a mechanism running, three
real dimensions — you write the page yourself and the app hosts it. There is no
`trax` verb for this: `POST /api/artifacts/content` on the server the seat is
already talking to, with an `Idempotency-Key` header (any uuid) and a JSON body of
`{"issue_id": "<the issue it belongs to>", "title": "…", "format": "html",
"html": "<the page>"}`; the 201 response carries `artifact_id`, which is what an
`embed` item in your flow JSON names. Every POST mints a NEW artifact — pass
`previous_artifact_id` to wire a `supersedes` edge — so mount the newest id, never
one you saved earlier in the session. The page is served sandboxed at
`/api/artifacts/<id>/html`, and `DESIGN.md`'s `A page you write yourself` section is
its contract: the libraries the policy allows, the fact that it can fetch nothing and
inherits no ink, and what a first draft has to get right to come out legible. Read
that section before you write the page, not after the screenshot.
`DESIGN.md`, beside this file, is the design brief, generated from the app's own
types. It states three things: the composition
grammar — every node the answer may be assembled from, each with the one rule saying
when to take it over its neighbour — the payload each node holds, field by field, and
the five passes to think in: read the question, read the graph around it, plan the
canvas, design each part against its own rule, render once. Read it
before writing an answer; what it
names is exactly what the renderer reads. The design is the app's; you only
fill it. `LAW.md`, beside it, is the other half of that read: what makes a
filled surface read well — how to arrive at one answer rather than a pile of them,
what the question is actually asking for, how to order it, what is never invented to
fill a slot, and the short list of what never belongs in an answer. It carries no settings: sizes, faces and inks are the app's decision and are
stated once, in its stylesheets, which is what makes `DESIGN.md` the only place what
exists is written. A schema cannot carry taste, so the fields are generated and the method
is written by hand; read both, and `web/src/ui/law.test.ts` proves the law stays a method
rather than growing a copy of the CSS. When the store holds nothing on the prompt, say so: an honest `open`
block naming the gap beats an invented one. `trax.subgraph` is the field itself — the balls
and the wires between them — and you show it when they ask to see the shape rather
than the reading.
`trax.timeline` (Lineage and
timeline) takes a record of any kind and shows its lead issues, bounded dated
directions, results, and signed evidence; it is off in a new canvas. The context
graph shows the record in the graph: what lies within `hops` of it (1 to 3,
default 2) lit, the ring past that dimmed, and the record and the inquiries
`highlight` lists (comma-separated ids) haloed. `trax.files` (Files) takes a
record of any kind and shows the newest files document filed for it: an Artifact
labelled `files` and `files:of:<record uuid>` by a staff service account or by the
account the record is filed under, such as the one the campaign summary service
files for a campaign's lead and brief. A `files:of:` label is a link, never the
key a producer refreshes its row by. It lists the tree's paths and each changed
file with its diff. `path` narrows both lists, as a prefix (`src/foo/`) or,
holding `*` or `?`, a glob (`**/*.py`; `**` crosses directories, `*` does not),
case ignored; `changed=true` hides the tree; `file` opens the diff of the changed
file at that path, or of every one with that name, among only those `path`
leaves. `path` and `file` are at most 512 characters. A reader who narrows the
pane writes these params too. Showing a visual again with
only a new `--record` moves it there and keeps its params (the graph's hops and
highlight, the files view's narrowing); any `--param` sets them anew, each one it
leaves out back to its default. Each write reads
the current revision and prints the resulting state. If another change wins the
race and the server returns 409, read the workspace again before retrying.
`highlight` points at what you are talking about: it marks up to 50 records, by
UUID, wherever the user's page draws them (graph nodes, list rows, a record's
relations), and an empty list clears the marks. Like `navigate` it is an event:
it changes no visual and no revision, a newer highlight replaces the last, and
it is not replayed to a tab that was not listening.
`hide chat` and `show chat` close and reopen the Chat window of the user's browser tab
so they can look at what you showed; hide it when they should look at the page
and show it when you want them to read your answer. It is an event too: no revision
is read or changed, only an agent key may send it, and a reloaded tab shows Chat.
`KEY to VALUE` changes one of the user's preferences (`theme`, `skin`,
`chat_side`, `highlight_mentions`, `terminal_font_size`, `home`): theirs, not your
account's, and every tab of theirs shows it at once. VALUE reads as JSON when it
parses (`false`, `null`), else as text; `null` puts the key back to its default.
`trax workspace WORKSPACE_UUID` prints the preferences as they stand. Only the
station's assistant may do it, and only for these UI keys; the user's own data
is not yours to read or set.
Pairing and disconnecting remain browser actions.

## An answer that wrote to the graph shows what it wrote

When the work left records behind — a claim you established, the runs that establish
it, the question they answer — those records and the wires between them are part of
the answer, not filing done afterwards. A reader who is told "filed as Belief#631" has
been handed an address; a reader shown the claim with its evidence under it, each wire
labelled with the relation the store holds, can see whether the case stands up.

So when an answer creates or re-wires records, the surface carries the territory it
changed, anchored on the claim the answer is about. It is one element among several
and usually not the largest — the finding leads, the evidence supports it — and it is
bounded, because a neighbourhood given the whole surface hands the reader back the
distillation that was your job.

This is the single most common gap in finished answers: the records get written, the
prose names them by number, and nothing is drawn. The brief says which node draws a
stored neighbourhood and which draws a case you are making yourself.

## Pre-display verification gate

Before publishing or displaying any generative-canvas answer, the model must run a
hard verification gate that catches render failures visible to a reader. This is
non-negotiable because a composition can pass all contracts and still fail to draw.

### Gate protocol (run BEFORE publish/display):

1. **Render live**: navigate to the URL where the answer will appear (`#/blocks`
   for catalog answers, `trax canvas show ...` for stored canvases).
2. **Screenshot**: capture the full answer height (not just viewport) using
   `_canvas-live.mjs` or equivalent:
   ```bash
   cd web && node _canvas-live.mjs /tmp/answer-surface.png
   ```
3. **Run verifier**: check the render against layout/render defects:
   ```bash
   cd web && node scripts/verify-canvas.ts /tmp/answer-surface.json
   ```
   Expects non-zero exit if findings exist; reads the JSON output for structured checks.
4. **Manual review**: open `/tmp/answer-surface.png` and inspect for:
   - **Line/text crossing**: no stem/mark crosses a label or readout
   - **Box overlap**: no container overlaps text content
   - **Clipping**: no horizontal text clipped at box edges
   - **Zero-size**: no element renders as 0x0 where content should exist
   - **Refused nodes**: no component rejected by catalog (prints "was not drawn")
   - **On-palette ink**: no purple/amber outside sanctioned positions
5. **Fix defects**: if ANY finding, correct the source JSON/HTML and re-render until
   the screenshot shows NO defects. Do not display a known-broken surface.
6. **Prove delivery channel**: verify the artifact reaches its consumer (e.g.,
   `curl HEAD /app/#/blocks` for catalog answers, screenshot the mounted canvas
   for live canvases). Count markers in the SHIPPED file, not the generator.

**Why**: An author never sees their own page; a seat never sees its own render.
A green contract does not guarantee a good render. A clean script report on an
ugly page is possible. The shot is the only place you can see both. If something
is wrong that you cannot fix, admit it in your answer and name it — an answer
that admits its pane is broken is worth more than one that lets the person
discover it.

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

## Environment variables

`trax env` lists the org's variables; `trax env NAME to VALUE` sets one and
`trax env NAME del` deletes one (admin role). A secret is write-only: pipe it
with `trax env secret NAME to -` or point at a file with `to @FILE`. A literal
secret on the command line is refused, and a listing shows `(secret)`, never a
value. Never echo a secret into a command, a row or your own output.

## Hosts

The registry of hosts campaigns may run on, and GPU jobs, moved out of trax:
`trax host` and `trax job` say so. A shell the web app opens sets
`TRAX_WORKSPACE` and `TRAX_CANVAS`, and
`LC_TRAX_*` copies that survive `ssh`; `trax` reads `TRAX_X`, else `LC_TRAX_X`, and
uses the saved profile whose URL is `LC_TRAX_SERVER`.

## Agent runs

Running Claude Code and Codex recorded, as an AgentSession reachable from Chat,
moved out of trax; `trax run` says so. `trax` reads the records they write
(`trax agentsession N`, `trax agentsession N records`) and sends a message into a
live session with `trax send @NAME TEXT`.

## Recovery

- If `trax` fails silently or exits 75, try a read with `TRAX_NO_DAEMON=1`.
  If it succeeds, restart the local trax daemon and retry normally once.
  Notify the user of the restart and result. Before repeating a failed write,
  check whether it already applied.
- Server unreachable or auth error: `trax profile`, then `trax profile url to
  ...` / `trax profile token to ...` / `trax profile current <name>`. Ask the
  user before changing profile settings on a shared host. A new key comes from
  `trax profile <name> login <url>`: it prints a code the user approves in a browser,
  so run it only with the user at hand.
- Abandoning work without losing history:
  `trax issue 7 status to abandoned --reason "..."`. Use `del` only when the
  row should disappear entirely.
