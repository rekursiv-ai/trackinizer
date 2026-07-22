# Trackinizer Agent-Session-Events API

Companion to [`api.md`](api.md); read that first. This doc composites the
agent-session capture subsystem in total: the `AgentSession` artifact kind,
the separate `agent_session_events` table, the ingest endpoints, and their
wire shapes. Sources of truth: `types/inquiries.py` (`AgentSession`),
`types/agent_session_events.py` (`AgentSessionEvent`), the
`agent_session_events` DDL in `server/assets/schema.sql`, and
`wire/wire_sessions.py` (the ingest wire bodies).

## 1. Two layers

```
AgentSession          Artifact row in `inquiries`; the macro/envelope;
                      queryable, edge-able, supersede-able via api.md.
agent_session_events  Separate append-only table, NOT an Inquiry;
                      turn-grained rows keyed to a session by `session_id`;
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
single value would be lossy. Per-turn model lives on
`agent_session_events.model` (§3.1); per-turn cwd is not tracked.

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

## 3. `agent_session_events` table

### 3.1 Columns (`server/assets/schema.sql`; typed by `types/agent_session_events.py`)

```
column       type         notes
session_id   UUID         NOT NULL; FK -> inquiries(id) ON DELETE CASCADE
seq          INTEGER      NOT NULL; CHECK >= 0
model        TEXT         nullable; per-turn model
kind         TEXT         NOT NULL; Kind = the Message member class name (§4.1)
timestamp    TIMESTAMPTZ  nullable; when the turn happened (agent clock)
created      TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(); DB write time
message      JSONB        NOT NULL DEFAULT '{}'; the typed Message, kind-selected
PRIMARY KEY (session_id, seq)
```

`message` is not opaque: it is one typed `Message` value (§4.1) encoded by
its own `to_json` and discriminated by `kind`. Large messages stay whole --
Postgres TOAST absorbs them, so there is no app-level blob offload.

### 3.2 Indexes

```
PRIMARY KEY (session_id, seq)   ordered per-session read + dedup
idx_agent_session_events_kind   (kind)
```

### 3.3 Properties

```
append-only           no UPDATE; events never edited/superseded/cited
dedup key             (session_id, seq); re-append is ON CONFLICT DO NOTHING
adjacent universe     deliberately outside change_log; events are not Change
                      rows by design (see design.md "Everything is provenance"):
                      a captured turn is not a knowledge mutation. Provenance
                      is intrinsic (immutable rows + AgentSession lifecycle).
not an Inquiry        no edges, cost, cascade, supersession, embeddings
tenant scope          derived by joining to inquiries (no org_id column)
cascade               purge of the AgentSession row deletes its events
audited boundary      AgentSession-artifact edits DO flow through change_log
                      (section 2.2 field routes); only event ingest does not
timestamp vs created  timestamp = turn's agent-clock time; created = DB write
```

## 4. Wire / domain types

### 4.1 `Kind` and the `Message` union (`types/agent_session_events.py`)

`Kind` is the class name of the `Message` member the row holds (PascalCase),
mirroring how `Inquiry.InquiryKind` stores `"Issue"` / `"AgentSession"`. The
`kind -> class` map is `{cls.__name__: cls}`; `message_for_kind(kind)`
resolves it for decode.

```
Kind                Message member        carries
UserMessage         UserMessage           human user-role text + attachments
AgentSendMessage    AgentSendMessage       agent user-role text + source label
AssistantMessage    AssistantMessage       one model turn: text / thinking /
                                          tool_calls[] (nested ToolCall) / tokens
