# Changelog

All notable trackinizer changes are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

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
- `trax export` and `GET /api/export` write the whole graph as JSON lines:
  every inquiry, edge, and `change_log` row, experiment metrics, and
  agent-session records, read in one snapshot. The header names the applied
  schema migrations, and an unchanged graph exports byte-for-byte the same,
  so the file works as a backup that outlives a datadir and as input to a
  mirror. Embeddings, session ciphertext, and auth tables are left out.

- A new web app replaces the old pages. It ships built inside the wheel and
  the server serves it at `/app/` by default (`--app-dir` still overrides
  it); `/` redirects there, and old `#/...` links keep working.
  - The graph is the home view: a force layout of the newest inquiries (any
    node limit), grouped by root by default with a list of roots, search,
    focus on a node with its subgraph one to three hops out, a key whose kinds
    click to show only that kind, and Replay in creation order.
  - Each kind has a list with List, Streams, Outline and Columns views. A
    detail has a Parents and Children rail, a two-hop graph preview and Show
    in graph; Activity shows every change kind; search has a page of its own.
  - A console for agent sessions: saved, pinnable views; agent and room
    facets; verbosity levels (messages, calls, output, everything); `@`
    suggestions ranked by the view; messages to live sessions; and an
    activity minimap over the last 7 days.
  - Agent-session transcripts read as prose. Each tool call and its result
    is one line that opens in place, runs of them fold into a counted group,
    and contents are highlighted: commands, files by language, diffs per
    side, JSON and terminal colours. Encrypted reasoning is not shown.
  - Side panels and the sidebar collapse (`[`, `]`, Cmd/Ctrl+B), a switch
    beside the user picks light or dark, `Kind#seq` references render as
    links to their inquiry, and absolute paths copy on click.
- `GET /api/web/graph?focus=<uuid>&hops=N` returns one inquiry's
  neighbourhood, nearest first.
- `GET /api/web/feed` filters by repeated `actor`, `room`, `cli` and `kind`,
  and `conversation=true` keeps only what people and agents said;
  `GET /api/web/feed/facets` counts agents and rooms, and
  `GET /api/web/feed/histogram` counts records per time bucket over at most 7
  days.
- `GET /api/inquiries` and `GET /api/web/search` take `fields=` to return only
  the named keys; `GET /api/inquiries?ancestors=narrows` adds each row's
  `narrows` ancestors; `GET /api/change_log` takes a repeated `kind` and
  `brief=true`.

### Changed

- The sign-in page takes the new app's look, over a slowly growing graph.
- Hashed web-app assets are cached for a year as immutable; `index.html`
  stays `no-cache`.
- `TRACKINIZER_NO_AUTH=1` now sets the CLI's default, as documented;
  `--auth` overrides it.
- Feed reads are bounded: a tail read of the newest records no longer scans
  the whole record table (seconds to milliseconds on millions of records).

### Removed

- The old UI pages (`index.html`, `me.html`, `admin.html`, `graph.html`,
  `console.html`) and the helpers only they used.
- Google OAuth sign-in is no longer part of the public package. Run
  `--no-auth` for single-user local use, with API tokens for the CLI and
  agents.

### Fixed

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
- A malformed request body returns HTTP 422 instead of 500. A stray key
  in a client-supplied `message` raised a bare `ValueError` out of the
  codec, which matched no handler; it is now a `SchemaError` the API
  maps to 422, reporting both the offending and the valid field names.
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
