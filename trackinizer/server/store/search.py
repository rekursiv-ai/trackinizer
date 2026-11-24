"""Search the session index: embeddings + scoped tsvector, RRF-merged.

The read half of the session-indexing design:
an embedding arm (an addon's, behind the ``vector_search`` slot) and
a full-text arm (``websearch_to_tsquery`` over ``session_records.search``) that
reciprocal-rank-fuse into one ranked list of hits, each addressed back to its
``(session_id, part, idx)`` so the console can open the session at that record.

Two arms, either or both:

- SEMANTIC -- the units nearest the query, which the addon finds under the
  reader's test core hands it (:data:`Kept`). A hit addresses the exact
  UNIT (``field``, ``chunk``) whose vector matched; its snippet is the head of
  the record's stored ``text`` (the vector table keeps no text, only its
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

:func:`search_inquiries` searches the inquiries the same way: an inquiry's text
(:data:`INQUIRY_TEXT_COLUMNS`) and the notes of the edges it starts from, each hit
addressed as ``(inquiry_id, field)``. Both arms apply the reader's inquiry read
filter, to a note's target as well as its owner.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

from treekle import from_plain

from trackinizer.server.access_policy import (
    AccessPolicy,
    Principal,
    inquiry_read_filter,
)
from trackinizer.server.audience import SessionReader, visible_sql
from trackinizer.server.notify import tx
from trackinizer.server.regex_timeout import apply_regex_statement_timeout
from trackinizer.server.store.session import SPAWN_EDGE_SQL
from trackinizer.server.store.session_feed import WHOLE_FEED, FeedScope
from trackinizer.server.values import manifest_bound, vetted_sql
from trackinizer.wire import wire_science_chat
from trackinizer.wire.audience import AUDIENCES


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.wire.wire_sessions import SearchMode


__all__ = [
    "INQUIRY_TEXT_COLUMNS",
    "InquirySearchHit",
    "Kept",
    "SessionSearchHit",
    "VectorHit",
    "VectorSearch",
    "search_inquiries",
    "search_sessions",
]


INQUIRY_TEXT_COLUMNS: Final = ("title", "description", "paper_abstract")
"""The ``inquiries`` columns an inquiry's text is made of: the ones the
``inquiries_stamp_change`` trigger watches and ``idx_inquiries_search`` covers
(schema.080), so the only columns the embedding add-on may embed."""


type Kept = Callable[[list[object]], str]
"""Appends its values to the query's and returns a test over ``v`` (``owner_id``,
``part``, ``idx``, ``field``) and ``i`` (the owner's ``inquiries`` row)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class VectorHit:
    """One unit the vector arm found, nearest first."""

    owner_id: UUID
    part: int
    idx: int
    field: str
    chunk: int


class VectorSearch(Protocol):
    """The vector arm of search, which an addon supplies."""

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
          engine: The server's database.
          query: The search text. Or an owner: then the query is its own newest
            unit that ``kept`` keeps, and no unit of its own is a hit.
          kept: The reader's and scope's test; it must be in the WHERE, before
            the LIMIT.
          filtered: ``kept`` may drop many candidates, so keep the index
            offering them until ``limit`` pass.
          limit: The most units to return.

        Returns:
          hits: Nearest first; ``None`` while the model is not loaded.

        """
        ...


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


async def search_sessions(
    engine: DatabaseEngine,
    *,
    query: str | UUID,
    spawned: str = "",
    vectors: VectorSearch | None,
    mode: SearchMode = "fused",
    limit: int = 20,
    scope: FeedScope = WHOLE_FEED,
) -> tuple[list[SessionSearchHit], bool]:
    """Search the session index, RRF-merging the arms that are present.

    Args:
      engine: The store's engine.
      query: The search text; its terms feed the FTS arm. Or a session: the
        records of other sessions nearest what it last said, found by its own
        vector, so by the semantic arm alone.
      spawned: The CLI's own id of a session the ``query`` session spawned, at
        any depth: the search is near that one instead. A CLI id alone names a
        session of each key that sent it, so it is read only under a session.
        With none such there are no hits, as for a session the reader does not
        read.
      vectors: The semantic arm, or ``None`` to skip it.
      mode: The arms to run: both, ``keyword`` alone, or ``vector`` alone.
      limit: Maximum merged hits to return, in ``[1, 200]``.
      scope: Whose sessions, as the feed names them, and who reads. Its record
        kinds are not searched by. The full-text arm keeps only the sessions the
        scope keeps and its reader reads before it ranks and cuts to ``limit``, so a
        hit on a hidden session neither appears nor takes the place of one the
        caller may see. The semantic arm does the same, though its HNSW index offers
        hidden units among the nearest: for a scope that may not keep every session
        it is told so (``filtered``), and keeps the index offering candidates until
        ``limit`` pass; so it is near a session, whose own units are the nearest
        and are all dropped. A scope that keeps every session is otherwise
        searched as it always was.

    Returns:
      hits: Merged, deduplicated hits ordered by descending score.
      searched: The semantic arm ran; ``False`` with none, or before its model
        loaded.

    Raises:
      ValueError: ``query`` is blank, or a session searched by ``keyword``, or
        words given with ``spawned``, or ``limit`` is outside ``[1, 200]``.

    """
    _refuse_malformed(query, limit=limit)
    if isinstance(query, UUID) and mode == "keyword":
        raise ValueError("a session is searched near by its vector, not by keyword")
    if isinstance(query, str) and spawned:
        raise ValueError("only a session spawned another: give one, not words")
    if isinstance(query, UUID) and spawned:
        # The nil id is no session's, so the arm answers as for one with no vector.
        query = await _spawned_below(engine, query, cli_session_id=spawned) or UUID(
            int=0,
        )
    scope = replace(scope, kinds=())  # pragma: no mutate[truthiness]
    nearest = (
        None
        if vectors is None or mode not in ("fused", "vector")
        else await vectors.nearest(
            engine,
            query=query,
            kept=partial(_kept_units, scope=scope)
            if isinstance(query, str)
            else partial(_kept_apart, scope=scope, session_id=query),
            # Near a session the test drops that session's own units, which are
            # the nearest of all: for any reader the index must keep offering.
            filtered=isinstance(query, UUID)
            or scope.names_sessions
            or not _reads_all(scope.reader),
            limit=limit,
        )
    )
    async with engine.acquire() as conn, tx(conn):
        await apply_regex_statement_timeout(conn)
        semantic = [] if nearest is None else await _semantic_arm(conn, nearest)
        fts = (
            await _fts_arm(conn, query, limit=limit, scope=scope)
            if isinstance(query, str) and mode in ("fused", "keyword")
            else []
        )
    return _merge(semantic, fts, limit=limit), nearest is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class InquirySearchHit:
    """One merged inquiry search hit: an inquiry's text, or a note of its edge.

    Attributes:
      inquiry_id: The inquiry the unit belongs to; an edge's ``from`` end.
      kind: Its kind.
      seq: Its number within the kind.
      title: Its title.
      field: ``"text"``, or ``"edge:<edge_kind>:<to_id>"`` for a note.
      score: The merged rank score, as :attr:`SessionSearchHit.score`.
      source: ``"semantic"``, ``"fts"`` or ``"both"``.
      snippet: ``ts_headline`` of the unit's text for the query.

    """

    inquiry_id: UUID
    kind: str
    seq: int
    title: str
    field: str
    score: float
    source: str
    snippet: str


async def search_inquiries(
    engine: DatabaseEngine,
    *,
    query: str,
    vectors: VectorSearch | None,
    policy: AccessPolicy | None,
    principal: Principal | None,
    sessions: SessionReader,
    mode: SearchMode = "fused",
    kind: str | None = None,
    limit: int = 20,
) -> tuple[list[InquirySearchHit], bool]:
    """Search inquiry texts and edge notes, RRF-merging the arms that are present.

    A session's own text is not searched (:func:`search_sessions` searches its
    transcript), but the notes of its edges are.

    Args:
      engine: The store's engine.
      query: The search text; its terms feed the keyword arm.
      vectors: The vector arm, or ``None`` to skip it.
      policy: The deployment's access policy; ``None`` reads every inquiry.
      principal: Who reads; ``None`` reads every inquiry.
      sessions: How the reader reads sessions: a note with a session at either end
        is read only by who reads that session.
      mode: The arms to run: both, ``keyword`` alone, or ``vector`` alone.
      kind: The one inquiry kind to search; ``None`` searches every kind.
      limit: Maximum merged hits to return, in ``[1, 200]``.

    Returns:
      hits: Merged hits ordered by descending score. Each arm keeps only the units
        the reader reads, a note only when both its ends are read, before it ranks
        and cuts to ``limit``.
      searched: The vector arm ran; ``False`` with none, or before its model loaded.

    Raises:
      ValueError: ``query`` is blank, or ``limit`` is outside ``[1, 200]``.

    """
    _refuse_malformed(query, limit=limit)
    reader = _InquiryReader(
        policy=policy,
        principal=principal,
        sessions=sessions,
        kind=kind,
    )
    nearest = (
        None
        if vectors is None or mode not in ("fused", "vector")
        else await vectors.nearest(
            engine,
            query=query,
            kept=reader.units,
            # Session units are most of the index, and this test drops every one.
            filtered=True,
            limit=limit,
        )
    )
    semantic = [(hit.owner_id, hit.field) for hit in nearest or []]
    async with engine.acquire() as conn, tx(conn):
        await apply_regex_statement_timeout(conn)
        keyword = (
            await _inquiry_keyword_arm(conn, query, limit=limit, reader=reader)
            if mode in ("fused", "keyword")
            else []
        )
        fused = _fuse(semantic, keyword, position=_itself, limit=limit)
        labels = await _inquiry_labels(conn, [unit for unit, _, _ in fused], query)
    return [
        InquirySearchHit(
            inquiry_id=unit[0],
            kind=labels[unit][0],
            seq=labels[unit][1],
            title=labels[unit][2],
            field=unit[1],
            score=score,
            source=source,
            snippet=labels[unit][3],
        )
        for unit, score, source in fused
        if unit in labels
    ], nearest is not None


_MAX_LIMIT: Final = 200

# The keyword surfaces, spelled as schema.080's indexes spell them so the planner reads
# them: an inquiry's text columns cut to 100,000 characters, whose tsvector stays
# under Postgres's 1 MB limit, and an edge's note.
_INQUIRY_TEXT: Final = vetted_sql(
    "left(",
    " || ' ' || ".join(f"coalesce(i.{column}, '')" for column in INQUIRY_TEXT_COLUMNS),
    ", 100000)",
)
_INQUIRY_KEYWORDS: Final = vetted_sql("to_tsvector('simple', ", _INQUIRY_TEXT, ")")
_NOTE_KEYWORDS: Final = "to_tsvector('simple', e.note)"


def _refuse_malformed(query: str | UUID, *, limit: int) -> None:
    """Raise ``ValueError`` for a limit outside ``[1, 200]`` or a blank query."""
    if limit < 1 or limit > _MAX_LIMIT:
        raise ValueError(f"limit must be in [1, {_MAX_LIMIT}]")
    if isinstance(query, str) and not query.strip():
        raise ValueError("query must not be blank")


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


# The snippet is the head of the record's stored ``text`` (the vector row keeps only
# ``text_md5``), joined back through ``session_records``. A unit whose record is gone
# since the addon read it is dropped.
# Audience: gated -- every unit passed the reader's test in the semantic arm.
async def _semantic_arm(conn: Conn, hits: list[VectorHit]) -> list[_ArmHit]:
    """Return the semantic arm's units as record hits, in its order."""
    rows = await conn.fetch(
        "SELECT r.session_id, r.part, r.idx, left(r.text, $4) AS snippet, "
        "i.title AS title FROM session_records r "
        "JOIN inquiries i ON i.id = r.session_id "
        "WHERE (r.session_id, r.part, r.idx) IN "
        "(SELECT * FROM unnest($1::uuid[], $2::int[], $3::int[]))",
        [hit.owner_id for hit in hits],
        [hit.part for hit in hits],
        [hit.idx for hit in hits],
        _SNIPPET_HEAD,
    )
    found = {
        (row["session_id"], row["part"], row["idx"]): (
            str(row["snippet"]),
            str(row["title"]),
        )
        for row in rows
    }
    return [
        _ArmHit(
            session_id=hit.owner_id,
            part=hit.part,
            idx=hit.idx,
            field=hit.field,
            chunk=hit.chunk,
            snippet=found[hit.owner_id, hit.part, hit.idx][0],
            title=found[hit.owner_id, hit.part, hit.idx][1],
        )
        for hit in hits
        if (hit.owner_id, hit.part, hit.idx) in found
    ]


