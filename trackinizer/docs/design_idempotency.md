# Trackinizer idempotency design

## Tl;dr

**Trackinizer's clients mint idempotency keys; the server mints row
identities.** That asymmetry is the entire reason trackinizer's
idempotency model differs from Stripe's, and every visible difference --
the header name, the body-drift behavior, the missing `409`, the
per-item batch semantics -- is a downstream consequence of it. The rest
of this doc unpacks the asymmetry, then walks each consequence to its
specific code site.

Load-bearing code:
- [`server/api/idempotency.py`](../server/api/idempotency.py) --
  `Idempotency-Key` header middleware (parses + injects into the
  per-request contextvar).
- [`server/store.py`](../server/store.py) -- `_submit_generic` (pre-probe +
  collision-recovery), `_lookup_existing_by_change` (probes
  `change_log` by client-supplied key), `emit_change`
  (per-`change_log.id` dedup + SAVEPOINT replay).
- [`client/client.py`](../client/client.py) -- `submit()` mints one UUID
  per call as `idempotency_key` (body field) for submits and one UUID
  per call as `Idempotency-Key` (header) for edits.

## The asymmetry

Stripe's API creates entities (`ch_xxx`, `pi_xxx`) whose ids are
**server-minted**. After a network failure on the first attempt, the
client has no way to recognize "this charge is the same one I tried
yesterday" by content alone -- two distinct charges with identical
bodies are a legitimate use case (the customer bought the same thing
twice). The only signal that distinguishes "retry" from "new request"
is the `Idempotency-Key` header. So the server must maintain
`(api_key, idempotency_key) -> response_body` outside the row data
itself, because that is the only place to look up "what id did I
return to you the first time."

Trackinizer's clients send `idempotency_key: <UUID>` in the request
body (or `Idempotency-Key: <UUID>` in the header for edits), and the
server uses that UUID as the `change_log.id` of the audit row for
the mutation. The mutation's **subject id** is always server-minted:
on a submit, the new `inquiries.id` is `uuid.uuid4()` picked
server-side; on an edit, the subject already exists and the caller
already knows its id. A retry inherently knows what to ask for: "did
the `change_log` row with id X commit?" -- answered by
`SELECT subject_id, kind, subject_kind FROM change_log WHERE id = X`.
The PK uniqueness constraint on `change_log` is the dedup mechanism.
**The database is the cache.**

This is the inversion every deviation collapses into. Stripe maintains
a parallel cache pointing at the rows it wrote because it has to.
Trackinizer's dedup record *is* the audit row, which already needs
to exist on every mutation for unrelated reasons (audit trail, cost
rollup, subscriber notification fan-out, cascade propagation).

## Why not just follow Stripe anyway?

A response cache on top of `change_log.id` would be redundant -- the
constraint that PK already enforces is exactly the constraint the
cache would enforce. Adding the cache would:

- Duplicate state already in the database (cache miss = re-derive from
  the row; cache hit = "the row exists, here it is").
- Introduce a 24-hour expiry where today's contract is permanent
  (`change_log` rows do not expire, so neither does dedup).
- Add a cache-coherence problem (what if the cache says "I wrote X"
  but the subject was purged?) that the PK-as-key model doesn't have.
- Require storing the original request body to detect drift -- the
  one Stripe affordance the PK model can't replicate -- which is the
  only thing the cache buys, and which the design decided was not
  worth the state.

The decision to not follow Stripe is therefore not "we don't like
their model" but "their model exists to solve a problem trackinizer
doesn't have, namely 'how does the server identify a retry when it
controls the id space.'"

