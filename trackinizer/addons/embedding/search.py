"""The vector arm of search, and the HNSW index it is served by.

``embedding.vectors.embedding`` is dimension-free ``halfvec``, so models of any
width share the table; the rows of one settings key are searched through that
key's PARTIAL HNSW index, which casts the column to the key's width
(``(embedding::halfvec(N)) halfvec_cosine_ops WHERE settings = '<key>'``).
:func:`ensure_index` makes it when the sweep starts, so a new model or setting
needs no migration, and :func:`drop_other_indexes` drops the other keys' once their
rows are gone.

:class:`Nearest` is what the add-on puts in the ``vector_search`` slot; core
search (:mod:`trackinizer.server.store.search`) merges its hits with the
keyword arm's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID
from weakref import WeakKeyDictionary

import asyncio
import re

from treekle import from_plain

from trackinizer.addons.embedding.model import Embedder
from trackinizer.addons.embedding.units import Settings
from trackinizer.server.notify import tx
from trackinizer.server.store.search import VectorHit
from trackinizer.server.values import vec_to_text, vetted_sql


if TYPE_CHECKING:
    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.store.search import Kept


__all__ = ["Nearest", "drop_other_indexes", "ensure_index"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Nearest:
    """The vector arm: the units of the configured key nearest a query.

    Attributes:
      model: Embeds the query.
      settings: Whose rows are searched, through whose index.
      loaded: Set once the sweep has loaded the model; until then the arm answers
        ``None``, so a search never loads a model.

    """

    model: Embedder
    settings: Settings
    loaded: asyncio.Event

    async def nearest(
        self,
        engine: DatabaseEngine,
        *,
        query: str | UUID,
        kept: Kept,
        filtered: bool,
        limit: int,
    ) -> list[VectorHit] | None:
        """Return the units nearest ``query`` that ``kept`` keeps, best first.

        Args:
          engine: The server's database; a connection is taken only once the query
            is embedded.
          query: The search text. Or an owner: then the query is its own newest
            vector that ``kept`` keeps, nothing is embedded, and no unit of its
            own is a hit.
          kept: The reader's and scope's test.
          filtered: ``kept`` may drop many candidates, so keep the index offering
            them until ``limit`` pass.
          limit: The most units to return.

        Returns:
          hits: Nearest first; none for an owner with no vector ``kept`` keeps;
            ``None`` while the model is not loaded.

        """
        if not self.loaded.is_set():
            return None
        embedded = (
            None
            if isinstance(query, UUID)
            else vec_to_text(await self.model.embed_query(query))
        )
        params: list[object]
        async with engine.acquire() as conn:
            if isinstance(query, UUID):
                vector = await _newest(conn, self.settings, owner_id=query, kept=kept)
                if vector is None:
                    return []
                params = [vector, limit, query]
                test = vetted_sql("v.owner_id <> $3 AND ", kept(params))
            else:
                params = [embedded, limit]
                test = kept(params)
            sql = _sql(self.settings, test)
            scan = await _scan(engine, conn, filtered=filtered)
            rows = await _nearest(conn, sql, params, limit=limit, scan=scan)
        return [
            VectorHit(
                owner_id=from_plain(row["owner_id"], UUID),
                part=from_plain(row["part"], int),
                idx=from_plain(row["idx"], int),
                field=from_plain(row["field"], str),
                chunk=from_plain(row["chunk"], int),
            )
            for row in rows
        ]


async def ensure_index(conn: Conn, settings: Settings) -> None:
    """Create the HNSW index of ``settings``' key if it does not already exist.

    Args:
      conn: A database connection.
      settings: The key the index serves and the width it casts to.

    """
    key, dim = _checked(settings)
    await conn.execute(
        vetted_sql(
            "CREATE INDEX IF NOT EXISTS vectors_hnsw_",
            key,
            " ON embedding.vectors USING hnsw ((embedding::halfvec(",
            dim,
            ")) halfvec_cosine_ops) WHERE settings = '",
            key,
            "'",
        ),
    )


async def drop_other_indexes(conn: Conn, settings: Settings) -> None:
    """Drop every key's HNSW index but ``settings``'.

    Args:
      conn: A database connection.
      settings: The key whose index stays.

    """
    key, _dim = _checked(settings)
    rows = await conn.fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'embedding' "
        "AND indexname ~ '^vectors_hnsw_[0-9a-f]{16}$' AND indexname <> $1",
        f"vectors_hnsw_{key}",
    )
    for row in rows:
        # The name matched the pattern above: only hex digits follow the prefix.
        await conn.execute(
            vetted_sql("DROP INDEX IF EXISTS embedding.", str(row["indexname"])),
        )


# Whether each engine's pgvector scans iteratively (0.8.0 and later). It is read from
# the database when a reader who may not read every session first searches by meaning,
# and then kept: the answer changes only with an extension upgrade, which a restart
# follows. The engines are held weakly, so a closed one takes its entry with it.
_ITERATIVE_SCAN: Final[WeakKeyDictionary[DatabaseEngine, bool]] = WeakKeyDictionary()

# How the arm keeps ``limit`` hits against the units its reader may not read: not at
# all, for a test that drops almost none; by an iterative index scan; or, where
# pgvector has none, by widening the list of candidates the index offers.
type _Scan = Literal["plain", "iterative", "widening"]

_KEY: Final = re.compile(r"\A[0-9a-f]{16}\Z")


# The key and the width are written into the SQL, never bound: an index name and a
# type modifier cannot be parameters, and a partial index serves only a predicate the
# planner can prove, which a bound parameter in a generic plan never is. So both are
# checked before any is written.
def _checked(settings: Settings) -> tuple[str, str]:
    """Return ``settings``' key and width as SQL text, once checked."""
    if _KEY.match(settings.key) is None or settings.dim < 1:
        raise ValueError(
            f"Expected a 16-hex-digit key and a width of at least 1, got "
            f"{settings.key!r} and {settings.dim}.",
        )
    return settings.key, str(settings.dim)


