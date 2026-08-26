# Design: ACP capture + drive mode for `trax run`

Status: proposed. Owner: Agent. Date: 2026-07-02.
Companion to `design_session_resume.md` (resume correlation) and
`design_session_messaging.md` (send/rooms). Supersedes the deferred
driver half of `trax/run/adapters/codex_appserver.py`.

## Problem

`trax run` captures agent sessions by scraping each CLI's on-disk
session log (`trax/run/adapters/{claude,codex,gemini}.py`) and injects
`trax send` messages as bracketed pastes into a PTY
(`lib/posix/relay.py`). Both halves work, but both are built on
surfaces the vendors do not own as contracts:

1. **Capture is pinned to undocumented log formats.** claude.py parses
   `~/.claude/projects/<hash>/<id>.jsonl` as observed at claude
   2.1.158; codex.py the rollout JSONL at codex-cli 0.136.0; gemini a
   whole-file rewrite. A format change degrades silently to
   `UnknownMessage` or dropped turns. Cross-run disambiguation rests on
   mtime heuristics (`_SPAWN_MTIME_GRACE_SEC`).
2. **Capture is lossy where the logs are.** Token usage is never
   populated (`AssistantMessage.tokens` is schema-only; codex counts
   live in skipped `event_msg`s). Attachments are dropped. gemini
   yields no tool results, thinking, timestamps, or model.
3. **Resume correlation only works for claude.**
   `session_id_from_path` returns `None` for codex and gemini, so the
   `cli_session_id` correlation designed in `design_session_resume.md`
   has one working adapter — and even that is client-unwired today.
