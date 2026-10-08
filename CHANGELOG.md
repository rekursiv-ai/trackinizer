# Changelog

All notable trackinizer changes are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- Variables: an org stores launch variables in a `variables` table, served by
  `/api/variables`. Secret values live in a backend chosen by
  `TRACKINIZER_SECRETS` (a file store or AWS Secrets Manager); the table keeps
  only the name and who set it.
- `trax env` lists, sets and deletes the org's variables, and `Client` gains
  `list_variables`, `put_variable` and `delete_variable`.
- `trax run` masks the values named in `TRAX_REDACT_NAMES` in every sink it
  writes, including multi-line and cut-off forms and base64 attachment bytes
  of uploaded records. It exits when a named value is missing or shorter than
  `MIN_SECRET_LENGTH`.
- `trax machine` registers machines and their roles, labels and facts.
  Matching `Client` methods and `/api/machines` routes cover list, get, put,
  label changes and delete.
- Machine credentials: an admin enrolls a machine for a one-use, 15-minute
  token; the host joins once, heartbeats, and reads online until 180 s of
  silence. A revoked credential answers 410, an unknown one 401.
- `trax` takes its server and key from `TRACKINIZER_URL` and
  `TRACKINIZER_TOKEN`. A malformed token is refused, and a lone token is
  ignored.
- Queued inbound messages carry `InboundDrainItem.source_role` and Chat
  messages carry `ChatMessage.author_role`, the sender's attested role.
- `Client.get` accepts a per-request `timeout`.

### Changed

- `trax env NAME del` reports success when the variable is already gone.
  Variable rows are written before secret values.
- `trax run` no longer masks a bare PEM armour fragment; full armour lines of
  a configured PEM value are still masked.
- **Breaking:** `Redactor.redact_json` and `redact_mapping` take `PlainTree`;
  `redact_mapping` returns `Mapping[str, PlainTree]`.

## 0.1.6 - 2026-10-07

### Added

- `Store.submit` accepts any concrete inquiry submission body, using the
  same typed dispatcher for single submissions and mixed-kind batches.

### Changed

- **Breaking:** Python JSON types move from `trackinizer.lib.custom_json` to
  `trackinizer.lib.codec`: client return annotations use `PlainTree`, and
  session-record JSON fields use `Mapping[str, PlainTree]`. Wire-model JSON
  aliases now live in `trackinizer.wire.json_types`.
- Session payloads use the shared codec's record tags, while provider JSON
  fields such as `extra` and tool arguments stay plain objects and arrays.
  Reads still accept older plain records; the web transcript and conversation
  feed accept both plain and tagged attachment arrays.
- Structured Artifact snapshots no longer have a second 30 MB check after
  citations are frozen. Publication requests remain capped at 30 MB, HTML
  remains capped at 30 MB, and each publisher's storage remains 500 MB.

### Fixed

- `trax agentsession SEQ run claude|codex` writes the resumed transcript
  before stamping its new CLI session id on the server, and stamps it before
  starting the runner. A failed file write or missing reasoning ciphertext
  leaves the server's session id unchanged.
- Resume checks the records the target will actually write, including when
  resuming in the same format, so dropped acts require `--lossy`. Native
  runner arguments after `--` pass through unchanged.
- `trax run` ends its session before waiting for the inbound poller to stop.
  Ending a session releases held inbound requests immediately, instead of
  making exit wait out the poll; queued messages are not drained to an
  exiting runner.
- Session search checks for locally cached weights before constructing an
  embedder, including `model=` overrides, and falls back to full text when
  weights are absent. Override instances are reused by resolved model name;
  `dim` without `model` answers 400.
- Session readers preserve malformed provider fields instead of aborting the
  rest of a Claude, Codex or sagent transcript. Claude replay also handles
  untyped foreign tool results and avoids duplicate calls when a result
  precedes its call.
- `trax` treats `run` and `metric` as literal field values unless they follow
  a complete command subject or clause. Adding an already-present edge now
  prints `exists:` instead of silently succeeding.