# ``'simple'`` matches the stored ``to_tsvector('simple', text)`` column;
# ``websearch_to_tsquery`` gives users quoted-phrase / OR / negation syntax. An FTS hit
# is a whole record, so it carries no unit (``field=""``, ``chunk=0``) and a
# ``ts_headline`` snippet.
async def _fts_arm(
    conn: Conn,
    query_text: str,
    *,
    limit: int,
    scope: FeedScope,
) -> list[_ArmHit]:
    """Return records matching the scoped tsvector, ts_rank order, best first."""
    # Exclude stale tail rows beyond the live manifest prefix (see _kept_units).
    manifest_join, manifest_predicate = manifest_bound("r")
    params: list[object] = [query_text, limit]
    # Audience: gated -- in the WHERE, before the ``LIMIT``, so a hidden hit that ranks
    # higher does not take the place of one the reader may see.
    visible = _kept(params, scope=scope)
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


def _reads_all(reader: SessionReader) -> bool:
    """Say whether ``reader`` reads every session but another person's private chat."""
    return (
        reader.audiences >= AUDIENCES
        and reader.every_session
        and reader.oversight
        and reader.hidden
    )


def _kept(
    params: list[object],
    *,
    scope: FeedScope,
) -> str:
    """Return the test, on ``i``, that a session is in ``scope`` and its reader reads it."""
    return " AND ".join(
        (
            *scope.clauses(params, kind=""),
            visible_sql(
                "i",
                params=params,
                reader=scope.reader,
            ),
        ),
    )


