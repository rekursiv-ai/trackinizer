---
name: trackinizer-db-migration
description: >
  Use when changing trackinizer's storage schema on the live deployment --
  adding/widening a constraint, an Inquiry kind, a column, or any DB shape
  change that an already-deployed database must be migrated to. Covers
  numbered migrations, the mandatory local catalog-diff gate, backups, the
  colossus deploy split, and squashing. Do not use for ordinary trax CLI
  usage (see the trax skill) or non-trackinizer databases.
---

# Database schema migration

How to evolve Trackinizer's storage schema on the live deployment: run a
numbered migration, squash consumed migrations back to a clean baseline,
identify which engine the live service uses, and work with `loop/ops`.

The goal of any schema change is never "the migration ran." It is:

- `assets/schema.sql` describes the fresh-database schema;
- each existing database is migrated to that same shape;
- a fresh-vs-live catalog diff is empty afterward.

Service start, a passing single command, or a row in `applied_migrations`
are smoke signals only -- not proof.

## Who does what

Two operators, one source of truth.

- **Josh (human)** owns all git mutation. Commits, pushes, and pulls are
  his. The agent never commits or pushes anything.
- **The localhost source checkout is the source of truth.** If the agent
  needs code changed, it edits on localhost and asks Josh to commit +
  push. The agent does not edit code on colossus.
- **The agent** owns `ssh colossus` operations: pulling deployed code,
  running migration scripts, taking backups, and database surgery -- each
  mutation with Josh's express consent.

### One-off scripts

Migration/verification scripts are not committed. The agent writes them
on localhost and `rsync`/`scp`s them to a scratch path on the target:

```bash
scp /tmp/my_migration.py colossus:/tmp/my_migration.py
ssh colossus '... run /tmp/my_migration.py ...'
```

### `/srv/rekursiv-ai/loop` is read-only

The deployed checkout is read-only to the agent, with exactly two
git-mutation exceptions Josh consents to (and which the agent may run
without re-asking):

```bash
ssh colossus 'cd /srv/rekursiv-ai/loop          && git pull'
ssh colossus 'cd /srv/rekursiv-ai/trackinizer   && git fetch && git checkout origin/main'
```

The second is the per-service worktree (pinned separately from the
canonical checkout). No other git mutation on colossus is permitted:
no commit, branch, reset, stash, merge, rebase, or push. Deployed code
moves only by Josh pushing to `origin/main` then the agent pulling /
checking out the above.

## Identify the live engine first

The procedure differs by engine, so always determine it before acting.
The source of truth is the deploy config and `loop/ops`, never memory.

```bash
ssh colossus 'grep -E "^TRACKINIZER_(ENGINE|DATADIR|DSN)" \
  /srv/rekursiv-ai/etc/trackinizer.deploy.env'
```

- `TRACKINIZER_ENGINE=pglite` -> single-process PGLite; data lives in the
  `TRACKINIZER_DATADIR` directory (default
  `$TRACKINIZER_DATA_DIR/pgdata`).
- `TRACKINIZER_ENGINE=pg` -> real Postgres; data lives in the database at
  `TRACKINIZER_DSN` (read from the 0640 secrets file
  `/srv/rekursiv-ai/etc/trackinizer.env`, not the deploy.env).

`loop/ops/colossus/setup-trackinizer.sh` provisions the unit and writes
the deploy config; `redeploy` reads the same `TRACKINIZER_ENGINE` to pick
its backup path. When in doubt, read those two scripts -- they are the
authority on how the service is wired.

> Do not trust the `--datadir` flag in the systemd `ExecStart`: it is
> always present but ignored when `--engine pg`. The engine var decides.

## Backups

Everything lands under `/srv/rekursiv-ai/backups/trackinizer/<stamp>/`.
Always back up before any DB mutation. The shape depends on the engine:

- **PGLite:** stop the service (PGLite is single-process), `tar` the
  datadir, restart.

  ```bash
  ssh colossus 'set -euo pipefail
  stamp=$(date +%Y%m%d-%H%M%S)
  d=/srv/rekursiv-ai/backups/trackinizer/$stamp; mkdir -p "$d"
  systemctl stop trackinizer.service
  tar -C "$TRACKINIZER_DATA_DIR" -czf "$d/pglite-pgdata.tar.gz" pgdata
  systemctl start trackinizer.service
  echo "backup=$d/pglite-pgdata.tar.gz"'
  ```