- `GET /api/web/feed` rejects invalid time windows, and feed histograms choose
  their starting point from records matching the requested filters.
- Graph mirroring checks existing edges through their source inquiries,
  instead of the newest-node graph window, so older edges are not reported
  as newly added. Live graph replay rejects `--seed` with `--traverse` or
  `--limit`.
- Malformed signed session and OAuth-state cookie payloads are ignored
  instead of raising a server error.

### Removed

- **Breaking:** `trackinizer.trax.run.materialize.materialize_claude`; use
  `materialize(target="claude", ...)` instead.

## 0.1.5 - 2026-10-03

### Added

- A new web app replaces the old pages. It ships built inside the wheel, and
  the server serves it at `/app/`; `/` and the old `/me`, `/admin`, `/graph`
  and `/console` paths redirect into it, and old `#/...` links keep working.
  `--app-dir DIR` serves a build kept elsewhere instead, read on every request,
  so a new build goes live without a restart. To use it on your own machine,
  run `trackinizer --no-auth` and open `http://127.0.0.1:8765/app/`.
  - The graph is the home view: a force layout of the newest inquiries (any
    node limit), grouped by root by default with a list of roots, search,
    focus on a node with its subgraph one to three hops out, a key whose kinds
    click to show only that kind, and Replay in creation order.
  - Each kind has a list, and Issue lists add Streams, Outline and Columns
    views. A detail has a Parents and Children rail, a two-hop graph preview
    and Show in graph; Activity shows every change kind; search has a page of
    its own.
  - A console for agent sessions: saved, pinnable views; agent and room
    facets; verbosity levels (messages, calls, output, everything); `@`
    suggestions ranked by the view; messages to live sessions; and an
    activity minimap over the last 7 days.
  - Agent-session transcripts read as prose. Each tool call and its result
    is one line that opens in place, runs of them fold into a counted group,
    and contents are highlighted: commands, files by language, diffs per
    side, JSON and terminal colours. Encrypted reasoning is not shown.
  - Settings lists the names that mean you: filters for "me" match your email
    and the names you add. The owner names on your rows are offered to add,
    the 8 most frequent first, then Show all; typing narrows them.
  - Side panels and the sidebar collapse (`[`, `]`, Cmd/Ctrl+B), a switch
    beside the user picks light or dark, `Kind#seq` references render as
    links to their inquiry, and absolute paths copy on click.
  - Hashed assets are cached for a year as immutable; `index.html` is
    `no-cache`, so a new build shows on the next load.
- `trax run sh -- CMD` wraps any command and captures its stdin, stdout and
  stderr as separate records.
- `trax run --detach` hosts the CLI in a background process that outlives the
  terminal, so a long-running agent needs no tmux. `--name` names the host,
  and `trax run ls`, `attach NAME` (Ctrl-\ detaches), `log NAME [--follow]`,
  `send NAME TEXT` and `stop NAME` reach it. Its state and scrollback live
  under `state_dir()/rekursiv-ai/trax/run/hosts/<name>/`.
- `trax agentsession SEQ run claude|codex [--lossy] [-- ARGS]` resumes a
  stored session in that CLI, whichever CLI captured it: it writes the session
  as the target's own session file and starts `trax run` on it, so new turns
  land in the same AgentSession. A conversion that would drop records is
  refused unless `--lossy` is given.
- Captured records are filter fields on AgentSession, so `trax agentsession
  tool_call re deploy` finds the sessions with a matching tool call.
- Session search: `trax search-sessions "QUERY"`, `GET
  /api/web/search_sessions` and `Client.search_sessions` merge full-text and
  embedding hits. Full text is the default. `--session-embedder` picks a model
  (Qwen3-Embedding 0.6B, 4B or 8B, Octen-Embedding-8B, or
  jina-embeddings-v5-text nano or small); `python -m
  trackinizer.server.prep_models` downloads its weights before serving, since
  a request never downloads them. `python -m
  trackinizer.server.tools.backfill_embedding DSN` writes the vectors, spread
  over GPUs, and `--follow` keeps them current. `--no-semantic` asks for full
  text alone.