# A session record's unit is its ``content``. Bind the manifest to the unit's record
# coordinates: a compaction-restart can leave a stale vector whose record is now beyond
# the live prefix, and the record's idx is what the bound applies to.
# The vector arm embeds this test inside a correlated scalar subquery over one owner
# row per candidate, so single-row chat lookup keeps Postgres from re-scanning every
# chat per candidate vector.
# Audience: internal -- a SQL fragment; the semantic arm that embeds it carries the gate.
def _kept_units(params: list[object], *, scope: FeedScope) -> str:
    """Return the semantic arm's test over ``v`` and ``i``: see :data:`Kept`."""
    return vetted_sql(
        "v.field = 'content' AND EXISTS (SELECT 1 FROM session_manifests m "
        "WHERE m.session_id = v.owner_id AND m.part = v.part AND v.idx < m.records) "
        "AND ",
        _kept(
            params,
            scope=scope,
        ),
    )


# A start links a spawned session ``produced_by`` its spawner (``sessions_routes``):
# each says much of what the other says, so near one the other is no find.
# Audience: internal -- a SQL fragment; the semantic arm that embeds it carries the gate.
def _kept_apart(params: list[object], *, scope: FeedScope, session_id: UUID) -> str:
    """Return :func:`_kept_units`' test, less what ``session_id`` spawned or was spawned by."""
    params.append(session_id)
    session = f"${len(params)}"
    return vetted_sql(
        "NOT EXISTS (SELECT 1 FROM edges e WHERE e.edge_kind = 'produced_by' "
        "AND ((e.from_id = v.owner_id AND e.to_id = ",
        session,
        ") OR (e.from_id = ",
        session,
        " AND e.to_id = v.owner_id))) AND ",
        _kept_units(params, scope=scope),
    )


