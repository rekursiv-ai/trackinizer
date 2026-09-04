# Trackinizer Session-Records API

Companion to [`api.md`](api.md); read that first. This doc composites the
agent-session capture subsystem in total: the `AgentSession` artifact kind,
the four `session_*` tables that hold a captured session as IR records, the
ingest endpoints, and their wire shapes. Sources of truth:
`types/inquiries.py` (`AgentSession`), `types/session_records.py`
(`SessionRecordRow`), the `session_*` DDL in `server/assets/schema.sql`, and
`wire/wire_session_ir.py` + `wire/wire_sessions.py` (the wire bodies).

The record vocabulary itself is NOT trackinizer's: it is the shared session
IR, `trackinizer.lib.agent.types.sessions`. One vocabulary reads a session, stores
it, and writes it back out as any CLI's native format, so this doc describes
where records live rather than what they mean.

## 1. Two layers

```
AgentSession       Artifact row in `inquiries`; the macro/envelope;
                   queryable, edge-able, supersede-able via api.md.
session_records    Separate append-only table, NOT an Inquiry;
                   record-grained rows keyed to a session by `session_id`;
                   no edges, no cost, no change_log, no supersession.
```

## 2. `AgentSession` (Artifact kind)

### 2.1 Kind tokens

```
AgentSession    InquiryKind / Artifact.Kind value (PascalCase)
agentsession    URL kind token (kind.lower())
```

### 2.2 Kind-specific fields

```
field            python              sql column                   type         PUT PATCH DELETE
cli              .cli                 agentsession_cli             TEXT         yes no    yes
cli_session_id   .cli_session_id      agentsession_cli_session_id  TEXT         yes no    yes
started          .started             agentsession_started         TIMESTAMPTZ  yes no    yes
ended            .ended               agentsession_ended           TIMESTAMPTZ  yes no    yes
```

No `cwd` / `model` on the session row: both can change mid-session, so a
single value would be lossy. Per-record model lives on
`session_records.model` (§3.1); per-record cwd rides the record's own
provider residual.

`cli_session_id` is load-bearing for resume, not decoration: `_resume_session`
(`server/store/session.py`) re-attaches a run by matching it, so a resume
stamps a freshly minted uuid there BEFORE the run opens (§9.2).

Inherited `Inquiry` base fields (`status`, `owner`, `summary`,
`description`, `labels`, `subscribers`, `marginal_cost*`, `id`, `seq`,
`created`, `modified`) per api.md §2.3.

### 2.3 Session routes (standard inquiry machinery, api.md §1.3/1.5/1.7)

```
POST   /api/inquiries/agentsession
GET    /api/inquiries/<uuid>
GET    /api/inquiries/agentsession/<seq>
GET    /api/inquiries?kind=agentsession
DELETE /api/inquiries/<uuid>
PUT    /api/agentsession/<uuid>/cli
PUT    /api/agentsession/<uuid>/cli_session_id
PUT    /api/agentsession/<uuid>/started
PUT    /api/agentsession/<uuid>/ended
DELETE /api/agentsession/<uuid>/cli
DELETE /api/agentsession/<uuid>/cli_session_id
DELETE /api/agentsession/<uuid>/started
DELETE /api/agentsession/<uuid>/ended
```

## 3. Storage: four tables (`server/assets/schema.sql`, migration 019)

### 3.1 `session_records` (typed by `types/session_records.py`)

```
column       type         notes
session_id   UUID         NOT NULL; FK -> inquiries(id) ON DELETE CASCADE
part         INTEGER      NOT NULL DEFAULT 0; which SOURCE FILE (§3.5)
idx          INTEGER      NOT NULL; CHECK >= 0; DERIVED position (§3.6)
kind         TEXT         NOT NULL; the record class's name
context_id   INTEGER      nullable; idx of the applying TurnContext, same part
timestamp    TIMESTAMPTZ  nullable; when the act happened (CLI clock)
created      TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(); DB write time
model        TEXT         nullable; denormalized from the applying TurnContext
payload      JSON         NOT NULL; the record as DataclassCodec JSON
text         TEXT         NOT NULL DEFAULT ''; the search projection (§3.7)
search       tsvector     GENERATED ALWAYS AS to_tsvector('simple', text) STORED
PRIMARY KEY (session_id, part, idx)
CHECK (context_id IS NULL OR context_id <= idx)
```

