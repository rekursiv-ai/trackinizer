# Subscriber push

## Goal

Let an agent (or any program) **hear about changes to rows it subscribes
to, while they happen**, without polling for them. Every inquiry carries a
`subscribers` list; every mutation writes a `change_log` row snapshotting
who was subscribed at commit time (`subscribers_snapshot`). This feature
routes each such change -- as a small JSON envelope -- into the stdin of
every subscriber's **live** `trax run` session
([`design_session_messaging.md`](design_session_messaging.md) built the
delivery pipe; this doc is the producer that feeds it).

The durable record stays in `change_log`. The push is a notification,
never the record.

```
  bob: trax issue 1 status to complete
    └─ commit writes change_log row (subscribers_snapshot=["alice"])
         └─ server sweep (0.5s) ─▶ inbound queue (per live session)
              └─ alice's trax run poller (~0.5s) drains her mailbox
                   └─ pump injects one line into her child's stdin
```

Load-bearing code:
- [`server/subscriber.py`](../server/subscriber.py) -- the sweep task
  (`push_changes_to_live_subscribers`), envelope (`_change_payload`),
  per-`(change, subscriber)` dedup key.
- [`server/store/read.py`](../server/store/read.py) --
  `what_changed_for_anyone` (cursor query; LEFT JOIN resolves the
  subject's short seq).
- [`trax/run/session.py`](../trax/run/session.py) -- `_render_inbound`
  (client-side shaping: what actually reaches the child).
- [`examples/subscriber_demo.sh`](../examples/subscriber_demo.sh) -- the
  executable spec, end to end against a real server.

## The envelope

One JSON object per change, metadata only:

```json
{"agent_message": "FYI: trax issue 42 status changed (by bob)",
 "id": "<change uuid>",
 "created": "2026-08-25T19:17:34+00:00",
 "actor": "bob",
 "kind": "status",
 "subject_kind": "Issue",
 "subject_id": "<inquiry uuid>",
 "subject_ref": "issue 42",
 "row": "trax issue 42"}
```

- `agent_message` -- the one line a model should read. Self-documenting:
  the `trax issue 42` inside it is the follow-up command.
- `subject_ref` / `row` -- the short per-kind address (`issue 42`), the
  way every trax verb addresses rows and a fraction of a UUID's tokens.
  Resolved by the sweep's LEFT JOIN on `inquiries.seq`; a purged subject
  (no row to join) falls back to the UUID, which stays resolvable via
  `trax id`.
- `caused_by` rides along on cascade-emitted events.

**No `old`/`new` delta** -- unbounded text (descriptions, abstracts) must
not ride a line-oriented injection; the recipient runs `row` for the
record. **No roster** -- recipients must not learn who else subscribes.

TODO: a `delta` follow-up command once a `trax change <id>` verb exists;
the server route (`GET /api/change_log/<uuid>`) is already there.

## Client-side shaping

The server pushes the **same envelope to every session**; the `trax run`
poller (`_render_inbound`) decides what reaches the child's stdin, because
only the client knows what is attached:

| session child | receives |
|---|---|
| model CLI (claude / gemini / codex) | only the `agent_message` line -- the other fields would spend model context on metadata it can fetch on demand |
| IO-stream (`trax run sh`) | the raw JSON line; the program parses it itself (reference jq client in the demo) |

Only the route-attested `trackinizer` sender unwraps. `source` is stamped
server-side from the principal, so another sender's JSON-looking text
renders as a plain `sender: text` message -- a client cannot forge an
envelope.

## Delivery semantics (deliberately weak)

Delivery inherits the inbound queue's stance ("stale steering is worse
than dropping", [`server/inbound.py`](../server/inbound.py)):

- **Live sessions only, drop-if-absent.** A subscriber with no running
  session misses the push. Offline catch-up is the polling surface
  (`what_changed_for_me`), which reads the same durable rows.
- **The cursor always advances.** A change whose delivery fails is logged
  and skipped -- its row remains readable forever, while a pinned cursor
  would block every later change for every subscriber.
- **Dedup, bounded.** Deliveries key on `uuid5(change_id, subscriber)`
  through `send_once`, so a replayed read does not double-inject (within
  the queue's bounded receipt window).
- **Boot cursor = now.** Changes committed while the server was down are
  never pushed (nobody had a live poller then either).

## Why a sweep and not LISTEN/NOTIFY

The doorbell was built first and cut, on measurement:

1. A dead LISTEN connection raises nothing -- the generator silently
   stops yielding (`lib/postgres/substrate.py`) -- so a periodic sweep
   had to exist anyway as the liveness backstop. Once it exists, it does
   all the correctness work; the doorbell only adds speed.
2. The speed is unobservable: end-to-end latency is gated by the client
   poller (~0.5s tick) regardless. Doorbell ≈ 0.25s average; 0.5s sweep
   ≈ 0.75s. Indistinguishable for text into an agent's stdin.
3. Every high-severity review finding in the push loop lived in the
   listen/reconnect machinery the sweep deletes.

Cost: one indexed query per 0.5s. The partial index
`idx_change_log_subscribed_created_id` (btree on `(created, id)` WHERE
`subscribers_snapshot != '{}'`) serves it -- measured Bitmap Index Scan at
~6% of the seq-scan cost at 5k rows. Note `cardinality(...) > 0` is NOT
an indexing fix: neither predicate is GIN-indexable; the partial btree
is. If a poller-free consumer ever appears (server-side SSE per
subscriber), the doorbell is a ~10-line re-add around the same drain
core, with the sweep kept as backstop.

## Diagnosing delivery

One INFO per subscriber delivery and one DEBUG per no-live-session skip
(`server/subscriber.py`). A "subscriber never got the event" report
bisects on the INFO line:

| INFO present? | conclusion |
|---|---|
| yes | server half worked -- suspect the client poller or the child |
| no | sweep never delivered -- suspect the cursor or the subscription |

The client side logs injection losses (`trax/run/pty_pump.py`: dead child,
master died mid-write) and capture losses (`StreamEventDropped=N` in the
end-of-run stats), so every hop that can drop a message says so.
