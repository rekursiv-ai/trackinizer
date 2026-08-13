# Trackinizer design

Centralized agent database for tracking schedulable work, knowledge
artifacts, citations, successes, failures, and an audit trail.

This is the authoritative design overview. It consolidates and replaces the
earlier per-topic design notes (model, schema generation, auth, and the
knowledge-store philosophy). Where this doc and the code disagree, **the
code wins** -- `types/` is the typed contract and
`assets/schema.sql` (with `server/schema_gen.py`) is the storage
realization. See also `docs/api.md` (the wire contract, plus the naming
rule in section 4.6) and `docs/db_schema_migration.md` (deploying schema
changes).

## Philosophy

Trackinizer is a Baconian, falsification-first knowledge store. The
intellectual core began as a markdown-and-git design ("leaves" in epistemic
directories, maintained by a librarian); trackinizer lifts that same
epistemology onto a typed server -- rows, edges, and an append-only audit
log -- without diluting it. This section states the principles; the rest of
the doc is their mechanical realization.

### Knowledge is falsifiable assertions

The atomic unit of knowledge is a **`Belief`**: a single proposition that
is true only while nothing disproves it. Beliefs are not filed by topic and
are not split preemptively; they are stated, cited, and -- when evidence
demands -- superseded by sharper formulations.

Atomicity is load-bearing: if any part of a proposition is falsified, the
whole proposition is. So a belief should assert exactly one thing. A broad
belief that turns out half-right is not patched in place -- it is superseded
by finer-grained successors (see Supersession).

### Provenance, not a structure of finding

There is no topic ontology. A category tree inevitably misfiles knowledge
and renders it invisible, and no fixed vocabulary fixes discoverability.
Instead, the only "structure" is **epistemic state** -- believed, false,
open, superseded -- which cannot be misfiled because it is a fact about the
status of belief, not a guess about where something belongs.

Discovery therefore comes from two sources, never from location: **content**
(embedding search over title/description) and the **citation/provenance
graph** (which beliefs cite which artifacts and each other). The epistemology
is a principle of provenance, not a scheme of finding.

### Evidence, and what counts as it

Support is modeled as citation edges from an `Artifact` up to the claim
(`Belief` or `Experiment`) it bears on, in two strengths. Polarity is not a
separate edge -- it is the sign of the edge's `valence` (`[-1, 1]`; positive
supports the claim, negative argues against it, magnitude is the evidential
weight, `0` neutral, default `0.5`):

| | for-vs-against |
|---|---|
| **load-bearing** | `proves` (signed valence) |
| **contextual** | `favors` (signed valence) |

`proves` is deliberately hyperbolic. "Credits" would be more precise, but the
strong word keeps *load-bearing* evidence (what the belief stands or falls on)
visibly apart from mere *context* (`favors`) that colors it without deciding it.
A `proves` citation with negative valence is the opposing reading (the trax CLI
spells it `disproves`, an alias that negates the valence); a `favors` citation
with negative valence is `disfavors`. Both store the same two edge kinds.

Evidence must be **retrievable and independently checkable**: the test is
whether someone else could fetch the source and evaluate it for themselves.
An `Artifact` is that retrievable thing -- an experiment, paper, code change,
or web source. A belief may cite another belief, but every evidence chain
must terminate at real artifacts; belief-citing-belief is a shortcut through
the provenance graph, never a substitute for grounding. An argument is not
an observation, and discussion is not evidence.

Experiments are the strongest evidence and the most fragile: a result is a
fact about one configuration (seed, hardware, versions). Generalizing from
it is the knowledge layer's job, not the experiment's.

### Counter-evidence is decisive

A belief is guilty until proven innocent. The moment load-bearing
counter-evidence stands against it (a `proves` citation with negative
valence), the proposition is false; a single such artifact suffices. It is
rehabilitated only if that counter-evidence is itself retracted (the edge
removed or its valence no longer negative). The same source can both support
and oppose a belief under different interpretations -- the opposing
interpretation is still damning.

Contextual material that *looks* like it should contradict a belief but
cannot rise to load-bearing (a `favors` citation with negative valence) does
not kill it. A belief that survives despite heavy negative-valence `favors`
is the most interesting kind: knowledge that *shouldn't* hold but does.

### The system never forms the verdict

Citations *inform* a belief's `judgement` (`proven` / `disproven` /
`unproven` / `undecidable`) and `confidence`, but never set them -- only an
author does. This mirrors the agent/maintainer split from the original
design: agents record what they observed (submit beliefs, attach evidence);
the author forms the verdict; automated machinery only raises *re-assessment
alerts* (`dependency_changed` cascading along edges), never a silent
truth-change. When a cited artifact moves, every belief leaning on it is
flagged for its owner to revisit -- the system surfaces the need to think,
it does not think for you.

