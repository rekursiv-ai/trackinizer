"""The web app's routes and read endpoints for the trackinizer FastAPI app.

Mounted by ``trackinizer --web`` (or programmatically via :func:`attach`).
Adds:

- ``GET /app/*`` -- a separately built web app, for the callers the API answers
  (``--app-dir``). With it, ``/`` answers 302 to ``/app/``, and ``/me``,
  ``/admin``, ``/graph`` and ``/console`` lead into the app likewise.
- ``GET /auth/login_page`` -- the sign-in page (``assets/login.html``).
- ``GET /static/*`` -- files from ``--static-dir``, to anyone.
- ``GET /api/web/search`` -- cross-kind ILIKE search over title/description.
- ``GET /api/web/recent_changes`` -- the most-recent ``change_log`` rows
  with their snapshots flattened to JSON.
- ``GET /api/web/lookup/{target_id}`` -- resolve a UUID to its kind.
- ``GET /api/web/get/{target_id}`` -- one inquiry with edges + backlinks +
  recent changes attached, for the web app's detail view.
- ``GET /api/web/feed`` -- every agent session's records interleaved, for the
  console, with its counts by session, room and kind (``/feed/facets``) and per
  time bucket (``/feed/histogram``).

All write verbs live on :mod:`trackinizer`'s :class:`Store` and its
``/api/*`` routes (every mutation takes an explicit ``actor`` argument
that lands on :attr:`Change.actor` alongside the server-stamped
``api_key_id``); there is no separate "curator" class in this
design.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Final,
    Literal,
    Protocol,
    cast,
)
from urllib.parse import quote
from uuid import UUID
from weakref import WeakSet

import asyncio
import math
import re
import shlex
import time
import uuid

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from trackinizer.lib.absent import ABSENT
from trackinizer.lib.codec import from_plain
from trackinizer.server.api._deps import get_store, tag_row
from trackinizer.server.api._regex_guard import regex_failures_as_400
from trackinizer.server.api._routes_shared import parse_fields
from trackinizer.server.auth import (
    AuthIdentity,
    current_user,
    require_role,
)
from trackinizer.server.config import Config, ConfigError
from trackinizer.server.embedders import registry
from trackinizer.server.notify import iter_sse_events, tx
from trackinizer.server.regex_timeout import apply_regex_statement_timeout
from trackinizer.server.semantic_mapper_footprint import FootprintMapper
from trackinizer.server.store.graph_focus import read_neighbourhood
from trackinizer.server.store.session_feed import WHOLE_FEED, FeedScope
from trackinizer.server.store.session_search import (
    SessionSearchHit,
    search_session_records,
)
from trackinizer.server.values import vetted_sql
from trackinizer.types.change_log import Snapshot
from trackinizer.types.inquiries import KIND_TO_CLASS, Inquiry
from trackinizer.wire.json_types import UtcDatetime
from trackinizer.wire.routes import MAX_LIST_LIMIT, inquiry_relation_fields
from trackinizer.wire.wire_sessions import (
    FeedCursor,
    FeedFacetsResponse,
    FeedHistogramResponse,
    FeedResponse,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

    import asyncpg

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.store.core import Store
    from trackinizer.types.embedder import QueryEmbedder


_CWD: Final = Path(__file__).resolve().parent


type WebView = dict[str, object]


router = APIRouter()


_SESSION_SEARCH_MAX_LIMIT: Final = 200
_SESSION_SEARCH_MAPPER: Final = FootprintMapper().name


# See ``_AppRoute``: shared caches must never store an /app/ response.
_APP_CACHE_CONTROL: Final = "private, no-cache"

# See ``_AppRoute``: a built file under ``assets/`` never changes under one URL.
_ASSET_CACHE_CONTROL: Final = "private, max-age=31536000, immutable"


# -- Read routes -------------------------------------------------------------


@router.get("/search")
async def web_search(
    request: Request,
    q: str,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    kind: Inquiry.InquiryKind | None = None,
    limit: int = 50,
    *,
    fields: Annotated[list[str] | None, Query(max_length=MAX_LIST_LIMIT)] = None,
) -> list[WebView]:
    """Cross-kind search.

    Grammar: whitespace separates terms and every term must match. A bare term
    matches ``title`` or ``description`` as a case-insensitive substring;
    ``title:RE`` / ``description:RE`` match a case-insensitive regex. Only ``"``
    groups a phrase: ``'`` and backslashes are ordinary characters.

    ``fields`` names the keys each match keeps, as on ``GET /api/inquiries``.

    Args:
      request: FastAPI request object for middleware access.
      q: Bare-token and field-scoped regex search string.
      identity: Authenticated user, validated to have viewer role.
      kind: Filter to one inquiry kind; None means search all kinds.
      limit: Maximum results, 1-1000.
      fields: Row keys to send, one per repeated param; unset sends every key.

    Returns:
      matches: Matching inquiries, each as ``self`` in ``/get`` renders it.

    """
    del identity
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be in [1, 1000]")
    names = parse_fields(fields)
    try:
        terms = _parse_query(q)
        clause, params = _build_term_clause(terms)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not terms:
        return []
    if kind is not None:
        params.append(kind)
        kind_clause = f" AND kind = ${len(params)}"
    else:
        kind_clause = ""
    params.append(limit)
    sql = vetted_sql(
        "SELECT * FROM inquiries WHERE (",
        clause,
        ")",
        kind_clause,
        " ORDER BY created DESC, id DESC LIMIT $",
        str(len(params)),
    )
    async with get_store(request).engine.acquire() as conn, tx(conn):
        # A field-scoped ``~*`` regex runs POSIX-side in Postgres, where a
        # pathological pattern (catastrophic backtracking) can pin the backend
        # for an unbounded time. Python ``re.compile`` only validates syntax,
        # not runtime cost, so a viewer could otherwise DoS the cluster with one
        # query. ``SET LOCAL statement_timeout`` (transaction-scoped, hence the
        # ``tx``) caps each search; a regex that exceeds it is aborted as a 400
        # rather than holding the connection. The bound is a server constant,
        # never client input, so it interpolates safely.
        await apply_regex_statement_timeout(conn)
        # ``_build_term_clause`` validates the regex with Python's ``re``, but
        # Postgres ``~*`` runs POSIX: patterns valid in Python and not POSIX
        # (``(?P<name>...)``, ``\z``, ...) trip the engine at query time, and
        # a pattern too expensive to finish trips the timeout above. Both are
        # the caller's mistake, so both are 400s.
        #
        # Shared with ``/api/inquiries`` rather than repeated here: the local
        # copy this replaced caught ``PostgresSyntaxError``, which an invalid
        # regex never raises (it is SQLSTATE 2201B), so it silently did
        # nothing while reading as though it worked.
        with regex_failures_as_400():
            rows = await conn.fetch(sql, *params)
    matches = [_row_to_dict(r) for r in rows]
    if names is None:
        return matches
    return [{k: v for k, v in m.items() if k in names} for m in matches]


@router.get("/search_sessions")
async def web_search_sessions(
    request: Request,
    q: str,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    semantic: bool = True,
    limit: int = 20,
    model: str = "",
    dim: int | None = None,
) -> WebView:
    """Hybrid session search: embeddings + scoped tsvector, RRF-merged.

    Runs the full-text arm always, and the semantic arm only when ``semantic``
    is requested AND a session embedder is available. With no embedder the route
    degrades to full-text-only rather than 500 -- a keyword search must never
    trigger a model load -- and flags the degrade in the response
    (``degraded: true``, ``semantic: false``). The query embedding uses the
    embedder's query-side prefix; the corpus stayed prefix-free at ingest.

    Args:
      request: FastAPI request object for middleware access.
      q: The search query. Full-text terms feed ``websearch_to_tsquery``.
      identity: Authenticated user, validated to have viewer role.
      semantic: Request the embedding arm (default true); ignored when no
        session embedder is available.
      limit: Maximum merged hits, 1-200.
      model: Optional A/B override -- a bare slug or full ``slug@dim`` of an
        embedder to query INSTEAD of the serving default (its rows must already
        be swept). Unknown names are a 400. Empty uses the process default.
      dim: Optional dim override paired with ``model`` (a Matryoshka model
        accepts any dim in its range); must agree with any ``@dim`` in ``model``.

    Returns:
      body: ``{"hits": [...], "semantic": bool, "degraded": bool}`` -- each hit
        carries ``session_id``/``part``/``idx`` (where the console opens),
        ``title``, ``field``/``chunk``, ``score``, ``source``, ``snippet``.

    """
    del identity
    if limit < 1 or limit > _SESSION_SEARCH_MAX_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be in [1, {_SESSION_SEARCH_MAX_LIMIT}]",
        )
    if not q.strip():
        raise HTTPException(status_code=400, detail="q must be non-empty")
    embedder = _search_embedder(request, semantic=semantic, model=model, dim=dim)
    query_vector = await embedder.embed_query(q) if embedder is not None else None
    degraded = semantic and embedder is None
    hits = await search_session_records(
        get_store(request).engine,
        query_vector=query_vector,
        query_text=q,
        mapper=_SESSION_SEARCH_MAPPER,
        model=embedder.name if embedder is not None else "",
        # The cast dim comes from the SELECTED embedder (override or default), so
        # the cosine arm hits that model's partial index. Unused when the arm is
        # skipped; 1 is a valid placeholder the query never reaches.
        dim=embedder.dim if embedder is not None else 1,
        limit=limit,
    )
    return {
        "hits": [_session_hit_to_dict(hit) for hit in hits],
        "semantic": query_vector is not None,
        "degraded": degraded,
    }


@router.get("/recent_changes")
async def web_recent_changes(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    limit: int = 50,
) -> list[WebView]:
    """Most-recent ``change_log`` rows, snapshot deltas flattened.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user, validated to have viewer role.
      limit: Maximum results, 1-1000.

    Returns:
      changes: Change log deltas, newest first.

    """
    del identity
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be in [1, 1000]")
    async with get_store(request).engine.acquire() as conn:
        rows = await conn.fetch(
            _CHANGE_SELECT + " ORDER BY c.created DESC, c.id DESC LIMIT $1",
            limit,
        )
    return [_change_to_dict(r) for r in rows]


@router.get("/lookup/{target_id}")
async def web_lookup(
    target_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WebView:
    """Resolve a UUID to its kind; 404 when not found.

    Args:
      target_id: UUID to look up.
      request: FastAPI request object for middleware access.
      identity: Authenticated user, validated to have viewer role.

    Returns:
      view: Lightweight node with kind and id.

    """
    del identity
    async with get_store(request).engine.acquire() as conn:
        kind = await conn.fetchval(
            "SELECT kind FROM inquiries WHERE id = $1",
            target_id,
        )
    if kind is None:
        raise HTTPException(status_code=404, detail="id not found")
    return {"kind": kind, "id": str(target_id)}


@router.get("/get/{target_id}")
async def web_get(
    target_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WebView:
    """Single inquiry + edges + backlinks + recent changes, for the SPA.

    ``self`` is the inquiry as ``GET /api/inquiries/{id}`` serves it, unset fields
    ``null``, less the relation fields: ``edges`` and ``backlinks`` carry those,
    grouped by edge kind with each peer's title and status. ``changes`` holds the
    latest 50, newest first.

    Args:
      target_id: UUID of the node to fetch.
      request: FastAPI request object for middleware access.
      identity: Authenticated user, validated to have viewer role.

    Returns:
      view: ``self``, ``edges``, ``backlinks`` and ``changes``.

    """
    del identity
    store = get_store(request)
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM inquiries WHERE id = $1",
            target_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        edges = await _edges_for(conn, target_id, direction="outbound")
        backlinks = await _edges_for(conn, target_id, direction="inbound")
        changes = await conn.fetch(
            _CHANGE_SELECT + " WHERE c.subject_id = $1"
            " ORDER BY c.created DESC, c.id DESC LIMIT 50",
            target_id,
        )
    return {
        "self": _row_to_dict(row),
        "locked": row["locked"],
        "edges": edges,
        "backlinks": backlinks,
        "changes": [_change_to_dict(c) for c in changes],
    }


_GRAPH_NODE_COLS: Final = (
    "id, kind, seq, title, status, created, belief_judgement, belief_confidence"
)

# The ids ``/graph`` returns: the newest ``$1`` nodes, each followed by the older nodes
# it shares an edge with, cut at ``$1``. A node ranks by the newest ``created`` among
# itself and the recent nodes it shares an edge with, a tie going to the newer node,
# so a cited paper sorts right behind the newest belief citing it.
#
# Neighbours count inside ``limit`` because it is the one bound the caller sets:
# closing the newest ``limit`` over every neighbour answered ``limit=1`` with 5,002
# nodes (R3-02). Neo4j Browser bounds its view the same way: ``initialNodeDisplay``
# caps every node drawn, and it draws only relationships between drawn nodes.
_GRAPH_KEPT_IDS: Final = (
    "WITH recent AS ("
    " SELECT id, created FROM inquiries ORDER BY created DESC, id DESC LIMIT $1"
    "), reached AS ("
    " SELECT id, created AS seen FROM recent"
    " UNION ALL SELECT e.to_id, r.created FROM edges e"
    " JOIN recent r ON e.from_id = r.id"
    " UNION ALL SELECT e.from_id, r.created FROM edges e"
    " JOIN recent r ON e.to_id = r.id"
    ") SELECT r.id FROM reached r JOIN inquiries i ON i.id = r.id"
    " GROUP BY r.id, i.created"
    " ORDER BY max(r.seen) DESC, i.created DESC, r.id DESC LIMIT $1"
)


@router.get("/graph")
async def web_graph(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    limit: int | None = None,
    focus: UUID | None = None,
    hops: int = 2,
) -> WebView:
    """Return the inquiry graph as typed nodes + directed edges, for the SPA.

    At most ``limit`` nodes, at least 1, and the edges between them. The newest
    inquiries come first, each followed by the older ones it links to in either
    direction, so an old-but-still-cited node (e.g. a foundational paper a new
    belief proves) stays in view; selection stops at ``limit``. Every edge joins
    two returned nodes.

    With ``focus``, the nodes are instead the focus and the inquiries nearest it
    (:func:`read_neighbourhood`): all within one edge, in either direction, before
    any two away, and so on to ``hops``, newest first within a hop, until
    ``limit``. Each node then carries ``hops``, its distance from the focus.

    Nodes are the light projection (id, kind, seq, title, status, created,
    belief judgement/confidence), ordered by ``created`` ascending so the replay
    animation adds them in authoring order; edges carry ``from_id`` -> ``to_id``,
    ``edge_kind``, and ``valence`` when present. The detail view
    (:func:`web_get`) serves the full per-kind fields for one node on demand.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user, validated to have viewer role.
      limit: Most nodes to return, neighbours included, at least 1; 1000, or 60
        with a ``focus``, when unset.
      focus: The inquiry whose neighbourhood to return; unset returns the newest.
      hops: With ``focus``, the most edges to walk from it, 1 to 3.

    Returns:
      graph: Nodes and edges for replay visualization, ordered by created time.

    Raises:
      HTTPException: 400 for a ``limit`` below 1 or ``hops`` outside 1 to 3; 404
        when ``focus`` names no inquiry.

    """
    del identity
    # No top: the graph view lets a person type any count, All included, and asks
    # before drawing a large one. On a benchmark of about 100,000 nodes, All answered
    # 42 MB (10 MB gzipped) in 0.7 s, and 20,000 nodes 8.6 MB in 0.16 s. The answer
    # is still bounded by what was asked, which ``limit=0`` once was not.
    if limit is not None and limit < 1:
        raise HTTPException(status_code=400, detail="limit must be at least 1")
    if hops < 1 or hops > 3:
        raise HTTPException(status_code=400, detail="hops must be in [1, 3]")
    async with get_store(request).engine.acquire() as conn:
        distances: dict[UUID, int] = {}
        if focus is None:
            kept, bound = vetted_sql("IN (", _GRAPH_KEPT_IDS, ")"), limit or 1000
        else:
            distances = await read_neighbourhood(
                conn,
                focus,
                hops=hops,
                limit=limit or 60,
            )
            if not distances:
                raise HTTPException(status_code=404, detail="focus not found")
            kept, bound = "= ANY($1)", list(distances)
        node_rows = await conn.fetch(
            vetted_sql(
                "SELECT ",
                _GRAPH_NODE_COLS,
                " FROM inquiries WHERE id ",
                kept,
                " ORDER BY created ASC, id ASC",
            ),
            bound,
        )
        edge_rows = await conn.fetch(
            "SELECT from_id, to_id, edge_kind, valence FROM edges "
            "WHERE from_id = ANY($1) AND to_id = ANY($1)",
            [r["id"] for r in node_rows],
        )
    nodes = [_graph_node(r) for r in node_rows]
    if distances:
        for node, row in zip(nodes, node_rows, strict=True):
            node["hops"] = distances[_record_uuid(row, "id")]
    return {"nodes": nodes, "edges": [_graph_edge(r) for r in edge_rows]}


@router.get("/subscribe")
async def web_subscribe(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> StreamingResponse:
    """SSE relay of mutated inquiry ids for the SPA.

    Forwards every ``NOTIFY_CHANNEL`` payload as ``{"id": "<uuid>"}``
    via :func:`iter_sse_events` -- shared with ``/api/change_log/stream``
    so both routes emit one wire shape. Gated at ``viewer`` like every
    read route (see the Auth section of ``docs/design.md``). The stream
    is unfiltered: every subscriber sees every mutated id and the SPA
    decides what to fetch. Per-subscriber server-side filtering is a
    future refinement, not a security boundary -- authz already gates
    who may open the stream at all.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user, validated to have viewer role.

    Returns:
      stream: Server-sent events of mutated node ids.

    """
    del identity
    engine = _state(request).engine
    return StreamingResponse(iter_sse_events(engine), media_type="text/event-stream")


_PROBE_MAX_SEC: Final = 600.0


@router.get("/subscribe/probe")
async def web_subscribe_probe(
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    first_after_sec: float = 0.0,
    every_sec: float = 0.0,
    for_sec: float = 60.0,
    forbid_transform: bool = False,
) -> StreamingResponse:
    """Stream probe frames on the caller's schedule, to test the proxy path.

    Answers what the live stream meets between this server and a browser
    without a redeploy: whether a hop holds the headers until the first body
    byte (a late ``first_after_sec``), whether it cuts an idle stream
    (``every_sec`` 0 and a long ``for_sec``), and whether ``forbid_transform``
    changes either. Each frame is ``data: {"seq": n, "t": <seconds since the
    request>}``, so a client can tell a frame held on the way from one sent
    late. The server ends the stream at ``for_sec``, so an earlier end is a
    proxy's cut.

    Args:
      identity: Authenticated user, validated to have viewer role.
      first_after_sec: Seconds before the first frame; none at or past
        ``for_sec``.
      every_sec: Seconds between frames after the first, at least 0.01; 0
        sends one frame.
      for_sec: Seconds until the server ends the stream.
      forbid_transform: Send ``Cache-Control: no-transform``, which tells a proxy
        not to compress or otherwise rewrite the body.

    Returns:
      stream: Server-sent probe frames.

    Raises:
      HTTPException: 400 when a duration is outside ``[0, 600]``, ``for_sec``
        is 0, or ``every_sec`` is between 0 and 0.01.

    """
    del identity
    schedule = (first_after_sec, every_sec, for_sec)
    if (
        any(math.isnan(sec) or sec < 0 or sec > _PROBE_MAX_SEC for sec in schedule)
        or for_sec == 0
        or 0 < every_sec < 0.01
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"first_after_sec, every_sec and for_sec must be in "
                f"[0, {_PROBE_MAX_SEC:g}]; for_sec above 0; every_sec 0 or at "
                f"least 0.01"
            ),
        )
    return StreamingResponse(
        _probe_frames(
            first_after_sec=first_after_sec,
            every_sec=every_sec,
            for_sec=for_sec,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-transform"} if forbid_transform else None,
    )


def feed_scope(
    actor: Annotated[list[str] | None, Query(max_length=MAX_LIST_LIMIT)] = None,
    room: Annotated[list[str] | None, Query(max_length=MAX_LIST_LIMIT)] = None,
    cli: Annotated[list[str] | None, Query(max_length=MAX_LIST_LIMIT)] = None,
    kind: Annotated[list[str] | None, Query(max_length=MAX_LIST_LIMIT)] = None,
) -> FeedScope:
    """Read the feed's filters: each repeatable, any of its values, all of them.

    Args:
      actor: Sessions by routing name.
      room: Sessions by a room they joined.
      cli: Sessions by the CLI they wrap.
      kind: Records by kind.

    Returns:
      scope: The records the filters keep.

    """
    return FeedScope(
        actors=tuple(actor or ()),
        rooms=tuple(room or ()),
        clis=tuple(cli or ()),
        kinds=tuple(kind or ()),
    )


@router.get("/feed")
async def web_feed(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    scope: Annotated[FeedScope, Depends(feed_scope)] = WHOLE_FEED,
    after_created: UtcDatetime | None = None,
    after_session: UUID | None = None,
    after_part: int | None = None,
    after_seq: int | None = None,
    since: UtcDatetime | None = None,
    until: UtcDatetime | None = None,
    conversation: bool = False,
    limit: int = 200,
    tail: bool = False,
) -> FeedResponse:
    """Cross-session captured-turn feed for the multi-agent console.

    Interleaves every session's turns into one time-ordered stream so the
    console shows many agents at once.

    ``after_created`` / ``after_session`` / ``after_part`` / ``after_seq``
    together form the composite keyset cursor a poll resumes past (all are
    required; the order key is composite so a bare ``created`` resume would
    skip same-instant ties, and ``part`` joined it when the feed moved to IR
    records -- position restarts within each source file). ``since`` / ``until``
    bound a fixed historical window; ``actor`` / ``room`` / ``cli`` / ``kind``
    filter (:func:`feed_scope`), and ``conversation=true`` keeps only what the
    facets count as conversation. ``tail=true`` returns the
    newest page (the live console's first load) so a backlog does not force a
    replay from the beginning. The response carries ``next_after`` (a composite
    cursor) for the next poll; an empty page echoes the supplied cursor so the
    tail does not rewind.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user, validated to have viewer role.
      scope: Which sessions and record kinds to read.
      after_created: Resume cursor: creation timestamp of last-seen turn.
      after_session: Resume cursor: session id of last-seen turn.
      after_part: Resume cursor: part index within the session.
      after_seq: Resume cursor: sequence number within the part.
      since: Absolute window start (inclusive).
      until: Absolute window end (inclusive).
      conversation: Keep only conversation, what a person or agent said.
      limit: Maximum turns to return.
      tail: Return newest page first (true) or oldest available (false).

    Returns:
      feed: Turns across all sessions, pagination cursor for next poll.

    """
    del identity
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be in [1, 1000]")
    _check_window(since, until)
    after = _feed_cursor(after_created, after_session, after_part, after_seq)
    events = await get_store(request).read_feed(
        after=after,
        since=since,
        until=until,
        scope=scope,
        conversation=conversation,
        limit=limit,
        tail=tail,
    )
    if events:
        last = events[-1]
        next_after = FeedCursor(
            created=last.created,
            session_id=last.session_id,
            part=last.part,
            seq=last.seq,
        )
    elif after is not None:
        next_after = FeedCursor(
            created=after[0],
            session_id=after[1],
            part=after[2],
            seq=after[3],
        )
    else:
        next_after = None
    return FeedResponse(events=events, next_after=next_after)


@router.get("/feed/facets")
async def web_feed_facets(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    scope: Annotated[FeedScope, Depends(feed_scope)] = WHOLE_FEED,
    since: UtcDatetime | None = None,
    until: UtcDatetime | None = None,
) -> FeedFacetsResponse:
    """Count the feed between ``since`` and ``until`` by session, room and kind.

    The counts are of the records ``/feed`` returns for the same window and
    filters, so the console can list agents, rooms and verbosity levels with
    their sizes before it loads any of them.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user, validated to have viewer role.
      scope: Which sessions and record kinds to count.
      since: Window start (inclusive); unset counts from the first record.
      until: Window end (inclusive); unset counts to the last.

    Returns:
      facets: Per session, room and record kind, what the window holds.

    """
    del identity
    _check_window(since, until)
    return await get_store(request).read_feed_facets(
        since=since,
        until=until,
        scope=scope,
    )


@router.get("/feed/histogram")
async def web_feed_histogram(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    scope: Annotated[FeedScope, Depends(feed_scope)] = WHOLE_FEED,
    since: UtcDatetime | None = None,
    until: UtcDatetime | None = None,
    buckets: int = 120,
) -> FeedHistogramResponse:
    """Count the feed's records per time bucket, over at most the last 7 days.

    Buckets have a round width (seconds up to hours, then whole days), the
    finest that fits the span in at most ``buckets``, and start on a multiple of
    it from the Unix epoch; every bucket from the one holding ``since`` through
    the one holding ``until`` is listed, empty ones as 0. A ``since`` more than
    7 days ago starts the span 7 days ago.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user, validated to have viewer role.
      scope: Which sessions and record kinds to count.
      since: Span start; unset starts at the first record of the last 7 days.
      until: Span end; unset ends now.
      buckets: Most buckets to return, 2 to 1000.

    Returns:
      histogram: The grid and each bucket's record count, oldest first.

    Raises:
      HTTPException: 400 for ``buckets`` outside 2 to 1000, an ``until`` before
        ``since``, or an ``until`` more than 7 days ago.

    """
    del identity
    if buckets < 2 or buckets > 1_000:
        raise HTTPException(status_code=400, detail="buckets must be in [2, 1000]")
    _check_window(since, until)
    # The histogram counts the span's records one by one, so it reaches back a week
    # at most. On a benchmark of 9 million records, spans of 1.9 million took up to
    # 0.16 s at the median and 0.21 s at worst.
    earliest = datetime.now(UTC) - timedelta(days=7)
    if until is not None and until.astimezone(UTC) < earliest:
        raise HTTPException(
            status_code=400,
            detail="until must be within the last 7 days",
        )
    return await get_store(request).read_feed_histogram(
        since=since,
        until=until,
        earliest=earliest,
        buckets=buckets,
        scope=scope,
    )


# -- JSON serialization ------------------------------------------------------


_SNAPSHOT_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(Snapshot))

_CHANGE_SELECT: Final = (
    "SELECT c.*, COALESCE(k_user.email, actor_user.email) AS principal "
    "FROM change_log c "
    "LEFT JOIN api_keys k ON k.id = c.api_key_id "
    "LEFT JOIN users k_user ON k_user.id = k.user_id "
    "LEFT JOIN users actor_user ON actor_user.email = c.actor AND c.api_key_id IS NULL"
)


_PEER_COLUMNS: Final = (
    "t.kind AS peer_kind, t.seq AS peer_seq, t.title AS peer_title, "
    "t.status AS peer_status, t.belief_judgement AS peer_judgement, "
    "t.created AS peer_created, t.issue_priority AS peer_priority"
)
"""SELECT fragment for the joined inquiry on the far end of an edge."""


async def optional_identity(request: Request) -> AuthIdentity | None:
    """Resolve the request's principal without raising on missing credentials.

    Wraps :func:`auth.current_user` so the HTML page routes can branch
    on "is the caller signed in?" without the 401 the bearer/session
    dependency raises when no credential is present. Used as a
    FastAPI dependency so tests can override it via
    ``app.dependency_overrides[optional_identity] = ...`` instead of
    monkey-patching the real auth lookup.

    Returns ``None`` when ``app.state.engine`` is missing (the FastAPI
    test apps in ``web_test.py`` mount the router without booting the
    real lifespan) so the page handlers degrade gracefully instead of
    raising ``AttributeError``.

    Args:
      request: FastAPI request object for middleware access.

    Returns:
      identity: Authenticated principal, or None if missing/invalid credentials.

    """
    if not hasattr(_state(request), "engine"):
        return None
    try:
        return await current_user(request)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        return None


def attach(
    app: FastAPI,
    *,
    assets_dir: Path | None = None,
    static_dir: Path | None = None,
    app_dir: Path | None = None,
) -> None:
    """Mount the read-API router, the sign-in page, ``/static/*`` and ``/app/*``.

    ``/static`` serves ``static_dir`` when given, so an operator can serve files
    written after deploy (e.g. a generated report) without copying them into
    the source tree; unset mounts none.

    ``/app/`` serves a separately built web app from ``app_dir`` to the callers
    the API answers (every caller under ``--no-auth``), with ``index.html`` at
    ``/app/``; any other caller's entry page redirects to the sign-in page, and
    its other files answer 401. The directory is
    resolved on every request, so it may be missing at startup (``/app/``
    answers 404) or be a symlink swapped to a new build without a restart.
    With it, the old UI's paths answer 302 into the app: ``/`` to ``/app/``,
    and ``/me``, ``/admin``, ``/graph`` and ``/console`` to the app's
    ``#/settings``, ``#/admin``, ``#/graph`` and ``#/console``. Without it,
    nothing is served at those paths.

    Idempotent: a second call is a no-op, so the first call's directories win.
    ``server._configure_app`` attaches the module-global app, and a test that
    reuses it must not stack duplicate routes (TRK-SRV-002).

    Args:
      app: FastAPI instance to mount routes on.
      assets_dir: Directory holding ``login.html`` (overrides the bundled one).
      static_dir: Directory to serve at /static; unset mounts none.
      app_dir: Directory of a built web app to serve at /app/; unset mounts none.

    """
    if app in _ATTACHED:
        return
    _ATTACHED.add(app)
    assets = assets_dir or (_CWD / "assets")
    app.include_router(router, prefix="/api/web")

    if static_dir is not None and static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    if app_dir is not None:
        _get(app, "/app/{path:path}", _AppRoute(files=_app_files(app_dir)))
        # Stored links name ``/`` with a hash, which the browser keeps across a
        # redirect whose ``Location`` has none, and the app's router reads the old
        # hashes. 302, not 301: a browser keeps a 301 for good, which would pin
        # these paths to ``/app/`` past any later move.
        for path, location in (
            ("/", "/app/"),
            ("/me", "/app/#/settings"),
            ("/admin", "/app/#/admin"),
            ("/graph", "/app/#/graph"),
            ("/console", "/app/#/console"),
        ):
            _get(app, path, _RedirectRoute(location=location))
    _add_login_route(app, assets / "login.html")


# Files are served through ``get_response`` from a route, not by mounting the
# ``StaticFiles`` app: a mount has no dependency injection for the session check, and
# it answers 500 to every request while the directory is missing (its ``check_config``
# raises), where this answers 404. ``lookup_path`` re-resolves the directory's
# ``realpath`` per request and refuses any path that leaves it, symlinks included.
#
# Every response, errors included, is ``private``. A shared cache in front of the
# server may store responses by file extension (Cloudflare does for ``.js``, 404s
# included; measured on production), which would hand one user's copy of the app to
# anyone and pin a 401 or 404 for everyone. ``private`` keeps shared caches out.
#
# A file found under ``assets/`` is named by its content's hash, so the browser keeps
# it for good (``_ASSET_CACHE_CONTROL``) and a repeat visit asks for none of them.
# That includes a 304, whose headers replace the stored ones: answered ``no-cache``,
# an asset cached before this rule would be revalidated for as long as its hash
# lasts. Everything else, errors included, gets ``_APP_CACHE_CONTROL``, whose
# ``no-cache`` makes browsers revalidate, so a new build's entry page is seen as soon
# as it lands.
@dataclass(frozen=True, slots=True, kw_only=True)
class _AppRoute:
    files: StaticFiles

    async def __call__(
        self,
        request: Request,
        path: str,
        identity: Annotated[AuthIdentity | None, Depends(optional_identity)],
    ) -> Response:
        try:
            response = await self._respond(request, path or "index.html", identity)
        except StarletteHTTPException as err:
            raise HTTPException(
                status_code=err.status_code,
                detail=err.detail,
                headers={**(err.headers or {}), "Cache-Control": _APP_CACHE_CONTROL},
            ) from err
        hashed = path.startswith("assets/") and response.status_code in {200, 304}
        response.headers["Cache-Control"] = (
            _ASSET_CACHE_CONTROL if hashed else _APP_CACHE_CONTROL
        )
        return response

    async def _respond(
        self,
        request: Request,
        path: str,
        identity: AuthIdentity | None,
    ) -> Response:
        if identity is not None:
            try:
                return await self.files.get_response(path, scope=request.scope)
            except StarletteHTTPException as err:
                # Starlette answers 401 when it cannot read a file. For a signed-in
                # caller that would read as "signed out"; an unreadable build is
                # the deploy's fault.
                if err.status_code != 401:
                    raise
                raise HTTPException(
                    status_code=500,
                    detail="the app build is not readable by the server",
                ) from err
        # Refused where the API refuses the caller. Only the entry page redirects: a
        # script or stylesheet cannot use a login page. It does so on a server that
        # signs no browser in too, since the login page says so; a 401 there left a
        # bare JSON error at the address a new user opens first.
        if path != "index.html":
            raise HTTPException(status_code=401, detail="not authenticated")
        return _login_redirect(request)


# Signed in or not: ``/app/`` decides sign-in. ``no-cache`` as on ``/app/``, so a change
# of where a path leads is seen at once.
@dataclass(frozen=True, slots=True, kw_only=True)
class _RedirectRoute:
    location: str

    async def __call__(self) -> RedirectResponse:
        return RedirectResponse(
            self.location,
            status_code=302,
            headers={"Cache-Control": _APP_CACHE_CONTROL},
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _LoginPageRoute:
    page_path: Path

    async def __call__(self) -> FileResponse:
        return FileResponse(self.page_path)


def _add_login_route(app: FastAPI, page_path: Path) -> None:
    """Mount the login page when its asset exists."""
    if page_path.is_file():
        _get(app, "/auth/login_page", _LoginPageRoute(page_path=page_path))


def _login_redirect(request: Request) -> RedirectResponse:
    """Return a 302 to ``/auth/login_page``, whose ``next`` leads back here."""
    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    return RedirectResponse(
        url=f"/auth/login_page?next={quote(next_url, safe='')}",
        status_code=302,
    )


# Every component must be present together: a partial cursor cannot resume the
# ``(created, session_id, part, idx)`` order and is a client error.
#
# ``part`` is the one exception -- it defaults to 0 when the rest are given, so a client
# written against the pre-IR three-part cursor still resumes rather than 400ing. It
# cannot skip rows: 0 is the lowest real part, and a legacy backfill sits at -1, which
# such a client never asked about.
def _feed_cursor(
    created: datetime | None,
    session_id: UUID | None,
    part: int | None,
    seq: int | None,
) -> tuple[datetime, UUID, int, int] | None:
    """Assemble the composite feed cursor, or ``None`` when unset."""
    if created is None and session_id is None and seq is None and part is None:
        return None
    if created is None or session_id is None or seq is None:
        raise HTTPException(
            status_code=400,
            detail="after_created, after_session, after_seq must be given together",
        )
    return (created, session_id, 0 if part is None else part, seq)


def _check_window(since: datetime | None, until: datetime | None) -> None:
    """Refuse a window that ends before it starts; a naive time is UTC."""
    if (
        since is not None
        and until is not None
        and until.astimezone(UTC) < since.astimezone(UTC)
    ):
        raise HTTPException(status_code=400, detail="until must not be before since")


# Fields: ``title``, ``description``. Bare tokens search both. ``shlex.split`` reads
# the query as a shell line, which is wrong twice: ``\`` escapes the next character,
# so ``title:\d+`` reached Postgres as ``d+``, and ``'`` opens a quote, so ``don't``
# failed as unterminated. Only ``"`` groups, the grammar ``docs/api.md`` states.
# ``#`` is not a comment either.
def _parse_query(q: str) -> list[tuple[str | None, str]]:
    """Tokenize a search query into ``(field, pattern)`` terms."""
    lexer = shlex.shlex(q, posix=True)
    lexer.whitespace_split = True
    lexer.quotes = '"'
    lexer.escape = ""
    lexer.commenters = ""
    out: list[tuple[str | None, str]] = []
    for tok in lexer:
        if not tok:
            continue
        if ":" in tok and not tok.startswith(":"):
            field, _, rest = tok.partition(":")
            if field in {"title", "description"}:
                if not rest:
                    raise ValueError(f"empty value for field {field!r}")
                out.append((field, rest))
                continue
        out.append((None, tok))
    return out


# Multiple bare tokens (``foo bar``) intersect: a row must match every token. Field-
# qualified tokens (``title:^x$``) compose with the same AND semantics. Field-qualified
# regexes are validated client- side via :func:`re.compile` so a malformed pattern
# surfaces as a :class:`ValueError` -- the route turns that into 400, not 500.
def _build_term_clause(
    terms: Sequence[tuple[str | None, str]],
) -> tuple[str, list[object]]:
    """Render ``terms`` as a SQL AND clause + bind params."""
    params: list[object] = []
    clauses: list[str] = []
    for query_field, pattern in terms:
        if query_field is None:
            # Escape the token's ILIKE wildcards so a bare ``%`` / ``_`` is a
            # literal term, not a match-everything pattern (TRK-SRV-001). The
            # backslash escape is itself escaped first so a literal ``\`` in
            # the token can't consume the following char. The ``%...%`` we add
            # is the intentional substring wildcard.
            escaped = (
                pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            params.append(f"%{escaped}%")
            ph = f"${len(params)}"
            clauses.append(
                f"(title ILIKE {ph} ESCAPE '\\' OR description ILIKE {ph} ESCAPE '\\')",
            )
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex for {query_field}: {exc}") from exc
            params.append(pattern)
            ph = f"${len(params)}"
            clauses.append(f"{query_field} ~* {ph}")
    return " AND ".join(clauses), params


# Carries id, kind, seq, title, status (so the view can dim retired ``abandoned`` /
# ``invalid`` nodes), created (the replay order key), and -- for Beliefs -- judgement +
# confidence so the view can encode a claim's verdict and certainty. The two belief
# fields are emitted only when present, so a non-Belief row stays clean.
def _graph_node(row: asyncpg.Record) -> WebView:
    """Light node projection for the graph view."""
    out: WebView = {
        "id": str(_record_uuid(row, "id")),
        "kind": _record_str(row, "kind"),
        "seq": _record_int(row, "seq"),
        "title": row["title"] or "",
        "status": _record_str(row, "status"),
        "created": _isoformat(_record_datetime(row, "created")),
    }
    if row["belief_judgement"] is not None:
        out["judgement"] = row["belief_judgement"]
    if row["belief_confidence"] is not None:
        out["confidence"] = row["belief_confidence"]
    return out


# ``valence`` is present only on ``proves`` / ``favors`` citations (NULL on structural
# edges); the key is omitted when absent so the SPA never has to special-case a null.
def _graph_edge(row: asyncpg.Record) -> WebView:
    """Typed directed link for the graph view."""
    out: WebView = {
        "from_id": str(_record_uuid(row, "from_id")),
        "to_id": str(_record_uuid(row, "to_id")),
        "edge_kind": _record_str(row, "edge_kind"),
    }
    valence = row["valence"]
    if valence is not None:
        out["valence"] = valence
    return out


# Kind-specific columns surface only when populated on this row; NULL columns are
# omitted so the JSON doesn't carry irrelevant nulls per kind.
def _session_hit_to_dict(hit: SessionSearchHit) -> WebView:
    """Flatten one ``SessionSearchHit`` to JSON for the console."""
    return {
        "session_id": str(hit.session_id),
        "part": hit.part,
        "idx": hit.idx,
        "field": hit.field,
        "chunk": hit.chunk,
        "score": hit.score,
        "source": hit.source,
        "snippet": hit.snippet,
        "title": hit.title,
    }


# The one inquiry serializer, so ``/get`` and ``GET /api/inquiries/{id}`` agree field
# for field, ``null`` for unset. The relation fields are dropped rather than served
# empty: ``from_row`` reads no edges, and ``web_get`` sends them as ``edges`` /
# ``backlinks``.
def _row_to_dict(row: asyncpg.Record) -> WebView:
    """Serialize an ``inquiries`` row as its kind's dataclass, less its relations."""
    kind = cast(Inquiry.InquiryKind, _record_str(row, "kind"))
    inquiry = KIND_TO_CLASS[kind].from_row(row)
    relations = inquiry_relation_fields()
    return {
        key: value for key, value in tag_row(inquiry).items() if key not in relations
    }


# Identity columns surface at top level; the flat ``old_*`` / ``new_*`` snapshot columns
# gather into nested ``old`` / ``new`` objects with only-populated keys present.
def _change_to_dict(row: asyncpg.Record) -> WebView:
    """Flatten a ``change_log`` row to JSON."""
    api_key_id = row.get("api_key_id")
    caused_by = row["caused_by"]
    out: WebView = {
        "id": str(_record_uuid(row, "id")),
        "created": _isoformat(_record_datetime(row, "created")),
        "actor": _record_str(row, "actor"),
        "principal": row.get("principal") or "",
        "api_key_id": (None if api_key_id is None else str(api_key_id)),
        "subject_id": str(_record_uuid(row, "subject_id")),
        "subject_kind": _record_str(row, "subject_kind"),
        "kind": _record_str(row, "kind"),
        "caused_by": str(caused_by) if caused_by else None,
        "reason": row["reason"] or "",
    }
    out["old"] = _snapshot_to_dict(row, prefix="old_")
    out["new"] = _snapshot_to_dict(row, prefix="new_")
    return out


def _snapshot_to_dict(row: asyncpg.Record, *, prefix: str) -> WebView:
    """Pull one side of the delta off a row as a populated-only JSON dict."""
    out: WebView = {}
    for column in _SNAPSHOT_COLUMNS:
        if column == "marginal_cost":
            out["marginal_cost"] = {
                "agent_usd": from_plain(row[prefix + "marginal_cost_agent_usd"], float),
                "resource_usd": from_plain(
                    row[prefix + "marginal_cost_resource_usd"],
                    float,
                ),
            }
            continue
        row_column = prefix + column
        if row_column not in row:
            continue
        value = row[row_column]
        if value is None:
            continue
        if column in ("labels", "subscribers", "issue_kind"):
            out[column] = from_plain(value, list[str])
        elif column == "experiment_codechanges":
            out[column] = [str(uid) for uid in from_plain(value, list[UUID])]
        elif isinstance(value, UUID):
            out[column] = str(value)
        else:
            out[column] = value
    return out


# ``peer_priority`` is the peer's own priority; ``priority`` on the same ref is the
# edge's (``_add_edge_annotation``), which overrides it in that parent's list.
def _peer_ref(row: asyncpg.Record, peer_id: UUID) -> WebView:
    """Build a UI ref ``{id, kind, seq, title, status, peer_created, ...}``."""
    out: WebView = {
        "id": str(peer_id),
        "kind": _record_str(row, "peer_kind"),
        "seq": _record_int(row, "peer_seq"),
        "title": row["peer_title"] or "",
        "status": _record_str(row, "peer_status"),
        "peer_created": _isoformat(_record_datetime(row, "peer_created")),
    }
    if row["peer_judgement"] is not None:
        out["judgement"] = row["peer_judgement"]
    if row["peer_priority"] is not None:
        out["peer_priority"] = row["peer_priority"]
    return out


async def _edges_for(
    conn: Conn,
    target_id: UUID,
    *,
    direction: Literal["outbound", "inbound"],
) -> WebView:
    """Edges grouped by edge_kind for ``target_id`` in the given direction."""
    if direction == "outbound":
        where_col, join_col, peer_col = "e.from_id", "e.to_id", "to_id"
    else:
        where_col, join_col, peer_col = "e.to_id", "e.from_id", "from_id"
    rows = await conn.fetch(
        vetted_sql(
            "SELECT e.from_id, e.to_id, e.edge_kind, "
            "e.priority, e.note, e.valence, e.labels, ",
            _PEER_COLUMNS,
            " FROM edges e LEFT JOIN inquiries t ON t.id = ",
            join_col,
            " WHERE ",
            where_col,
            " = $1",
        ),
        target_id,
    )
    groups: dict[str, list[WebView]] = {}
    for row in rows:
        ref = _peer_ref(row, _record_uuid(row, peer_col))
        _add_edge_annotation(ref, row)
        groups.setdefault(_record_str(row, "edge_kind"), []).append(ref)
    out: WebView = {**groups}
    return out


def _add_edge_annotation(ref: WebView, row: asyncpg.Record) -> None:
    """Attach edge-local metadata to a peer ref."""
    if row["priority"] is not None:
        ref["priority"] = row["priority"]
    if row["note"]:
        ref["note"] = row["note"]
    if row["valence"] is not None:
        ref["valence"] = row["valence"]
    if row["labels"]:
        ref["labels"] = from_plain(row["labels"], list[str])


class _AppState(Protocol):
    store: Store
    engine: DatabaseEngine
    # Populated lazily by ``_session_embedder``; ``config`` is set by the app
    # lifespan (absent in the duck-typed test apps, hence the getattr reads).
    config: object
    session_embedder: QueryEmbedder | None
    # A/B override embedders, cached per stored name by ``_override_embedder`` so
    # repeated ``?model=`` queries reuse one loaded model.
    session_embedder_overrides: dict[str, QueryEmbedder]


class _AppLike(Protocol):
    state: _AppState


def _state(request: Request) -> _AppState:
    """Return the dynamically populated FastAPI application state."""
    # Structural, not ``isinstance(app, FastAPI)``: web_test drives these
    # handlers with a duck-typed request a nominal check would reject.
    return cast(_AppLike, request.app).state


def _record_str(row: asyncpg.Record, key: str) -> str:
    """Read a required text column from an asyncpg record."""
    value = row[key]
    assert isinstance(value, str)
    return value


def _record_int(row: asyncpg.Record, key: str) -> int:
    """Read a required integer column from an asyncpg record."""
    value = row[key]
    assert isinstance(value, int)
    return value


def _record_datetime(row: asyncpg.Record, key: str) -> datetime:
    """Read a required timestamp column from an asyncpg record."""
    value = row[key]
    assert isinstance(value, datetime)
    return value


def _record_uuid(row: asyncpg.Record, key: str) -> UUID:
    """Read a required UUID column from an asyncpg record."""
    value = row[key]
    assert isinstance(value, UUID)
    return value


def _isoformat(value: object) -> str:
    """ISO-format a datetime; fall back to ``str()`` for non-datetimes."""
    return value.isoformat() if isinstance(value, datetime) else str(value)


# Built once per process from ``config.session_embedder`` and cached on
# ``app.state`` -- a real embedder still loads its weights lazily on the first
# ``embed_query``, so caching the instance is cheap and only the first semantic
# search pays the model load. ``None`` (unset knob or no config) means the
# semantic arm is unavailable and the route degrades.
def _session_embedder(request: Request) -> QueryEmbedder | None:
    """Return the process's default session-search embedder, or ``None``."""
    state = _state(request)
    cached = getattr(state, "session_embedder", ABSENT)
    if cached is not ABSENT:
        return cast("QueryEmbedder | None", cached)
    # ``isinstance`` narrowing, not ``getattr(config, ...)``: the lifespan
    # stores a real ``Config`` (``api/app.py``), so a typed read means a field
    # rename breaks type-checking here instead of silently disabling the
    # semantic arm. Duck-typed test apps carry no config and read as None.
    config: object = getattr(state, "config", None)
    name = config.session_embedder if isinstance(config, Config) else ""
    dim = config.session_embedder_dim if isinstance(config, Config) else None
    embedder = registry.build_cached_session_embedder(name, dim=dim)
    state.session_embedder = embedder
    return embedder


# The A/B override: ``?model=`` (optionally ``?dim=``) selects a challenger
# embedder distinct from the serving default, WITHOUT disturbing
# ``state.session_embedder``. Instances are cached per ``(name, dim)`` on
# ``state.session_embedder_overrides`` so repeated A/B queries reuse one model --
# a real model loads ~8 GB of weights on first ``embed_query``, so rebuilding per
# request would reload them every query. Two dims of one model are DISTINCT cache
# entries (different truncated vectors, different stored identity). The cache is
# bounded by the registry (a handful of keys), so no eviction is needed. An
# unknown name/dim is a client error (400), not a degrade -- degrade is for a
# configured model whose weights are absent, which the lazy-load path handles.
def _override_embedder(
    request: Request,
    name: str,
    dim: int | None,
) -> QueryEmbedder | None:
    """Return the locally cached A/B model; degrade if uncached, 400 if unknown."""
    state = _state(request)
    if not hasattr(state, "session_embedder_overrides"):
        state.session_embedder_overrides = {}
    overrides = state.session_embedder_overrides
    try:
        key = (
            name if registry.is_weightless(name) else registry.resolved_name(name, dim)
        )
        cached = overrides.get(key)
        if cached is not None:
            return cached
        embedder = registry.build_cached_session_embedder(name, dim=dim)
    except ConfigError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    if embedder is not None:
        overrides[key] = embedder
    return embedder


def _search_embedder(
    request: Request,
    *,
    semantic: bool,
    model: str,
    dim: int | None,
) -> QueryEmbedder | None:
    """Select the query embedder: the ``model``/``dim`` override, else the default."""
    if dim is not None and not model:
        raise HTTPException(status_code=400, detail="dim requires model")
    if not semantic:
        return None
    if model:
        return _override_embedder(request, model, dim)
    return _session_embedder(request)


async def _probe_frames(
    *,
    first_after_sec: float,
    every_sec: float,
    for_sec: float,
) -> AsyncGenerator[bytes]:
    """Frames from ``first_after_sec`` every ``every_sec``, then silence to ``for_sec``."""
    start = time.monotonic()
    due_sec = first_after_sec
    seq = 0
    while due_sec < for_sec:
        await asyncio.sleep(due_sec - (time.monotonic() - start))
        yield f'data: {{"seq": {seq}, "t": {time.monotonic() - start:.3f}}}\n\n'.encode()
        if every_sec == 0:
            break
        seq += 1
        due_sec += every_sec
    await asyncio.sleep(for_sec - (time.monotonic() - start))


# Apps ``attach`` has already mounted; weak, so a discarded test app is freed.
_ATTACHED: WeakSet[FastAPI] = WeakSet()


def _get(app: FastAPI, path: str, endpoint: Callable[..., Awaitable[object]]) -> None:
    """Mount a GET-only route that the OpenAPI schema leaves out."""
    app.get(path, include_in_schema=False)(endpoint)


def _app_files(app_dir: Path) -> StaticFiles:
    """Serve ``app_dir``; ``check_dir=False`` lets the server start before a build."""
    return StaticFiles(directory=app_dir, check_dir=False)