- **Postgres:** `pg_dump` while the service runs (consistent snapshot, no
  stop needed).

  ```bash
  ssh colossus 'set -euo pipefail
  stamp=$(date +%Y%m%d-%H%M%S)
  d=/srv/rekursiv-ai/backups/trackinizer/$stamp; mkdir -p "$d"
  pg_dump --no-owner --no-privileges trackinizer > "$d/trackinizer-pg.sql"
  gzip "$d/trackinizer-pg.sql"
  echo "backup=$d/trackinizer-pg.sql.gz"'
  ```

`redeploy trackinizer` also takes an engine-appropriate backup
automatically before changing code (PGLite tar / `pg_dump --format=custom`).

## Safety and authority

Any colossus mutation -- service stop/start, file writes, database
writes, backup creation, datadir swaps -- requires Josh's express
consent, scoped narrowly to the stated operation. Read-only inspection
(`status`, `psql -c "SELECT ..."`, `git status`, catalog queries) is
always fine.

---

## Roadmap A: run a numbered migration

> **Diff before you deploy.** The catalog diff is a free, offline check
> that needs no server. Run it *locally* against an old-shape DB and make
> it empty **before** the first push (step 4.5). Deploying first and
> finding drift via smoke-test failures means one redeploy + ledger-reset
> per missed object -- the single biggest source of wasted round-trips.
> One missed object is a symptom: the same root change (e.g. a new kind)
> usually fans out across many objects. Find them all at once with the
> diff; never patch one error, redeploy, and hope.

Use when changing storage for an existing deployed database that should
keep its history. A numbered migration is complete iff:

- `assets/schema.sql` is the desired now-state for fresh databases;
- `assets/schema.NNN.sql` moves the previously deployed schema to it;
- it was tested from an *old* DB copy, not only a fresh one;
- a catalog diff between migrated and fresh `schema.sql` is empty
  **locally, before any deploy** (step 4.5);
- live `applied_migrations` contains `schema.NNN.sql` after deploy;
- app smoke checks pass.

Allowed catalog-diff exceptions: empty-DB row-count differences; sequence
current values differing in either direction (a value *behind* the data
is reconciled to `MAX(seq)` on the next `bootstrap`); internal PG/PGLite
OIDs, relfilenodes, stats, physical storage metadata. Everything else --
tables, columns,
nullability, defaults, constraints, indexes, FKs, sequence definitions --
must match unless Josh explicitly waives the drift.

Steps (Josh commits/pushes the code parts; agent runs colossus parts):

1. **Update the canonical source** (localhost). Usually this means editing
   the metadata that feeds generated placeholders in `assets/schema.sql`,
   not hand-editing generated SQL. Verify the *expanded* schema.
2. **Add the next numbered migration** `assets/schema.NNN.sql` that moves
   the previous deployed schema to the current `schema.sql`. Do not write
   one that only fixes the first observed error unless the catalog diff
   proves that is the only drift.
3. **Update tests** that enumerate migrations (`schema_gen_test.py`,
   `store_test.py`) to expect the new file and prove bootstrap records it.
4. **Validate locally** (localhost):
   ```bash
   uv --quiet run --frozen pytest \
     trackinizer/server/schema_gen_test.py \
     trackinizer/server/store_test.py -q
   uv --quiet run --frozen basedpyright --threads=6 \
     trackinizer/server/schema_gen_test.py \
     trackinizer/server/store_test.py
   ```
4.5. **Prove the catalog diff empty locally, BEFORE pushing.** This is the
   gate that prevents the deploy/smoke-fail/patch loop. Write (or extend) a
   migration test that:
   - bootstraps a fresh PGlite DB, then mutates it into the *old* shape --
     drop every object the migration adds (tables, columns, sequences) and
     revert every constraint the change widens (see the blast-radius
     checklist below), and clear the numbered rows from
     `applied_migrations`;
   - re-runs `bootstrap` (which applies the migration);
   - asserts **full parity** against a second, fresh-bootstrapped store --
     not just the one object you noticed. Compare *sets* of: table names,
     constraint definitions (`pg_get_constraintdef`), sequence names, and
     indexes. (The AgentSession migration that first exercised this pattern
     has since been squashed; recover its test from git history if you want
     a worked example.)

   Use **set comparison**, never line `diff`: constraint defs span multiple
   lines and sort to different positions, so `diff` reports spurious drift
   on identical sets. Compare `set(fresh) == set(live)` (or `comm` on
   `sort -u`ed files).

   If this test is green, the live diff after deploy will be empty too. If
   you cannot make it green locally, the migration is incomplete -- do not
   push.
