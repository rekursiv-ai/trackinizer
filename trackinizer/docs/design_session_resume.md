# Design: The runner resume -- what is captured, and how it re-attaches

Status: implemented. The capture surface below was measured on 2026-10-18
against claude 2.1.302 and codex 0.163.0, each driven in tmux under
the runner's `<cli> --no-sync --out` and compared with the files the CLI wrote.
Supersedes the lazy-open + "requested name" behavior documented as a known
gap in `design_session_messaging.md`.

## The rule

A run captures the session it STARTS, or the one it is TOLD it continues.
Every transcript that existed before the CLI started is skipped as an earlier
or concurrent run's, and every file under a session id the run does not own
is dropped. So whatever the CLI moves into after it starts -- another
session, a copy, a background session -- is not captured.

`trax agentsession <seq> run <cli>` is the supported way to resume. A resume
typed after `--` is captured only when it names the session by id; anything
else runs uncaptured, and the runner says so.

## What the CLIs do

### claude (2.1.302)

- A session is `<config>/projects/<encoded-cwd>/<session-id>.jsonl`,
  append-only, named by a lowercase dashed UUID. Subagents write
  `<project>/<session-id>/subagents/agent-<id>.jsonl`; a workflow's agents
  write `.../subagents/workflows/wf_<id>/agent-<id>.jsonl` beside a
  `journal.jsonl`.
- `--session-id <uuid>` names a new session. Claude refuses it beside
  `--resume` or `--continue` unless `--fork-session` is also given: "--session-id
  can only be used with --continue or --resume if --fork-session is also
  specified."
- `--resume <id>` (also `--resume=<id>`, `-r <id>`, `-r<id>`, `-pr <id>`)
  appends to that session's EXISTING file and keeps its id. From another
  directory it appends to the file under the directory where the session
  started, not under this one.
- `--resume` with no value, or with any value that is not the exact id
  (a search term, an uppercase or dashless id, `-r=<id>`), opens the picker.
  `--continue` / `-c` continues the most recent session in this directory.
  Either way claude chooses the session after it starts.
- `--fork-session` beside a resume copies the history into a NEW file under a
  new id.
- Inside a session: `/clear` (aliases `/reset`, `/new`) starts a new id and
  file; `/resume` switches to another session's file; `/branch` starts a new
  id holding a copy of the history; `/fork` continues a copy as a background
  session under its own id while this one carries on; `/compact` stays in the
  same session.
- `~/.claude/sessions/<pid>.json` holds the process's CURRENT `sessionId`; it
  changed on `/clear`, `/resume` and `/branch`. It is undocumented.

### codex (0.163.0)

- A session is `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ISO>-<uuid>.jsonl`,
  append-only. A fork's first line carries `forked_from_id`; a subagent's
  carries `parent_thread_id` and `source: {"subagent": ...}`.
- `codex resume <id>` and `codex exec resume <id>` append to the EXISTING
  rollout (2 lines became 13; no new file). `resume --last`, the picker, and a
  session name leave codex to choose.
- `codex fork <id>` writes a new rollout.
- Inside a session: `/new` and `/clear` start a new rollout; `/resume`
  switches to an existing one; `/fork` starts a new rollout and the TUI
  carries on in it; `/compact` stays in the same rollout.

## How a run is bound to its session

1. **Baseline.** Every transcript present before the spawn is skipped. This is
   what keeps an earlier session, or a concurrent run's, out of this one.
2. **Owner.** A new claude session gets an id trax mints and passes as
   `--session-id`; a codex run claims the rollout its child holds open. Files
   under any other id are dropped.
3. **Scope.** A claude file outside the cwd's project directory is dropped.
4. **`resume_path`.** The one exemption from 1 and 3: it is followed from
   offset 0, so the turns already stored land back on their own keys, and its
   id goes to the server at open, which re-attaches the AgentSession that
   captured it.

`resume_path` comes from `trax agentsession <seq> run`, which materializes the
stored session as the CLI's own file, or from a resume typed after `--` that
names an id whose transcript exists. That one is searched for by id under the
CLI's whole session root, so a session started in another directory is found
where claude keeps writing it. Both are decided in `main`, before a
`--detach` host forks, so a warning reaches the user's terminal rather than
the host's log. A fork of either follows nothing: it never writes the old
file, so the run treats it as a new session and mints its id.

## What is captured

Evidence: **M** measured live on 2026-10-18; **T** asserted by a unit test;
**I** inferred from the CLI's help or the code.

### Starting the run

