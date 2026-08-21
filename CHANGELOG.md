# Changelog

All notable trackinizer changes are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

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