4. **Injection is a TUI hack.** The paste + delayed-Enter protocol
   (empirically tuned around codex's 120 ms paste suppression) targets
   a human-facing TUI. It cannot express structured turns, cannot know
   when a turn ended, and cannot carry permission decisions.
5. **There is no headless mode.** The wrapper assumes a human driving
   a local terminal. Product devbox agents (one agent per isolated
   sandbox, driven entirely by `trax send`) have no runner: the PTY
   path requires a terminal and dies with it.

## Context

The ecosystem converged on the Agent Client Protocol (ACP, Zed +
JetBrains: <https://agentclientprotocol.com>) as the wire contract for
driving agent CLIs: JSON-RPC 2.0 over the adapter subprocess's stdio,
with `initialize` / `session/new` / `session/load` / `session/prompt`
requests, `session/update` notifications (message, thought, tool-call,
plan chunks), and `session/request_permission` callbacks. Vendor-side
adapters are maintained in an ACP registry (claude via
`@zed-industries/claude-agent-acp`, codex via
`@zed-industries/codex-acp`, gemini speaks ACP natively). Rivet's
sandbox-agent rewrote itself onto ACP before freezing; OpenHands and
agentOS drive third-party agents over ACP. Betting our capture on ACP
outsources per-CLI format churn to maintained adapters and gives us
the structured surfaces the logs never had.

`codex_appserver.py` already proved the shape in-repo: a parser
mapping structured JSON-RPC notifications to the `Message` union, with
the subprocess driver deliberately deferred. ACP is that driver,
generalized across CLIs.

## Requirements

1. **Additive mode, not a replacement.** `trax run claude` (PTY +
   scrape, human-interactive) is unchanged. `trax run --acp claude` is
   a new headless mode. The PTY path remains the interactive default
   until ACP capture fidelity is proven equal or better per adapter.
2. **Same wire protocol to the server.** ACP mode emits the same typed
   `Event`s through the same `Sink`/`ResilientSink` (batching, seq
   idempotency, degrade-to-local-file). No server changes.
3. **`trax send` delivers as a structured prompt.** In ACP mode the
   0.5 s inbound poll forwards each queued message as `session/prompt`
   instead of a PTY paste. Messages arriving while a turn is in flight
   queue locally and deliver at the next turn boundary.
4. **Resume becomes adapter-agnostic.** The ACP session id returned by
   `session/new` is sent as `SessionStart.cli_session_id` at open
   (closing the client half of `design_session_resume.md`), and
   `session/load` is used for native resume where the adapter
   advertises it.
5. **Adapters are pinned.** A version manifest (adapter package →
   exact version, checked into the repo) resolves what to spawn; no
   floating `npx <pkg>@latest`. Unpinned = unsupported.
6. **Permission requests are answered by policy.** V1 is a static
   policy on `RunConfig` (default: allow-all, matching the sandbox
   posture where the box, not the tool gate, is the boundary), with
   the decision captured as a `SystemMessage` event for audit.
7. **Token usage is populated when the adapter surfaces it.** Fill
   `AssistantMessage.tokens` from `session/update` metadata where
   present; absent metadata degrades to today's behavior (None).

## Design

Three new units, one wiring change:

| Unit | Path | Job |
|---|---|---|
| ACP client | `trax/run/acp/client.py` | subprocess spawn + JSON-RPC 2.0 over stdio: `initialize` handshake, request/response correlation, notification dispatch, cancel, shutdown |
| Translation | `trax/run/adapters/acp.py` | `session/update` notifications → `Event`s (the `codex_appserver.py` analog), with chunk→turn aggregation |
| Drive loop | `trax/run/acp_runner.py` | the `_spawn_and_drain` sibling: session open → prompt loop → sink |
| Wiring | `trax/run/session.py`, `trax/verbs.py` | `--acp` flag routes `run()` to the drive loop |

### Threading model

Reuses the existing discipline. The ACP client owns one reader thread
over the adapter's stdout; that thread is the **single sink writer**
(the role the drain thread plays today, `session.py:259`). The inbound
poll thread is unchanged except its delivery target: an injection
queue consumed by the drive loop instead of `ThreadedRelay.submit`. Writes
to the adapter's stdin go through one lock. `LockedSink` already
serializes cross-thread sink access.

### Chunk → turn aggregation

ACP streams deltas (`agent_message_chunk`, `agent_thought_chunk`,
`tool_call` + `tool_call_update`). trax events are turn-grained. The
translator accumulates chunks per session and flushes a typed
`AssistantMessage` (text + thinking + tool_calls) at turn boundaries:
the `session/prompt` response arriving (stop reason), a tool-call
transition, or adapter exit. `ToolResult`s emit per
`tool_call_update` completion, pairing `call_id` exactly as
`codex_appserver.py` does. Unrecognized update kinds fall back to
`UnknownMessage` — never dropped.

### Drive loop lifecycle

```
spawn adapter (pinned manifest) → initialize
→ session/load (resume, if cli_session_id and adapter capability)
  | session/new (fresh; returned id → SessionStart.cli_session_id)
→ initial prompt (argv tail, if given)
→ loop: drain injection queue → session/prompt → stream updates → sink
→ on SIGTERM/queue-close: session/cancel → drain → sink.close → exit
```

Adapter crash mid-turn: the reader thread observes EOF, flushes the
partial turn, emits a `SystemMessage` recording the abnormal exit, and
the runner exits nonzero. `ResilientSink` degrade semantics are
inherited unchanged: a server outage never loses events.

### What this deliberately does not fix

Out of scope here, tracked as separate issues: the process-local
inbound queue on the server (`server/inbound.py`, drop-if-absent),
session liveness/heartbeat + zombie reaping, and remote human attach.
ACP mode narrows the last one: a headless agent's turns are fully
captured, so a read-only live view needs only the event stream, not a
terminal.

## Phases and estimate

| Phase | Contents | New code (impl + tests) | Estimate |
|---|---|---|---|
| 1 | client core, claude translation, drive loop happy path: `trax run --acp claude` captures + `trax send` injects | ~900 + ~1,200 LOC | 3–5 focused days |
| 2 | resume wiring (req. 4), permission policy (req. 6), codex adapter, pinned manifest (req. 5) | ~400 + ~600 LOC | 3–4 days |
| 3 | token usage (req. 7), gemini-via-ACP, edge hardening (mid-turn crash, queue-during-turn, cancel races) | ~300 + ~500 LOC | 2–3 days |

Roughly 1.5–2.5 engineer-weeks to the repo's test bar (the existing
wrapper is 2.6 k LOC impl / 3.8 k LOC tests; ACP mode lands in the
same ratio). A demonstrable phase-1 prototype is 1–2 days.

## Risks

- **ACP spec and adapter churn.** Adapters are 0.x
  (`claude-agent-acp` 0.20.x, `codex-acp` 0.1.x). Mitigation: the
  pinned manifest (req. 5) plus drift tests that pin observed
  `session/update` shapes, mirroring the log-format pin discipline in
  the scraping adapters.
- **Behavioral drift vs the raw CLI.** `claude-agent-acp` wraps the
  Agent SDK: model selection, settings, and session storage differ
  from the interactive CLI. Capture is purely stream-based (the point),
  but flags a user passes after `--` may not map. Mitigation: ACP mode
  documents its own flag surface; no pass-through pretense.
- **Aggregation edge cases.** Interleaved thinking/tool chunks across
  parallel tool calls. Mitigation: the claude scraping adapter already
  solves turn aggregation for the same underlying stream; port its
  block-folding rules and test against recorded ACP transcripts.
- **Node in the box.** npx-distributed adapters need node. Non-issue
  for devboxes (claude CLI already requires it); the manifest supports
  platform binaries where the registry ships them.

## Alternatives considered

- **Adopt rivet-dev/sandbox-agent.** Rejected 2026-07-02 after a
  code-level comparison: no peer messaging (one agent per session),
  requires inbound HTTP into the sandbox (trax is outbound-only by
  design), resume is a lossy synthesized replay, no cost tracking, and
  the project froze mid-RC in March 2026 (bus factor 1, zero known
  production users). Its lasting contribution is validating the ACP
  bet — its final rewrite replaced a bespoke event schema with exactly
  this architecture.
- **Per-CLI structured drivers** (`codex app-server`, claude
  `--output-format stream-json`). Strictly dominated by ACP: same
  driver work per CLI, no shared schema, no third-party maintenance.
  `codex_appserver.py` stays as the precedent that motivated this
  design.
- **Keep scraping only.** Status quo; acceptable for the interactive
  wrapper (it stays), but it cannot host headless devbox agents and
  carries the fragility and loss documented above.