The matching question -- "why not let the client mint `inquiries.id`
too?" -- has its own answer in the [Adversarial
considerations](#adversarial-considerations) section below.

## Baseline: Stripe's contract

Repeated here for reference; this is what trackinizer is *not* doing
and why. Each bullet below maps to a "Consequence" section further
down.

- **Key:** client sends `Idempotency-Key: <opaque-string>` on a
  mutating request.
- **Storage:** server stores
  `(api_key, idempotency_key) -> (status, response_body)` for a finite
  window (Stripe: 24 hours).
- **Replay:** a retry returns the original response **byte-for-byte**,
  regardless of what the second request body contained.
- **Drift:** a retry with the same key but a different body returns
  `400 idempotency_error` -- surfaces client bugs that would otherwise
  silently drop the second intent.
- **Concurrency:** a retry that arrives while the original is still
  in-flight returns `409 concurrent_request`.
- **Safe verbs:** GET / DELETE silently ignore the key (already
  idempotent by HTTP semantics).

References:
- Stripe API reference: idempotent requests
  -- https://docs.stripe.com/api/idempotent_requests
- Stripe Engineering: designing robust and predictable APIs with
  idempotency
  -- https://stripe.com/blog/idempotency
- IETF draft: HTTP Idempotency-Key header field
  -- https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/

The model is two pieces: a **key** (opaque, client-minted) and a
**response cache** (server-side, keyed by the pair above). The cache is
the source of truth for what "this request" returned.

## Trackinizer's model

Trackinizer does not maintain a response cache. It instead uses
**`change_log.id` as the idempotency mechanism**: the client supplies
the UUID that will become the audit row's PK; collisions on that PK
are dedup signals.

### Two delivery surfaces, one column

Two transports carry the client-supplied key, depending on the
mutation type:

| Transport            | Carried by         | Used for                                |
|----------------------|--------------------|-----------------------------------------|
| `idempotency_key`    | Submit body field  | `POST /api/submit_*` and batch items    |
| `Idempotency-Key`    | HTTP header        | Edits, edge mutations, purges, anything not a submit |

Both land in the same per-request `ContextVar` slot
(`store._CLIENT_CHANGE_ID`) and `emit_change` consumes them
identically. The split is convenience: a header doesn't compose with
batches (one header per HTTP request, N items per batch), so each
batch item carries its own body-field key.

The trax client mints one UUID per logical operation:

- For submits, `client/client.py:submit()` adds `idempotency_key` to
  the body. The trax client also still mints an `Idempotency-Key` header
  for its own retry loop, but `_submit_generic` overrides the slot
  from the body field, so the header is decorative for submits.
- For edits, `client/client.py:_request()` always mints an `Idempotency-Key`
  header on non-GET requests.

### Server-minted `inquiries.id`

The new inquiry's `id` is **server-minted** (`uuid.uuid4()` inside
`_submit_generic`). Clients have no way to predict it before the
response. On a retry, the server returns the *original* inquiry's id
from the existing `change_log.created` row, not a fresh server-minted
id. The client never sees a different id from one attempt to the
next.

### Consume-once slot semantics

`Idempotency-Key` / `idempotency_key` is delivered to the request as a
single-cell mutable holder (`store._ChangeIdSlot`) in a per-request
`ContextVar`. The first `emit_change` in the request claims it and
clears the slot; subsequent emits inside the same request (cascade
rows, `gather` siblings, batch items) fall back to server-minted
change ids.

Without consume-once, a single key would collide with itself on the
second `change_log` INSERT in any request that issues more than one
change. The slot's mutability across `asyncio.gather` siblings (they
share the holder *object* by reference, even though `ContextVar`
gives each task its own binding) is what makes the contract robust
across concurrent fan-out within one request.

### Submit retry mechanics

`store._submit_generic`:

1. If `idempotency_key` is set, probe `change_log` for that id
   (`_lookup_existing_by_change`). A hit returns the original
   `subject_id` directly; the caller sees the original inquiry's id
   without any write happening.
2. Miss -> mint `row_id = uuid.uuid4()`, set the contextvar slot
   from `idempotency_key`, open the txn, insert the new inquiry row,
   write embeddings, call `emit_change(kind="created")`.