ToolResult          ToolResult            one tool invocation's result
Compaction          Compaction            a context-window compaction
UnknownMessage      UnknownMessage        escape hatch: raw unrecognized record
```

`ToolCall` is nested inside `AssistantMessage.tool_calls`, never its own row.
Session lifecycle (`started` / `ended`) is not an event -- it lives on the
`AgentSession` row (§2.2). `AssistantMessage` is the only member with
`tokens` (a `TokenCount`); USD cost is inferred from the counts, not stored.

### 4.2 `EventBody` (wire carrier of `AgentSessionEvent`)

```
field      type      notes
seq        int       >= 0; harness-assigned per session
kind       Kind      §4.1
timestamp  datetime? null allowed
model      str?
message    JSON      default {}; the typed Message member, kind-encoded
```

`from_event(AgentSessionEvent)` / `to_event(session_id)` convert to and from
the domain type; `message` is `member.to_json()` and decodes via
`message_for_kind(kind).from_json(message)`.

### 4.3 `SessionStart`

```
field            type       notes
cli              str        required; min_length 1
cli_session_id   str?       min_length 1
summary          str?
started          datetime?
actor            str?       audit actor; default principal email
idempotency_key  uuid?      dedups the created AgentSession row
```

### 4.4 `SessionEnd`

```
field            type       notes
ended            datetime?
cli_session_id   str?       min_length 1; backfill
actor            str?
```

### 4.5 Response types

```
SessionStartResponse   {id: uuid, seq: int, cli_session_id: str?}
AppendEventsResponse   {appended: int, skipped: int}
ReadEventsResponse     {events: [EventBody]}
SessionEndResponse     {id: uuid, ended: datetime?}
```

## 5. Ingest endpoints

### 5.1 Routes

```
POST /api/sessions/start
POST /api/sessions/<session_id>/events
GET  /api/sessions/<session_id>/events
POST /api/sessions/<session_id>/end
```

### 5.2 Per-route

```
route                              role    body / params         response               codes
POST /api/sessions/start           writer  SessionStart          SessionStartResponse   201 401 403 422
POST /api/sessions/<id>/events     writer  AppendEventsRequest   AppendEventsResponse   200 401 403 404 422
GET  /api/sessions/<id>/events     viewer  (query params 5.6)    ReadEventsResponse     200 400 401 403 404
POST /api/sessions/<id>/end        writer  SessionEnd            SessionEndResponse     200 401 403 404 422
```

### 5.3 `AppendEventsRequest` body

```
{
  "events": [<EventBody §4.2>, ...]    // min_length 1
}
```

### 5.4 Semantics

```
start            mints an AgentSession row (server id); dedups on idempotency_key
events           batch append; ON CONFLICT (session_id, seq) DO NOTHING;
                 appended = rows newly written, skipped = collisions
end              backfills ended/cli_session_id; sets status=complete
unknown session  any <id> not an AgentSession row -> 404
```

### 5.5 Divergence from api.md

```
not in api.md §1     these routes live only here (the sessions namespace)
no change_id         responses carry no change_id: events are the adjacent
                     universe, deliberately outside change_log (§3.3)
no Idempotency-Key   header unused on /events; dedup is (session_id, seq),
                     the correct primitive for an append-only log
start idempotency    via SessionStart.idempotency_key (mirrors inquiry create)
read pagination      GET /events reuses api.md 4.3 grammar (§5.6)
```

### 5.6 GET /events query params

```
param      type   default  notes
limit      int    50       min 1; max 1000
offset     int    0        min 0
seq_range  str    -        repeated `a..b` interval; union; min 0
kind       str    -        one Kind (§4.1); else 400
```

Divergence from api.md 4.3: event `seq` starts at 0 (harness-assigned),
so a `seq_range` bound accepts 0 here, unlike inquiry `seq` (min 1).
`seq_range` repeats one `a..b` interval per param, their union selecting
disjoint windows, exactly as the inquiry list does. No `filter`/`sort`
params; order is fixed `seq ASC`.

## 6. Large messages (TOAST, no app-level offload)

```
storage          message JSONB stays whole on the row; no blob sidecar
mechanism        Postgres TOAST transparently out-of-lines large values
heavy media      a message carrying bytes chooses the FilePath / WebUrl
                 Attachment variant over inline BytesAttachment, so the row
                 stays a reference rather than embedding the payload
```

## 7. Store seam (`server/store.py`)

```
submit_agentsession(SubmitAgentSession, *, api_key_id=, actor=) -> UUID
append_events(session_id, events) -> (appended, skipped)
read_session_events(session_id, *, limit=None, offset=0,
                    seq_ranges=(), kind=None) -> list[EventBody]
```

```
swap point       append_events / read_session_events are the only event-store
                 touchpoints; ClickHouse / Parquet migration replaces them
paginated read   read_session_events bounds the window (limit/offset/seq/kind)
                 so a large session never materializes whole in one call
```

## 8. Client SDK (`client/client.py`)

```
session_start(SessionStart) -> SessionStartResponse
append_events(session_id, Sequence[EventBody]) -> AppendEventsResponse
read_events(session_id, *, limit=50, offset=0, seq_ranges=(),
            kind=None) -> list[EventBody]
session_end(session_id, SessionEnd | None) -> SessionEndResponse
```

```
idempotency      session_start mints idempotency_key when omitted
read             read_events GETs one paginated page (§5.6)
```

## 9. Capture path (`trax/run/`)

```
trax run <cli> [--no-sync] [--out PATH] -- <cli args>
```

```
adapters         claude, gemini, codex (file-tailers); codex_appserver (parser)
in-memory type   adapters.Event (one parsed turn) -> wire EventBody at the sink
seq              harness-assigned, run-wide monotonic, per session
server           resolved from the active trax profile (URL + auth)
sink (default)   TrackinizerSink -> start / append_events (batch) / end
sink (--no-sync) FileSink -> local JSONL (also --out PATH, --dry-run)
```

## 10. Scale tiers (`docs/db_schema_migration.md`)

```
phase 0   plain Postgres agent_session_events table
phase 1   Timescale hypertable (deploy-time ALTER; not bootstrap DDL)
phase 2   ClickHouse for agent_session_events (> ~1e9 rows)
phase 3   Parquet on object storage (cold tier)
```