5. **Josh pushes; agent deploys** (`redeploy trackinizer`, which backs up
   first and lets startup run bootstrap/migrations). If the migration's
   ledger row already exists from a prior incomplete attempt, bootstrap
   skips it; clear it first so the corrected migration re-runs:
   `DELETE FROM applied_migrations WHERE name = 'schema.NNN.sql'` (a live
   DB write -- get consent), then restart.
6. **Run the fresh-vs-live catalog diff** (below) -- the same set
   comparison as 4.5, now against live. Any drift = incomplete.
7. **Smoke-check** the behavior that motivated the migration, end to end.

### Blast radius: adding an Inquiry kind

A new `Inquiry` kind is not one column. The kind name and its per-kind
columns are woven through generated DDL in many places, and `schema.sql`
builds them all into fresh databases -- so an existing DB needs the
migration to add **every** one. Enumerate these before writing the
migration; the step-4.5 diff confirms you missed none:

- **Per-kind columns** on `inquiries` (one per `ColumnSpec` field) plus
  their `CASE WHEN kind = '<Kind>'` CHECK constraints.
- **Per-kind ref sequence** `seq_<kind>` (e.g. `seq_agentsession`); ref
  minting calls `nextval` on it.
- **Any kind-specific table** outside `inquiries` (e.g.
  `agent_session_events`) plus its indexes and FKs.
- **`inquiries.inquiries_kind_check`** -- the kind enum; the insert's
  `kind` value must be admitted.
- **`change_log` kind enums** -- all of: `change_log_kind_check` (gains the
  new per-kind *field-change* kinds, e.g. `agentsession_*`),
  `change_log_subject_kind_check`, `change_log_old_peer_kind_check`,
  `change_log_new_peer_kind_check`. Every write emits an audit row that
  these gate.
- **`edges`** edge-validity CHECK -- the from/to-kind pair enumeration, if
  the new kind can be an edge endpoint.

Reads never project these, so a read smoke-test passes while every *write*
500s/409s. Always smoke-test a **write** (and for kinds with a child table,
a child-table write too).

### When `schema.sql` alone cannot move existing data

`schema.sql` only builds *fresh* databases; it never rewrites an existing
one. When existing data violates a stricter new constraint, or a rename
must rewrite stored values (column renames, `change_log.kind` value
rewrites), prefer an **offline rebuild + swap** over fragile in-place DDL.
This is the same machinery as a squash, applied to migrate data forward:

- **Postgres:** bootstrap a sibling DB from current `schema.sql`, copy
  rows in with the transform applied (drop the `change_log_caused_by_fkey`
  self-FK for the bulk load, re-add after), verify the catalog diff, then
  swap with `ALTER DATABASE ... RENAME` (service stopped, connections
  terminated). Keep the old DB as `trackinizer_old` until satisfied.
- **PGLite:** dump live table data, bootstrap a fresh datadir from
  `schema.sql`, load the transformed data, verify, swap datadirs (keep the
  old datadir at a named path).

Self-referential audit rows (`change_log.caused_by`) require either load
ordering or dropping/deferring the self-FK during the bulk load.

A bulk load that writes literal `seq` values leaves the freshly created
per-kind sequences at their start, so the next `nextval` would re-mint a
live ref. You do **not** need to replay `setval` after the load:
`Store.bootstrap` runs `_reconcile_sequences` on every start, advancing
each sequence to `MAX(seq)` (monotonic, idempotent, skips empty kinds).
The next service start self-heals the counters.

Likewise, a load that copies `inquiries` but not `inquiry_embeddings`
leaves semantic search blind to those rows. You do not need to re-embed
manually: `bootstrap` also runs `_backfill_embeddings`, which embeds any
inquiry missing a row for a registered embedder. This is safe at boot
only because the production embedder is the deterministic hash stub; a
future network/model embedder must move backfill off the startup path.

