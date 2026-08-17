# Trackinizer throughput plan

Status: proposed

This document records the Trackinizer changes intentionally excluded from the
observability change. The current change may add request, query, and
authentication timings. It must not change query semantics, authentication,
database shape, stream routing, connection ownership, or worker count.

## Load target

The first operational target is one 48-job research study. Its controller has
several concurrent workflows, but Trackinizer should not need workload-specific
behavior. The server should support this representative burst:

- 48 simultaneous selector reads.
- 48 owner compare-and-set claims.
- 48 detail reads and receipt writes.
- Change delivery to every interested workflow.
- Ordinary UI and operator reads during the burst.

Component acceptance targets:

- Bearer authentication p90 below 50 milliseconds.
- Indexed selector p90 below 500 milliseconds.
- Owner and receipt writes p90 below two seconds.
- No transport retries, timeouts, or connection resets.
- No single event creates workflow-count-multiplied full scans.

The end-to-end acceptance target remains 48 scheduler acceptances within two
minutes. Agent generation and scheduled job execution are excluded from the
Trackinizer latency budget.

## Measured evidence

Study 037 ran for about 21.5 minutes and never approached 48 running jobs.
Trackinizer was secondary to source publication, but still materially slow:

- 164 HTTP transport retries occurred.
- 113 retries were connection timeouts.
- 51 retries were connection resets.
- Selector p90 ranged from 12.77 to 22.32 seconds.
- The busiest recorded minute averaged about 5.6 successful responses/second.
- 489 coalesced observer wakes caused global filtered rescans.
- The controller made 1,073 filtered inquiry-list requests.
- The controller made 602 detail requests.

Production probes later measured approximately:

- 0.11 seconds for the version endpoint.
- 1.69 seconds for a filtered query returning no rows.
- 3.85 seconds for a representative exact selector.

An operator also observed one server CPU core saturated. This is consistent
with one Uvicorn worker doing synchronous scrypt verification and Python row
filtering. It is not, by itself, proof that adding workers solves the root
causes.

## Root causes

### Filters execute after a broad database read

`Store.list_kind` lowers kind, status, and sequence ranges into SQL. Any wire
filter causes all matching kind/status rows to be materialized before Python
evaluates labels, subscribers, owner, regex, and other clauses. `LIMIT` and
`OFFSET` apply only after that filtering.

This preserves the filtering contract, but common KnowOp2 selectors become
linear in every row of the kind. The schema already has GIN indexes for labels
and subscribers plus indexes for kind/status and owner. The query path does not
use those indexes for wire filters.

### Bearer verification performs scrypt per request

API keys store an indexed visible prefix and a scrypt hash. Every authenticated
request selects the prefix candidates and performs at least one synchronous
scrypt verification. Prefix misses deliberately perform dummy scrypt work.

Scrypt is correct for human-chosen passwords. Trackinizer tokens are generated
with 256 bits of entropy, so password-hardening work is unnecessary for online
token lookup. With one server worker, request authentication serializes CPU
work before the route executes.

### Every workflow receives the global change stream

`/api/change_log/stream` forwards every post-commit inquiry notification.
Clients decide whether a change matters after receipt. A burst touching
multiple inquiry IDs can therefore wake every workflow, and each workflow may
perform its own filtered recovery scan.

KnowOp2 now preserves bounded sets of changed IDs while coalescing, reducing
the worst amplification. Trackinizer still broadcasts irrelevant events to
every workflow.

### Clients frequently rebuild connection pools

Several workflow paths construct short-lived synchronous clients for related
reads and writes. Under burst load this creates avoidable TLS connections and
increases the chance of connection resets. Client ownership must remain
explicit so a cancelled thread cannot close a connection still in use.

### One worker contains all CPU and request concurrency

The current Colossus service starts Uvicorn with `--workers 1`. This makes the
single CPU core visible and simplifies PGlite ownership. PostgreSQL-backed
deployments can use multiple workers, but worker scaling should follow query
and authentication fixes. Otherwise each worker repeats expensive scans and
only moves the saturation threshold.

## Proposed changes

### 1. Lower safe filters into SQL

Add a filter compiler beside `Store.list_kind`. It should lower only clauses
whose SQL semantics exactly match the canonical Python evaluator:

- `labels is VALUE` using array containment.
- `subscribers is VALUE` using array containment.
- `owner is VALUE` using equality.
- Null checks for labels, subscribers, and owner.

Unsupported clauses, including regex, remain Python post-filters. If any
post-filter remains, pagination stays after Python filtering. Fully lowered
queries apply pagination in SQL.

Required tests:

- Compare SQL and Python results for every lowered operator.
- Mix lowered and fallback filters without changing pagination semantics.
- Run the generated SQL against PostgreSQL, not only a fake connection.
- Use `EXPLAIN (ANALYZE, BUFFERS)` to prove existing indexes are selected.
- Run 48 simultaneous representative selectors against realistic row counts.

