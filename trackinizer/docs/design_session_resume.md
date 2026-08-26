# Design: eager session open + `trax run` resume

Status: proposed. Owner: Agent. Supersedes the lazy-open + "requested name"
behavior documented as a known gap in `design_session_messaging.md`.

## Problem

`trax run claude --as scientist` today:

1. **Opens the session lazily** — on the first captured event, not at start.
   A run that captures nothing leaves no row; but the child has *already
   forked* before the session (and its granted handle) exist.
2. **Exports the requested name**, not the granted one. On a collision the
   server routes the session as `scientist#2` while the child still believes
   it is `scientist` (`session.py:_routing_env`). So `@scientist` misroutes —
   the child can't tell peers its real address (#453).
3. **Does not model resume.** Every `trax run` invocation mints a brand-new
   `AgentSession` (new id, new handle, fresh `seq=0` log). `trax run claude --
   --resume <id>` is passed verbatim to the CLI; trax never correlates the
   resumed run to the prior session, so its log forks and its handle is
   re-minted.

## Requirements (decided)

1. **`--as` is a request; the server grants a guaranteed-unique handle.** A
   non-unique requested name is awarded `name`, then `name#2`, `name#3`, ...
   monotonically — like a sequence. Handles are **never released or reused**
   (a session may resume later, so its name is reserved for its lifetime).
2. **The child must know its granted handle.** Inside `trax run`, the agent
   learns it is `scientist#2` (so `trax send`/peer addressing uses the real
   address). Exported as `TRAX_ACTOR`.
3. **The session opens eagerly, when `trax run` authenticates** — not lazily
   on first event. Accepted cost: a run that captures nothing still creates a
   session row (a run that started *is* a session).
4. **Resume re-attaches to the existing session.** A resumed run continues the
   same `AgentSession`: same row, **same granted handle**, appended log (not a
   fresh `seq=0`). Resume is correlated by the **CLI's own session id**
   (claude's `sessionId` = the `<session-id>.jsonl` filename; codex's thread
   id), carried in `SessionStart.cli_session_id` (the wire field already
   exists).
5. **Fresh runs backfill `cli_session_id`** once the CLI reveals it (claude
   mints its `sessionId` when it starts, possibly after trax auths).

## Model

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

`start_session` gains a pre-step: if `req.cli_session_id` is **non-null** and
matches an existing AgentSession's `agentsession_cli_session_id`, **re-attach**
— return that session's `(id, owner)`, do not mint a new handle or row. If that
session is ended, **re-open it in the same step**: clear `agentsession_ended`,
flip status complete→active, in one statement under the lifecycle CHECK
(mirroring `end_session`'s atomic live→ended move). The response returns
`max(seq)+1` so the sink continues the log.

The match SQL must guard non-null (`WHERE agentsession_cli_session_id = $1`,
$1 non-null) -- never `IS NOT DISTINCT FROM NULL`, which would correlate every
fresh (null-id) run to the first null-id row.

**Re-open lives ONLY here, not in `append_events`.** `append_events` keeps the
#465 ended-guard verbatim ("session has ended; cannot append"). Because
`start_session` runs once at auth, before any append, a re-opened session is
already live by the time the sink appends -- append never sees an ended
session, and #465's zombie-guard stays intact and meaningful. (An earlier draft
re-opened inside `append_events`; that contradicted #465 and smeared re-open
across the hot path. Dropped.)

**Seq seeding (load-bearing).** The sink hardcodes `_next_seq = 0` and ignores
the `SessionStartResponse.seq`. A resumed run would collide every seq against
the PK `(session_id, seq)` and silently `ON CONFLICT DO NOTHING`-drop the whole
resumed log. The sink MUST seed `_next_seq` from the response on re-attach.

### Eager open — gated on sync

`run` opens the session at auth, before `ThreadedRelay(env=_routing_env(...))`, **only
on the sync path** (`config.sync and config.client is not None`). A
`--no-sync` / `--out` / `--dry-run` run has no server, no client, no session
(`FileSink.session_id` is permanently None) — it must NOT eager-open or it
crashes / mints a phantom row. Local runs keep exporting the requested
`config.actor` (no collision arbiter exists offline).

On the sync path:

1. Determine `cli_session_id` if known up front (resume: the `--resume <id>`
   arg; fresh: unknown → `None`).
2. `session_start(SessionStart(cli, actor, rooms, cli_session_id))` →
   `(session_id, granted_owner, seq)`.
3. Build `_routing_env` with `granted_owner` (not the requested name).
4. Fork the child with that env.

The sink no longer opens lazily; `run` hands it an already-open `session_id`
and seeds `_next_seq` from the response `seq`.

### Fresh-run cli_session_id — backfilled at close

A fresh claude run's `sessionId` is unknown at auth (claude mints it after it
starts, naming `<session-id>.jsonl`). So a fresh run opens with
`cli_session_id=None` and backfills the id at **close**, via `end_session`'s
existing backfill path (already wired + tested). Consequence: a fresh run is
**not self-correlating** — it becomes resumable only on its *next* `--resume`.
This is acceptable per the best-effort framing below. Step "adapter extraction"
is therefore net-new: claude derives the id from the `.jsonl` filename (the
adapter currently parses line content and discards the path) and threads it to
the close.

## Non-goals

- Reusing a freed handle (handles are monotonic; never reclaimed on collision).
- Cross-principal resume (a resume is scoped to the authenticated principal).
- Capturing the agent's outbound `trax send` as a structured event (#452,
  closed — already a tool-call in the log).

## Open question (resume identification)

Resume keys on `cli_session_id`. For claude this is unambiguous (the `--resume`
arg, also the `.jsonl` filename). For a CLI that does **not** expose a stable
session id, resume degrades to "fresh session" (no correlation key → new row).
That is acceptable: resume is best-effort, gated on the CLI providing an id.

## Risks

- **Empty rows.** Eager open creates a row even for a zero-event run. Accepted
  (requirement 3). Mitigation: none needed — a started run is a real session.
- **Index migration.** Widening `live`→`all` uniqueness must not reject
  existing data: two *ended* sessions today may share an owner (the old index
  allowed it once one ended). The migration must detect and `#N`-rename
  collisions before adding the stricter index, or the `CREATE UNIQUE INDEX`
  fails. This is the highest-risk step — needs a real-PG parity test with
  seeded collisions.
- **Re-open lifecycle.** Appending to an ended session re-clears `ended`; the
  lifecycle CHECK and the live-owner uniqueness must both hold across that
  transition.