---

## Roadmap B: squash consumed migrations

Use when the only deployed database has already consumed every numbered
migration and the repo should return to a clean baseline:

- `assets/schema.sql` is the only schema file in the repo;
- consumed `assets/schema.NNN.sql` files are removed;
- the deployed schema equals a fresh DB from `schema.sql`;
- the deployed `applied_migrations` contains only `schema.sql`.

Do **not** squash if more than one database might still need the numbered
migrations. If a second DB may exist anywhere, keep migrations forever.

### Preconditions

1. **Prove there is only one deployed DB.** If a second may exist, stop.
2. **Verify the live DB consumed the numbered migrations:**
   ```bash
   ssh colossus 'psql -d trackinizer -tAc \
     "SELECT name FROM applied_migrations ORDER BY name"'
   ```
   (PGLite: run the equivalent through the app engine against the datadir.)
3. **Run a fresh-vs-live catalog diff first.** If non-empty, finish the
   migration journey before squashing.

### Local cleanup (Josh commits/pushes)

1. Keep the durable shape in `assets/schema.sql` (the raw file may not
   change when placeholders derive from Python metadata; verify the
   expanded schema).
2. Delete consumed numbered files: `rm assets/schema.001.sql ...`.
3. Update tests so the baseline is explicit:
   ```python
   names = [name for name, _body in schema_migrations()]
   assert names == ["schema.sql"]
   ```
4. Validate locally (same commands as Roadmap A step 4).

### Deploy + remote ledger reset (agent, with consent)

Reset the ledger only *after* the deployed code no longer contains
numbered files -- otherwise startup re-records them. Confirm the deployed
asset set first:

```bash
ssh colossus 'cd /srv/rekursiv-ai/trackinizer && git rev-parse --short HEAD && \
  find trackinizer/server/assets -maxdepth 1 \
    -name "schema*.sql" -printf "%f\n" | sort'
# expect: schema.sql
```

Then, backup first (engine-appropriate), and delete only historical rows,
keeping `schema.sql`:

```sql
DELETE FROM applied_migrations WHERE name <> 'schema.sql';
```

---

## Required fresh-vs-target catalog diff

Run it three times: locally against an old-shape DB **before pushing**
(step 4.5, the gate that saves round-trips), against the rebuilt/migrated
DB before any swap, and against live after deploy. It snapshots tables,
columns, constraints, indexes, and sequences and compares to a throwaway
DB bootstrapped from current code.

**Compare sets, not lines.** A constraint definition spans multiple lines
and sorts to a different position depending on its neighbours, so a naive
`diff` of two `sort`ed listings reports spurious drift on *identical*
sets. Normalize each constraint to one line
(`regexp_replace(pg_get_constraintdef(con.oid), '\s+', ' ', 'g')`), then
compare as sets -- `set(fresh) == set(target)` in Python, or
`comm -3 <(sort -u fresh) <(sort -u target)` returning empty. Treat any
asymmetric member as real drift; treat pure reordering as noise.

- **Postgres:** create a scratch `trackinizer_fresh` (owner `jvdillon`,
  `CREATE EXTENSION vector`), bootstrap it via the app, then set-compare
  the normalized `pg_get_constraintdef(...)`, `information_schema.columns`,
  `pg_tables`, `pg_class WHERE relkind='S'`, and `pg_indexes` listings
  between the target DB and `trackinizer_fresh`. Drop the scratch after.
- **PGLite:** bootstrap a fresh datadir in a tempdir and compare the same
  catalog sections against the target datadir.

Expected:

```text
tables: OK
columns: OK
constraints: OK
indexes: OK
sequences: OK
```

Any asymmetric member means the work is not complete. Do not explain it
away unless Josh explicitly waives that exact drift.

## Live deployment checklist

After Josh pushes and the agent deploys (`redeploy trackinizer` or the
manual worktree checkout):

1. Confirm deployed SHA + asset set (read-only `ssh`).
1.5. Confirm the **running** SHA via the live endpoint (below). A redeploy
   that pulled code but did not restart the service leaves an old binary
   serving new code on disk -- the checkout SHA and the running SHA then
   disagree, and the catalog diff alone will not catch it.