### Supersession is non-lossy and orthogonal

Better formulations absorb older ones through `supersedes` edges. A
superseded belief's knowledge is fully preserved in its successor, so
supersession is terminal and lossless -- counter-evidence arising later
applies to the successor, not the retired original. Supersession is
independent of both truth (`judgement`) and framing (`status`): a `proven`
belief can be superseded by a sharper one, and an `invalid` (retracted
framing) belief can still read `proven`.

### Everything is provenance

The append-only `change_log` is the audit trail -- every submit, edit, edge,
and cost adjustment, with old/new snapshots. `purged` rows leave tombstones.
Nothing is quietly forgotten; the history of belief is itself first-class.

`change_log` audits **knowledge mutations** only. Bulk capture logs are an
adjacent universe: `agent_session_events` (agent-session turns; see
`docs/api_agent_session_events.md`) is append-only and deliberately *outside*
`change_log`, because a captured turn is not a change to what the org
believes -- forcing it in would bury the high-signal audit feed under
ingest volume. Its provenance is intrinsic: the immutable rows plus the
owning `Session` artifact's own lifecycle (a real Inquiry, whose edits
*do* flow through `change_log`). The rule: knowledge mutations are
audited; an append-only capture log records itself.

### What this substrate enables next

Trackinizer is deliberately the foundation, not the ceiling. The typed
beliefs, the citation/provenance graph, and the append-only `change_log`
are exactly the substrate the following build on -- each is future work the
design is meant to make tractable, not something it forecloses:

- **Evidence-strength spectrum** -- ranking sources experiment > paper >
  preprint > blog, refining today's flat load-bearing/contextual split. The
  edge already exists to carry the grade. One way to set that grade
  *decentrally* is crowd voting on an artifact's ground-truthiness (see
  `docs/vision.md`): agents vote, the verdict establishes how trustworthy
  the artifact is, and -- because the vote grades the *artifact*, not a
  belief's `judgement` -- it changes belief verdicts only by flowing through
  the `proves` citation edges, the same path any artifact change takes.
  This keeps the rule that only an author (here, the crowd acting on
  evidence) sets a belief's verdict, and the cascade still flags every
  dependent belief to revisit.
- **Autonomous consolidation** (a "librarian" layer) -- a process reading
  the full belief set to detect contradictions, merge redundant beliefs,
  generalize repeated observations, and reduce over-broad beliefs into finer
  successors. Today authors do this by hand and the cascade only flags;
  trackinizer's graph + audit log are what such a layer would read and
  write. It stays a layer *above* the store, so the store itself keeps its
  rule that only authors form verdicts.
- **Emergent authority** -- ranking a belief by how much the citation graph
  leans on it (PageRank-style), computed from the edges rather than any
  explicit importance field.
- **Surprise-as-signal** -- surfacing the belief that survives amid heavy
  negative-valence `favors` as the store's most interesting knowledge, read
  straight off the edge graph.

## Model

Every domain noun is an `Inquiry`. Two branches:

- `Issue` -- schedulable work or desired outcomes. Carries
  `issue_kind: tuple["feature" | "bug" | "task" | "question", ...]`,
  `validation`, integer `priority`, decomposition edges, prerequisite
  (`requires`) edges, and produced artifacts.
- `Artifact` -- knowledge produced and cited: generic `Artifact`,
  `Experiment`, `Paper`, `Belief`, `CodeChange`, `WebResult`,
  `WebSearch`.

Every edge is stored child -> parent: `from` is the younger/dependent
vertex, `to` is its older parent. There are exactly seven edge kinds.

```text
              OLDER (parent)

  {narrow,require}s  {supersedes,produced_by}  {prove,favor}s   cites_paper
         ▲                     ▲                      ▲              ▲
         │                     │                      │              │
       Issue                Inquiry         {Belief,Experiment}    Paper
         │                     │                      │              │
         └─ from = child ──────┴──────────────────────┴──────────────┘

              NEWER (child)
```

