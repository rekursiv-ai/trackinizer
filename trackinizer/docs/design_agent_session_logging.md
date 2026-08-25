# Agent session logging

## Goal

Let trackinizer record agent sessions at turn granularity so any session
can be reconstructed exactly. Priority CLIs: claude (1), gemini (1),
codex (1), cursor (3). Single human, 1k-10k agents. Multi-user,
multi-agent-per-user.

The four fields every captured session must reconstruct: **model
response**, **thinking/reasoning**, **tool call**, **tool result**. All
four are recoverable from both priority CLIs (claude, codex); see
[Empirical findings](#empirical-findings-2026-05-31).

This doc covers **capture** (agent -> log). The reverse direction --
**messaging** a live session (world -> agent: web UI / another agent
sends a message into a running `trax run`) -- is designed in
[`design_session_messaging.md`](design_session_messaging.md), which
reuses this subsystem's session row and transport.

## Status (2026-05-31)

A live-capture spike settled the two gates that block the build:

1. **Can we drive + scrape both CLIs?** Yes. `trax run claude` and
   `trax run codex` each ran a prompt non-interactively and the harness
   captured their output.
2. **What can we scrape?** All four required fields from both CLIs,
   including **codex reasoning** -- recoverable straight from the rollout
   JSONL when codex is spawned with `model_reasoning_summary=detailed`.
   (An earlier spike conclusion that this needed `codex app-server` was a
   flag error; the rollout tailer suffices.)

The build is complete: the `AgentSession` artifact kind, the
`agent_session_events` table (typed by `types/agent_session_events.py`),
the ingest + read API, and the client SDK / `--sync` sink all exist. The
authoritative reference is [`api_agent_session_events.md`](api_agent_session_events.md);
this doc is the design narrative and rationale behind it.

The spike also found two load-bearing bugs in the harness, fixed first
(see [Two bugs](#two-bugs-the-spike-exposed-both-fixed-first-in-the-plan)).

## What we're wiring

```
┌────────────┐                    ┌──────────────────┐
│ trax-claude│                    │   Trackinizer    │
│ trax-gemini│ ─POST events─────▶ │  /api/sessions/* │
│ trax-codex │   HTTP+JSON        └────────┬─────────┘
│ trax-sh    │                             │
└────────────┘                             ▼
   wraps the CLI,        ┌───────────────────────────────────────┐
   spawns it in its      │ Postgres (+ Timescale extension, opt)  │
   verbose/streaming     │  - inquiries (AgentSession row)        │
   mode, forwards the    │  - agent_session_events (hypertable)   │
   structured stream     │  - message JSONB (typed Message union) │
                         └───────────────────────────────────────┘
```

**`trax run sh` -- the IO-stream adapter.** Besides the model CLIs, `sh`
wraps ANY binary in a live, addressable session (`trax run --as alice sh
-- CMD [ARGS...]`): the child's own stdin/stdout are the whole interface.

- **Capture**: no session log exists to tail, so the PTY stream is the
  source -- each newline-terminated output line becomes one
  `AssistantMessage` (framed and byte-clamped by `LineCapture`, escape
  sequences stripped).
- **Injection**: inbound messages arrive as plain newline-terminated
  stdin lines -- no bracketed paste. The pump silences slave echo so
  injections are not re-captured as output.
- **Parsing**: semantic parsing belongs to the wrapped program, not trax
  (`trax/run/adapters/iostream.py`; reference client in
  `examples/trax_run_sh_demo.sh`).
- **Extending**: a new stream dialect (say JSON-lines to richer events)
  is one `StreamAdapter` subclass overriding `parse` plus a registry
  entry.

Two layers, one DB today:

- **AgentSession** = `Artifact` kind in `inquiries`. Queryable, edge-able to
  Issues/CodeChange, supersede-able for resumed sessions.
- **agent_session_events** = append-only table, NOT in `inquiries`.
  Turn-grain rows whose `kind` is the class name of the typed `Message`
  member they hold: `UserMessage`, `AgentSendMessage`, `AssistantMessage`,
  `ToolResult`, `Compaction`, `UnknownMessage`. The `message` JSONB is that
  typed value, not opaque CLI JSON; Postgres TOAST absorbs large ones (no
  app-level blob offload).

## Empirical findings (2026-05-31)

Spike against live binaries: **claude 2.1.158**, **codex 0.135.0** (both
newer than the `cli-scraping-investigation.md` versions, and the schemas
shifted). Method: drive each CLI through `trax run` with a real prompt,
then inspect the captured `Event`s and the CLI's native logs.

### Gate 1 -- capture works for both

Both CLIs ran the prompt and the harness saw their output. Remote
control via `trax run` is proven for claude and codex.

### Gate 2 -- all four fields recover from both

| field | claude 2.1.158 | codex 0.135.0 (rollout JSONL) |
|---|---|---|
| model response | ✅ `assistant` line, `message.content[].type=="text"` | ✅ `event_msg/agent_message` |
| **thinking** | ✅ `content[].type=="thinking"` (+`signature`) | ✅ `reasoning` item `summary[].text` |
| tool call | ✅ `content[].type=="tool_use"` `{name,input}` | ✅ `response_item/function_call` |
| tool result | ✅ user line, `content[].type=="tool_result"` | ✅ `response_item/function_call_output` |

**Codex thinking IS recoverable from the rollout JSONL** -- the same
on-disk file the existing adapter tails -- provided codex is spawned with
`-c model_reasoning_summary=detailed`. With that flag, the rollout's
`reasoning` item carries `summary: [{type:"summary_text", text: "..."}]`
with real plaintext chain-of-thought ("**Clarifying user requests** --
The user is asking..."). `codex exec --json` surfaces the same text in an
`item.completed type=reasoning` event.

Earlier spike runs concluded reasoning was app-server-only; that was a
**flag error** -- those runs omitted `model_reasoning_summary=detailed`,
so the summary came back empty. With the flag, the rollout tailer alone
recovers thinking. No JSON-RPC driver is required.

Three reasoning representations, confirmed against `codex-rs` source:

- **`summary[].text`** -- plaintext CoT summary. Present with
  `model_reasoning_summary=detailed`. This is what we capture.
- **`content`** -- raw (non-summary) CoT. Always `null` to clients.
- **`encrypted_content`** -- raw CoT, encrypted, server-decryptable only.

Raw plaintext CoT is **not** recoverable, and this is not a config gate:
`codex-rs/core/src/client.rs:747` hardcodes
`include = ["reasoning.encrypted_content"]` on every reasoning request,
with no `config.toml` / `-c` override. `x-reasoning-included` is a
*response* header (did the server include reasoning), not a request
toggle. Summary CoT is the recoverable thinking; it is sufficient for
the thinking field.

**`app-server` is an optional fidelity upgrade, not required.** Driving
`codex app-server` over stdio JSON-RPC streams the same summary as live
`item/reasoning/summaryTextDelta` deltas (spike captured 64). That buys
streaming granularity, not new content. Deferred to optional work; the
rollout tailer is the Phase-0 path. If pursued, build against the
version-pinned schema from `codex app-server generate-json-schema --out`.

### Real claude on-disk schema (claude 2.1.158)

Sessions: `~/.claude/projects/<path-hash>/<session-id>.jsonl`,
append-only. One top-level `type` per line:

| `type` | discriminator | maps to |
|---|---|---|
| `user` (`message.content` is a string) | user prompt | `UserMessage` |
| `user` (`content=[{type:"tool_result"}]`) | tool output | `ToolResult` |
| `assistant` (`content[].type` in `thinking`/`text`/`tool_use`) | model turn | `AssistantMessage` (tool_use → nested `ToolCall`) |
| `attachment`, `ai-title`, `queue-operation`, `last-prompt` | bookkeeping | (skip) |

`sessionId` is stamped on every line (reliable scoping/dedup key). One
assistant line can carry thinking **or** text **or** a `tool_use` block,
so classification must inspect `message.content[].type`, not just the
top-level `type`.

### Two bugs the spike exposed (both fixed first in the plan)

- **Bug A -- claude adapter matches a dead schema.** It looks for
  `kind ∈ {meta,history,tool_state}` or a `descriptor` substring; real
  2.1.158 logs have neither, so **every** line classifies as `unknown`
  (`{'unknown': 13}` running the adapter on the real spike log; the
  harness summary showed `unknown=25585`, zero classified). Rewrite
  `_classify` for the `type` + `content[].type` schema above.
- **Bug B -- cross-session pollution.** The harness rescans *every*
  directory under the CLI's session root each poll and re-emits unrelated
  sessions' lines: 25,585 events captured for a 13-line session; 64 MB
  sink files; codex showed `session_start=66` (66 foreign sessions swept
  in). Both adapters must scope to the single session/rollout file
  created by the current run, rather than scanning the whole session
  root.

### Schema implication: typed Message, not opaque payload

The two CLIs' native records do **not** share a wire shape (claude
`tool_use.input` is a JSON object; codex tool args are a JSON-encoded
string; claude `thinking`+`signature` vs codex `reasoning.summary` /
`encrypted_content`). The adapters normalize each into a **typed `Message`
member** so one query works across CLIs -- the union mirrors sagent's
provider-unified model interface (`sagent/types/runtime.py`).

- **Typed columns** (cross-CLI queryable): `session_id`, `seq`, `kind`,
  `timestamp`, `model`.
- **`message` JSONB**: one typed `Message` value (`UserMessage` /
  `AgentSendMessage` / `AssistantMessage` / `ToolResult` / `Compaction` /
  `UnknownMessage`), selected by `kind` (the member's class name). It is
  encoded by the member's own `to_json` and decoded via
  `message_for_kind(kind).from_json(...)`. `AssistantMessage` aggregates a
  turn's text + thinking + every `ToolCall` (nested) + `TokenCount`.

`ToolCall` is **nested** in `AssistantMessage.tool_calls`, never its own
row or column: one model turn can fire several tools, so a flat
`tool_name` column would be lossy. `UnknownMessage` is the escape hatch --
an unrecognized CLI record is wrapped verbatim rather than dropped.
`cli_session_id` (claude `sessionId`, codex thread id) is a correlation
key on the `AgentSession` row, not on the events. Event `seq` is
harness-assigned per session, because claude lines carry no monotonic
counter and dir-wide scanning is unsafe (Bug B).

## Choices we made and why

### Storage backend

|                              | Postgres+Timescale | ClickHouse | DuckDB+Parquet | Postgres alone | Druid / Pinot |
|------------------------------|:-:|:-:|:-:|:-:|:-:|
| One DB to operate now        | ✅ | ❌ | ❌ | ✅ | ❌ |
| Scales to 10⁹ events         | 🟡 | ✅ | ✅ | ❌ | ✅ |
| Live-tail via NOTIFY         | ✅ | 🟡 | ❌ | ✅ | ❌ |
| Storage $/GB cheap           | ❌ | 🟡 | ✅ | ❌ | 🟡 |
| Day-2 ops burden manageable  | ✅ | 🟡 | ✅ | ✅ | ❌ |
| Reversible without re-ingest | ✅ | ✅ | ✅ | n/a | ❌ |

**Phase 0 = Postgres.** `agent_session_events` ships as a **plain Postgres
table** first (`PRIMARY KEY (session_id, seq)` dedup). The Timescale
hypertable + hypercore compression is a deploy-time `ALTER`
(`create_hypertable`), not bootstrap DDL: PGlite (the default prototype
substrate) cannot run it, and it is classified incidental below. Plain
Postgres is bit-identical for correctness; the hypertable is pure scale.

**Phase 1 (when events cross ~10⁹) = ClickHouse for `agent_session_events`
only.** `inquiries` stays in Postgres forever. Migration is mechanical
because the seam (`append_events()`, `read_session_events()`) is one
function each.

**Phase 2 (cold tier) = Parquet on S3/R2.** Old partitions roll out;
DuckDB queries on demand for researcher use.

### Why the production deployment migrated pglite → real Postgres

The migration was triggered by *operational* pain (no `psql`, single
connection, no monitoring), not by storage need. But it was a prerequisite:
Timescale doesn't exist for pglite, and event ingest at any volume needs
multi-connection. Done; provisioning lives in the internal ops tree.

### Transport / API protocol

|                                  | REST+JSON | gRPC | OTel/OTLP | Kafka |
|----------------------------------|:-:|:-:|:-:|:-:|
| Works from any CLI today         | ✅ | 🟡 | 🟡 | ❌ |
| Standard, debuggable with `curl` | ✅ | ❌ | 🟡 | ❌ |
| Covers our event shapes natively | ✅ | ✅ | ❌ | ✅ |
| Needed for one producer/consumer | ✅ | ✅ | ✅ | ❌ |
| Stable spec for agent telemetry  | ✅ | ✅ | ❌ | n/a |

**REST+JSON.** Wire bodies in `wire/wire_sessions.py`, carrying the
`types/agent_session_events.py` domain type. Four endpoints: `start`,
`events` (POST + GET), `end`. Idempotency follows `design_idempotency.md`
(server-minted ids, client-supplied `idempotency_key`).

### Why not OTel (yet)

OTel's GenAI semantic conventions are draft. The stable parts cover
single-LLM-call shapes; multi-turn sessions, sub-agent spawn, compaction
events are unspecified. Adopting OTel now means inventing our own
extension attributes for ~40% of what we need — same custom work, plus
mapping cost in both directions. **Add `/v1/logs` and `/v1/traces` OTLP
ingestion as a second front door when** (a) a real OTel-emitting CLI shows
up, or (b) the GenAI conventions stabilize for agent shapes. Doors-open
discipline: use OTel field names where they're stable (`model`,
`tokens_in`, `finish_reason`) so the future remap is mechanical.

### Why not Kafka (yet)

Kafka exists for multi-consumer fan-out. Today: one producer (ingest
API), one consumer (Postgres writer). **Add Kafka when a second consumer
appears** — real-time alerts, agent-coordination signals, or analytics
rollups that need the firehose independent of storage. Not before.

## CLI capture

Per-CLI shim wraps the binary, spawns it in its native verbose/streaming
mode, and forwards the structured event stream to trackinizer's ingest
API. User runs `trax run <cli> <args>` instead of `<cli> <args>`; shim is
a thin process boundary, not a daemon.

|                                  | trax-claude     | trax-gemini    | trax-codex     | trax-cursor   |
|----------------------------------|:-:|:-:|:-:|:-:|
| Priority                         | 1️⃣ | 1️⃣ | 1️⃣ | 3️⃣ |
| Capture shape                    | tail `~/.claude/projects/<h>/<id>.jsonl` | `gemini --experimental-acp` | tail `~/.codex/sessions/.../rollout-*.jsonl` (spawn with `-c model_reasoning_summary=detailed`) | `agent --print --output-format stream-json` |
| Protocol                         | append-only JSONL on disk | JSON-RPC 2.0 over stdio (ACP) | append-only JSONL on disk | JSONL events on stdout |
| Captures user msgs               | ✅ | ✅ | ✅ | ✅ |
| Captures assistant text + deltas | ✅ | ✅ | ✅ `event_msg/agent_message` | ✅ |
| Captures thinking / reasoning    | ✅ `content[].thinking` | ✅ | ✅ `reasoning` item `summary[].text` (needs `model_reasoning_summary=detailed`) | ❌ |
| Captures tool calls + args       | ✅ | ✅ | ✅ `response_item/function_call` | ✅ |
| Captures tool results            | ✅ | ✅ | ✅ `response_item/function_call_output` | ✅ |
| Captures token counts            | ✅ | ✅ | ✅ (`event_msg/token_count`) | ❌ |
| Captures model identity          | ✅ | ✅ | ✅ (`turn_context.model`) | ✅ |
| Lossless via POSIX shim alone    | ✅ | ✅ | ✅ (raw CoT excepted; encrypted) | ❌ (needs API proxy) |

**Phase 0: trax-claude + trax-gemini + trax-codex.** All three are
lossless via native streaming / on-disk-log mode alone. **Codex tails its
rollout JSONL** (like claude tails its session file) and must be spawned
with `-c model_reasoning_summary=detailed` so the `reasoning` item's
`summary[].text` is populated (Empirical findings). The only loss is raw
chain-of-thought, which codex encrypts unconditionally; the plaintext
summary is captured.

**Optional fidelity: codex `app-server`.** Driving `codex app-server`
over stdio JSON-RPC streams the same reasoning summary as live deltas
(`item/reasoning/summaryTextDelta`). Same content, finer granularity.
Deferred; not required for Phase 0.

**Future: trax-cursor (partial).** `agent --print --output-format
stream-json [--stream-partial-output]` captures user / assistant / tool
events, but cursor strips thinking (architecturally suppressed in print
mode -- recoverable from *no* output path) and emits no token counts, and
the headless `agent` binary writes no on-disk session log. So unlike
codex/claude there is no file-tail fallback: recovering thinking is
impossible at the CLI and tokens need an opt-in `trax-proxy` (local TLS
MITM) the user installs separately. Ship the partial shim first; proxy
later if demand surfaces. See
[`cli-scraping-investigation.md`](cli-scraping-investigation.md) for the
empirical investigation.

The shim is the only CLI-specific code. Everything below it (event
batching, retry, idempotency, POST to `/api/sessions/events`) is shared.

## Multi-tenant

Tenant scope lives on the `AgentSession` row (its `owner` / producing
principal); `agent_session_events` derives scope by joining on
`session_id` rather than carrying a denormalized `org_id` column (dropped
as speculative -- re-add only if RLS profiling shows the join hurts). GDPR
delete cascades from the `AgentSession` row via the FK. Per-tenant
retention policy + ingest rate limit ride on the same join.

## Build plan

Decomposed into independently shippable, testable steps. Verification
command per step: `uv --quiet run --frozen pytest -n 8 <paths>`. Steps
0.5a / 0.5b are the capture-correctness gate (no server needed; test
against recorded fixtures) and fix the two bugs above. Steps 1–6 are the
ingest pipeline. 7–8 are deferrable. Both codex and claude stay
rollout/session-log tailers; no JSON-RPC driver is needed for Phase 0.

**0.5a. Fix claude adapter + scope harness (Bug A + Bug B, claude
side).** Rewrite claude `_classify` for the real `type` +
`message.content[].type` schema; bind the harness to the single session
file of the current run (no dir-wide rescan). Touches
`trax/run/adapters/claude.py` (+ test, real-log fixtures),
`trax/run/session.py`. Tested: real-log fixtures → correct `kind`s;
harness emits only the wrapped session's events.

**0.5b. Fix codex adapter + reasoning flag + scope harness (Bug B +
codex thinking).** Keep the rollout-JSONL tailer; classify the
`reasoning` item (`summary[].text` → thinking), `event_msg/agent_message`,
`response_item/function_call`(+`_output`), `token_count`,
`turn_context.model`. Spawn codex with `-c model_reasoning_summary=detailed`
so the summary is populated. Scope the tailer to the single rollout file
of the current run (no dir-wide rescan). Touches
`trax/run/adapters/codex.py` (+ test, real-rollout fixtures),
`trax/run/session.py`. Small fix, not a rewrite -- the app-server driver
is deferred to Step 7.

**1. `AgentSession` artifact kind.** `AgentSession(Artifact)` with `cli`,
`cli_session_id`, `started`/`ended` (model/cwd dropped as lossy); added to
`InquiryKind` + `Artifact.Kind`. Touches `types/inquiries.py` (+ test),
`server/assets/schema.sql` (codegen), `wire/bodies.py`. Tested: `from_row`
round-trip; schema-gen emits `agentsession_*` columns + CHECK; drift
tests pass. Kind URL token is `agentsession`.

**2. `trax agentsession` CLI verb + client read path.** `AgentSession`
lists/shows/creates through the existing `Kind` machinery (`KIND_LOWER`
entry + render columns). Touches `trax/grammar.py`, `trax/verbs.py`,
`trax/render.py` (+ tests). Shippable alone (human-facing read path).

**3. `wire_sessions.py` ingest contract.** Bodies + responses for
`start`/`events`/`end`; the `EventBody` wire carrier (`seq`, `kind`,
`timestamp`, `model`, `message`), with `from_event` / `to_event`
converters; route templates. `wire/wire_sessions.py`; touches
`wire/routes.py`, `wire/import_purity_test.py`.

**4. `agent_session_events` table + store seam.** Source of truth
`types/agent_session_events.py` (`AgentSessionEvent` + the `Message`
union); flat-table DDL (`PRIMARY KEY (session_id, seq)`, `message JSONB`);
`Store.append_events()` (ON CONFLICT DO NOTHING) + `read_session_events()`.
Touches `server/assets/schema.sql`, `server/store.py` (+ test). Tested:
append idempotent on `(session_id, seq)`; ordered read; out-of-order seqs
sort.

**5. Ingest + read API routes.** `POST /api/sessions/start` (mints
AgentSession, server id), `/<id>/events` (batch append),
`GET /<id>/events` (paginated read), `/<id>/end`; mutations `writer`,
read `viewer`. New `server/api/sessions_routes.py`; touches
`server/api/app.py`, `routes_drift_test.py`. Tested: start→events→read→end;
duplicate batch no-ops; 401/403; unknown session 404.

**6. Client SDK + harness sink → ingest (first end-to-end slice).**
`client.session_start/append_events/session_end`; a `TrackinizerSink`
replacing the local-file sink (batch + retry + idempotency); `--sync`
flag (local-file default until validated). Both adapters feed it. Touches
`client/client.py` (+ test), `trax/run/session.py`, new
`trax/run/sink.py`. Tested: sink batches/retries; full start→events→end
against an in-process server; CLI-exit closes the session; thinking
events survive the round-trip for both CLIs. + `integration_test.py`.

**7. (Optional) codex `app-server` adapter.** Drive
`codex app-server --stdio` (JSON-RPC) for streaming-granularity reasoning
deltas (`item/reasoning/summaryTextDelta`) and live tool events. Same
content as the Step-0.5b rollout tailer, finer grain. Build against the
version-pinned schema from `codex app-server generate-json-schema --out`.
Touches `trax/run/adapters/codex.py`, `trax/run/session.py`. Pure
fidelity upgrade; raw (non-summary) CoT stays unrecoverable (encrypted).

**8. (Optional) Timescale deploy.** `create_hypertable` + compression note
in `docs/db_schema_migration.md`; a deploy-time `ALTER`, not bootstrap DDL.
(The original plan also included a `payload_ref` blob offload for >16 KB
payloads; that was dropped -- the typed `message` JSONB stays whole and
Postgres TOAST absorbs large values, so no app-level blob store exists.)

Critical path is short: 0.5a + 0.5b are small adapter fixes (no JSON-RPC
driver), then the linear 1→6 ingest pipeline. Smallest first win: 0.5a.

**Post-build restructure (done).** After the linear plan landed, the
event payload was promoted from opaque per-CLI JSON to the typed `Message`
discriminated union (the now-state above): `kind` became the member class
name, `payload`→`message`, `ToolCall` nested in `AssistantMessage`,
`TokenCount` added, and the blob offload removed in favor of TOAST. The
adapters normalize each CLI's native record into a `Message` member.

## What's load-bearing vs. incidental

**Load-bearing** (changing breaks the design):
- Server-minted `session_id`; clients never name a session.
- `agent_session_events` is append-only and outside `inquiries`.
- `PRIMARY KEY (session_id, seq)` is the per-event dedup mechanism.
- `message` is a typed `Message` member, selected by `kind` (its class name).
- `kind` always equals `type(message).__name__`; enforced at construction.
- Two seam functions (`append_events`, `read_session_events`) make storage
  swappable.
- Codex must be spawned with `model_reasoning_summary=detailed`, else the
  rollout `reasoning` item's `summary[].text` is empty (no thinking).

**Incidental** (could change without redesign):
- The exact `Message` member set (new members promote from `UnknownMessage`).
- REST+JSON vs gRPC for the same wire shape.
- **Timescale hypertable**: a deploy-time `ALTER`, not part of the data
  model; plain Postgres is the Phase-0 substrate.

## When to revisit

| Trigger | What changes |
|---|---|
| `agent_session_events` > 10⁹ rows | Phase 1: swap event store to ClickHouse |
| `agent_session_events` > 10¹² rows or storage $$$ dominates | Phase 2: Parquet on object storage for cold tier |
| Real CLI emits OTel agent traces | Add `/v1/logs` + `/v1/traces` OTLP endpoint |
| Second downstream consumer wants the firehose | Add Kafka between ingest API and consumers |
| Public third-party tenancy | Add `Idempotency-Key` header alias, per-tenant API key scoping |
| codex / claude CLI upgrade | Re-verify rollout/session-log schemas against the new version |

None of these require rewriting the data model or the wire contract. They
layer on the existing seam.

## Addendum: the drain timer, its discovery scan, and the inbound poll

`trax run` waits on timers in **two** loops: the filesystem drain
(`_drain_filesystem_loop`, `run/session.py`) and the inbound message poll
(`_inbound_poll_loop`, same file). Neither was chosen over an alternative
-- the CLI-agnostic file tailer was the Phase-0 decision, and a timer is
what a tailer needs when nothing wakes it.

The drain loop does two jobs per tick: it re-runs session-file DISCOVERY
and then reads new bytes. Those are separable, and the cost is entirely in
the first, so they are treated separately below.

Nothing here touches the wire contract or the `Message` model.

Measurement provenance, since it varies by claim:

- The scan costs in (a) re-run from `docs/probes/scan_cost.py`. They are
  host- and history-dependent -- claude's figure scales with how many
  project directories that CLI has accumulated -- so re-run before relying
  on them.
- The PTY and hook TIMINGS below came from one-off harnesses that spawn a
  real CLI (API credits, a live binary, a live session). Those are NOT
  committed. Each is reported with its sample size inline; treat them as
  single observations that ruled an option in or out, not as
  characterizations.

### a. The discovery scan is the cost, not the tick

`_scan_and_read` re-runs the whole session-file search on every 0.2s tick,
though "which file is mine" is a once-per-run question. Via
`docs/probes/scan_cost.py`:

| adapter | dirs walked | files matched | scan (median) | tick occupancy |
|---|--:|--:|--:|--:|
| claude | 999 | 1989 | 86.0ms | 43% |
| codex | 1 | 1345 | 14.2ms | 7% |
| gemini | 5 | 19 | 0.2ms | 0% |

"Tick occupancy" is the share of each 0.2s tick the drain thread spends
walking directories -- wall-clock time in that thread, largely `stat()`
syscalls. It is NOT a CPU-utilization figure. A separate cold-cache run
measured ~570ms for claude; the 86ms above is warm.

Claude's cost is fan-out: `session_dirs()` returns every project directory
the CLI has ever used (`adapters/claude.py`, an unfiltered `iterdir()`).
Codex and gemini are already narrow, which is why their rows are cheap.

**Scoping claude's `session_dirs()` to the run's own project directory is
the single highest-value change here, and it is independent of the timer.**
The directory name appears to be the cwd with path separators replaced
(observed: cwd `/tmp/work/trax-hook` ->
`~/.claude/projects/-tmp-work-trax-hook`), but the full
encoding is NOT specified anywhere in this repo and one observation is not
a spec. Deriving it wrong drops capture to zero, which is worse than the
scan. Establish the encoding against adversarial paths (dots, symlinks,
resolved vs unresolved cwd, `--resume` from a different cwd) before
relying on it.

The filename within that directory is a session UUID the CLI mints at
startup, so it cannot be predicted -- but the search space is one
directory, not 999.

### b. Wake-on-write instead of a tick

A regular file cannot be waited on: POSIX reports it always ready, so
`select` returns immediately at EOF and a naive wait spins. Verified:

    select on regular file at EOF: returned in 0.03ms, readable=True, read()=''

The kernel does have the signal, but reaching it costs something:

| platform | mechanism | availability |
|---|---|---|
| Linux | inotify (`IN_CREATE`/`IN_MODIFY`) | **no stdlib module**; `ctypes` against `libc` `inotify_init1`, or a dependency (`watchdog` / `inotify_simple`), neither currently in `pyproject.toml` |
| macOS | kqueue (`EVFILT_VNODE`) | `select.kqueue`, stdlib -- **unverified**, no macOS host was available |
| macOS | FSEvents (directory-level, adds creation) | requires the `_watchdog_fsevents` compiled extension |

GNU `tail -F` resolves this the same way (its binary links inotify
symbols) and falls back to a sleep loop where that is unavailable.

Two consequences for any implementation:

- The macOS path is unproven here. Shipping Linux-native with a timer
  fallback elsewhere is the honest first step.
- The cross-run guards that exist to prevent Bug B (the `baseline`
  snapshot and the `spawn_time` mtime floor) are discovery logic, not
  timer logic. An event-driven reader must state explicitly what replaces
  them; `IN_CREATE` on a single scoped directory plausibly does, but that
  is untested.

### c. Compaction rewrites the transcript

Claude compaction does not append -- it writes the pre-compaction
transcript aside (`pre_compact_N.jsonl`) and leaves a `session.jsonl` that
can be SMALLER than before. `_drain_appended_lines` handles the shrink by
resetting the byte offset to 0, but **the partial-line buffer for that
path is not discarded**, so stale bytes from the old file prepend to the
first line of the new one. This is a live bug in the current tree, not a
property of polling: any reader needs the same guard. A fix is proposed in
PR #122.

The `PreCompact` / `PostCompact` hook events are documented but were NOT
fired during this investigation (`/compact` is not reachable from a
non-interactive `-p` run), so hook coverage of compaction is unverified.

### d. Inbound messages: lowest priority

`_inbound_poll_loop` polls `drain_inbound` every 0.5s -- one request per
running agent, and the loop skips the call until the sink has a session
id. This is the pull half of session messaging and is already scoped for
replacement in `design_session_messaging.md` ("Transport: HTTP polling
now; NOTIFY/SSE is the upgrade"): the server has the push fanout the SPA
uses, and `httpx` streams responses, so the client needs no new
dependency. The missing piece is a server route that holds the request
open.

The interval is a keyword default on the loop function, reachable from
tests but not from `RunConfig`. PR #122 proposes promoting it to a config
field. Either way that makes it tunable, not event-driven.

### e. Two capture gaps found along the way

Both are discovery defects rather than polling ones, and both are
independent of everything above:

- **Subagent transcripts are never captured.**
  `ClaudeAdapter.matches_session_file` requires
  `path.parent.parent == projects_dir`; subagent logs live deeper
  (`<session>/subagents/agent-<id>.jsonl`). The runner's `rglob` visits
  them and the depth check then rejects them. A walk of one developer's
  tree found 1121 `.jsonl` files at that depth and 26 deeper still.
- **All three CLIs now ship hooks**, including gemini, whose hook system
  postdates this document -- so the portability argument that motivated
  the tailer no longer holds as stated. Claude's payload carries
  `transcript_path` and, for subagents, `agent_transcript_path`; one
  non-interactive probe (n=1, harness not committed) found every file that
  grew was named by one of those two fields. Codex
  (`hooks.json` / `notify`) and gemini hook payloads were read from vendor
  documentation, NOT exercised. Hooks would make discovery exact, at the
  cost of one integration per CLI.

A `Stop` hook alone is not a sufficient drain trigger: in that probe it
arrived 125ms BEFORE the turn's final append. `SessionEnd` followed the
last append, so a hook-driven drain needs the session-end sweep the timer
loop already performs.

### What was ruled out

**PTY output as the drain trigger.** The pump already sits in `select` on
the master fd and already tees stdin to the slash-command detector, so
teeing the other direction looks free. It does not work: the CLI paints
the terminal BEFORE writing the log. Across one claude session (n=1,
harness not committed), every append landed 21-195ms after the last PTY
read; one append's next PTY read came 27.8s later, when the human
typed `/exit`, and two appends had no following read at all. A drain woken
by PTY output runs before the bytes exist and then never runs again --
strictly worse than the timer it would replace. The PTY carries
ANSI-rendered output anyway (thinking collapsed, tool args truncated), so
it is a timing signal at best, never a content source.