`payload` is `JSON`, **not `JSONB`**, and the distinction is load-bearing:
jsonb REORDERS object keys and drops duplicates, so a payload written
`{"addedNames":...,"addedLines":...}` reads back sorted and the rewritten
session file differs from the one the CLI wrote. That breaks byte-exact
round-trip, which resume depends on. Nothing queries inside the column (it is
written whole and read whole), so jsonb's indexing buys nothing to pay for it.
`session_manifests.metadata` is `JSON` for the same reason.

`context_id` uses `<=`, not `<`: a claude `TurnContext` is appended at its own
index and names ITSELF, where a codex stream has none until its first
`turn_context` line.

`'simple'`, not `'english'`: a generated column needs an IMMUTABLE expression
so the config must be a literal either way, and stemming mangles the
identifiers and paths that fill a transcript.

### 3.2 `session_manifests` -- one row per part

```
column      type      notes
session_id  UUID      NOT NULL; FK -> inquiries(id) ON DELETE CASCADE
part        INTEGER   NOT NULL DEFAULT 0
name        TEXT      NOT NULL; the file's BASENAME, never its path
metadata    JSON      NOT NULL; the whole SessionMetadata, encoded
ir_id       UUID      NOT NULL; the Session.session_id the file declared
format      TEXT      NOT NULL; the convert adapter, or '' (§9.3)
records     INTEGER   NOT NULL; CHECK >= 0; live prefix bound
PRIMARY KEY (session_id, part)
UNIQUE (session_id, name)
```

`name` is a basename because a path differs across machines and the same
session resumed elsewhere must resolve to the same part. The `UNIQUE`
serializes part assignment: two appends racing on an unseen file both compute
`max(part)+1`, and the loser re-reads the winner's.

`records` is a live prefix bound -- every reader takes `idx < records` -- so a
file that SHRANK leaves its tail rows inert rather than deleted.

`metadata` is not decoration: claude's ascii-escaping convention (a majority
flag plus its exception bitmap) rides in `SessionMetadata.extra`, and the
codex launch line is rebuilt from it. Rewriting without it yields a file that
differs from the captured one while every record matches.

### 3.3 `session_ciphertext` -- the sealed half, split out

```
column      type      notes
session_id  UUID      NOT NULL; FK -> inquiries(id) ON DELETE CASCADE
part        INTEGER   NOT NULL DEFAULT 0
idx         INTEGER   NOT NULL; CHECK >= 0
bytes       BYTEA     NOT NULL; STORAGE EXTERNAL
PRIMARY KEY (session_id, part, idx)
```

Split from the record so retention can drop ciphertext without touching what
search indexes. `Thinking.encrypted` is the IR's only ciphertext, so the
record's own key suffices; the reader splices it back (§7).

Stored verbatim as base64 ASCII, NOT decoded: claude writes standard base64
and codex base64url, so one decode/encode pair cannot round-trip both.
`STORAGE EXTERNAL` because ciphertext does not compress -- measured 1.025x
under pglz (it EXPANDS), against 0.405x for plaintext of the same table.

### 3.4 `session_slash_commands` -- what the log cannot see

```
column      type         notes
session_id  UUID         NOT NULL; FK -> inquiries(id) ON DELETE CASCADE
seq         INTEGER      NOT NULL; CHECK >= 0; SERVER-assigned
timestamp   TIMESTAMPTZ  NOT NULL; the keystroke detector's submit clock
created     TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
command     TEXT         NOT NULL; the verb without its slash
args        TEXT         NOT NULL DEFAULT ''
PRIMARY KEY (session_id, seq)
```

A slash command (`/exit`, `/model gpt-5`) is handled inside the CLI's TUI and
never written to its session log, so it is captured by observing the human's
keystrokes on the PTY (`trax/run/slash.py`). It is **not a record**: a record
has an `idx`, its position in a file's normalized stream, and a command that
was never written to any file has none. Consuming an `idx` for one would
renumber every record after it, and a replay -- which re-derives positions
from the file -- would then disagree with what was stored.

