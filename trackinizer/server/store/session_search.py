"""Search the session index: embeddings + scoped tsvector, RRF-merged.

The read half of the session-indexing design:
an embedding arm (cosine over ``session_embeddings``, HNSW) and
a full-text arm (``websearch_to_tsquery`` over ``session_records.search``) that
reciprocal-rank-fuse into one ranked list of hits, each addressed back to its
``(session_id, part, idx)`` so the console can open the session at that record.

Two arms, either or both:

- SEMANTIC -- ``embedding <=> $query`` cosine distance over the
  ``(mapper, model)`` slice, top-k, HNSW-served. A hit addresses the exact
  UNIT (``field``, ``chunk``) whose vector matched; its snippet is the head of
  the record's stored ``text`` (``session_embeddings`` keeps no text, only its
  md5, so the snippet joins back to ``session_records``).
- FTS -- ``search @@ websearch_to_tsquery('simple', $text)`` over the scoped
  tsvector, ranked by ``ts_rank``, top-k. The config MUST be ``'simple'`` --
  the stored column is ``to_tsvector('simple', text)`` (schema.019), and a
  mismatched config lexes differently and matches nothing. An FTS hit is a
  whole record (no unit), so it carries ``field=""``, ``chunk=0`` and a
  ``ts_headline`` snippet.

Merge: when both arms run, fuse by Reciprocal Rank Fusion (score
``sum 1/(RRF_K + rank)`` over the arms a hit appears in, rank 1-based), which
needs only per-arm ORDER, not comparable raw scores -- a cosine distance and a
``ts_rank`` are not on one scale. A hit in both arms outranks a same-rank hit
in one. A single arm passes through on its own rank. Dedup is on
``(session_id, part, idx)``: several units of one record may match the semantic
arm, and only the best-ranked unit represents the record in the merged list.

This is the STORE layer only: the query embedding (QwenEmbedder query-side,
WITH the ``Instruct:`` prefix -- the asymmetry the model card documents) and
the API route are the next task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID
from weakref import WeakKeyDictionary

import re

from treekle import from_plain

from trackinizer.server.audience import OPEN_ONLY, visible_sql
from trackinizer.server.notify import tx
from trackinizer.server.values import manifest_bound, vec_to_text, vetted_sql
from trackinizer.wire.audience import AUDIENCES


if TYPE_CHECKING:
    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine


__all__ = [
    "SessionSearchHit",
    "search_session_records",
]


# The standard RRF constant (Cormack et al. 2009): large enough that the top
# ranks do not dominate, small enough that deep ranks still separate. 60 is the
# value the literature and every mainstream hybrid-search implementation use.
RRF_K: Final = 60

# Snippet head for a semantic hit, whose source text is not stored on the
# embedding row. Matches the console's collapsed-result width; the full text is
# a record read away.
_SNIPPET_HEAD: Final = 200


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionSearchHit:
    """One merged search hit, addressed to its record position.

    Attributes:
      session_id: The owning AgentSession.
      part: Which file of the session.
      idx: The record's position within the part -- where the console opens.
      field: The matched unit's field (``"content"`` / ``"head"`` for a
        semantic hit; ``""`` for an FTS hit, which matches a whole record).
      chunk: The matched unit's chunk ordinal (``0`` for FTS).
      score: The merged rank score -- RRF when both arms ran, otherwise the
        single arm's own ``1/(RRF_K + rank)``. Higher ranks first.
      source: Which arm(s) produced the hit: ``"semantic"``, ``"fts"``, or
        ``"both"``.
      snippet: A short preview -- ``ts_headline`` for an FTS hit, the head of
        the record's text for a semantic hit.
      title: The owning AgentSession's title, so the console can label the hit
        without a second lookup.

    """

    session_id: UUID
    part: int
    idx: int
    field: str
    chunk: int
    score: float
    source: str
    snippet: str
    title: str


async def search_session_records(
    engine: DatabaseEngine,
    *,
    query_vector: list[float] | None,
    query_text: str,
    mapper: str,
    model: str,
    dim: int,
    limit: int = 20,
    audiences: frozenset[str] = OPEN_ONLY,
) -> list[SessionSearchHit]:
    """Search the session index, RRF-merging the arms that are present.

    Args:
      engine: The store's engine; one connection for the search.
      query_vector: The query embedding for the semantic arm, or ``None`` to
        skip it. Its length must equal ``dim`` when present.
      query_text: The full-text query for the FTS arm; ``""`` skips it.
      mapper: The ``SemanticMapper`` policy name keying the embedding slice.
      model: The embedder name keying the embedding slice.
      dim: The selected embedder's stored dimension. The cosine arm casts both
        the column and the query to ``halfvec(dim)`` so it hits ``model``'s
        partial HNSW index (a bare ``halfvec`` compare over the dimension-free
        column seqscans 4.7M rows); the cast MUST match the index's cast.
      limit: Maximum merged hits to return, in ``[1, 200]``.
      audiences: What the caller reads. The full-text arm keeps only the sessions in
        it before it ranks and cuts to ``limit``, so a hit on a hidden session neither
        appears nor takes the place of one the caller may see. The semantic arm does
        the same, though its HNSW index offers hidden units among the nearest: for a
        caller that lacks an audience it has the index keep offering candidates until
        ``limit`` pass, by an iterative scan where pgvector has them (0.8.0 and later)
        and by widening the list otherwise. A caller that reads every audience is
        searched as it always was. The shortfall that remains is where the index
        ends: ``hnsw.max_scan_tuples``, 20,000 index tuples by default, with iterative
        scans, and 1,000 candidates without.

    Returns:
      hits: Merged, deduplicated hits ordered by descending score.

    Raises:
      ValueError: Neither arm is present (``query_vector is None`` and
        ``query_text`` is blank), ``limit`` is outside ``[1, 200]``, or
        ``query_vector``'s length disagrees with ``dim``.

    """
    if limit < 1 or limit > _MAX_LIMIT:
        raise ValueError(f"limit must be in [1, {_MAX_LIMIT}]")
    if dim < 1:
        raise ValueError(f"dim must be positive, got {dim}")
    has_text = bool(query_text.strip())
    if query_vector is None and not has_text:
        raise ValueError("search needs at least one arm: a query_vector or query_text")
    if query_vector is not None and len(query_vector) != dim:
        raise ValueError(
            f"query_vector has {len(query_vector)} dims, expected {dim}",
        )
    async with engine.acquire() as conn:
        semantic = (
            await _semantic_arm(
                conn,
                query_vector,
                mapper=mapper,
                model=model,
                dim=dim,
                limit=limit,
                audiences=audiences,
                scan=await _scan_for(engine, conn, audiences),
            )
            if query_vector is not None
            else []
        )
        fts = (
            await _fts_arm(conn, query_text, limit=limit, audiences=audiences)
            if has_text
            else []
        )
    return _merge(semantic, fts, limit=limit)


_MAX_LIMIT: Final = 200

# Whether each engine's pgvector scans iteratively (0.8.0 and later). It is read from
# the database when a reader that lacks an audience first searches by meaning, and then
# kept: the answer changes only with an extension upgrade, which a restart follows. The
# engines are held weakly, so a closed one takes its entry with it.
_ITERATIVE_SCAN: Final[WeakKeyDictionary[DatabaseEngine, bool]] = WeakKeyDictionary()

# How the semantic arm keeps ``limit`` hits against the audiences its reader lacks: not
# at all, for a reader that lacks none; by an iterative index scan; or, where pgvector
# has none, by widening the list of candidates the index offers.
type _Scan = Literal["plain", "iterative", "widening"]


# A reader of every audience loses no candidate to the audience test, so it is scanned
# as it always was and the database is asked nothing more for it.
async def _scan_for(
    engine: DatabaseEngine,
    conn: Conn,
    audiences: frozenset[str],
) -> _Scan:
    """Choose how the semantic arm keeps ``limit`` hits against ``audiences``."""
    if audiences >= AUDIENCES:
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


@dataclass(frozen=True, slots=True, kw_only=True)
class _ArmHit:
    """One arm's ranked hit before merging: a record position plus its unit."""

    session_id: UUID
    part: int
    idx: int
    field: str
    chunk: int
    snippet: str
    title: str


