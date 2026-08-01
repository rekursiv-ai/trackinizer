# trackinizer🐾

[![PyPI version](https://img.shields.io/pypi/v/trackinizer.svg)](https://pypi.org/project/trackinizer/)
[![CI](https://github.com/rekursiv-ai/trackinizer/actions/workflows/package-validation.yml/badge.svg?branch=main)](https://github.com/rekursiv-ai/trackinizer/actions/workflows/package-validation.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Discord](https://img.shields.io/discord/1530237005311639592?logo=discord&logoColor=white&label=Discord&color=5865F2)](https://discord.gg/2GZFPPvCqn)

## Quick Start

```bash
# Mac:
#   # Required for quick install.
#   brew install uv
#   # Optional for a real Postgres backend; the default pglite engine
#   # bundles its own vector extension and needs neither.
#   brew install postgresql@18 pgvector

# Ubuntu/Debian:
#   # Required for quick install.
#   sudo apt-get install -y curl &&
#       curl -LsSf https://astral.sh/uv/install.sh | sh
#   # Optional for a real Postgres backend. Extension packages are named
#   # for their server major, so read it off the metapackage rather than
#   # picking one (18 above 24.04, 16 on 24.04).
#   sudo apt-get install -y postgresql &&
#       sudo apt-get install -y "postgresql-$(apt-cache depends postgresql |
#           grep -oP 'postgresql-\K\d+' | head -1)-pgvector"

# Web UI on http://localhost:8000.
uvx --from trackinizer python -m trackinizer.server
```

Centralized agent database for inquiries (Issues + Artifacts), work, and
knowledge. Three storage tables (`inquiries`, `edges`, `change_log`) backed
by Postgres (real or PGlite). FastAPI on top.

`types/` is the design contract. Every other module is a realization of
that contract over Postgres + HTTP.

## The UI

The optional SPA (`server/web.py`) browses the same records the API serves.

**Graph** — the whole inquiry web (Issues, Beliefs, Papers, Experiments, …) as typed nodes and edges.

<img src="https://raw.githubusercontent.com/rekursiv-ai/trackinizer/main/trackinizer/docs/screenshots/graph.png" width="640" alt="Force-directed graph of the inquiry web">

**Console** — live multi-agent chat, filterable by room and date.

<img src="https://raw.githubusercontent.com/rekursiv-ai/trackinizer/main/trackinizer/docs/screenshots/chat.png" width="640" alt="Live multi-agent console">

**Belief** — a record with its `before`/`after` relationship panels.

<img src="https://raw.githubusercontent.com/rekursiv-ai/trackinizer/main/trackinizer/docs/screenshots/belief.png" width="640" alt="Belief record with parent/child edges">

**Paper** — abstract, authors, and `cites` edges to other papers.

<img src="https://raw.githubusercontent.com/rekursiv-ai/trackinizer/main/trackinizer/docs/screenshots/paper.png" width="640" alt="Paper record with abstract and citations">

**Experiment** — outcome, labels, and links to the beliefs it proves or disproves.

<img src="https://raw.githubusercontent.com/rekursiv-ai/trackinizer/main/trackinizer/docs/screenshots/experiment.png" width="640" alt="Experiment record with outcome and relationships">

## Module layout

```
trackinizer/
├── README.md                       ← this file
├── docs/                           api.md (HTTP surface); old/ (design notes)
├── conftest.py                     integration fixtures (Postgres engine)
├── *_test.py, integration_test.py
│
├── types/                          design contract: pure types, no I/O
│   ├── __init__.py                 re-exports for ergonomic imports
│   ├── errors.py                   ConflictError, NotFoundError
│   ├── columns.py                  Row Protocol, ColumnSpec, FlatColumn,
│   │                                 column_specs(), flat_column_specs()
│   ├── cost.py                     Cost (signed; +/- for deltas)
│   ├── embedder.py                 Embedder Protocol
│   ├── inquiries.py                Inquiry, Issue, Artifact, Experiment,
│   │                                 Paper, Belief, CodeChange, WebResult,
│   │                                 WebSearch, results_or_empty
│   ├── edges.py                    Edge, EdgeKindPolicy, edge_policies
│   └── change_log.py               Change, Snapshot
│
├── wire/                           THE HTTP contract; imports only types/
│   ├── routes.py                   route table (inquiry_field_routes,
│   │                                 edge_field_routes), path templates,
│   │                                 KIND_URL_TOKEN -- all derived from
│   │                                 ColumnSpec metadata
│   ├── bodies.py                   Submit{Issue,...}, SubmitBatch,
│   │                                 FieldSet[T]/FieldOp[T]/FieldMutation
│   ├── edge_bodies.py              CreateEdge, CreateEdgeBatch
│   ├── filters.py                  Filter, FilterOp, FILTER_FIELD_ALIASES,
│   │                                 canonical_filter_field
│   ├── refs.py                     SeqRef, UuidRef, Ref
│   └── row_filter.py               match_filter (server + CLI-fake shared)
│
├── client/                         standalone HTTP SDK (wire/ + httpx)
│   ├── client.py                   Client: typed methods over /api/*
│   └── errors.py                   ClientError
│
├── trax/                           the `trax` CLI (a thin layer over client/)
│   ├── __main__.py, cli.py         entrypoint + dispatch
│   ├── grammar.py                  CLI token vocabulary, field/alias tables
│   ├── parser.py                   tokens → typed Actions / ListQuery
│   ├── verbs.py                    per-verb command handlers
│   ├── render.py, commands.py, profile.py
│   └── run/                        `trax run` agent-CLI session runner
│
└── server/                         the FastAPI server + storage (imports
    │                                wire/ + types/; never imported by them)
    ├── __main__.py                 entrypoint: arg parsing, uvicorn, web mount
    ├── api/
    │   ├── app.py                  FastAPI() + lifespan + exception handlers
    │   ├── submit.py               POST /api/inquiries/{kind} (+ /batch)
    │   ├── edit.py                 PUT/PATCH/DELETE /api/inquiries/{id}/{field}
    │   ├── edge.py                 /api/edges/{from}/{kind}/{to}[/{field}]
    │   ├── query.py                GET /api/inquiries*, /api/change_log*
    │   ├── auth_routes.py, oauth_routes.py, admin_routes.py
    │   ├── idempotency.py          Idempotency-Key → change_log.id middleware
    │   └── _deps.py, _routes_shared.py
    ├── store.py                    Store (orchestrator) + StubEmbedder
    ├── primitives.py               insert_inquiry/edge, upsert_embedding,
    │                                 reject_edge_cycle, validate_list_references
    ├── setter_dispatch.py          COLUMN_SPECS, RUNTIME_HOOKS; the table
    │                                 Store._set_field dispatches against
    ├── projection.py               row + edges → typed Inquiry
    ├── schema_gen.py               generated DDL (kind columns, change_log
    │                                 mirror, kind matrix, per-kind sequences)
    ├── sql.py                      schema migration loader (assets/*.sql)
    ├── sql_fragments.py            _NEXT_ISSUE_SQL, _COST_SUBTREE_SQL, ...
    ├── migrations.py               numbered data migrations
    ├── values.py                   canonical_strs, list_or_none, ...
    ├── notify.py                   NOTIFY_CHANNEL, tx, post-commit fanout
    ├── auth.py, session.py         bearer/session auth, principals
    ├── config.py                   Config, build_engine, build_embedder
    ├── web.py                      optional SPA mount + /api/web/* reads
    └── assets/                     index.html SPA + schema.sql + *.html
```

## Package dependency graph

Four layers, two legs sharing one contract spine. An arrow means
"imports / depends on" and points toward the dependency.

```
┌──────────────┐                      ┌──────────────┐
│     trax     │                      │    server    │   leaves: nothing
│  (CLI)       │                      │ (__main__)   │   imports these
└──────┬───────┘                      └──────┬───────┘
       │                                     │
       ▼                                     ▼
┌──────────────┐                      ┌──────────────┐
│    client    │                      │     api      │   server leg adds
│  (httpx SDK) │                      │  (FastAPI    │   Store; client leg
│              │                      │   handlers)  │   adds httpx
└──────┬───────┘                      └──────┬───────┘
       │                                     │
       └──────────────┬──────────────────────┘
                      │  both import
                      ▼
              ┌───────────────┐
              │     wire      │   transport contract = THE API
              │  bodies       │   definition (request/response
              │  routes       │   models, route table, filters, refs)
              │  filters      │
              │  refs         │
              └───────┬───────┘
                      │
                      ▼
              ┌───────────────┐
              │     types     │   domain dataclasses;
              │  inquiries    │   imports nothing internal
              │  edges        │
              │  change_log   │
              │  cost         │
              └───────────────┘
```

Two legs, one shared spine:

- client leg: `trax` → `client` → `wire` → `types`
- server leg: `server/__main__` → `server/api` → `wire` → `types`

The whole point of this split: `types/` + `wire/` + `client/` form a
self-contained client distribution. `wire/` and `client/` never import
`api`, `server`, `store`, `web`, `fastapi`, `asyncpg`, or `trax`, so a
published Python client carries no server dependencies.

## Layering rules

1. **`types/` is a closed island.** Nothing in `types/` imports anything
   outside it. Everything else eventually imports from it. The
   dataclasses, Protocols, and `ColumnSpec` metadata in `types/` are the
   data design contract; the rest of the package realizes it.

2. **`wire/` is the API contract; it is the single definition of the
   HTTP surface.** It holds the Pydantic request/response bodies
   (`FieldSet[T]`, `FieldOp[T]`, `FieldMutation`, `Submit*`, edge
   bodies), the `Filter`/`Ref` shapes, and the route table the server
   registers from and the client builds requests from. It imports
   `types/` and nothing else internal. Server and client both derive
   from it, so neither hand-writes a path or a body shape -- that is
   what prevents server/client/doc drift.

3. **`client/` is the standalone SDK; `trax` is a thin CLI over it.**
   `client/` is `wire/` + `httpx`. `trax` owns only CLI concerns
   (grammar, parsing, rendering) and calls `client.Client`. Neither
   `client/` nor `wire/` may import the server side or the CLI.

4. **No cycles.** Each arrow above goes one direction.
   `server/primitives.py` imports `server/setter_dispatch.RUNTIME_HOOKS`
   and *mutates* it at import time (late-binding the `results` /
   `codechanges` validators). `server/store.py` imports `primitives`,
   so the side effect lands before any `Store` instance is constructed.

5. **`server/sql.py` is orthogonal.** It loads `assets/schema.sql`
   from disk. `server/schema_gen.py` substitutes generated bodies into
   the loaded text at bootstrap; the two never import each other.

6. **`server/api/` is the HTTP boundary.** Routes are thin:
   pydantic-validate the wire body, call one `Store` method, serialize
   the result. Anything non-trivial belongs in `server/store.py`, not in
   a route.

## Reading order

Pick one of these orders depending on what you came in for.

- **"What does Trackinizer model?"**
  Start in `types/`. Read `inquiries.py`, then `edges.py`, then
  `change_log.py`. Read `docs/design.md` for the model and philosophy.

- **"What is the HTTP API / how do I avoid drift?"**
  Start in `wire/routes.py`: the route table is derived from the
  `ColumnSpec` metadata in `types/inquiries.py`. The server registers
  handlers by iterating it (`server/api/edit.py`, `edge.py`), the client
  builds requests from it (`client/client.py`), so neither hand-writes a
  path. `server/api/routes_drift_test.py` and `assets_drift_test.py`
  fail if a handler or the SPA diverges from the table.

- **"How does a submit reach the database?"**
  `server/api/submit.py` → `wire/bodies.py` (body validation) →
  `server/store.py` (`submit_X`) → `server/primitives.py`
  (`insert_inquiry`, `insert_edge`).

- **"How does an edit fan out notifications?"**
  `server/api/edit.py` → `server/store.py` (`set_X` → `_set_field`) →
  `server/setter_dispatch.py` (`RUNTIME_HOOKS[col]`) → `server/notify.py`
  (post-commit buffer + `LISTEN/NOTIFY`).

- **"How does the schema get built?"**
  `server/__main__.py` → `store.bootstrap()` → `server/sql.py`
  (`schema_migrations()`) → `server/schema_gen.py`
  (`substitute_schema_placeholders()`) → the four `_generate_*`
  functions. Generated text is derived from `ColumnSpec` metadata on the
  dataclasses in `types/`. See `docs/db_schema_migration.md` for running
  migrations, squashing, and deploying schema changes.

- **"How does a `dependency_changed` cascade work?"**
  `store.emit_change` → `store._cascade_dependency_changed` →
  `store._parent_edges` → `types/edges.EdgeKindPolicy`. The policy
  table is the single declaration site for which endpoint of each
  edge kind is the dependent.

## Running

```bash
uv run python -m trackinizer.server                        # pglite (default), with web UI
uv run python -m trackinizer.server --engine pg --dsn ...  # against real Postgres
uv run python -m trackinizer.server --no-web               # API only
```

See `example.sh` for a worked end-to-end submit/edit/query session.

## System dependencies

Integration tests (`@pytest.mark.integration`) provision a real Postgres
via `pytest-postgresql` and require pgvector. PGlite bundles its own
`vector` extension, so the default `pglite` engine has no system deps.

```bash
# Ubuntu/Debian. Extension packages are always named `postgresql-NN-<ext>`
# -- there is no unversioned alias -- so derive NN from the metapackage
# instead of hardcoding it (18 above 24.04, 16 on 24.04).
sudo apt-get install -y postgresql
sudo apt-get install -y "postgresql-$(apt-cache depends postgresql |
    grep -oP 'postgresql-\K\d+' | head -1)-pgvector"

# macOS. Homebrew's pgvector supports postgresql@17 and @18.
brew install postgresql@18 pgvector
```