`seq` is server-assigned (`max(seq)+1`) because a sink counter restarts at 0
on a resumed run and would collide. Not in search: a result keys
`(session_id, part, idx)` and a command has no such coordinate.

### 3.5 Parts: a session is several files

A session spans several FILES -- claude splits on compaction, codex forks --
and ingest tails them as they appear, so it cannot fuse (fusing needs every
part up front). Each file is one `part`, with `idx` from 0 within it.
Read-time order is still the provider's own: the manifest keeps each part's
`SessionMetadata`, which carries the fork link `fuse.chain` resolves.

`part = -1` is RESERVED for turns backfilled from the retired
`agent_session_events` table (§10). A session captured before the IR and
resumed after has real records at `part = 0`, and mapping the legacy
turn-space onto that part would collide with no offset that reconciles them.

### 3.6 Why `idx` is derived

`idx` is the record's position in its file's normalized stream, never counted
by the writer. A claude compaction REWRITES the session file, so the runner
re-feeds lines it already sent; a counter would append a second copy of every
retained record, while a derived `idx` lands each one back on the key it
already holds. That is what makes ingest idempotent, and it is why the runner
needs no line-level dedup.

### 3.7 The `text` projection

`text` is computed once at ingest by `types/session_records.py::search_text`
and never re-derived. Two consequences:

- The legacy backfill writes a `text` that rule would compute as `''` (its
  payloads are `UncategorizedRecord`), so a reindex would erase legacy
  searchability.
- `Thinking.encrypted` is excluded by construction. It lives in
  `session_ciphertext` precisely so dropping it leaves the record searchable,
  which a projection that indexed it would defeat.

Capped at `MAX_SEARCH_TEXT_BYTES` (1,000,000) in BYTES, not characters: the
column is `GENERATED ... STORED`, so an oversized value aborts the INSERT
rather than degrading, and Postgres bounds a tsvector by encoded size. A
character cap would let 4-byte codepoints past it.

### 3.8 Indexes

```
PRIMARY KEY (session_id, part, idx)   ordered per-part read + dedup
idx_session_records_search            GIN (search)
idx_session_records_kind              (session_id, kind)
```

### 3.9 Properties

```
append-only           records are never edited/superseded/cited; a restart
                      OVERWRITES a part (§5.4), which is replacement, not edit
dedup key             (session_id, part, idx); re-append is ON CONFLICT
adjacent universe     deliberately outside change_log; records are not Change
                      rows by design (see design.md "Everything is provenance"):
                      a captured act is not a knowledge mutation
not an Inquiry        no edges, cost, cascade, supersession, embeddings
tenant scope          derived by joining to inquiries (no org_id column)
cascade               purge of the AgentSession row deletes its records
audited boundary      AgentSession-artifact edits DO flow through change_log
                      (§2.2 field routes); only record ingest does not
timestamp vs created  timestamp = the CLI's clock; created = DB write
notify                a record append DOES wake /api/web/subscribe, unlike
                      experiment_metrics: a live viewer follows a capture
```

## 4. Wire / domain types

### 4.1 `RecordBody` (`wire/wire_session_ir.py`)

```
field       type       notes
idx         int        >= 0; DERIVED by the sender, never counted (§3.6)
kind        str        min_length 1; the record class's name
context_id  int?       >= 0; may EQUAL idx (§3.1)
timestamp   datetime?
model       str?
payload     JSON       default {}; DataclassCodec JSON, ciphertext removed
text        str        default ''; the search projection, computed at ingest
ciphertext  str?       Thinking.encrypted verbatim, base64 ASCII
```

`RecordBody.of(row)` / `.row(session_id, part)` convert to and from
`SessionRecordRow`. `kind` is open TEXT, not a closed Literal: the IR has 21
concrete members and gains more.

Two fields the client never decides. `part` is absent entirely -- it is
resolved server-side from the file's basename, so a restarted or resumed
client cannot invent a conflicting number. `text` is recomputed at ingest, so
a client cannot decide what its own transcript matches.