# ``UNION`` drops a session seen before, so a cycle of edges ends the walk. The
# oldest, as ``SessionStore.spawned_session`` picks: a start re-opens that one, so it
# is the session an agent's records go to.
# Audience: internal -- it names a session and reads nothing of it; the semantic arm
# asked near that session applies the reader's test to it.
async def _spawned_below(
    engine: DatabaseEngine,
    root: UUID,
    *,
    cli_session_id: str,
) -> UUID | None:
    """Return the oldest session ``root``'s spawned, at any depth, under ``cli_session_id``."""
    async with engine.acquire() as conn:
        found = await conn.fetchval(
            vetted_sql(
                "WITH RECURSIVE down(id) AS ("
                "SELECT edges.from_id FROM edges WHERE edges.to_id = $1 AND ",
                SPAWN_EDGE_SQL,
                " UNION SELECT edges.from_id FROM edges "
                "JOIN down ON edges.to_id = down.id WHERE ",
                SPAWN_EDGE_SQL,
                ") SELECT child.id FROM down "
                "JOIN inquiries AS child ON child.id = down.id "
                "WHERE child.agentsession_cli_session_id = $2 "
                "ORDER BY child.seq LIMIT 1",
            ),
            root,
            cli_session_id,
            wire_science_chat.FORK_EDGE_LABEL,
        )
    return None if found is None else from_plain(found, UUID)