| forward (child's view) | stored `from` -> `to` |
|---|---|
| `narrows` | narrower Issue -> broader Issue |
| `requires` | requirer Issue -> prerequisite Issue |
| `produced_by` | produced Inquiry -> producer Inquiry |
| `supersedes` | successor Inquiry -> predecessor Inquiry |
| `proves` | citing Artifact -> cited {Belief, Experiment} |
| `favors` | citing Artifact -> cited {Belief, Experiment} |
| `cites_paper` | citing Paper -> cited Paper |

Issues decompose only into Issues. Produced Artifacts are provenance, not
children in an ownership tree. Artifacts record outputs and evidence; they
carry no priority, validation, decomposition, or prerequisites.

## States

`Inquiry.Status` is the row lifecycle for every kind:

| value | meaning |
|---|---|
| `active` | live and relevant |
| `complete` | concluded or finished |
| `abandoned` | author stopped pursuing it |
| `invalid` | retracted, no longer applicable, or framed incorrectly |

Supersession is not a status -- it is `supersedes` edges with
`supersedes` / `superseded_by` projections.

`Belief.Judgement` is orthogonal to status (`proven`, `disproven`,
`unproven`, `undecidable`). A Belief can be `status="invalid"` and
`judgement="proven"`: the proposition may be true while the row framing was
retracted.

## Tables

Three core tables plus auth and embeddings. The Python dataclass hierarchy
is the typed API; the tables are the storage layer.

### `inquiries`

One row per `Inquiry`; `kind` discriminates the dataclass. Per-kind columns
are gated by `CASE WHEN kind=...` CHECKs, and per-kind sequences populate
`seq` for short-refs (`Issue#7`, `Belief#3`).

Ordered embedded lists live as row-level arrays here, not in `edges`:

- `subscribers TEXT[]` -- agent ids notified on changes.
- `experiment_codechanges UUID[]` -- chronological `CodeChange` ids on
  `Experiment`.
- `websearch_results JSONB` -- ordered `(id, kind)` pairs on `WebSearch`
  (`kind` is `WebResult` or `Paper`).

The rule: edges for unordered graph relationships; embedded arrays for
ordered, append-mostly lists where rank matters on every read.

**Column naming.** A base field (applies to every kind) is bare
(`status`, `owner`). A kind-specific column carries its owning kind as a
prefix in storage (`Paper.source` -> column `paper_source`), so the flat
shared table is self-identifying. The Python attribute, CLI token, and
kind-scoped HTTP body stay bare; the prefix appears only on the flattened
surfaces (SQL column, `change_log` mirror, `Change.Kind`). `Cost`
flattens to `marginal_cost_agent_usd` / `marginal_cost_resource_usd`. The
full rule, with a worked example, is in `docs/api.md` section 4.6.

### `edges`

Normalized Inquiry -> Inquiry graph; the SQL truth-source for all unordered
relationships.

Every edge is stored child -> parent (`from` younger/dependent, `to` older
parent). Seven kinds, each named from the child's view:

| `edge_kind` | `from` -> `to` | projection |
|---|---|---|
| `narrows` | narrower Issue -> broader Issue | `Issue.narrows` / `Issue.narrowed_by` |
| `requires` | requirer Issue -> prerequisite Issue | `Issue.requires` / `Issue.required_by` |
| `produced_by` | produced Inquiry -> producer Inquiry | `Inquiry.produced_by` / `Inquiry.produces` |
| `supersedes` | successor Inquiry -> predecessor Inquiry | `Inquiry.supersedes` / `Inquiry.superseded_by` |
| `proves` | citing Artifact -> cited {Belief, Experiment} | `Artifact.proves` / `{Belief,Experiment}.proved_by` |
| `favors` | citing Artifact -> cited {Belief, Experiment} | `Artifact.favors` / `{Belief,Experiment}.favored_by` |
| `cites_paper` | citing Paper -> cited Paper | `Paper.cites` / `Paper.cited_by` |

`A requires B` means B must be done first, so B is do-time older; every other
edge orders by creation-time. For-vs-against is not a separate edge: it is the
sign of the citation edge's signed `valence` (`[-1, 1]`; positive supports,
negative argues against, magnitude is weight). `proves` is load-bearing (votes
in the proof predicate); `favors` is context.

`cites_paper` is the odd kind out, and deliberately so: it is a HISTORICAL,
bibliographic citation between two external sources, not our epistemic
judgement. It therefore carries no `valence`, infers no provenance (it is
absent from both `PRODUCED_INFERENCE_PRECEDENCE` and
`PRODUCED_INFERENCE_SUPPRESSED`, so it neither stamps a `produced_by` nor
vetoes one a coexisting edge would stamp), and is exempt from the acyclicity
bar -- two real papers can legitimately cite each other, so a cycle there is
data, not an error.

Issue->Issue edges may carry a contextual `priority`; root priority stays on
`Issue.priority`. The four edge-metadata columns are `priority`, `note`,
`valence`, `labels`. Citation kinds take active voice (the citing artifact is
the unstated subject); the claim's fields take passive voice (`proved_by`).
Edges are acyclic across the whole graph except `cites_paper`, which keeps the
re-assessment cascade finite (the exempt kind is inert: it drives no cascade).

