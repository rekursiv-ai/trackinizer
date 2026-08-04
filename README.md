# trackinizer🐾

[![PyPI version](https://img.shields.io/pypi/v/trackinizer.svg)](https://pypi.org/project/trackinizer/)
[![CI](https://github.com/rekursiv-ai/trackinizer/actions/workflows/package-validation.yml/badge.svg?branch=main)](https://github.com/rekursiv-ai/trackinizer/actions/workflows/package-validation.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Discord](https://img.shields.io/discord/1530237005311639592?logo=discord&logoColor=white&label=Discord&color=5865F2)](https://discord.gg/2GZFPPvCqn)

Epistemological database for agent and human efforts, beliefs, and findings.

## Quick Start

```bash
# Mac:
#   # Required for quick install.
#   brew install uv
#   # Optional for a real Postgres backend; default uses pglite engine.
#   brew install postgresql@18 pgvector

# Ubuntu/Debian:
#   # Required for quick install.
#   sudo apt-get install -y curl
#   curl -LsSf https://astral.sh/uv/install.sh | sh
#   # Optional for a real Postgres backend; default uses pglite engine.
#   PG_MAJOR="$(apt-cache depends postgresql | grep -m1 -oP 'postgresql-\K\d+')"
#   sudo apt-get install -y postgresql "postgresql-$PG_MAJOR-pgvector"

uv tool install trackinizer

# Local server; web UI at http://127.0.0.1:8765.
trackinizer

# CLI to trackinizer server.
trax
```

Centralized agent database for inquiries (Issues + Artifacts), work, and
knowledge. Three core tables (`inquiries`, `edges`, `change_log`) backed
by Postgres (real or PGlite). FastAPI on top.

`types/` is the design contract. Every other module is a realization of
that contract over Postgres + HTTP.

## The UI

The optional SPA (`server/web.py`) browses the same records the API serves.

**Graph** -- the whole inquiry web (Issues, Beliefs, Papers, Experiments, …) as typed nodes and edges.

<img src="https://raw.githubusercontent.com/rekursiv-ai/trackinizer/main/trackinizer/docs/screenshots/graph.png" width="640" alt="Force-directed graph of the inquiry web">

**Console** -- live multi-agent chat, filterable by room and date.

<img src="https://raw.githubusercontent.com/rekursiv-ai/trackinizer/main/trackinizer/docs/screenshots/chat.png" width="640" alt="Live multi-agent console">

**Belief** -- a record with its `before`/`after` relationship panels.

<img src="https://raw.githubusercontent.com/rekursiv-ai/trackinizer/main/trackinizer/docs/screenshots/belief.png" width="640" alt="Belief record with parent/child edges">

**Paper** -- abstract, authors, and `cites` edges to other papers.

<img src="https://raw.githubusercontent.com/rekursiv-ai/trackinizer/main/trackinizer/docs/screenshots/paper.png" width="640" alt="Paper record with abstract and citations">

**Experiment** -- outcome, labels, and links to the beliefs it proves or disproves.

<img src="https://raw.githubusercontent.com/rekursiv-ai/trackinizer/main/trackinizer/docs/screenshots/experiment.png" width="640" alt="Experiment record with outcome and relationships">

## The model

Everything in the system is an `Inquiry`, which has two variants: an
`Issue` is a unit of "work" and an `Artifact` is the output of that work.
Giving both a single type is what lets the same edges relate them -- work
can produce knowledge, and knowledge can elicit more work, without crossing a
type boundary.

Each row below lists the fields that class adds; every kind also has
everything above it.

```
Inquiry                  # An effort, ongoing or completed.
│   id
│   seq
│   owner
│   account
│   status
│   title
│   description
│   labels
│   marginal_cost
│   subscribers
│   superseded_by
│   supersedes
│   produces
│   produced_by
│   created
│   modified
│
├── Issue                # Work to pursue.
│       issue_kind
│       validation
│       priority
│       narrows
│       narrowed_by
│       requires
│       required_by
│
└── Artifact             # Knowledge produced and cited.
    │   proves
    │   favors
    │
    ├── Experiment       # Empirical measurement.
    │       codechanges
    │       outcome
    │       config
    │       proved_by
    │       favored_by
    │
    ├── Belief           # Proposition.
    │       judgement
    │       confidence
    │       proved_by
    │       favored_by
    │
    ├── Paper            # Bibliographic source.
    │       abstract
    │       authors
    │       publication_type
    │       venue
    │       subvenue
    │       publish_date
    │       source
    │       google_scholar_cluster_id
    │       google_scholar_cites_id
    │       cites
    │       cited_by
    │
    ├── CodeChange       # One git commit.
    │       sha
    │
    ├── WebSearch        # One query.
    │       query
    │       provider
    │
    ├── WebResult        # One URL.
    │       url
    │
    └── AgentSession     # A captured CLI run.
            cli
            cli_session_id
            started
            ended
            rooms
            opened_by_api_key_id
```

Relationships are directed and every one has an inverse view, so a parent
and a child describe the same edge from either end:

```
                              OLDER  (parent)

    {narrows,requires}     {produced_by,supersedes}     {proves,favors}
            ▲                         ▲                        ▲
            │                         │                        │
          Issue ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄▷ Inquiry ◁┄┄┄┄┄┄┄┄┄ {Belief,Experiment}
            │                         │                        │
            ▼                         ▼                        ▼
{narrowed_by,required_by}  {produces,superseded_by}  {proved_by,favored_by}

                               NEWER  (child)
```

- Parents are always older than children, on each edge's own clock:
  creation-time for every edge except `requires`, which is completion-time.
- Any Inquiry can be `produced_by` older ones (its origins) and
  `superseded_by` others (M:N knowledge surgery).
- An Issue can be `narrowed_by` (broader to narrower) or `required_by` (it
  is the prerequisite another waits on). Both are Issue to Issue.
- `proves` / `favors` go from any Artifact to a `Belief` or `Experiment`
  -- anything may cite, only a claim may be cited. They carry a valence in
  [-1, 1]: sign is polarity, magnitude is weight. `proves` votes in the
  proof predicate; `favors` is context that informs but does not vote.
- `cites` / `cited_by` is Paper to Paper and stands apart from the six
  above: a bibliographic fact we record rather than a claim we reason
  over. No valence, no scheduling effect, and exempt from the acyclicity
  rule, since mutual citation is real data and not a cycle we own.

See [`docs/epistemy.md`](trackinizer/docs/epistemy.md) for why each verb
is named what it is.

## Storage

Three tables carry the model and other tables support them.

| table | holds |
|---|---|
| `inquiries` | one row per Inquiry, all kinds. `kind` discriminates; `(kind, seq)` gives the short ref (`Issue#7`) from a per-kind sequence. Optional columns are nullable, so NULL is the single encoding of "unset". |
| `edges` | every relationship, as `(from_id, to_id, edge_kind)`. Citation edges carry `valence`. A CHECK constrains which kind pairs each edge kind admits, and cycles are rejected on insert. |
| `change_log` | append-only audit. Each row is one change with `(old_*, new_*)` snapshot pairs; milestone rows carry no delta, their signal is that they exist. Drives the `trax recent` feed and idempotency replay. |

Per-kind detail that does not fit one row lives in its own table, keyed
back to `inquiries(id)` and deleted with it:

| table | holds |
|---|---|
| `experiment_metrics` | `(key, step, value)` time series for an Experiment. CHECKs mirror the wire's validators -- non-blank bounded key, non-negative step, finite value -- so a stored row can always be read back. |
| `agent_session_events` | the ordered event log of an AgentSession, `(session_id, seq)`, each with a JSONB message. Backs the live console feed. |
| `inquiry_embeddings` | one vector per `(inquiry_id, model)` for semantic search. |

Auth is three more: `users`, `api_keys` (scrypt-hashed, prefix-indexed),
and `allowlist`.

The typed fields on `Inquiry` (`produces`, `supersedes`, citation lists)
are projections the Store fills by reading `edges` -- the edge table is the
real storage.

## Package dependency graph

Four layers, two legs sharing one contract spine. An arrow means
"imports / depends on" and points toward the dependency.

```
┌──────────────┐                      ┌──────────────┐
│    trax      │                      │    server    │   leaves: nothing
│    (CLI)     │                      │  (__main__)  │   imports these
└──────┬───────┘                      └──────┬───────┘
       │                                     │
       ▼                                     ▼
┌──────────────┐                      ┌──────────────┐
│   client     │                      │     api      │   server leg adds
│ (httpx SDK)  │                      │  (FastAPI    │   Store; client leg
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
trackinizer                        # pglite (default), with web UI
trackinizer --engine pg --dsn ...  # against real Postgres
trackinizer --no-web               # API only
```

`trax` is the client half and talks to any reachable server, so it is
useful on its own:

```bash
trax help                          # grammar and subjects
trax issue                         # list issues
```

From a source checkout, both are `uv run python -m trackinizer.server`
and `uv run python -m trackinizer.trax`.

See `example.sh` for a worked end-to-end submit/edit/query session.

## System dependencies

Integration tests (`@pytest.mark.integration`) use a Postgres server backend
via `pytest-postgresql` and also requires `pgvector`.

PGlite bundles its own `vector` extension, so the default `pglite` engine has
no system deps.

### Ubuntu/Debian

```bash
# Extension packages are always named `postgresql-NN-<ext>` -- there is no
# unversioned alias -- so derive NN from the metapackage instead of hardcoding
# it (18 above 24.04, 16 on 24.04).
PG_MAJOR="$(apt-cache depends postgresql | grep -m1 -oP 'postgresql-\K\d+')"
sudo apt-get install -y postgresql "postgresql-$PG_MAJOR-pgvector"
```

### macOS

```bash
# Homebrew's pgvector supports postgresql@17 and @18.
brew install postgresql@18 pgvector
```

## Citing

If you find our work useful, please consider citing:

```bibtex
@misc{rekursivai2026trackinizer,
      title={Trackinizer - Epistemological database for agent and human efforts, beliefs, and findings.}
      author={Joshua V. Dillon and Dan Kondratyuk},
      year={2026},
      howpublished={Github},
      url={https://github.com/rekursiv-ai/trackinizer},
}
```

## License

Apache License 2.0