`ciphertext` rides BESIDE the payload rather than inside it, so the server can
store it under the record's own key in `session_ciphertext` and retention can
drop every session's ciphertext without rewriting a single record.

### 4.2 `ManifestBody` / `PartBody`

```
ManifestBody   name (min_length 1), metadata JSON, ir_id uuid,
               format str = '', records int >= 0
PartBody       part int >= 0, name, format, records int >= 0,
               metadata JSON, ir_id uuid?
```

`ManifestBody` is re-sent with EVERY append batch, not written once, because
it CHANGES as the file grows: `records` counts what has arrived, and the
metadata a normalizer reports is only correct for the prefix it consumed --
claude's ascii-escaping convention is a majority not decided until the stream
ends.

### 4.3 `SlashCommandBody`

```
field      type       notes
timestamp  datetime   required; the only clock a typed command has
command    str        min_length 1; the verb without its slash
args       str        default ''
```

No `seq`: the server assigns one (§3.4).

### 4.4 `SessionStart` / `SessionEnd` (`wire/wire_sessions.py`)

```
SessionStart     cli (min_length 1), cli_session_id?, title?, started?,
                 actor?, account?, rooms?, idempotency_key?
SessionEnd       ended?, cli_session_id?, actor?
```

Every scalar string field rejects whitespace-only values: each is matched or
stored verbatim, so `"   "` can never match and is a client bug. A `rooms`
element additionally rejects a comma -- `trax run` exports rooms comma-joined
into `TRAX_ROOMS`, so a room `'a,b'` is indistinguishable from two rooms.

### 4.5 Response types

```
SessionStartResponse   {id: uuid, seq: int, cli_session_id: str?, actor: str?}
AppendRecordsResponse  {part: int?, written: int, skipped: int,
                        slash_commands: int}
ReadPartsResponse      {parts: [PartBody]}
ReadRecordsResponse    {part: int, records: [RecordBody]}
SessionEndResponse     {id: uuid, ended: datetime?}
```

`SessionStartResponse.seq` is vestigial: it continued the retired event log's
numbering and no client reads it.

`AppendRecordsResponse.part` is `None` exactly when the request named no file.

## 5. Ingest endpoints

### 5.1 Routes

```
POST /api/sessions/start
POST /api/sessions/<session_id>/records
GET  /api/sessions/<session_id>/records
GET  /api/sessions/<session_id>/parts
POST /api/sessions/<session_id>/end
```

There is deliberately **no `search` route**. Matching records is a filter on
the AgentSession list (`trax agentsession tool_call re bar`, §8), not a second
query surface with its own grammar.

### 5.2 Per-route

```
route                               role    body / params         response               codes
POST /api/sessions/start            writer  SessionStart          SessionStartResponse   201 401 403 422
POST /api/sessions/<id>/records     writer  AppendRecordsRequest  AppendRecordsResponse  200 401 403 404 422
GET  /api/sessions/<id>/records     viewer  (query params §5.5)   ReadRecordsResponse    200 400 401 403 404
GET  /api/sessions/<id>/parts       viewer  -                     ReadPartsResponse      200 401 403 404
POST /api/sessions/<id>/end         writer  SessionEnd            SessionEndResponse     200 401 403 404 422
```

### 5.3 `AppendRecordsRequest` body

```
{
  "name": "<basename>",         // '' only on a slash-command-only append
  "manifest": <ManifestBody>,   // null exactly when name is ''
  "restart": false,
  "records": [<RecordBody>, ...],        // max 1000
  "slash_commands": [<SlashCommandBody>] // max 1000
}
```

ONE part per request. A model validator enforces the pairing: records need a
part, and a part needs a named file with its manifest. A body naming no file
carries only slash commands -- a command typed before the CLI has written a
transcript belongs to the SESSION, not to any file.

`MAX_RECORD_BATCH` is 1000. A tailer batches whatever a wake delivered and a
claude compaction re-feeds a whole file at once, so the natural batch is
unbounded unless the wire caps it.

Slash commands ride the record append rather than a route of their own, so a
run interrupted mid-flush cannot leave a command stored without the turns
around it, or the reverse.

