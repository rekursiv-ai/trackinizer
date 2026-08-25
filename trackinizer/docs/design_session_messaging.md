# Agent session messaging

## Goal

Let a human (via web UI) or another agent **send a message into a live
`trax run` session** while the human keeps using that session's native
terminal. Trackinizer already logs every session
([`design_agent_session_logging.md`](design_agent_session_logging.md));
this makes it also the **router** for messages flowing the other way --
into the agent. Hub-style chat, but the messages route through
trackinizer instead of a separate hub.

Two CLIs are first-class and must have **equal support**: claude and
codex. Both are verified injectable (see
[Empirical findings](#empirical-findings)).

Scope boundary:

- **Injection in** (world -> agent): the new mechanism this doc designs.
- **Capture out** (agent -> log): already built; unchanged. The session
  logger records the injected message as a normal turn when the agent
  consumes it, so messaging needs no new logging path.

## Status

The injection primitive is proven on both priority CLIs; the routing and
naming model is settled; nothing is built yet. This doc is the design to
implement against. Implementation order is in the
[Roadmap](#roadmap): a read-only viewer ships first (Phase 1a) as the
instrument that proves `trax run` scrapes correctly, then single-session
injection (Phase 2a), then the full chat system (Phase 2).

```
  web UI chat ─┐                                    ┌─▶ human's terminal (native TUI)
               ├─ trax send ─▶ trackinizer ─push──┤
  other agent ─┘   (resolve @actor:room,           │   trax run claude --as scientist
                    route to live session)          │     owns PTY master
       trax run ◀── subscribe (pull) ───────────────┘     │
          │  receives routed message                      ▼
          ▼                                          claude / codex (PTY slave)
   bracketed-paste(text) + delayed Enter ───────────▶ sees injection AND human keystrokes
```

The server never dials the client: `trax run` **pulls** (a subscribe /
long-poll on its own outbound connection), exactly as the SPA's
`/api/web/subscribe` SSE relay already does
(`server/notify.py:85`). There is no inbound endpoint on the client.

## Why the log cannot be the channel

The captured `agent_session_events` are a record of what the agent
*did* -- written *after* the agent acts, downstream of it. To send a
message *into* a running session you need a path *upstream* of the
agent, to its input. Writing a row to `agent_session_events` does not
cause the CLI to read anything; the agent never reads that table. The
log is a transcript, not a mailbox.

So the asymmetry is irreducible:

| direction | source of truth | exists? |
|---|---|---|
| agent -> world (output) | the captured log | ✅ fully built |
| world -> agent (input) | the CLI's stdin / PTY | ❌ nothing writes there today |

`trax run` today is **write-only into trackinizer** (the drain thread
produces events, `trax/run/session.py`) and **out of the agent's input
path** (bare `subprocess.Popen(argv)`, inheriting fds, holding no
injectable handle). The messaging channel is the wire that did not
exist. It is **not** built by extracting sends from the log -- see
[Additional channel, not log-extraction](#additional-channel-not-log-extraction).

## Empirical findings

Tested against live binaries: **claude 2.1.159**, **codex 0.136.0**.
Method: spawn the CLI on a PTY we own, write an injected prompt to the
master fd while no human types, confirm the model produces the requested
token.

### Both CLIs accept PTY injection into a live interactive session

| CLI | injection into live TUI | result |
|---|---|---|
| claude 2.1.159 | write to PTY master | ✅ confirmed (model emitted the token) |
| codex 0.136.0 | write to PTY master | ✅ confirmed (model emitted the token) |

The human and the injector are **peers on the same PTY master**: both
write to the one slave fd, and the CLI cannot distinguish injected bytes
from typed ones. So the human keeps the native TUI and the server can
splice in. This is the whole point -- owning the subprocess's PTY is
sufficient; no CLI remote-control feature is required.

### Codex needs a specific injection protocol (read from `codex-rs` source)

claude accepted a naive write. Codex did not, until the protocol matched
its TUI input model. Three load-bearing facts, confirmed in
`~/projects/codex/codex-rs/tui/src`:

- **Wait for composer-ready before injecting.** Codex boots an MCP layer
  (`codex_apps`) that stalls the input composer for ~10s; injecting early
  races an un-ready prompt. The pump must gate on readiness, not a fixed
  timer.
- **Defeat paste-burst grouping.** `bottom_pane/paste_burst.rs` groups
  fast-arriving chars (`PASTE_BURST_CHAR_INTERVAL = 8ms`) into a single
  paste. Inject via **bracketed paste** (`\e[200~ … \e[201~`) -- codex
  enables it (`tui.rs:176 EnableBracketedPaste`) and treats the block as
  one atomic unit -- or type slower than 8ms/char.
- **Enter must be separate and delayed.** `PASTE_ENTER_SUPPRESS_WINDOW =
  120ms` (`paste_burst.rs:151`): an Enter within 120ms of burst/paste
  chars is suppressed (absorbed, not submit). Send `\r` as its own write,
  >120ms after the text.

The submit key is `KeyCode::Enter` (`chat_composer.rs:549`), i.e. `\r` in
raw mode.

### One injection protocol serves both CLIs

```
1. detect composer-ready
2. write  \e[200~ <text> \e[201~   (bracketed paste, atomic)
3. wait  > 120ms
4. write  \r                        (submit)
```

Bracketed paste also solves interleaving with the human: the injected
text lands as one atomic block rather than racing the human's
in-progress keystrokes char-by-char.

## Choices we made and why

### Additional channel, not log-extraction

The server could learn "agent X wants to send to Y" two ways: parse it
out of the capture firehose, or carry it on an explicit channel.

|                              | extract from logs | additional channel |
|------------------------------|:-:|:-:|
| send decoupled from logging  | ❌ | ✅ |
| "send but don't log" possible| ❌ | ✅ |
| network: only sends on wire  | ❌ (whole transcript) | ✅ |
| `trax run` limits its chatter| ❌ | ✅ |

**Additional channel.** Extraction fuses send with capture -- you could
never have one without the other, and you would process the entire
transcript to find the few sends. The channel keeps them orthogonal:
`trax run` recognizes a send locally and talks to the server **only on
sends**, not every turn.

### `trax run` is the edge detector

Because the send is recognized at the edge, `trax run` owns both
directions: the inject-in (PTY write) and the send-out (recognize +
forward). The server is a pure router in the middle -- it resolves the
target name to a live session and pushes. This mirrors an internal hub's
shape (agent emits a send, the wrapper catches it, the hub routes it),
with trackinizer as the hub.

### Send expression: a `trax send` command, not an output sentinel

How an agent expresses "send to Y": invoke a `trax send @Y:room "…"`
command the wrapper intercepts, or emit a sentinel string in its output
the wrapper greps for.

|                          | `trax send` command | output sentinel |
|--------------------------|:-:|:-:|
| sender identity attested by | the wrapper's `--as` | the agent's own text |
| agent can forge sender   | ❌ no | ✅ yes (writes `from:scientist`) |
| cross-CLI uniform        | 🟡 per-CLI tool plumbing | ✅ all emit text |
| delivery confirmation    | ✅ command returns a receipt | ❌ none |

**Command.** The deciding axis is masquerade: a sentinel makes the sender
a string the agent controls, so any agent can impersonate another by
emitting `@target from:scientist …`. The command is stamped by the
wrapper, which alone holds the session's `--as` identity, so the sender
cannot be forged. An internal hub reached the same conclusion -- sender comes
from `$AGENT_NAME` set by the trusted wrapper, not from message text. The
**human uses the same `trax send`**, from a shell; one path, sender
always attested by who ran it.

### Naming: the name is the `Actor`

`Inquiry.Actor` (`types/inquiries.py:134`, `type Actor = str`, "a user or
**agent identity** string") is already the agent-name primitive: free-form,
already set by `--as` via `resolve_actor` (`trax/verbs.py:703`), already
landing on every `change_log` row as an agent label
(`types/change_log.py:230`). No separate `friendly_name` is introduced.

- `trax run claude --as scientist`; default
  `os.getenv("AGENTNAME", "Agent")` (matches the existing `resolve_actor`
  precedence).
- **`session/start` renegotiates** the actor when it collides with a
  *live* session (appends a suffix), and returns the granted name. The
  **renegotiated name becomes the actor everywhere** -- one concept, an
  honest `scientist#2` when two scientists are live. Audit authorship and
  the routing handle never diverge.

Today nothing enforces actor uniqueness (duplicates are fine for
authorship); routing adds uniqueness **only among live sessions**, on
`start`.

### Rooms: namespaces for addressing

A `rooms: list[str]` field on the **`AgentSession`** row (not the event:
membership is mutable session state, reassignable via the existing
`agentsession_*` field-PUT routes; an event is append-only per-turn).

- An actor **joins** multiple rooms; `rooms` is the membership list.
- **Address = `@actor:room`.** Bare `@actor` is sugar only when the actor
  is in exactly one room; otherwise ambiguous and rejected.
- **Inbound** injection carries a `[room] sender:` prefix so the agent
  knows the context of a message (a single PTY receives all rooms'
  messages interleaved into one stdin).
- **Outbound**, the agent names the target room explicitly per send
  (`@target:room`); there is no implicit "current room," because the
  agent has one physical conversation, not one per room.

### Delivery: drop-if-absent + synchronous receipt

If the target's `trax run` is not connected when a message arrives:

|                        | drop-if-absent | durable hold |
|------------------------|:-:|:-:|
| matches live-channel norm (IRC, chat hubs, pipes) | ✅ | ❌ |
| avoids stale steering  | ✅ | ❌ (held msg injected into a moved-on session) |
| sender learns outcome  | ✅ receipt | 🟡 eventually |
| implementation         | ✅ none | ❌ queue + timer |

**Drop-if-absent, with a synchronous delivery receipt.** Injection is
*live steering*; a message held and injected minutes later into a session
that has moved on is worse than dropping it. The sender learns
`delivered` / `undelivered` immediately (the `trax send` command's return
value). This matches an internal hub (a write to a dead stdin
is simply lost) and IRC. A durable **inbox** -- "queue work for an agent
not yet running" -- is a different feature, deferred until it is a real
want.

### Transport: HTTP polling (kept -- the NOTIFY upgrade was tried and cut)

**As built: `trax run` polls.** The inbound poll loop calls
`GET /api/sessions/<id>/inbound` (drain) on a fixed interval and injects
whatever it drains (`trax/run/session.py` `_inbound_poll_loop`). Server
state is the process-local `InboundQueue` (`server/inbound.py`). This is
the simplest thing that closes the loop and is fully testable without a
streaming primitive; the cost is up-to-`poll_interval` latency and idle
chatter.

**The NOTIFY/SSE upgrade was evaluated during the subscriber-push work and
rejected on measurement** (see `design_subscriber.md`, "Why a sweep
and not LISTEN/NOTIFY"): a dead LISTEN connection fails silently, so a
polling backstop must exist regardless, and end-to-end latency is gated by
this very poll loop -- server-side immediacy is unobservable. The seam
(`drain_inbound` / `enqueue`) still permits a streaming swap if a
poller-free consumer ever appears. The diagram's "subscribe (pull)"
denotes the client-initiated pull, not server push.

The server-side **subscriber push** (change notifications routed into
subscribers' live sessions through this same inbound queue) is documented
in `design_subscriber.md`.

## What we're wiring

```
┌──────────────┐  trax send @actor:room "…"   ┌──────────────────┐
│  web UI      │ ───────────────────────────▶ │   Trackinizer    │
│  other agent │   (POST; sender = caller)    │  resolve name,   │
│  trax run    │                              │  route to live   │
└──────────────┘ ◀── subscribe (pull/SSE) ─── │  session, push   │
       ▲            routed message              └──────────────────┘
       │                                                 │
       │ writes bracketed-paste + Enter to PTY master    │
       ▼                                                 │
  claude / codex (PTY slave)            ────capture──────┘
  human ALSO drives the native TUI       (existing logger;
                                          injected msg logged
                                          as a normal turn)
```

## Roadmap

Three phases, each delivering a usable artifact and a verification gate.
The order is deliberate: the viewer is built **first as the instrument
that proves `trax run` scrapes correctly**, before any injection work.
Both CLIs ship together at each messaging step -- codex parity is a
requirement and the injection protocol is shared.

### Phase 1a -- view sessions (the verification tool)

A read-only web UI: list sessions, click one, watch its turns render as
markdown, live via SSE. **Gate: if turns render, `trax run claude|codex`
is scraping to the server correctly.** No injection yet.

Verified starting facts (read from the SPA, not assumed):

- `AgentSession` is a full inquiry kind on the **server** (routes exist,
  `api_agent_session_events.md:51`), but the SPA omits it: `ALL_KINDS`
  (`server/assets/index.html:324`) draws from `ARTIFACT_KINDS`
  (`:320`), which lists `Artifact, Experiment, Paper, Belief, CodeChange,
  WebResult, WebSearch` -- no `AgentSession`. So it does not list today.
- The drift test is path-only and one-directional
  (`assets_drift_test.py:81`): it checks every SPA `/api/...` literal has
  a route, **not** kind coverage. Adding `AgentSession` breaks nothing.
- The SPA's SSE (`index.html:1535`, `/api/web/subscribe`) is
  inquiry-grained: payload is a mutated inquiry `{id}` (`web.py:202`),
  and appended events fire **no** NOTIFY (events sit outside
  `inquiries`/`change_log`, `api_agent_session_events.md:98`). Live event
  streaming therefore needs a new notify-on-append hook.

Steps:

1. **List `AgentSession`.** Add `"AgentSession"` to the SPA kind set
   (`index.html:321`). Lists + detail + picker come for free via the
   existing kind machinery. (Free; safe under the drift test.)
2. **Notify on event append.** `Store.append_events` fires a Postgres
   notify carrying the session id (+ a marker distinguishing it from
   inquiry mutations). Reuses `server/notify.py`. Touches
   `server/store.py`. This hook is the one server change Phase 1a needs
   *because* live SSE (not polling) was chosen; Phase 2 reuses it.
3. **Event SSE stream.** A session-scoped event stream the SPA subscribes
   to (a dedicated `/api/sessions/<id>/events/stream`, or extend the
   `/api/web/subscribe` payload to carry an event marker). Reuses
   `iter_sse_events`. Touches `server/web.py`.
4. **Session-detail event view.** SPA panel fetches
   `/api/sessions/<id>/events` (spelled as a single literal so the drift
   scan covers it, `assets_drift_test.py:12`), renders each `Message`
   member (`UserMessage` / `AssistantMessage` -- text + thinking +
   tool_calls / `ToolResult` / `Compaction`) as markdown, and appends on
   each SSE event. Active vs. previous = `ended IS NULL` on the session
   row. Touches `index.html`.

### Phase 2a -- push a message to one instance

Inject into a single session by its `session_id` -- no naming, no rooms.
Proves the inject loop end to end. Split so the PTY pump (the largest
client change) retires independently of the UI.

5. **`--as` + actor renegotiation on `session/start`.** `trax run --as`
   sets the session actor; default `os.getenv("AGENTNAME", "Agent")`.
   `session/start` makes the actor unique among *live* sessions (suffix on
   collision) and returns the granted name; `trax run` adopts it. Touches
   `trax/run/session.py`, `server/api/sessions_routes.py`,
   `wire/wire_sessions.py` (`SessionStartResponse` already carries
   server-minted fields). Tested: two concurrent `--as scientist` runs get
   distinct live names.
6. **PTY pump in `trax run` (2a-i).** Replace the bare
   `subprocess.Popen(argv)` (`trax/run/session.py`) with a PTY: allocate
   master/slave, spawn the CLI on the slave, raw-mode the real terminal,
   copy real-stdin <-> master <-> real-stdout, forward `SIGWINCH`. Capture
   (the drain thread) is untouched. New module under `trax/run/`;
   `session.py` stays orchestration-only. Tested: human I/O byte-
   transparent; the CLI TUI renders natively under the pump. Retire the
   pump risk here, no UI involved.
7. **Injection: routed message -> PTY (2a-ii).** `trax run` subscribes to
   its session's routed-message channel (the Phase-1a notify path,
   session-scoped). On a message: write `\e[200~<text>\e[201~`, wait
   >120ms, write `\r`; gate on composer-ready (codex MCP-boot delay,
   [Empirical findings](#empirical-findings)). A push-to-session route
   (by `session_id`) + a chat box in the detail view. Touches the pump,
   `client/client.py`, `server/api`, `index.html`. Tested against **both**
   CLIs: a routed message reaches the model; concurrent human typing is
   not corrupted (bracketed-paste atomicity).

### Phase 2 -- broader chat system

Name resolution, rooms, agent-to-agent, receipts.

8. **`rooms` on `AgentSession`.** Add `rooms: list[str]` (field-PUT via the
   existing `agentsession_*` machinery). `trax run --room` (repeatable)
   sets initial membership. Touches `types/inquiries.py` (+ test),
   `server/assets/schema.sql` (codegen), `server/api/sessions_routes.py`.
   Tested: `from_row` round-trip; join / leave via PUT.
9. **Routing: `trax send` + server resolve/push.** `trax send @actor:room
   "…"` resolves the name (scoped to room) to a live session, pushes via
   the notify fanout, returns `delivered` / `undelivered`
   (drop-if-absent). New `server/api/messaging_routes.py`; new `trax send`
   verb (`trax/grammar.py`, `trax/verbs.py`); `client.send_message`.
   Tested: `@actor` unambiguous resolve; `@actor:room` scoped resolve;
   bare `@actor` ambiguous -> error; undelivered receipt when offline;
   `@*` / prefix targeting.
10. **Agent-initiated send (as built: env identity, not interception).**
    An agent inside a session sends by invoking the same `trax send @Y`
    (step 9) -- no new send mechanism. To address peers it needs to know
    its own routing identity, so `trax run` exports `TRAX_ACTOR` /
    `TRAX_ROOMS` into the wrapped CLI's environment (`_routing_env`,
    injected via the pump's child `env`). Sender attribution is the
    authenticated principal on the `/api/messages` route, which is
    unforgeable.

    The originally-planned **tool-call interceptor** (parse the captured
    turn, recognize a `trax send` tool call, re-emit it with the `--as`
    name) was **not built**: it duplicates a send the agent already made,
    requires per-CLI tool-call parsing, and buys only cosmetic attribution
    (the `--as` name instead of the principal) -- which in the single-human
    / shared-token deploy is not even distinguishing. Revisit only if
    per-agent inbound attribution becomes a real requirement. Tested: env
    identity reaches the child; the human/agent `trax send` path resolves
    and enqueues (step 9 tests).
11. **Web UI multi-room chat.** Extend the Phase-1a/2a detail view to
    address `@actor:room`, show room context (`[room] sender:`), and
    target across rooms. Touches `index.html`.
12. **Multi-agent console (as built).** A dedicated IRC-like console page
    (`server/assets/console.html`, served at `/console`) shows every
    session's turns in one time-ordered feed, not one session at a time. It
    is backed by a cross-session aggregated feed route
    (`GET /api/web/feed`, `Store.read_feed`) that interleaves
    `agent_session_events` across sessions ordered by the server `created`
    clock, each turn joined to its session's routing identity
    (`actor`/`rooms`/`cli`). The page polls the feed with a **composite**
    keyset cursor (`(created, session_id, seq)` -- the full order key, so a
    same-`created` tie split across a page boundary is never skipped); the
    first live page is fetched `tail=true` (newest N) so a backlog does not
    replay from the beginning. It supports a **time-range** filter
    (`since`/`until` for history vs. the live tail), per-room and per-agent
    pill filters applied client-side over a capped retained buffer, and a
    targeting input (`@actor` / `@actor:room` / `@*` broadcast over the
    visible agent/room pairs) that posts to `/api/messages`. A dedicated
    `(created, session_id, seq)` index on `agent_session_events` serves the
    hot polling path. Transport is HTTP polling, consistent with the
    [Transport](#transport-http-polling-now-notifysse-is-the-upgrade)
    stance; the feed route is the seam a later NOTIFY/SSE push swaps in
    behind. Tested: cross-session interleave + room/actor filters + the
    `since` cursor (`read_feed` integration test); the route's
    `next_after` cursor and limit bounds (`web_test`).

Critical path: Phase 1a (1->2->3->4) gates everything as the scrape
verifier; then 5->6->7 for single-session injection; then 8->9->10->11 for
chat. Smallest first win: step 1 (one string).

## What's load-bearing vs. incidental

**Load-bearing** (changing breaks the design):

- The channel is **separate from the log**; sends are not extracted from
  `agent_session_events`.
- `trax run` owns the **PTY master**; the human and the injector are peers
  on it.
- Injection protocol: **bracketed paste + Enter delayed >120ms**. Each
  `inject` is synchronous through submit (paste, wait, `\r`) so back-to-back
  injections never merge into one submit. Empirically claude accepts
  injection from `t~=0` (the PTY buffers); an explicit composer-ready gate
  was found unnecessary for claude and is unverified for codex (local auth
  blocked) -- the poller currently gates only on session-open.
- The routing name **is the `Actor`**; the renegotiated name is the actor
  everywhere (no separate handle). *As built*, one seam diverges: the
  `TRAX_ACTOR` env exported into the wrapped CLI is the **requested** name,
  not the granted one, because the session opens lazily (on first captured
  event) and the granted name is unknown at fork time. See
  [Known gaps](#known-gaps--todo).
- Sender is **attested by the route, never by request body** (anti-
  masquerade). *As built*, the inbound enqueue route stamps the
  **authenticated principal** (`identity.email`); the `--as`-name
  attestation belongs to Phase 2's agent-initiated `trax send`, where the
  caller's session resolves the routing name. `body.source` is always
  ignored.
- Delivery is **drop-if-absent**; live steering must not be staled by a
  durable hold.

**Incidental** (could change without redesign):

- The exact send-expression surface (a `trax send` binary vs. an MCP
  tool) -- both are wrapper-intercepted and wrapper-attested.
- The renegotiation suffix scheme (`#2` vs. other).
- Reusing NOTIFY/SSE vs. another push transport for the routed message.
- The `[room] sender:` inbound prefix format.

## Known gaps / TODO

- **CLI slash-commands are captured best-effort, not exhaustively.** Commands
  the user types into the CLI's own TUI -- `/exit`, `/model`, `/clear` -- are
  handled inside the CLI and never written to its rollout/session log, so the
  log tailer cannot see them. *As built*, the pump tees the human's stdin
  (the `on_input` observer, `trax/run/pty_pump.py`) into a keystroke detector
  (`trax/run/slash.py`); a submitted leading-`/` line becomes a
  `SlashCommand` capture event, emitted by the drain thread (the single sink
  writer) so it serializes with file-sourced turns. It is **best-effort**:
  raw-mode stdin carries control bytes, so the detector handles the common
  editing keys (backspace, Ctrl-U, Enter, Ctrl-C) and treats anything else as
  text. A command recalled via arrow-key history or edited mid-line with
  cursor motion may be missed or mis-read -- a miss costs only an un-logged
  command, never corrupts capture. Injected (server-routed) bytes bypass the
  tee, so they are never mistaken for typed commands.

- **`TRAX_ACTOR` exports the requested name, not the granted one.** The
  session opens lazily, on the first captured event, so at `pty.fork` time
  the server has not yet minted the session (and thus has not renegotiated a
  collision suffix). The child therefore sees `TRAX_ACTOR=scientist` even
  when the server routes the session as `scientist#2`. An agent that tells a
  peer "ping me at `@scientist`" then misroutes on a collision. Closing it
  means **opening the session before the fork** (a pre-spawn reservation), at
  the cost of an empty session row for a run that captures nothing. Accepted
  for now: collisions are rare, the effect is a misrouted message
  (drop-if-absent), not data loss, and lazy-open keeps abandoned runs from
  leaving empty rows. Routability of an idle session has the same root cause
  -- a run is not addressable until its first captured event opens the row.

## When to revisit

| Trigger | What changes |
|---|---|
| Need to message an agent **not yet running** | Add a durable inbox (separate from live steering) |
| A third CLI must be messageable | Verify its input model; extend the injection protocol |
| Name collisions across users become common | Scope actor uniqueness per-user, not global-live |
| Per-room response channels wanted | The single-PTY model can't; would need per-room sub-sessions |
| Web UI needs sub-second transcript latency | The notify-on-append hook (Phase 1a, step 2) already covers it |

None of these rewrite the channel or the naming model; they layer on it.