# The snippet is the head of the record's stored ``text`` (the embedding row keeps only
# ``text_md5``), joined back through ``session_records``.
# The distance expression casts BOTH the dimension-free column and the query to
# ``halfvec(dim)`` so it matches ``model``'s partial HNSW index
# ``(embedding::halfvec(dim)) ... WHERE model = ...``; without the matching cast the
# planner cannot use the partial and seqscans. ``dim`` is a validated positive int
# (never user text), so its interpolation is injection-safe -- a bound parameter cannot
# carry a type modifier.
async def _semantic_arm(
    conn: Conn,
    query_vector: list[float],
    *,
    mapper: str,
    model: str,
    dim: int,
    limit: int,
    audiences: frozenset[str],
    scan: _Scan,
) -> list[_ArmHit]:
    """Cosine-nearest units in the ``(mapper, model)`` slice, best first."""
    # Bind the manifest to the RECORD coords (r), not the embedding row: a
    # compaction-restart can leave a stale embedding whose record is now beyond
    # the live prefix, and the record's idx is what the bound applies to.
    manifest_join, manifest_predicate = manifest_bound("r")
    params: list[object] = [
        vec_to_text(query_vector),
        mapper,
        model,
        limit,
        _SNIPPET_HEAD,
    ]
    # Audience: gated -- in the WHERE, which the HNSW index does not see: it offers its
    # ``hnsw.ef_search`` nearest units of every audience and the filter drops the hidden
    # ones afterwards. A reader that lacks an audience would therefore get fewer than
    # ``limit`` hits, or none, when more hidden units than that sit nearest its query,
    # and the shortfall would tell it that they do. ``scan`` has the index keep offering
    # candidates until ``limit`` of them pass the filter (see ``_nearest``). The FTS arm
    # needs none: it ranks every match before it cuts.
    visible = visible_sql("i.agentsession_audience", params=params, audiences=audiences)
    sql = (
        f"SELECT e.session_id, e.part, e.idx, e.field, e.chunk, "  # noqa: S608 -- ``dim`` is a validated int; a typmod cannot be a bound parameter.
        f"left(r.text, $5) AS snippet, i.title AS title "
        f"FROM session_embeddings e "
        f"JOIN session_records r "
        f"  ON r.session_id = e.session_id AND r.part = e.part AND r.idx = e.idx "
        f"JOIN inquiries i ON i.id = e.session_id "
        f"{manifest_join}"
        f"WHERE e.mapper = $2 AND e.model = $3 AND {manifest_predicate} AND {visible} "
        f"ORDER BY (e.embedding::halfvec({dim})) <=> $1::halfvec({dim}) "
        f"LIMIT $4"
    )
    rows = await _nearest(conn, sql, params, limit=limit, scan=scan)
    return [_semantic_hit(row) for row in rows]


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
    """Run the semantic query, with the index kept offering candidates as ``scan`` says."""
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
    """Ask for the semantic query's rows, widening the candidate list while short."""
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