### 5.4 Semantics

```
start            mints an AgentSession row (server id); dedups on
                 idempotency_key; resumes an existing row when
                 cli_session_id matches one (§9.2)
records (POST)   upserts the manifest FIRST (it assigns the part and bounds
                 every reader), then appends. Normal batch: ON CONFLICT DO
                 NOTHING -- the runner re-feeds routinely and the stored row
                 is already correct. restart=true: ON CONFLICT DO UPDATE,
                 because a compaction is a REPLACEMENT and disk is truth.
                 Records and ciphertext share one transaction, so a record
                 can never be readable while its ciphertext is missing.
end              backfills ended/cli_session_id; sets status=complete
unknown session  any <id> not an AgentSession row -> 404
```

### 5.5 GET query params

```
records:
param           type   default  notes
part            int    0        >= 0 else 400
after_idx       int    -1       EXCLUSIVE lower bound, not an offset
limit           int    50       min 1; max 1000 else 400
plaintext_only  bool   false    skip the ciphertext splice

parts: none
```

`after_idx` is an exclusive bound rather than an offset so paging is stable
while a capture is still appending: an offset would re-window every time the
part grew.

`plaintext_only` is what a VIEWER wants -- only a replay needs the encrypted
half, and it is the largest thing on the row.

### 5.6 Divergence from api.md

```
not in api.md §1     these routes live only here (the sessions namespace)
no change_id         responses carry no change_id: records are the adjacent
                     universe, deliberately outside change_log (§3.9)
no Idempotency-Key   header unused on /records; dedup is the derived
                     (session_id, part, idx), the correct primitive for a
                     stream re-fed from disk
start idempotency    via SessionStart.idempotency_key (mirrors inquiry create)
read pagination      keyset (after_idx), not the limit/offset api.md 4.3 uses
```

## 6. Large payloads (TOAST, no app-level offload)

```
storage          payload JSON stays whole on the row; no blob sidecar
mechanism        Postgres TOAST transparently out-of-lines large values
ciphertext       the ONE exception: split to its own table so retention can
                 drop it (§3.3), and STORAGE EXTERNAL because it does not
                 compress
```

## 7. Store seam (`server/store/session_ir.py`, `session.py`)

```
submit_agentsession(SubmitAgentSession, *, api_key_id=, actor=) -> UUID
append_session_records(session_id, rows, *, restart=False,
                       slash_commands=()) -> (written, skipped, slash)
read_session_records(session_id, *, part, after_idx=-1, limit=500,
                     plaintext_only=False) -> list[SessionRecordRow]
upsert_session_manifest(session_id, *, name, metadata, ir_id, format,
                        records) -> part
read_session_manifests(session_id) -> list[SessionManifest]
read_session_slash_commands(session_id) -> list[SlashCommandRow]
read_feed(*, after=, since=, until=, room=, actor=, limit=, tail=)
    -> list[FeedEvent]
```

```
swap point       append_session_records / read_session_records are the only
                 record-store touchpoints
paginated read   read_session_records bounds the window so a large session
                 never materializes whole in one call
ciphertext       spliced back on read unless plaintext_only; the row type
                 itself never splices (it holds one row; the bytes are in
                 another table)
```

## 8. Querying records (`trax`)

Record kinds are LIST-shaped filter columns on the `AgentSession` row, not a
second query surface:

```
trax agentsession tool_call re bar
trax agentsession shell_command_result has "pg_advisory_lock"
```

Each lowers to a correlated `EXISTS` over `session_records` with the kind
BAKED INTO the template, never passed as an operand. Which kinds qualify is
DERIVED, not listed: a class is a filter field iff its `search_text`
projection can be non-empty (`wire/session_record_fields.py`), so a filter on
`TurnContext` -- which indexes nothing -- does not exist rather than silently
never matching.

## 9. Capture and resume (`trax/run/`)

### 9.1 Capture

```
trax run <cli> [--no-sync] [--out PATH] -- <cli args>
```