@dataclass(frozen=True, slots=True, kw_only=True)
class _InquiryReader:
    """Who searches the inquiries, and of which kind: the tests both arms apply.

    A session is no inquiry hit, but owns and is pointed to by edge notes; such a
    note is read only by who reads the session as session search would.
    """

    policy: AccessPolicy | None
    principal: Principal | None
    sessions: SessionReader
    kind: str | None

    def inquiry(self, params: list[object]) -> str:
        """Return the test, on ``i``, that a unit's owner is searched and is read.

        Args:
          params: The query's parameters; the test's own are appended.

        Returns:
          test: A SQL boolean over ``i``.

        """
        if self.kind is not None:
            params.append(self.kind)
        return vetted_sql(
            "" if self.kind is None else f"i.kind = ${len(params)} AND ",
            self.reads("i.id", params),
        )

    # Audience: gated -- a session end is read by the reader's transcript test.
    def reads(self, reference: str, params: list[object]) -> str:
        """Return the test that the inquiry ``reference`` names is read.

        Args:
          reference: A trusted SQL expression naming an inquiry id.
          params: The query's parameters; the test's own are appended.

        Returns:
          test: A SQL boolean: the policy's read filter, and a session's transcript
            test when the inquiry is a session.

        """
        return vetted_sql(
            inquiry_read_filter(
                reference,
                policy=self.policy,
                principal=self.principal,
                params=params,
            ),
            " AND EXISTS (SELECT 1 FROM inquiries s WHERE s.id = ",
            reference,
            " AND (s.kind <> 'AgentSession' OR ",
            visible_sql(
                "s",
                params=params,
                reader=self.sessions,
            ),
            "))",
        )

    # A note's target is the last part of its field; the text's field casts to NULL.
    # Audience: internal -- a SQL fragment; the vector arm that embeds it carries the
    # gate.
    def units(self, params: list[object]) -> str:
        """Return the vector arm's test over ``v`` and ``i``: see :data:`Kept`.

        Args:
          params: The query's parameters; the test's own are appended.

        Returns:
          test: A SQL boolean: an inquiry unit, read at its owner and, for a note,
            at its target; a session's text is never one.

        """
        return vetted_sql(
            "v.field <> 'content' AND ",
            self.inquiry(params),
            " AND CASE WHEN v.field = 'text' THEN i.kind <> 'AgentSession' ELSE ",
            self.reads("NULLIF(split_part(v.field, ':', 3), '')::uuid", params),
            " END",
        )


# Both surfaces in one ranking, each tested in the WHERE before the LIMIT: the
# inquiry, and a note's target as well as its owner.
async def _inquiry_keyword_arm(
    conn: Conn,
    query: str,
    *,
    limit: int,
    reader: _InquiryReader,
) -> list[tuple[UUID, str]]:
    """Return the inquiry units matching ``query``, ts_rank order, best first."""
    params: list[object] = [query, limit]
    rows = await conn.fetch(
        vetted_sql(
            "SELECT i.id AS owner_id, 'text' AS field, ts_rank(",
            _INQUIRY_KEYWORDS,
            ", q) AS rank FROM websearch_to_tsquery('simple', $1) q "
            "CROSS JOIN inquiries i WHERE ",
            _INQUIRY_KEYWORDS,
            " @@ q AND i.kind <> 'AgentSession' AND ",
            reader.inquiry(params),
            " UNION ALL SELECT e.from_id, 'edge:' || e.edge_kind || ':' || e.to_id, "
            "ts_rank(",
            _NOTE_KEYWORDS,
            ", q) FROM websearch_to_tsquery('simple', $1) q CROSS JOIN edges e "
            "JOIN inquiries i ON i.id = e.from_id WHERE e.note IS NOT NULL AND ",
            _NOTE_KEYWORDS,
            " @@ q AND ",
            reader.inquiry(params),
            " AND ",
            reader.reads("e.to_id", params),
            " ORDER BY rank DESC, owner_id, field LIMIT $2",
        ),
        *params,
    )
    return [(from_plain(row["owner_id"], UUID), str(row["field"])) for row in rows]