# The distance casts BOTH the dimension-free column and the query to the key's width,
# so it matches the key's partial index; without the matching cast the planner cannot
# use it and scans every row. The test reads the owner's row in a scalar subquery, run
# once per candidate the index offers: as a join, the planner hashed every vector of
# the key against the owners and sorted all of them (2.1 s on prod's 568k).
# Audience: gated -- ``test`` is the reader's, in the WHERE before the LIMIT.
def _sql(settings: Settings, test: str) -> str:
    """Return the arm's statement: ``$1`` the query vector, ``$2`` the limit."""
    key, dim = _checked(settings)
    return vetted_sql(
        "SELECT v.owner_id, v.part, v.idx, v.field, v.chunk "
        "FROM embedding.vectors v WHERE v.settings = '",
        key,
        "' AND (SELECT ",
        test,
        " FROM inquiries i WHERE i.id = v.owner_id)",
        " ORDER BY (v.embedding::halfvec(",
        dim,
        ")) <=> $1::halfvec(",
        dim,
        ") LIMIT $2",
    )


# The newest is the last chunk of the last record: what the owner said last.
# Audience: gated -- ``kept`` is the reader's test, applied to the owner asked about.
async def _newest(
    conn: Conn,
    settings: Settings,
    *,
    owner_id: UUID,
    kept: Kept,
) -> str | None:
    """Return ``owner_id``'s newest vector that ``kept`` keeps, as text; ``None`` for none."""
    key, _dim = _checked(settings)
    params: list[object] = [owner_id]
    vector = await conn.fetchval(
        vetted_sql(
            "SELECT v.embedding::text FROM embedding.vectors v "
            "WHERE v.owner_id = $1 AND v.settings = '",
            key,
            "' AND (SELECT ",
            kept(params),
            " FROM inquiries i WHERE i.id = v.owner_id) "
            "ORDER BY v.part DESC, v.idx DESC, v.chunk DESC LIMIT 1",
        ),
        *params,
    )
    return None if vector is None else from_plain(vector, str)


async def _scan(engine: DatabaseEngine, conn: Conn, *, filtered: bool) -> _Scan:
    """Choose how the arm keeps ``limit`` hits against what its test drops."""
    if not filtered:
        return "plain"
    known = _ITERATIVE_SCAN.get(engine)
    if known is None:
        version = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'",
        )
        found = re.match(r"(\d+)\.(\d+)", version) if isinstance(version, str) else None
        known = _ITERATIVE_SCAN[engine] = found is not None and (
            int(found[1]),
            int(found[2]),
        ) >= (0, 8)
    return "iterative" if known else "widening"


# Both scans run in a transaction and set only for it, never for the connection: the
# PGlite connection is the only one there is, and a pooled one serves other queries
# next, which a setting left behind would change.
async def _nearest(
    conn: Conn,
    sql: str,
    params: list[object],
    *,
    limit: int,
    scan: _Scan,
) -> list[Record]:
    """Run the arm's query, with the index kept offering candidates as ``scan`` says."""
    match scan:
        case "plain":
            return await conn.fetch(sql, *params)
        case "iterative":
            async with tx(conn):
                await conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
                return await conn.fetch(sql, *params)
        case "widening":
            async with tx(conn):
                return await _widened(conn, sql, params, limit=limit)


# Without iterative scans the index offers its ``hnsw.ef_search`` nearest units and no
# more, whatever ``LIMIT`` asks. So a query that falls short is asked again with a
# list four times as long, up to 1000, the most pgvector accepts. The first answer
# comes before the setting is read: the extension's settings exist on a connection only
# once a query has used it.
async def _widened(
    conn: Conn,
    sql: str,
    params: list[object],
    *,
    limit: int,
) -> list[Record]:
    """Ask for the arm's rows, widening the candidate list while short."""
    rows = await conn.fetch(sql, *params)
    if len(rows) < limit:
        ef_search = int(from_plain(await conn.fetchval("SHOW hnsw.ef_search"), str))
        while len(rows) < limit and ef_search < 1000:
            ef_search = min(4 * ef_search, 1000)
            await conn.execute(
                "SELECT set_config('hnsw.ef_search', $1, true)",
                str(ef_search),
            )
            rows = await conn.fetch(sql, *params)
    return rows