### `change_log`

Append-only audit. A closed-set `Change.Kind` gates which `old_*`/`new_*`
snapshot columns may be populated; generated CHECKs enforce the
kind-to-column matrix. `subject_id` is FK-free so a `purged` tombstone
outlives its target. Per-event USD spend is `new.marginal_cost -
old.marginal_cost`.

**Change kinds = field names.** A field edit's kind is the field's flat
storage name: bare for base fields (`status`, `title`, `owner`,
`labels`, `subscribers`, `marginal_cost`), `<kind>_<field>` for
kind-specific ones (`belief_judgement`, `paper_source`,
`experiment_codechanges`, ...). Non-field events keep verbs: `created`,
`purged`, `edge_added`, `edge_removed`, `edge_annotation_changed`,
`dependency_changed`, `implicit_subs_opened`/`closed`. There is no separate
`change_kind` metadata -- it is derived from the storage name.

`change_log` also splits **principal** (`api_key_id`, server-stamped) from
**actor** (`actor TEXT`, free-form provenance); see Auth.

## Schema generation

`ColumnSpec` metadata on dataclass fields is the single source. Each
editable `Inquiry`/`Edge` field carries
`field(metadata=ColumnSpec(...))`; `ColumnSpec` is a `UserDict` holding
`{"colspec": self}`, so it doubles as the `metadata` mapping. Three derived
views (`types/columns.py`):

- `column_specs(cls)` -- keyed by bare field name; the wire/route view.
- `storage_column_specs(cls)` / `COLUMN_SPECS` -- keyed by flat storage
  name; the codegen + setter-dispatch view.
- `flat_column_specs(cls)` -- expands `flatten` composites into scalar axes.

`storage_name(field, spec)` encodes the naming rule above. `schema.sql`
ships `{placeholders}` that `substitute_schema_placeholders()` fills at
`Store.bootstrap`:

- `{per_kind_sequences}`, `{inquiry_kind_columns}`
- `{change_log_mirror}` (the `old_X`/`new_X` columns + populated-iff CHECKs)
- `{change_log_kind_matrix}` (kind -> allowed subject_kind)
- `{edge_metadata_columns}` + old/new mirrors
- literal-set lists `{edge_kinds}`, `{inquiry_kinds}`, `{artifact_kinds}`,
  `{change_kinds}` from the PEP-695 `Literal` aliases.

Migrations: `schema_migrations()` yields the `schema.sql` baseline, followed
by any numbered `schema.NNN.sql` files an evolving deployment accumulates;
`Store.bootstrap` applies them under an advisory lock and records each in
`applied_migrations`. The schema currently ships as a single clean baseline
(no numbered files). Deploying schema changes is `docs/db_schema_migration.md`.

## Wire

HTTP + JSON for sync calls, SSE for live subscriptions. FastAPI server;
clients use any HTTP library. The full contract is `docs/api.md`; the
shapes below are the spine.

### Submit

```text
POST /api/inquiries/<kind>     # kind token = kind.lower(): issue, paper,
                               # belief, experiment, codechange, webresult,
                               # websearch, artifact
POST /api/inquiries/batch
```

Every body carries the Inquiry-base fields (`title`, `description`,
`owner`, `labels`, `subscribers`, `marginal_cost`) plus its kind-specific
fields, with relationships submitted through the fields that own them
(Issue `narrows`/`requires`, Belief citations, Experiment
`codechanges`, WebSearch `results`). Each submit validates, inserts one
inquiry row, inserts relationship edges, emits `created`, and notifies
subscribers in one transaction. Clients mint an idempotency key per submit;
the server mints row ids (`docs/design_idempotency.md`).

### Edit (one route per field)

```text
PUT|DELETE /api/inquiries/<id>/<field>      # base fields + cost axes
PUT|DELETE /api/<kind>/<id>/<field>         # kind-specific fields
PATCH      .../<list-or-cost field>         # add/sub
```

Kind-specific field routes are scoped under their owning kind
(`PUT /api/paper/<id>/source`), mirroring the Python `paper.source` and CLI
`trax paper`. Base fields and cost axes stay under `/api/inquiries`. `status`
`owner`, `status`, and `judgement` accept an `expected` compare-and-set guard.
Field edits are
author-owned; the system never auto-mutates them.

### Edges, purge, reads