# ``'simple'`` matches the stored ``to_tsvector('simple', text)`` column;
# ``websearch_to_tsquery`` gives users quoted-phrase / OR / negation syntax. An FTS hit
# is a whole record, so it carries no unit (``field=""``, ``chunk=0``) and a
# ``ts_headline`` snippet.
async def _fts_arm(
    conn: Conn,
    query_text: str,
    *,
    limit: int,
    audiences: frozenset[str],
) -> list[_ArmHit]:
    """Return records matching the scoped tsvector, ts_rank order, best first."""
    # Exclude stale tail rows beyond the live manifest prefix (see _semantic_arm).
    manifest_join, manifest_predicate = manifest_bound("r")
    params: list[object] = [query_text, limit]
    # Audience: gated -- in the WHERE, before the ``LIMIT``, so a hidden hit that ranks
    # higher does not take the place of one the reader may see.
    visible = visible_sql("i.agentsession_audience", params=params, audiences=audiences)
    rows = await conn.fetch(
        vetted_sql(
            "SELECT r.session_id, r.part, r.idx, "
            "ts_headline('simple', r.text, q) AS snippet, i.title AS title "
            "FROM websearch_to_tsquery('simple', $1) q "
            "CROSS JOIN session_records r "
            "JOIN inquiries i ON i.id = r.session_id ",
            manifest_join,
            "WHERE r.search @@ q AND ",
            manifest_predicate,
            " AND ",
            visible,
            " ORDER BY ts_rank(r.search, q) DESC, r.session_id, r.part, r.idx LIMIT $2",
        ),
        *params,
    )
    hits: list[_ArmHit] = []
    for row in rows:
        session_id = row["session_id"]
        part = row["part"]
        idx = row["idx"]
        snippet = row["snippet"]
        title = row["title"]
        assert isinstance(session_id, UUID)
        assert isinstance(part, int)
        assert isinstance(idx, int)
        assert isinstance(snippet, str)
        assert isinstance(title, str)
        hits.append(
            _ArmHit(
                session_id=session_id,
                part=part,
                idx=idx,
                field="",
                chunk=0,
                snippet=snippet,
                title=title,
            ),
        )
    return hits