- Subscribers hear about changes: each change to an inquiry is pushed, as a
  one-line JSON notice naming the `trax` command to read it, into the live
  `trax run` session of every subscriber.
- `trax next owner to ACTOR`, `POST /api/inquiries/next_issue` and
  `Client.claim_next_issue` pick and claim the next available Issue in one
  step, so concurrent agents never get the same Issue. Bare `trax next` still
  only previews.
- A leading filter field queries every kind: `trax title re retry`.
- An opt-in agent canvas (Settings, "Enable agent-guided canvas"; off by
  default) puts visuals beside lists and details: chat, a record's context
  graph, an evidence timeline and Artifact content. Paired with one of your
  live `trax run` sessions, it lets you chat about a record, and the agent
  shows, hides, focuses or places visuals with `trax workspace`. A canvas
  saves and reopens as a named workflow. Routes: `GET /api/visuals`,
  `/api/workspaces/...`, `/api/workspace-presets/...` and `PUT
  /api/me/visual-workspace`.
- Shared Artifact content: `POST /api/artifacts/content` publishes structured
  findings, with graph citations frozen at publication, or HTML, as an
  Artifact an Issue produced; `previous_artifact_id` publishes a revision that
  supersedes the last. Teammates read it at `GET
  /api/artifacts/{id}/content`, and HTML opens as a sandboxed page at `GET
  /api/artifacts/{id}/html`. Each body is capped at 30 MB, and each
  publisher at 500 MB in all.
- Addons: `--addons FACTORY` (or `TRACKINIZER_ADDONS`) names a deployment
  config whose addons run services inside the server or in a process of their
  own (`python -m trackinizer.addons run|show`) and mount routes under
  `/api/addons/<name>`. `--addon-override PATH=VALUE` edits it, and `GET
  /api/addons` lists what it runs. See `docs/addons.md`.
- Derived belief confidence: `trax confidence KIND SEQ`, `GET
  /api/inquiries/{id}/confidence`, and `Client.confidence_for` fold a
  Belief/Experiment's currently-true `proves` citations into a log-odds sum and
  map it through a logistic (`sigmoid(sum(citation_confidence * valence))`).
  Neutral 0.5, symmetric, recursion into claimable citers, DAG one-pass.
  Read-only: it never writes the stored row, so it can disagree with a human's
  prior, which is the point.
- Derived load-bearing authority: `trax authority KIND SEQ`, `GET
  /api/inquiries/{id}/authority`, and `Client.authority_for` expose PageRank
  over each citation-relation graph (`proves`, `favors`, `cited_by`, and the
  union `issue` = `requires`/`narrows`), stored in four `inquiries` columns
  (migration 025). A periodic background sweep recomputes them off the request
  path, coalescing edge-change bursts.
- `trax export`, `GET /api/export` and `Client.export` write the whole graph
  as JSON lines: every inquiry, edge, and `change_log` row, experiment
  metrics, and agent-session records, read in one snapshot. The header names
  the applied schema migrations, and an unchanged graph exports byte-for-byte
  the same, so the file works as a backup that outlives a datadir and as
  input to a mirror. Embeddings, session ciphertext, auth tables, per-user
  canvas state and the bodies of published Artifact content are left out.
- `GET /api/web/graph?focus=<uuid>&hops=N` returns one inquiry's
  neighbourhood, nearest first.
- `GET /api/web/feed` filters by repeated `actor`, `room`, `cli` and `kind`,
  and `conversation=true` keeps only what people and agents said;
  `GET /api/web/feed/facets` counts agents and rooms, and
  `GET /api/web/feed/histogram` counts records per time bucket over at most 7
  days.
- `GET /api/inquiries` and `GET /api/web/search` take `fields=` to return only
  the named keys; `GET /api/inquiries?ancestors=narrows` adds each row's
  `narrows` ancestors, and `narrows` is a filter field on Issues (`isnull`
  for roots); `GET /api/change_log` takes a repeated `kind` and `brief=true`.