```text
POST|DELETE /api/edges/<from>/<edge_kind>/<to>
PUT|PATCH|DELETE /api/edges/<from>/<edge_kind>/<to>/<annotation>
DELETE /api/inquiries/<id>                  # purge; leaves a tombstone

GET /api/inquiries/<id>                      # one row + projections
GET /api/inquiries/<kind>/<seq>              # short-ref
GET /api/inquiries?kind=&filter=&limit=...   # list + filters
GET /api/inquiries/next_issue                # oldest unblocked active Issue
GET /api/inquiries/<id>/cost?deep=           # cost; deep rolls up subtree
GET /api/inquiries/<id>/proves_belief
GET /api/change_log[?...]  /api/change_log/<id>  /api/change_log/stream
GET /api/web/{search,recent_changes,lookup/<id>,get/<id>}   # SPA read API
```

## Cascade

Trackinizer's automated cascade lives in `Store`. It never mutates
author-owned fields -- it emits re-assessment alerts.

1. When an inquiry changes, Store finds every parent with an edge to that
   child and emits `dependency_changed` on the parent.
2. The alert recurses upward through the edge graph.
3. A visited set plus write-time acyclicity keep the cascade finite.

Refutation is not a separate edge: a contradicting result is a `proves`
citation with negative `valence`, which flags every dependent claim through
the same cascade but never auto-flips it -- an author reviews and sets
`invalid` if convinced. The scheduler (`next_issue`) skips active Issues with
unmet (non-terminal) `requires` prerequisites.

SSE (`/api/change_log/stream`, `/api/web/subscribe`) streams mutated-inquiry
ids over Postgres `LISTEN/NOTIFY` (or PGlite's in-process bus). Offline
agents catch up via the `change_log` filtered by `subscribers_snapshot`.

## Auth

Trackinizer is its own identity system: Google OAuth for browsers, per-user
API keys for CLI/agents, role-based authz. Three tables:

- `users` -- `email`, `name`, `role ∈ {viewer,writer,admin}`,
  `status ∈ {active,disabled}`.
- `api_keys` -- `user_id` (FK CASCADE), scrypt `secret_hash`, `prefix`, a
  per-key `role` ceiling, `last_used_at`, `revoked_at`.
- `allowlist` -- `email_or_pattern` (literal or `*@domain`) -> granted role.

`current_user` resolves one principal per request: bearer header first
(prefix lookup + scrypt verify, skipping disabled/revoked), then a signed
`trackinizer_session` cookie, else 401. `require_role(min_role)` gates each
route: reads need `viewer`, mutations `writer`, user/allowlist management
`admin`. A key's effective role is `min(user_role, key_role)`; a key may not
exceed its user's role.

Google OAuth: `/auth/login` -> consent with a signed state cookie;
`/auth/callback` verifies state, checks the allowlist (403 off-list),
upserts the user, and sets a 30-day `HttpOnly`/`Secure` cookie. Re-login
keeps the existing role -- no silent elevation.

Bootstrap: when `users` is empty and `TRACKINIZER_BOOTSTRAP_ADMIN` is set,
the first boot seeds an allowlist row, an admin user, and an api_key, and
writes the secret once to a mode-0600 `bootstrap_token` file. Idempotent.

`--no-auth` (`TRACKINIZER_NO_AUTH=1`) short-circuits to a synthetic admin
for local demos only -- never in production.

## Storage

Single store: Postgres + pgvector, with PGlite as the default in-process
substrate for prototyping. Same SQL and schema; only DSN acquisition and
pub/sub differ.

- `vector(384)` embeddings in `inquiry_embeddings` keyed by
  `(inquiry_id, model)`, so multiple embedders coexist per inquiry.
- `LISTEN/NOTIFY` for subscription fan-out.
- Recursive CTEs for decomposition rollups and blocker checks.
- No separate vector DB, search engine, or graph DB.

## Module layout

```text
types/    Inquiry hierarchy, Edge, Change/Snapshot, Cost, ColumnSpec.
wire/     HTTP contract: Submit/Field bodies, the route table, filters.
client/   httpx SDK over the wire layer.
trax/     the CLI (verbs, grammar, parser, render).
server/   FastAPI app, Store, auth, schema generation, SQL assets.
```

Dependency direction: `types/` -> `wire/` -> `{client, trax, server}`.
`client`/`trax` import only `types` + `wire` (enforced by
`import_purity_test`); the server owns the storage realization.

## Reading the code

1. `types/inquiries.py` -- the typed contract and closed-set discriminators.
2. `types/columns.py` + `server/schema_gen.py` -- how metadata becomes SQL.
3. `server/assets/schema.sql` -- the storage realization and CHECKs.
4. `server/store.py` -- mutations, change emission, the cascade.
5. `wire/routes.py` -- the derived route table the server and client share.
