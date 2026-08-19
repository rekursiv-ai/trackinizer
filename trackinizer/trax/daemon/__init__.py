"""Connect-or-spawn daemon that amortizes ``trax``'s module-import cost.

A ``trax`` invocation spends ~145ms importing the HTTP client (httpx and its
dependency cone) before it does any work, and pays it again on every run. A
polling swarm of 70 agents therefore burns whole cores on imports alone. The
daemon holds those imports for the life of a login session, and shares one
connection pool per resolved server; the CLI becomes a thin client that
ships ``argv`` over a Unix socket and prints what comes back.

``protocol`` and ``client`` must not reach the CLI's import graph: a single
import of the HTTP client in either would re-introduce the very cost the
daemon exists to remove, on every invocation, daemon or not.
``protocol_test`` pins that behaviorally.

Two properties the design turns on, both easy to lose in a later edit:

* **Per-request state is per-CONTEXT, never process-global.** The daemon is
  threaded, so binding ``os.environ`` or the working directory around a
  request leaks into every other request in flight -- measured, not
  theorized. See ``trax.context``.
* **A delivered request is never retried locally.** The daemon runs the verb
  before it replies, so a lost response may name an applied write. Falling
  back in-process would mint a fresh idempotency key and duplicate it.
"""