- `GET /api/meta/edges` lists, for each edge kind, the kinds each end admits,
  the names of its two directions and the annotations it takes.
  `GET /api/sessions/{id}/turns` reads a session's latest conversation turns
  without the tool records between them.
- `Client.wait_until_ready` blocks until a server that was just started
  answers.

### Changed

- **Breaking:** an agent session is stored as the records its CLI wrote, one
  row per act, instead of as per-turn events. `trax run` uploads them to `POST
  /api/sessions/{id}/records`, read back with `GET .../records` and `GET
  .../parts`, replacing `/api/sessions/{id}/events`; `Client.append_records`
  and `Client.read_session_records` replace `append_events` and
  `read_events`; the feed returns records; and `trax run --out` writes one
  record per line. A rewritten session file is ingested again without
  duplicates, and encrypted reasoning is kept apart from the searchable text.
  Upgrade the server and every `trax` together: a 0.1.4 `trax run` cannot
  sync to this server, nor this `trax run` to a 0.1.4 server.
- **Breaking:** the first start on a 0.1.4 database applies migrations 019 to
  032 before the server listens, so back up the datadir (or `pg_dump`) first.
  020 copies every stored session event into the new tables, which takes a
  while on a large table, and 021 then drops the old table. 031 tightens the
  metric-key check: if a stored key is whitespace only, the migration fails
  and leaves the database as it was; delete that row and start again. On
  `--engine pg`, the session-search tables need pgvector's `halfvec` type
  (pgvector 0.7 or newer).
- **Breaking:** `trax next` and `GET /api/inquiries/next_issue` offer only
  Issues with no owner, and an inquiry created through `trax` no longer takes
  the acting user as its owner: it has one only when one is given. Claim work
  with `trax next owner to ACTOR`.
- **Breaking:** `GET /api/web/graph` returns at most `limit` nodes, neighbours
  included, instead of the newest `limit` plus every older neighbour. `limit`
  has no upper bound, and `limit=0`, which meant the whole graph, now answers
  400; pass a count at least the number of inquiries instead.
- A `trax experiment ... metric` command takes at most one `to`, `sort` and
  `limit`, and a second is an error. The documented form with two `to`s
  wrote nothing, since its masks AND into no cell. A read returns at most
  1000 cells, which is also the default.