# A vector hit's text is read here, after the merge, and so is every hit's label; a
# unit gone since its arm read it is dropped.
# Audience: gated -- every unit passed the reader's test in its arm.
async def _inquiry_labels(
    conn: Conn,
    units: Sequence[tuple[UUID, str]],
    query: str,
) -> dict[tuple[UUID, str], tuple[str, int, str, str]]:
    """Return each unit's inquiry kind, seq and title, and its snippet."""
    rows = await conn.fetch(
        vetted_sql(
            "SELECT h.owner_id, h.field, i.kind, i.seq, i.title, "
            "ts_headline('simple', CASE WHEN h.field = 'text' THEN ",
            _INQUIRY_TEXT,
            " ELSE e.note END, q) AS snippet "
            "FROM unnest($1::uuid[], $2::text[]) AS h(owner_id, field) "
            "CROSS JOIN websearch_to_tsquery('simple', $3) q "
            "JOIN inquiries i ON i.id = h.owner_id "
            "LEFT JOIN edges e ON e.from_id = h.owner_id "
            "AND 'edge:' || e.edge_kind || ':' || e.to_id = h.field "
            "WHERE h.field = 'text' OR e.note IS NOT NULL",
        ),
        [owner_id for owner_id, _field in units],
        [field for _owner_id, field in units],
        query,
    )
    return {
        (from_plain(row["owner_id"], UUID), str(row["field"])): (
            str(row["kind"]),
            from_plain(row["seq"], int),
            str(row["title"]),
            str(row["snippet"]),
        )
        for row in rows
    }


def _itself[T](unit: T) -> T:
    """Return ``unit``: an inquiry unit is its own position."""
    return unit


# RRF over the two arms: a hit's score is ``sum 1/(RRF_K + rank)`` across the arms it
# appears in (rank 1-based, in each arm's own order). Dedup is on the position, keeping
# the hit that ranked best within its arm, the semantic arm's first -- several chunk
# units of one record collapse to the one that matched.
def _fuse[H, P: (tuple[UUID, int, int], tuple[UUID, str])](
    semantic: Sequence[H],
    fts: Sequence[H],
    *,
    position: Callable[[H], P],
    limit: int,
) -> list[tuple[H, float, str]]:
    """Reciprocal-rank-fuse the arms; dedup on position; top ``limit``, best first."""
    scores: dict[P, float] = {}
    best: dict[P, H] = {}
    sources: dict[P, set[str]] = {}
    for source, arm in (("semantic", semantic), ("fts", fts)):
        seen: set[P] = set()
        for rank, hit in enumerate(arm, start=1):
            at = position(hit)
            # First occurrence in this arm is the position's best rank here, so only
            # it contributes and only it may claim the representative hit.
            if at in seen:
                continue
            seen.add(at)
            scores[at] = scores.get(at, 0.0) + 1.0 / (RRF_K + rank)
            sources.setdefault(at, set()).add(source)
            best.setdefault(at, hit)
    # Descending score; the position breaks a tie, so the order is deterministic.
    ranked = sorted(scores, key=lambda at: (-scores[at], at))[:limit]
    return [
        (
            best[at],
            scores[at],
            "both" if len(sources[at]) == 2 else next(iter(sources[at])),
        )
        for at in ranked
    ]


def _merge(
    semantic: list[_ArmHit],
    fts: list[_ArmHit],
    *,
    limit: int,
) -> list[SessionSearchHit]:
    """Reciprocal-rank-fuse the arms; dedup on record position; top ``limit``."""
    return [
        SessionSearchHit(
            session_id=hit.session_id,
            part=hit.part,
            idx=hit.idx,
            field=hit.field,
            chunk=hit.chunk,
            score=score,
            source=source,
            snippet=hit.snippet,
            title=hit.title,
        )
        for hit, score, source in _fuse(
            semantic,
            fts,
            position=_record_position,
            limit=limit,
        )
    ]


def _record_position(hit: _ArmHit) -> tuple[UUID, int, int]:
    """Return the record a session hit is at."""
    return (hit.session_id, hit.part, hit.idx)