3. If a concurrent racer committed first between the probe and the
   write, `emit_change`'s `change_log` INSERT collides on the
   client-supplied key. The collision is re-raised through the
   outer `tx()` (`store.py:2580+`), the whole transaction rolls
   back (dropping the new inquiry row + embeddings + any
   `post_insert` edge writes), and the caller's
   `except asyncpg.UniqueViolationError` re-probes to return the
   winner's `subject_id`.
4. A `finally` block clears the contextvar slot on every exit path,
   so a leftover key can never attach to the next submit in the same
   task context.

### Edit retry mechanics

`store.set_description` and siblings:

1. Read current value.
2. If unchanged, return without writing -- the second retry is a
   no-op.
3. Otherwise UPDATE + `emit_change` in one transaction. The second
   retry collides on `change_log.id` PK; the SAVEPOINT in
   `emit_change` rolls back the cost UPDATE; `emit_change` then
   checks whether the existing `change_log` row's `(actor,
   subject_id, kind)` tuple matches the retry. Match -> treat as
   replay, return the original change id. Mismatch -> raise
   `ConflictError`, route layer maps to HTTP 409 (`api/app.py:114`,
   `unique_violation_handler`).

Submit and edit paths share the SAVEPOINT collision detector but
diverge in what they do with it: for `kind="created"` the racer's
`subject_id` always differs from the original's (server-minted
fresh per attempt), so `emit_change` re-raises and the outer
`_submit_generic` re-probes; for edits the caller already knows the
`subject_id`, so a `(actor, subject_id, kind)` match is a clean
replay.

## Consequences (the visible deviations)

Each subsection below is one place where trackinizer's behavior
differs from Stripe's contract. They are presented as **consequences**
of the asymmetry, not as standalone design choices.

### Consequence 1: dedup is permanent, not 24h

*Source:* Stripe's cache must expire because it is a separate storage
tier with finite capacity. Trackinizer's "cache" is the `change_log`
row, which has no expiry.

A client that retries a week later still gets the original row.
A trackinizer restart that loses all in-memory state still defends
the property. The database does the work.

The flip side: the response is **re-derived from the row**, not
byte-replayed. For every current endpoint this is equivalent because
the row determines the response. An endpoint whose response depends on
non-row state (one-time secrets, externally-minted ids returned only
on first commit) would break the equivalence; such endpoints should
either mint server-side state and store it on the row, or opt out of
the idempotency contract entirely.

### Consequence 2: body drift under a reused key is silently accepted

*Source:* Stripe can detect drift because the cache holds the original
request body alongside the response. Trackinizer has no cache,
therefore no original body to compare against. Detecting drift would
require *adding* a body cache -- exactly the indirection the design
avoids.

Stripe returns `400 idempotency_error` when a retry reuses the key but
sends a different body. Trackinizer accepts the retry as replay and
discards the new body's drift, keeping the originally-committed
values.

The replay-equivalence check trackinizer *does* perform is on
`(actor, subject_id, kind)` in `emit_change`. From the code:

> Replay is identified by `(actor, subject, kind)` matching the
> original row -- a retry of *that* mutation. Field-level drift
> (different reason, cost, snapshot) within a matching tuple is
> silently treated as replay; the originally-committed values win and
> the retry's are discarded. That's deliberate: the client promised
> idempotency by reusing the UUID. A genuinely different operation
> (different subject or kind) is a client bug and 409s.

Why this is acceptable in practice:

- The UUID is the client's promise. `client/client.py:submit` mints a
  fresh UUID per logical attempt; a corrected request gets a fresh
  UUID and runs as a new operation.
- The "silent drop of second intent" that Stripe's `400` is meant to
  catch is caught at the `(actor, subject, kind)` tuple, which is what
  identifies "the same mutation" semantically. Drift inside that tuple
  is at field-level granularity (a different `reason` string, a
  different `cost_delta`).
- Stripe's contract serves third-party callers writing against an
  unfamiliar API. Trackinizer's first-party clients are the trax CLI
  and the SPA, both of which obey "fresh UUID per attempt" by
  construction.

### Consequence 3: no documented `409 concurrent_request`

*Source:* Stripe needs an explicit `409` because its response cache
has no native concurrency story -- two racers on the same key would
each try to populate the cache, and the server must externally
arbitrate. Trackinizer's "cache" is the `change_log` row, and Postgres
arbitrates row-level writes natively -- one writer commits, the other
collides on the PK, the outer tx rolls back, the loser re-probes and
returns the winner's `subject_id`. The `409` becomes unnecessary
because the outcome is already correct.

Concrete behavior: a concurrent retry sees the second writer raise
`UniqueViolationError` from inside `emit_change`'s SAVEPOINT, that
error propagates through the outer `tx()`, asyncpg rolls back the
loser's inquiry row + embeddings + any post-insert edges, and
`_submit_generic`'s catch re-probes `change_log` to return the
winner's `subject_id`. The caller sees a normal success with the
original inquiry's id, not a `409`. The status code is the route's
ordinary one (`201` for the submit routes), the *same* code a first
write returns -- a replay is reported as the create it idempotently
stands in for, never downgraded to `200`, so create and replay are
indistinguishable by status (only the returned id reveals the reuse).

A future caller that needs to distinguish "I won the race" from
"someone else won the race" would have to compare the response
inquiry's `created.actor` (or any other field stamped at first-write
time) against what it sent. No current caller needs this.

### Consequence 4: header is `Idempotency-Key`, value must be a UUID

*Source:* the header value goes directly into `change_log.id`, which
is a Postgres `UUID` column. Accepting Stripe's "any opaque string up
to 255 chars" would force either a hash step (UUIDv5 over the value)
or a separate `idempotency_keys` mapping table. Both reintroduce the
parallel-cache indirection the design avoids.

The stricter type also gives the server an early `400` for malformed
input (`api/idempotency.py:34-43`) instead of a deep-stack failure
during INSERT.

If trackinizer ever exposes a public API to third parties, an
`Idempotency-Key` alias header that accepts an opaque string and
deterministically derives a UUID (UUIDv5 over the value) would give
Stripe-shaped ergonomics without changing the storage model. This is
a small, additive change, not a redesign.

### Consequence 5: batches dedup per-item, not per-batch

*Source:* trackinizer's unit of dedup is "one PK collision per
`change_log` write." Stripe's cache holds one entry per
`Idempotency-Key` regardless of side-effects because the cache is
keyed on the request, not the writes. A batch of N items writes N
`change_log` rows and therefore needs N keys; there is no single
"the batch row" to collide on.

`POST /api/submit_batch` requires `idempotency_key` on **every** item
(`wire/bodies.py:SubmitBatch._require_idempotency_keys`).
Per-item idempotency lives entirely on per-item `idempotency_key`
collisions with `change_log.id`. The validation rejects batches that
don't carry per-item UUIDs, so this is enforced at parse time rather
than discovered at runtime.

The batch-level `Idempotency-Key` header, if any, would bind only the
first item's audit row under consume-once semantics; subsequent items
ignore it. Per-item body keys are the right primitive for batches.

## Adversarial considerations

Client-minted UUIDs open a class of attack Stripe's server-minted ids
do not: an attacker who learns a UUID a legitimate writer is about to
use can race to claim it. Trackinizer's design closes this by
construction.

### The targeted-UUID race

Eve (an authenticated writer on the same instance) wants to attach
junk content to a row Alice's agent is about to create. Under a
hypothetical model where `inquiries.id` is client-minted:

1. Alice's agent mints UUID `X`. The UUID lives in Alice's logs,
   agent transcripts, or subscriber notifications -- anywhere Eve
   might observe it.
2. Eve races: POSTs `{summary: "lol pwned", client_request_id: X}`
   before Alice's request arrives. Row `id=X` is now Eve's.
3. Alice's POST arrives. The server's idempotency probe finds row
   `X` already exists, returns it as a successful replay.
4. Alice's downstream code attaches edges, edits, and citations to
   `X`, all pointing at Eve's content.

### How trackinizer closes it

`inquiries.id` is server-minted. Eve cannot pre-claim a row id Alice
is about to use because Alice never picks one; she picks an
`idempotency_key`. The worst Eve can do:

- Race the same `idempotency_key`: the winner's `change_log.created`
  row carries the winner's `actor`, `api_key_id`, and content.
  Alice's retry probes `change_log` for that key, finds Eve's row,
  and returns Eve's `subject_id`. Alice's *response* now points at
  Eve's inquiry -- bad.

But this requires Eve to observe Alice's pre-submit UUID, which is a
side-channel disclosure (logs, transcripts, predictable RNG). The
defense is *don't disclose pre-submit UUIDs*; the data model
guarantees that without disclosure, UUIDv4's 122 bits of entropy
make Eve's guess effectively impossible.

### Compromised credentials are out of scope

A compromised API key or session cookie means the attacker
authenticates as the legitimate user. Idempotency cannot defend
against that -- by definition the dedup layer trusts authenticated
callers. Compromise is the auth + audit + revocation layer's
problem.

## Anatomy: load-bearing vs. incidental

### Load-bearing (changing these breaks the contract)

- **Clients supply the idempotency keys.** Removing this and minting
  server-side collapses to "no idempotency" -- the client can no
  longer identify a retry.
- **Server mints `inquiries.id`.** Letting the client name the new
  row's id re-opens the targeted-UUID race in [Adversarial
  considerations](#adversarial-considerations).
- **Pre-probe + outer-tx-rollback on collision.** Removing the
  pre-probe surfaces every retry as an exception-and-recover path,
  noisy and slow. Removing the outer rollback leaves orphan
  inquiry rows when the racer loses.
- **Consume-once slot semantics for the key.** Removing this breaks
  any request that issues more than one `emit_change` -- cascades,
  batches, and gather siblings would all collide on `change_log.id`.
- **`(actor, subject_id, kind)` as the edit replay-equivalence
  tuple.** Tightening it (full body comparison) implements Stripe
  semantics but requires storing the original body. Loosening it
  (only `subject_id`) makes two distinct kinds of mutation against
  the same row indistinguishable.
- **`finally` clears the slot.** Removing this leaks the
  idempotency key into the next submit's `emit_change` on any
  unexpected error path.

### Incidental (could change without breaking the contract)

- **Permanence of the key.** Nothing relies on the key surviving
  forever. `change_log` rows do, but a future archival tier could
  move old rows to cold storage and the idempotency contract would
  still hold on the hot tier where retries actually happen.
- **The exact spelling `Idempotency-Key` / `idempotency_key`.** Renaming
  is a one-line middleware/field change. The PK-as-key model
  survives the rename intact.
- **UUIDv4 specifically.** Any UUID variant works. The trax client
  uses v4 for unguessability, but v7 (time-ordered) would be a
  drop-in replacement if monotonic ids ever mattered for an index.

## When to revisit

Three triggers would justify rethinking the asymmetry itself, not
just the surface ergonomics:

1. **A public third-party API.** Stripe-shaped ergonomics
   (`Idempotency-Key` + opaque string) become a usability
   requirement. The alias-header approach in Consequence 4 covers
   this without touching the storage model.
2. **An endpoint whose response depends on non-row state.** The
   re-derive-from-row equivalence in Consequence 1 breaks. Either
   exclude such endpoints from the idempotency contract or add a
   per-endpoint response cache for them specifically.
3. **A caller that needs to distinguish racing from sequential
   retries.** Expose `created.actor` / `created.api_key_id` in the
   response so the caller can compare. Today no caller needs this.

None of these require abandoning the PK-as-key model; they layer on
top.