Likely files:

- `server/store/read.py`
- `server/store/read_test.py`
- `server/schema_migration_test.py`

No schema change should be necessary for the first operators because the
required GIN and scalar indexes already exist.

### 2. Add indexed lookup for generated bearer tokens

Add a nullable `api_keys.secret_digest BYTEA` column with a unique partial
index covering live non-null digests. Store SHA-256 of newly generated tokens.
Keep the scrypt hash for existing rows and defense-in-depth.

Request lookup becomes:

1. Hash the presented high-entropy token with SHA-256.
2. Select the live row by exact indexed digest.
3. Authorize without scrypt when the digest matches.
4. On a digest miss, check legacy prefix rows with null digests.
5. Verify legacy rows with scrypt and backfill the digest.

Unknown generated tokens should require indexed queries but no dummy scrypt.
The digest must never replace high-entropy token generation or expose
plaintext.

Required tests:

- Fresh tokens authenticate without calling scrypt.
- Legacy tokens authenticate once and receive a digest.
- Revocation excludes digest rows.
- Concurrent legacy upgrades remain idempotent.
- Schema bootstrap upgrades existing databases safely.
- Fresh and upgraded schemas have identical catalogs.
- 48 concurrent authentications meet the latency target.

Likely files:

- `server/auth.py`
- `server/auth_test.py`
- `server/assets/schema.sql`
- A numbered migration when required by the deployed schema.
- `server/schema_migration_test.py`

This step requires the database-migration procedure and a production backup.

### 3. Reuse HTTP connections within owned workflow lifetimes

Give each workflow action an explicit client lifetime covering its related
Trackinizer calls. Do not retain one process-global synchronous client. Do not
close a client while cancelled `asyncio.to_thread` work still owns it.

Required tests:

- One action reuses one connection pool across its reads and writes.
- Cancellation waits for thread-owned client cleanup.
- Parallel actions never share unsafe mutable client state.
- A 48-action burst creates bounded connections and zero reset retries.

This work belongs primarily in KnowOp2 adapters and the Trackinizer client. It
must not add another scheduler or durable assignment model. Trackinizer remains
the event source, and `Inquiry.owner` remains the claim fence.

### 4. Add scoped change subscriptions

Extend the change stream with server-evaluated routing constraints. Start with
kind because it is stable, cheap, and broadly reusable. Add label or subscriber
scope only after measuring the remaining irrelevant delivery.

The server must preserve the startup race contract:

1. Establish the subscription.
2. Perform a durable recovery scan.
3. Consume events buffered during that scan.

Required tests:

- An irrelevant kind produces no event for the subscriber.
- Relevant events remain ordered and at-least-once.
- Reconnect recovery cannot miss a committed mutation.
- Forty-eight irrelevant changes cause bounded selector reads.
- Existing unscoped web subscriptions retain their behavior.

Likely files:

- `server/api/query.py`
- `server/notify.py`
- `client/client.py`
- KnowOp2 observer configuration and tests.

### 5. Replace offset pagination in control paths

The current `list_kind_all` contract documents that concurrent inserts can
shift offset pages. Correctness-critical scans should use a stable cursor based
on `(created, id)` or one server-side snapshot. Operator display endpoints may
retain ordinary offset pagination.

Required tests:

- Insert rows between pages without omissions or duplicates.
- Cancellation and admission scans see every snapshot member exactly once.
- Cursors remain stable across identical timestamps.

### 6. Benchmark multiple workers

After steps 1--5, run the same 48-way workload with one, two, and four Uvicorn
workers against PostgreSQL. Measure throughput, CPU by worker, database pool
pressure, p50, p90, and maximum latency.

Select the smallest worker count meeting the targets. PGlite remains
single-process unless its ownership contract changes. The service configuration
must derive worker count from the selected database engine rather than silently
running an unsafe PGlite topology.

## Verification sequence

Each step should ship independently and retain the observability fields from
the current change.

1. Run unit and fake-database tests.
2. Run schema and filter tests against real PostgreSQL.
3. Seed production-shaped inquiry and API-key cardinalities.
4. Run isolated 48-way selector, authentication, write, and stream gates.
5. Run the full local KnowOp2 pipeline against the isolated server.
6. Deploy a bounded canary without changing production data.
7. Confirm latency and retry targets from structured logs.
8. Launch the next numbered study only after every component gate passes.

The next study must not enable repair routing. Recovery behavior remains the
normal deterministic receipt, claim, cancellation, and replay machinery.

## Rollback boundaries

- SQL lowering can fall back to the canonical Python evaluator.
- Digest lookup retains legacy scrypt rows until migration is verified.
- Scoped streams retain the current unscoped endpoint.
- Client reuse can revert per action without changing wire contracts.
- Worker count remains a deployment setting after single-worker correctness.

No rollback should weaken owner compare-and-set claims, idempotency keys,
change-log durability, or receipt reconciliation.