| Command | The CLI | Captured | Evidence |
|---|---|---|---|
| The runner | new session, id from trax | yes | M |
| `trax agentsession <seq> run claude` or `... run codex` | appends to the materialized file | yes; re-attaches | T |
| `trax agentsession <seq> run claude -- --fork-session` | new file, new id | the fork, as a new session | T |
| The runner's `-- --resume <id>`, any spelling above | appends to the existing file | yes, from its start; re-attaches | M, T |
| the same, for a session started in another directory | appends to THAT directory's file | yes | M (claude `-p`), T |
| The runner's `-- --resume <id> --fork-session`, `-c --fork-session` | new file, new id | yes, as a new session | T |
| The runner's `-- -c`, `--resume`, `--resume <term>`, `-r=<id>`, an id with no transcript | claude picks | no; warned at start | M, T |
| The runner's `-- resume <id>` | appends to the existing rollout | yes, from its start; re-attaches | M (codex), T |
| The runner's `-- resume --last`, the picker, a name, an id with no rollout | codex picks | no; warned at start | T |
| The runner's `-- fork <id>` | new rollout | yes | M |

### Inside the running session

| Command | The CLI | Captured after it | Evidence |
|---|---|---|---|
| claude `/clear`, `/reset`, `/new` | new id and file | no; warned at exit | M |
| claude `/resume` back to this run's session | its own file | yes | M |
| claude `/resume` to another session | that session's file | no; warned at exit | M (claude), I |
| claude `/branch` | new id, copied history | no; warned at exit | M |
| claude `/fork` | background session under its own id; this one carries on | the fork no; this session yes | M |
| claude `/compact` | same session | yes | I |
| codex `/new`, `/clear` | new rollout | no; warned at exit | M (`/new`), I (`/clear`) |
| codex `/resume` back to this run's session | its own rollout | yes | M |
| codex `/resume` to another session | that rollout | no; warned at exit | I |
| codex `/fork` | new rollout; the TUI carries on in it | no; warned at exit | M |
| either CLI: a subagent or a workflow's agents | their own files | no | M |

Every typed slash command is still recorded as a `SlashCommand` row (9 of 9
in the live runs), so a reader of the transcript sees where it stopped.

## What is not captured, and why

- **A switch inside the session.** The run owns one session id for its whole
  life. Following the CLI into another session needs its CURRENT id: claude's
  `~/.claude/sessions/<pid>.json` (measured, undocumented), a claude
  `SessionStart` hook passed with `--settings` (documented), or codex's child
  holding a new rollout open. None is wired yet; the run warns at exit and
  names the commands it saw.
- **A resume the CLI picks.** The picker, `--continue` and `--last` choose
  after the CLI starts, so the run cannot name the file in advance. The
  mechanism above would cover these too.
- **Subagents and workflow agents.** Claude's adapter matches only
  `<project>/<session>.jsonl`, and codex's owner keeps one id. The importer
  already treats `<id>.jsonl` plus everything under `<id>/`, and codex
  rollouts chained by `forked_from_id`, as ONE session
  (`sagent/session/convert.py`, `_parts_of`); live capture should share
  that rule. Part names must then disambiguate: every workflow writes its own
  `journal.jsonl`. Tracked as a follow-up.

## Not measured

- gemini: neither its resume nor its session commands. Its adapter reports
  every run as a new session.
- claude `--from-pr`, `--teleport`, `/teleport`, `/rewind`, and `--bg`,
  `--cloud`, `--desktop`, which hand the session to another process.
- an interactive claude resume from another directory (measured in `-p` mode).

## Server: eager open and re-attach

### Requirements (decided)

1. **`--as` is a request; the server grants a guaranteed-unique handle.** A
   non-unique requested name is awarded `name`, then `name#2`, `name#3`, ...
   monotonically -- like a sequence. Handles are **never released or reused**
   (a session may resume later, so its name is reserved for its lifetime).
2. **The child must know its granted handle.** Inside the runner, the agent
   learns it is `scientist#2` (so `trax send`/peer addressing uses the real
   address). Exported as `TRAX_ACTOR`.
3. **The session opens eagerly, when the runner authenticates** -- not lazily
   on first event. Accepted cost: a run that captures nothing still creates a
   session row (a run that started *is* a session).
4. **Resume re-attaches to the existing session.** A resumed run continues the
   same `AgentSession`: same row, **same granted handle**, appended log (not a
   fresh `seq=0`). Resume is correlated by the **CLI's own session id**
   (claude's `sessionId` = the `<session-id>.jsonl` filename; codex's thread
   id), carried in `SessionStart.cli_session_id`.
5. **A run stamps `cli_session_id` as soon as it knows it**: at open for a
   resume and for a fresh claude run (trax minted the id), at close for a fresh
   codex run (codex reveals its id only by writing).

### Model

