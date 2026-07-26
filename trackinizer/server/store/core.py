""":class:`Store` plus :class:`StubEmbedder` and ``EMBEDDING_DIM``.

Owns CRUD against the three-table backend; emits changes; cascades. The
concrete behavior is split across mixins (submit / read / edit / edge /
session / cascade); this module keeps the lifecycle (construction, bootstrap,
embedding) and composes them into the public :class:`Store`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from typing import Final
from uuid import UUID

import asyncio
import hashlib

import asyncpg

from trackinizer.lib.postgres import Conn
from trackinizer.server import auth as _auth
from trackinizer.server.auth import bootstrap_admin
from trackinizer.server.notify import tx
from trackinizer.server.primitives import upsert_embedding
from trackinizer.server.schema_gen import (
    SEQ_FOR_KIND,
    substitute_schema_placeholders,
)
from trackinizer.server.sql import schema_migrations
from trackinizer.server.store.cascade import _CascadeAuditMixin
from trackinizer.server.store.change_id_slot import (
    set_client_change_id,
)
from trackinizer.server.store.edge import (
    INFERRED_PROVENANCE_REASON,
    _EdgeMixin,
)
from trackinizer.server.store.edit import _EditMixin
from trackinizer.server.store.metrics import _MetricsMixin
from trackinizer.server.store.read import _ReadMixin
from trackinizer.server.store.session import _SessionMixin
from trackinizer.server.store.shared import (
    EMBEDDING_DIM,
    _StoreShared,
)
from trackinizer.server.store.submit import (
    SUBMIT_METHOD,
    _SubmitMixin,
)
from trackinizer.server.values import vetted_sql


__all__ = [
    "EMBEDDING_DIM",
    "INFERRED_PROVENANCE_REASON",
    "SUBMIT_METHOD",
    "Store",
    "StubEmbedder",
    "_xorshift_floats",
    "schema_migrations",
    "set_client_change_id",
]

# asyncpg raises a plain ``InterfaceError('connection is closed')`` (and a
# mid-operation ``ConnectionDoesNotExistError`` whose message mentions "closed
# in the middle") when the PGlite Node drops the socket -- the transient case
# bootstrap retries. Every *other* ``InterfaceError`` is API misuse (codec
# error, released connection, operation in progress): a deterministic bug that
# must surface, not be retried. Match on the connection-closed phrasing rather
# than the (shared) exception class.
_TRANSIENT_FAULT_MARKERS: Final = (
    "connection is closed",
    "closed in the middle",
)


def _is_transient_pglite_fault(err: BaseException) -> bool:
    """Whether ``err`` is a connection-closed fault (retry) vs API misuse (raise)."""
    text = str(err).lower()
    return any(marker in text for marker in _TRANSIENT_FAULT_MARKERS)


class _LifecycleMixin(_StoreShared):
    """Bootstrap and embedding-backfill lifecycle for :class:`Store`.

    Construction (``__init__``) and the :meth:`_embed_all` helper live on the
    shared base :class:`_StoreShared`; this mixin adds the boot-time schema /
    embedding reconciliation that runs against ``self.engine``.
    """

    def should_bump_api_key_last_used(self, key_id: UUID) -> bool:
        """Return True iff a ``last_used_at`` UPDATE should fire for ``key_id``.

        True for the first hit and any hit past
        :data:`auth.LAST_USED_BUMP_INTERVAL_SEC`; False inside the interval.
        Records the bump time on True. Evicts the oldest entry once the cache
        exceeds :data:`auth.LAST_USED_BUMPED_AT_MAX_ENTRIES` so a flood of
        distinct keys cannot pin unbounded memory.
        """
        # Read clock and interval through the module so test monkeypatching
        # of ``auth.monotonic_clock`` is observed.
        now = _auth.monotonic_clock()
        last = self._last_used_bumped_at.get(key_id)
        if last is not None and now - last < _auth.LAST_USED_BUMP_INTERVAL_SEC:
            return False
        if (
            last is None
            and len(self._last_used_bumped_at) >= _auth.LAST_USED_BUMPED_AT_MAX_ENTRIES
        ):
            oldest_id = min(
                self._last_used_bumped_at,
                key=self._last_used_bumped_at.__getitem__,
            )
            del self._last_used_bumped_at[oldest_id]
        self._last_used_bumped_at[key_id] = now
        return True

    async def _backfill_embeddings(self, conn: Conn) -> None:
        """Embed any inquiry missing a row for a registered embedder.

        A datadir rebuild or dump reload that copies ``inquiries`` but not
        ``inquiry_embeddings`` leaves semantic search blind to those rows.
        Idempotent via the ``(inquiry_id, model)`` PK: an inquiry already
        embedded by every embedder is skipped, and a freshly added embedder
        backfills only its own missing rows.

        Safe to run synchronously at boot only because every production
        embedder is the deterministic hash :class:`StubEmbedder` (no model,
        no network). A future network/model embedder must move this off the
        startup path -- N blocking ``embed`` calls would stall boot.
        """
        for embedder in self.embedders:
            rows = await conn.fetch(
                "SELECT id, title FROM inquiries i WHERE NOT EXISTS ("
                "SELECT 1 FROM inquiry_embeddings e "
                "WHERE e.inquiry_id = i.id AND e.model = $1)",
                embedder.name,
            )
            for row in rows:
                await upsert_embedding(
                    conn, row["id"], embedder.name, await embedder.embed(row["title"])
                )

    async def bootstrap(self, *, attempts: int = 6) -> None:
        """Apply the canonical schema when this database has not seen it.

        ``attempts`` is the number of fresh passes before a transient PGlite WASM
        fault gives up. Under whole-suite ``pytest -n`` load PGlite's WASM
        Postgres can trap (``RuntimeError: unreachable`` inside
        ``execProtocolRawSync``) part-way through the bootstrap DDL; the Node
        child then exits and the next ``engine.acquire()`` rebuilds a fresh one
        (``PGliteEngine._live_conn``). A trapped transaction commits nothing, so
        the idempotent pass safely replays. Several attempts absorb a cluster of
        traps (each independently recoverable) while a deterministic failure --
        bad DDL, missing extension -- is not in the caught set and still raises on
        the first pass. Each retry fires only on a caught fault, so a healthy boot
        pays nothing.

        Retries the whole pass on a transient PGlite fault. Bootstrap holds one
        connection across a burst of heavy DDL; on the PGlite substrate that DDL
        runs on a WASM Postgres that, under whole-suite load (``pytest -n``), can
        trap (``RuntimeError: unreachable``) mid-statement. The Node child then
        exits, so the failure reaches asyncpg as one of: the active connection
        dropped (``ConnectionDoesNotExistError`` / ``InterfaceError``), or -- if
        the next ``acquire`` races the exiting Node before ``_live_conn`` rebuilds
        it -- a refused reconnect (``ConnectionRefusedError``). All are
        transient: re-entering ``engine.acquire()`` rebuilds a fresh Node, and a
        trapped transaction commits nothing, so the idempotent pass replays
        cleanly (advisory lock, ``IF NOT EXISTS``, ``applied_migrations`` ledger,
        ``ON CONFLICT DO NOTHING``). The caught set is deliberately narrow:
        ``ConnectionRefusedError`` not bare ``OSError``, and only the
        *connection-closed* ``InterfaceError`` (see
        :func:`_is_transient_pglite_fault`) not every ``InterfaceError`` -- the
        latter also covers asyncpg API-misuse (codec error, operation in
        progress), which is a deterministic bug that must surface on the first
        pass rather than burn the retry budget. A missing schema asset
        (``FileNotFoundError``) likewise surfaces immediately.
        """
        for attempt in range(attempts):
            try:
                await self._bootstrap_once()
                return
            except (asyncpg.PostgresConnectionError, ConnectionRefusedError):
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))
            except asyncpg.InterfaceError as err:
                if not _is_transient_pglite_fault(err):
                    raise  # asyncpg API misuse -- deterministic, do not retry
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))

    async def _bootstrap_once(self) -> None:
        """One idempotent bootstrap pass; see :meth:`bootstrap`.

        The ``applied_migrations`` table records the schema asset name so
        repeated boots do not re-run DDL. The schema executes inside a
        transaction; a failure leaves the database at the prior version.

        Concurrent bootstrap calls (two processes starting against the
        same Postgres database) serialize through a session-level
        advisory lock so neither races on ``INSERT INTO
        applied_migrations`` -- the loser would otherwise crash on a
        unique-constraint violation.

        After migrations, every per-kind ref sequence is reconciled up to
        the maximum ``seq`` already present. A datadir rebuild or dump
        reload that bulk-loads rows with literal ``seq`` values leaves the
        freshly created sequence at its start, so the next ``nextval``
        would re-mint a live ref; the reconcile closes that gap and is a
        monotonic no-op once aligned. The same rebuild can drop
        ``inquiry_embeddings`` rows, so every inquiry missing an embedding
        is re-embedded here as well.
        """
        async with self.engine.acquire() as conn:
            await conn.execute(
                "SELECT pg_advisory_lock(hashtext('trackinizer.bootstrap'))"
            )
            try:
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS applied_migrations ("
                    "name TEXT PRIMARY KEY, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())"
                )
                applied = {
                    r["name"]
                    for r in await conn.fetch("SELECT name FROM applied_migrations")
                }
                migrations = list(schema_migrations())
                if not migrations:
                    raise RuntimeError(
                        "schema_migrations returned no entries; SQL assets may be missing"
                    )
                baseline_name, baseline_body = migrations[0]
                numbered_migrations = migrations[1:]
                if baseline_name not in applied:
                    is_fresh_database = (
                        await conn.fetchval("SELECT to_regclass('public.inquiries')")
                        is None
                    )
                    if is_fresh_database:
                        async with tx(conn):
                            await conn.execute(
                                substitute_schema_placeholders(baseline_body)
                            )
                            for name, _body in migrations:
                                await conn.execute(
                                    "INSERT INTO applied_migrations (name) "
                                    "VALUES ($1) ON CONFLICT (name) DO NOTHING",
                                    name,
                                )
                        applied.update(name for name, _body in migrations)
                    else:
                        await conn.execute(
                            "INSERT INTO applied_migrations (name) VALUES ($1) "
                            "ON CONFLICT (name) DO NOTHING",
                            baseline_name,
                        )
                        applied.add(baseline_name)
                for name, body in numbered_migrations:
                    if name in applied:
                        continue
                    async with tx(conn):
                        await conn.execute(substitute_schema_placeholders(body))
                        await conn.execute(
                            "INSERT INTO applied_migrations (name) VALUES ($1) "
                            "ON CONFLICT (name) DO NOTHING",
                            name,
                        )
                # Seed the admin allowlist row when ``users`` is empty
                # (auth v2 Phase 1). Idempotent: once any user logs in
                # the path no-ops. Runs after migrations so the table
                # exists on first boot.
                await bootstrap_admin(conn)
                await _reconcile_sequences(conn)
                await self._backfill_embeddings(conn)
            finally:
                # If the body died because the connection dropped, the unlock
                # raises the same family -- and that fresh exception would
                # replace the original cause in the traceback. Suppress only the
                # connection-death set so the real failure propagates; the
                # advisory lock is session-scoped and dies with the connection
                # anyway. A genuine unlock failure on a live connection still
                # surfaces.
                with suppress(
                    asyncpg.PostgresConnectionError,
                    asyncpg.InterfaceError,
                    ConnectionRefusedError,
                ):
                    await conn.execute(
                        "SELECT pg_advisory_unlock(hashtext('trackinizer.bootstrap'))"
                    )


class Store(
    _LifecycleMixin,
    _ReadMixin,
    _MetricsMixin,
    _SessionMixin,
    _SubmitMixin,
    _EditMixin,
    _EdgeMixin,
    _CascadeAuditMixin,
):
    """Owns CRUD against the three-table backend; emits changes; cascades.

    Submits go through :meth:`submit_X` per kind. Reads through
    :meth:`get_inquiry` (returns the appropriate dataclass based on
    row's ``kind``). Edits through the per-field ``edit_*`` methods,
    each emitting a corresponding ``Change`` row.

    The implementation is split across mixins by concern; this class only
    composes them. ``__init__`` and the shared instance state live on the
    common base :class:`_StoreShared`.

    The base list catalogs every concern ``Store`` carries -- read / submit /
    session / edit / edge / cascade -- even though some (edit / edge / cascade)
    are already reachable through ``_SessionMixin`` -> ``_SubmitMixin``'s
    linearization. The redundancy is deliberate documentation: a reader sees the
    full concern set here without tracing the MRO. The order is C3-consistent
    with the dependency chain (cascade is the base of the mutation MRO).
    """


async def _reconcile_sequences(conn: Conn) -> None:
    """Advance each per-kind ref sequence to the maximum ``seq`` in rows.

    Monotonic and idempotent: a kind with no rows is skipped (its sequence
    keeps minting from the start), and a sequence already at or beyond the
    table maximum is left untouched. Only a sequence lagging its data --
    the signature of a datadir rebuild or dump reload that wrote literal
    ``seq`` values without replaying ``setval`` -- is bumped, so the next
    ``nextval`` cannot re-mint an existing ref.
    """
    for kind, seq_name in SEQ_FOR_KIND.items():
        max_seq = await conn.fetchval(
            "SELECT MAX(seq) FROM inquiries WHERE kind = $1", kind
        )
        if max_seq is None:
            continue
        # ``GREATEST`` guard never lowers a healthy sequence; ``setval``'s
        # implicit ``is_called=true`` makes the next value ``max_seq + 1``.
        await conn.execute(
            vetted_sql(
                "SELECT setval('",
                seq_name,
                "', GREATEST((SELECT last_value FROM ",
                seq_name,
                "), $1))",
            ),
            max_seq,
        )


class StubEmbedder:
    """Deterministic hash-based embedder for tests and offline bootstrap."""

    name = "stub"
    dim = EMBEDDING_DIM

    async def embed(self, text: str) -> list[float]:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        rng = _xorshift_floats(int.from_bytes(seed[:8], "little") or 1)
        vec = [next(rng) for _ in range(self.dim)]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


def _xorshift_floats(seed: int) -> Iterator[float]:
    """Deterministic ``uint64 -> float64 in [-1, 1)`` generator.

    Used by :class:`StubEmbedder` to produce stable per-text vectors
    without depending on numpy / random's global state.

    Yields:
      value: The next pseudo-random float in ``[-1, 1)``.

    """
    state = seed & ((1 << 64) - 1) or 1
    while True:
        state ^= (state << 13) & ((1 << 64) - 1)
        state ^= state >> 7
        state ^= (state << 17) & ((1 << 64) - 1)
        # Map the top 53 bits to a double in [0, 1), then scale to [-1, 1).
        yield (state >> 11) * (2.0 / (1 << 53)) - 1.0