```
adapters         claude, gemini, codex (file-tailers); iostream (PTY scrape)
normalizer       one per FILE, from trackinizer.lib.agent.sessions; a session spans
                 several files, and sharing one would number the second
                 file's records after the first's
in-memory type   the IR record itself -> wire RecordBody at the sink
idx              DERIVED from stream position (§3.6), never harness-assigned
server           resolved from the active trax profile (URL + auth)
sink (default)   TrackinizerSink -> start / append_records (batch) / end
sink (--no-sync) FileSink -> local JSONL (also --out PATH, --dry-run)
```

### 9.2 Resume

```
trax agentsession 42 run claude [--lossy] [-- <cli args>]
```

Order of operations, and the order is load-bearing:

1. `PUT /api/agentsession/<id>/cli_session_id` with a freshly minted uuid.
2. Materialize the newest native part at the path that uuid names.
3. Enter the runner with `--resume <uuid>`, `resume_path=<file>`.

Step 1 must precede the run: `_resume_session` re-attaches by finding an
EXISTING row whose `cli_session_id` matches, so without the stamp the run
forks a second AgentSession.

The uuid is minted FRESH -- reusing the captured one would collide locally
and, for a codex capture, name an id claude never issued. The path derives
from the LOCAL cwd, so resuming in another directory is expected. The
`sessionId` inside the file is rewritten to the minted uuid, which is
REQUIRED and not belt-and-braces: the writer replays the id each record
states, so a file rewritten without it names the session it came from while
its filename names the new one.

Loss is MEASURED, not predicted from the format pair: the conversion is
performed, re-read, and the record-kind counts compared. `--lossy` gates
proceeding when they differ.

### 9.3 Which targets resume

```
target   resumable  why not
claude   yes        -
codex    no         session_id_from_path returns None: no stable per-session id
gemini   no         rewrites one document in place; no resume-by-id
sh       no         no native log (format = '')
```

Any other target is a 422 naming it. All four stay downloadable. The
cross-CLI case that matters still works: a codex-captured session resumed as
claude, because the records are provider-neutral.

A part whose `format` is `''` is searchable but NEVER resumable -- there is no
native file its records rewrite as, so resume refuses rather than producing
one the CLI would reject. Every legacy backfilled part (§10) is such a part.

## 10. Legacy: the retired `agent_session_events` table

Migrations 020 (backfill) and 021 (drop) retired the 8-member `Message`
union. What a reader still sees:

```
part          -1, a reserved namespace (§3.5)
kind          always UncategorizedRecord -- the union it came from was lossy,
              so promoting a row to a typed IR record would assert structure
              the capture never recorded
payload.kind  'legacy/<Kind>', the original discriminator as provenance
ciphertext    moved to session_ciphertext, thinking_encrypted concatenated
              with thinking_signature in the order the provider wrote them
manifest      one per legacy session, format = '', records = max(seq)+1
```

020 is guarded for a FRESH install, where no legacy table exists: the whole
block is a `DO` with its statements `EXECUTE`'d as text, because PL/pgSQL
parses a block at compile time and a plain `IF to_regclass(...)` fails before
the guard can run.

## 11. Client SDK (`client/client.py`)

```
session_start(SessionStart) -> SessionStartResponse
append_records(session_id, *, name='', manifest=None, records=(),
               restart=False, slash_commands=()) -> AppendRecordsResponse
read_session_parts(session_id) -> list[PartBody]
read_session_records(session_id, *, part=0, after_idx=-1, limit=1000,
                     plaintext_only=False) -> list[RecordBody]
set_cli_session_id(session_id, cli_session_id, *, actor='agent') -> None
session_end(session_id, SessionEnd | None) -> SessionEndResponse
```

```
idempotency      session_start mints idempotency_key when omitted
read             read_session_records PAGES until the part ends, rather than
                 trusting one request: a real transcript exceeds any page, and
                 a truncated materialization is a file the provider rejects --
                 or worse, accepts as a shorter conversation than happened
```

## 12. Scale tiers (`docs/db_schema_migration.md`)

```
phase 0   plain Postgres session_records table
phase 1   Timescale hypertable (blocked: dedup PK excludes partition col)
phase 2   ClickHouse for session_records (> ~1e9 rows)
phase 3   Parquet on object storage (cold tier)
```