def _semantic_hit(row: Record) -> _ArmHit:
    """Narrow one semantic-arm row into an ``_ArmHit``."""
    session_id = row["session_id"]
    part = row["part"]
    idx = row["idx"]
    field = row["field"]
    chunk = row["chunk"]
    snippet = row["snippet"]
    title = row["title"]
    assert isinstance(session_id, UUID)
    assert isinstance(part, int)
    assert isinstance(idx, int)
    assert isinstance(field, str)
    assert isinstance(chunk, int)
    assert isinstance(snippet, str)
    assert isinstance(title, str)
    return _ArmHit(
        session_id=session_id,
        part=part,
        idx=idx,
        field=field,
        chunk=chunk,
        snippet=snippet,
        title=title,
    )


# RRF over the two arms: a hit's score is ``sum 1/(RRF_K + rank)`` across the
# arms it appears in (rank 1-based, in each arm's own order). Dedup is on the
# record position, keeping the unit that ranked best within its arm -- several
# chunk units of one record collapse to the one that matched.
def _merge(
    semantic: list[_ArmHit],
    fts: list[_ArmHit],
    *,
    limit: int,
) -> list[SessionSearchHit]:
    """Reciprocal-rank-fuse the arms; dedup on position; top ``limit``."""
    scores: dict[tuple[UUID, int, int], float] = {}
    best_unit: dict[tuple[UUID, int, int], _ArmHit] = {}
    sources: dict[tuple[UUID, int, int], set[str]] = {}
    for source, arm in (("semantic", semantic), ("fts", fts)):
        seen: set[tuple[UUID, int, int]] = set()
        for rank, hit in enumerate(arm, start=1):
            position = (hit.session_id, hit.part, hit.idx)
            # First occurrence in this arm is the record's best rank here, so
            # only it contributes and only it may claim the representative unit.
            if position in seen:
                continue
            seen.add(position)
            scores[position] = scores.get(position, 0.0) + 1.0 / (RRF_K + rank)
            sources.setdefault(position, set()).add(source)
            best_unit.setdefault(position, hit)
    merged = [
        SessionSearchHit(
            session_id=position[0],
            part=position[1],
            idx=position[2],
            field=best_unit[position].field,
            chunk=best_unit[position].chunk,
            score=score,
            source=(
                "both" if len(sources[position]) == 2 else next(iter(sources[position]))
            ),
            snippet=best_unit[position].snippet,
            title=best_unit[position].title,
        )
        for position, score in scores.items()
    ]
    # Descending score; position tie-break keeps the order deterministic when
    # two records fused to the same score.
    merged.sort(key=lambda h: (-h.score, h.session_id, h.part, h.idx))
    return merged[:limit]