Three identities stay distinct (this is the root fix for #453):

| Concept | Field | Unique? | Minted by |
|---|---|---|---|
| principal | `api_key_id` / `email` | yes (account) | auth |
| actor (provenance) | `change_log.actor` | no | caller's `--as` |
| routing handle | `inquiries.owner` | yes (all sessions) | server, increment |

The handle is the session's permanent address. `actor` on audit rows is the
free provenance string. They were conflated; the only place they must agree is
the session's own `owner == granted handle`.

### Uniqueness is a reservation property, NOT a widened index

Requirement 1 ("never reuse a handle") is a *reservation* property, not a
live-uniqueness one. It is enforced in `reserve_session_actor` by reading
**all** session owners (drop `AND agentsession_ended IS NULL` from its SELECT)
when picking the next free `#N`.

The DB index `uq_inquiries_live_session_owner` stays **unchanged** (live-only).
It is a race backstop for concurrent live starts; resume correlates by
`cli_session_id`, not owner, so it needs no widened index.

**No schema migration.** Widening the index to "all sessions" would crash-loop
bootstrap on real data -- two *ended* sessions legitimately share an owner
today (the live index permits it once one ends), so a stricter `CREATE UNIQUE
INDEX` fails. Renaming historical owners to satisfy it would also desync the
audit trail (`owner == change_log.actor` for the create event), violating the
Model invariant above. So reservation, not migration, carries "never reuse".

### Resume correlation + re-open at `start_session` (NOT at append)

`start_session` has a pre-step: if `req.cli_session_id` is **non-null** and
matches an existing AgentSession's `agentsession_cli_session_id`, **re-attach**
-- return that session's `(id, owner)`, do not mint a new handle or row. If
that session is ended, **re-open it in the same step**: clear
`agentsession_ended`, flip status complete->active, in one statement under the
lifecycle CHECK (mirroring `end_session`'s atomic live->ended move).

The match SQL must guard non-null (`WHERE agentsession_cli_session_id = $1`,
$1 non-null) -- never `IS NOT DISTINCT FROM NULL`, which would correlate every
fresh (null-id) run to the first null-id row.

**Re-open lives ONLY here, not in the append path.** The append keeps the
#465 ended-guard verbatim ("session has ended; cannot append"). Because
`start_session` runs once at auth, before any append, a re-opened session is
already live by the time the sink appends -- append never sees an ended
session, and #465's zombie-guard stays intact and meaningful. (An earlier draft
re-opened inside the append; that contradicted #465 and smeared re-open
across the hot path. Dropped.)

**Seq seeding -- OBSOLETE.** This was load-bearing while the harness assigned
`seq`: the sink hardcoded `_next_seq = 0`, so a resumed run collided every seq
against the PK and silently dropped the resumed log, and the sink had to seed
from `SessionStartResponse.seq`.

The IR removed the failure mode rather than the fix. A record's `idx` is
DERIVED from its position in its file's normalized stream, and a resumed run
reads a file whose positions are what they always were -- so a re-fed record
lands back on the key it already holds instead of colliding at 0. There is no
counter to seed; `SessionStartResponse.seq` is vestigial and unread.

### Eager open -- gated on sync

`run` opens the session at auth, before
`ThreadedRelay(env=_routing_env(...))`, **only on the sync path**
(`config.sync and config.client is not None`). A `--no-sync` / `--out` /
`--dry-run` run has no server, no client, no session (`FileSink.session_id`
is permanently None) -- it must NOT eager-open or it crashes / mints a
phantom row. Local runs keep exporting the requested `config.actor` (no
collision arbiter exists offline).

On the sync path:

1. Name the `cli_session_id` known up front: a resume's (see "How a run is
   bound to its session"), or the id trax minted for a fresh claude run. A
   fresh codex run has none yet.
2. `session_start(SessionStart(cli, actor, rooms, cli_session_id))` ->
   `(session_id, granted_owner)`.
3. Build `_routing_env` with `granted_owner` (not the requested name).
4. Fork the child with that env.

### A fresh codex run's id is backfilled at close

Codex mints its rollout id after it starts, so a fresh codex run opens with
`cli_session_id=None` and backfills the id at **close**, via `end_session`'s
backfill path. Consequence: a codex run killed before it closes is ended
unstamped, and a later `codex resume <id>` of it opens a second AgentSession.
A fresh claude run has no such gap: trax mints its id and sends it at open.

## Non-goals

- Reusing a freed handle (handles are monotonic; never reclaimed on collision).
- Cross-principal resume (a resume is scoped to the authenticated principal).
- Capturing the agent's outbound `trax send` as a structured event (#452,
  closed -- already a tool-call in the log).

## Risks

- **Empty rows.** Eager open creates a row even for a zero-event run. Accepted
  (requirement 3). Mitigation: none needed -- a started run is a real session.
- **Re-open lifecycle.** Appending to an ended session re-clears `ended`; the
  lifecycle CHECK and the live-owner uniqueness must both hold across that
  transition.
- **CLI drift.** The resume flags, the refusal of `--session-id`, and where
  each CLI writes are the CLIs' behavior, not a contract. The adapters' parse
  tests pin each spelling; re-measure on a new CLI version before trusting
  this document's tables.