- Dependencies: httpx2 replaces httpx, and every install now carries torch,
  transformers, huggingface-hub and peft for the session embedders (a `pip
  install` on Linux pulls PyPI's CUDA build of torch), plus configgle,
  bashlex, zstandard, and watchdog on macOS. Python 3.12 to 3.14, as before.
- `--log-level` also sets uvicorn's access log, so `--log-level WARNING`
  drops the line per request.
- The agent skill moved from `trax/docs/trax-skill.md` to
  `trax/docs/skills/trax/SKILL.md`, with each kind's guide in a directory
  beneath it, so linking one directory installs them all.
- The sign-in page takes the new app's look, over a slowly growing graph.
- Feed reads are bounded: a tail read of the newest records no longer scans
  the whole record table (seconds to milliseconds on millions of records).
- The README opens with a 27-second demo video, and its screenshots show the
  new app.

### Removed

- **Breaking:** `trax search` and `Client.search`. Filter queries replace them
  (`trax issue title re retry`, or `trax title re retry` across kinds), and
  `GET /api/web/search` remains.
- **Breaking:** the server's `--workers` flag. The server runs as one process
  again: live-session message queues are held in memory, and several workers
  lost or duplicated messages.
- **Breaking:** Google OAuth sign-in, with `/auth/login`, `/auth/callback`
  and the environment variables that configured them. Run `--no-auth` for
  single-user local use, with API tokens for the CLI and agents. A deployment
  can mount its own sign-in routes; the sign-in page offers them when `GET
  /auth/login/ready` answers.
- **Breaking:** the modules `trackinizer.types.agent_session_events` and
  `trackinizer.trax.run.pty_pump`; `trackinizer.lib.custom_types` is now
  `trackinizer.lib.absent`.
- The old UI pages (`index.html`, `me.html`, `admin.html`, `graph.html`,
  `console.html`).

### Fixed

- `TRACKINIZER_NO_AUTH=1` takes effect when the server starts from the
  `trackinizer` command, as documented; the new `--auth` overrides it.
- A run that dies without ending its session (killed, host crashed) no longer
  leaves its AgentSession active forever: the server ends a polling session
  after 15 minutes without a poll or an upload, and reopens it if the run
  comes back.
- A `trax run` that loses the server keeps its session and keeps listening
  for messages. It writes to a local file meanwhile and catches the server up
  every 30 s once it is back, instead of staying local for the rest of the
  run; a run started during an outage registers when the server returns, and
  at exit a run says whether the server is missing part of it.
- `trax run` keeps records it used to lose: it reads every session file to
  its end at exit, follows files on macOS that a writer holds open, re-arms
  its file watch after a transient failure, goes on after a failed parse or
  upload instead of dropping later turns, and ignores the session files of
  other runs in other directories. Claude's session bookkeeping is no longer
  captured as empty turns, and one bad inbound message no longer discards the
  rest of its batch.
- The live stream (`/api/web/subscribe`) sends a comment at once and every
  25 s, so it opens behind a proxy that holds headers until the first byte
  and survives its idle timeout. `GET /api/web/subscribe/probe` tests a
  proxy path.
- `python -m trackinizer.trax` no longer prints nothing and exits 75 on every
  command when its daemon was started from inside another checkout.
- Metric reads and writes answer 409, not 500 or a wrong match, for a step
  past BIGINT, a negative step, NaN or infinity, and a blank or NUL key; a key
  of tabs can no longer be stored, where it made every later read of its
  experiment answer 500.
- `/api/web/search` treats `'` and `\` as ordinary characters, so `don't` and
  `title:\d+` mean what they say; only `"` groups words.
- `--as` reaches creates as well as edits.
- The `Bearer` scheme is matched case-insensitively, and an empty Bearer
  credential answers 401 instead of falling through to the session cookie.
- `POST /auth/logout` with an unparsable `Origin` or `Referer` answers 403 and
  keeps the session cookie, instead of 500.

## 0.1.4 - 2026-08-20

### Added

- A per-user `trax` daemon amortizes CLI startup. The first invocation
  spawns it on a Unix socket and later ones ship their argv to it
  instead of re-importing the HTTP client stack -- ~145ms per run,
  against the ~1.4ms the server spends answering, so a polling swarm of
  agents no longer burns whole cores on imports. It backs the
  `python -m trackinizer.trax` entry point; the installed `trax`
  console script still runs in-process. `TRAX_NO_DAEMON=1` forces the
  in-process path, and `trax run` never delegates because it owns a PTY.
- The server accepts `--workers N` above 1 on `--engine pg`. Uvicorn's
  forked workers re-import the app themselves and reject a constructed
  app object (exit 3), so the app is now built by a factory that
  re-derives its configuration in each child. PGlite is still refused
  above one worker, since two engines on one workdir corrupt it.
- Row filters lower into SQL for regex, comparison, and array operators
  as well as text equality, so Postgres applies the predicate and the
  `LIMIT` in one indexed query. Previously a filtered listing fetched
  every candidate row and filtered it in Python after the window, which
  also dropped matches past the limit unseen.
- Verified bearer tokens are cached for 60 seconds with bounded
  eviction. `scrypt` is deliberately ~30ms, which is correct for a login
  form and ruinous for an API key replayed on every request: it was 30ms
  of a 32ms request and capped the server near 110 req/s regardless of
  core count. A revoked key or disabled user keeps working until the
  entry expires.
- Experiment `config` is readable and editable from the CLI --
  `trax experiment 12 config to '{"lr": 0.1}'`, or `config to @cfg.json`
  -- and prints as indented JSON, so the output round-trips back through
  `@file`. It takes one standard JSON object; it is not a filter field.
- `Client.transition_owner` compare-and-sets a row's owner and gets a
  409 when the current owner is not the expected one, matching the
  status and judgement transitions.
- `trackinizer/trax/docs/bench_trax_concurrency.sh` reports `trax`
  latency deciles under concurrency, timing the full CLI path and a bare
  authenticated HTTP request separately so a regression names the tier
  that caused it instead of only the total.

### Changed

- Per-user files moved under a `rekursiv-ai` namespace segment:
  profiles now resolve to `config_dir()/rekursiv-ai/trax/profiles`, and
  the PGlite datadir and bootstrap token to
  `data_dir()/rekursiv-ai/trackinizer/`. Nothing migrates the old
  locations, so an existing profile or database is simply not found and
  has to be moved by hand.
- Filters whose two evaluators would disagree are refused with a 400
  naming the spelling that works, rather than answered differently
  depending on which evaluator ran. Refused: a regex on a column with no
  SQL form, because Python's backtracking has no deadline where
  Postgres has a statement timeout (`(a+)+$` over 30 characters measures
  79.89 seconds); an ambiguous escape such as `\b`, which is a backspace
  to Postgres and a word boundary to Python (use `\y`); a Python-only
  escape such as `\z` or `\N{...}`; a comparison operator on a column
  the two engines order differently; and a field no column answers,
  which previously read as NULL and so made `ne` keep every row.
- A query carrying a caller-supplied regex or numeric operand runs under
  a statement timeout (5s, `TRACKINIZER_SEARCH_TIMEOUT_MS`), and a
  Postgres-side rejection of that operand is reported as 400 rather than
  surfacing as a 500.
- Listing inquiries serializes rows through a purpose-built encoder
  instead of `jsonable_encoder`, which measured 5.2ms of a 9ms 50-row
  listing -- more than the SQL, the edge fetch, and model construction
  combined. The output is unchanged and is pinned against the old
  encoder for every kind.
- Every HTTP response carries an `x-request-id` header, and the server
  logs one structured line per request with method, path, status,
  timing, and worker pid, so a slow request can be traced across
  workers.
- The Codex session adapter honors `$CODEX_HOME` instead of assuming
  `~/.codex`, and recognizes the records 2026-08 rollouts added
  (`custom_tool_call`, inter-agent `agent_message`, encrypted-only
  reasoning) rather than filing them as unknown.

### Fixed

- `trax graph` shows every issue. A cyclic component in which each issue
  is required by another contributed no root, so the traversal never
  entered it and those issues vanished from the dependency view without
  a trace; they are now swept in under a `(cycle: ...)` note. A shared
  subtree is also expanded once and referenced afterwards, instead of
  being re-rendered per path -- work exponential in depth, which never
  returned on a real table.
- `trax` resolves an `@path` value against the caller's working
  directory and reads `$USER` for the audit actor from the invoking
  environment, so a command run through the daemon records the same
  thing it would have run directly.

### Removed

- `userdirs.data_dir` and its siblings no longer take an application
  name; each returns a base directory the caller joins its own namespace
  onto. `userdirs.resolve_working_dir` and `config.default_datadir` are
  gone with no replacement.

## 0.1.3 - 2026-08-01

### Changed

- README carries a one-line description below the badges; PyPI renders the
  README, so the project page had been showing the previous text.

## 0.1.2 - 2026-08-01

### Added

- `trackinizer` and `trax` ship as console scripts, so `uv tool install`
  puts both on PATH. `trax` talks to any reachable server and needs no
  local one.

### Changed

- README documents the Inquiry model and the storage tables in place of a
  module file tree, and states that Postgres is optional -- the default
  PGlite engine bundles its own vector extension.

## 0.1.1 - 2026-07-31

### Fixed

- Cascade-dependent semantics for `proves` / `favors` citations.
- Citation-edge provenance inference regression.

### Changed

- `Final` annotations on protocol and structural constants; module-level
  response constants inlined in the edge API.
- Testing module public and internal exports reorganized.

## 0.1.0 - 2026-07-29

- Initial public release of trackinizer: a Postgres-backed agent-tracking
  server and client for Inquiries (Issues + Artifacts), with a wire protocol,
  a typed client, and a `trax` CLI.