2. Confirm the backup landed under `/srv/rekursiv-ai/backups/trackinizer/`.
3. Let startup run bootstrap/migrations.
4. Run the fresh-vs-live catalog diff.
5. Smoke-check the motivating behavior (a real read path and a write).
6. Report: backup path, `applied_migrations` rows, the five `OK` diff
   lines, the running-SHA check, and the smoke command + result.

## Detect a stale running binary (`/api/version`)

A schema/catalog diff proves the *database* is migrated; it says nothing
about whether the *process* serving requests is the current build. A pull
without a restart, a crashed-then-old-respawned unit, or a worker that
never reloaded all leave new code on disk and an old binary in memory.
The symptom is subtle: requests succeed but behave like the old version
(e.g. a newly added `seq_range` param silently ignored, returning
out-of-window rows). Diagnosing that from responses alone wastes a round
trip; ask the server its build instead.

`GET /api/version` is unauthenticated and store-free, returning
`{"sha": "<hex>"}`. The SHA resolves, first hit wins:

1. `$TRACKINIZER_SHA` -- inject it at deploy so the value is authoritative
   even when `.git` is absent. Set it in the systemd unit's environment
   (e.g. `Environment=TRACKINIZER_SHA=%i` wired from the deploy script's
   `git rev-parse HEAD`) so each restart stamps the build it launched.
2. `git rev-parse HEAD` in the server's checkout -- the fallback when the
   env var is unset (local/dev runs).
3. `"unknown"` -- neither resolvable; the route still answers 200.

Check it after every deploy:

```bash
# Running build the live service reports:
curl -fsS https://trackinizer.example/api/version            # {"sha":"<hex>"}
# Or, equivalently, through the CLI:
trax version

# Expected build (what Josh pushed / the checkout is pinned to):
ssh colossus 'cd /srv/rekursiv-ai/trackinizer && git rev-parse HEAD'
```

The two must match. If `/api/version` 404s, the running binary predates
this endpoint -- itself proof the service is stale and must be restarted.
If they differ, restart the unit (with consent) and re-check before
trusting any behavioral smoke test.

## agent_session_events: Timescale hypertable (deploy-time, optional)

`agent_session_events` ships as a **plain Postgres table** in `assets/schema.sql`
(Phase 0). It is bit-identical for correctness without Timescale; the
hypertable is pure scale and is a **deploy-time `ALTER`, never bootstrap
DDL** -- PGlite (the default prototype substrate) cannot run
`create_hypertable`, so it must stay out of `schema.sql`.

When event volume warrants it on a real Postgres+Timescale cluster, run
once (not via the numbered-migration path, since it is environment-scoped
and PGlite-incompatible):

```sql
-- Requires the timescaledb extension on the target cluster.
SELECT create_hypertable('agent_session_events', by_range('created'),
                         migrate_data => true);
-- Optional columnar compression for cold partitions:
ALTER TABLE agent_session_events SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'session_id');
SELECT add_compression_policy('agent_session_events', INTERVAL '7 days');
```

This changes only physical layout: the `(session_id, seq)` primary key,
the FK to `inquiries`, and every query keep working unchanged. Skip it
entirely on PGlite or any non-Timescale Postgres. The `payload_ref` column
(object-store offload for >16 KB payloads) is already present and needs no
migration.

## Common failure modes

- Marked applied but live still drifts -- the migration fixed one symptom,
  not the whole schema. Trust the catalog diff, not the ledger.
- Constraint discovery via `pg_constraint.conkey` misses table-level
  CHECKs. Use `pg_get_constraintdef(con.oid)`.
- Existing data violates stricter fresh constraints -- transform it, or
  rebuild offline and swap; do not loosen the canonical schema silently.
- Self-referential `change_log.caused_by` rows break naive bulk load --
  drop/defer the self-FK or order by causation.
- Resetting `applied_migrations` before the deployed code drops numbered
  files -- startup re-records them.
- Assuming `schema.sql` rewrites existing DBs -- it does not. Existing DBs
  change only via numbered migrations, manual reconciliation, or
  rebuild/swap.
- Trusting the `--datadir` flag to tell you the engine -- read
  `TRACKINIZER_ENGINE`.
