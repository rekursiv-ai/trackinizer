"""Optional SPA + read endpoints for the trackinizer FastAPI app.

Mounted by ``trackinizer --web`` (or programmatically via :func:`attach`).
Adds:

- ``GET /`` -- the single-page SPA (``assets/index.html``).
- ``GET /static/*`` -- the SPA's static assets (when present).
- ``GET /app/*`` -- a separately built web app, for signed-in users only
  (``--app-dir``).
- ``GET /api/web/search`` -- cross-kind ILIKE search over title/description.
- ``GET /api/web/recent_changes`` -- the most-recent ``change_log`` rows
  with their snapshots flattened to JSON.
- ``GET /api/web/lookup/{target_id}`` -- resolve a UUID to its kind.
- ``GET /api/web/get/{target_id}`` -- one inquiry with edges + backlinks +
  recent changes attached, for the SPA detail view.

All write verbs live on :mod:`trackinizer`'s :class:`Store` and its
``/api/*`` routes (every mutation takes an explicit ``actor`` argument
that lands on :attr:`Change.actor` alongside the server-stamped
``api_key_id``); there is no separate "curator" class in this
design.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Final,
    Literal,
    Protocol,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)
from urllib.parse import quote
from uuid import UUID

import asyncio
import math
import re
import shlex
import time
import uuid

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from trackinizer.lib.absent import ABSENT
from trackinizer.lib.custom_json import FloatCodec, IntCodec, ListCodec
from trackinizer.server.api._deps import tag_row
from trackinizer.server.api._regex_guard import regex_failures_as_400
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
from trackinizer.server.store.session_search import (
    SessionSearchHit,
    search_session_records,
)
from trackinizer.server.values import vetted_sql
from trackinizer.types.change_log import Snapshot
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import KIND_TO_CLASS, Inquiry, InquiryEdge
from trackinizer.wire.wire_sessions import FeedCursor, FeedResponse


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    import asyncpg

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.store.core import Store


_CWD: Final = Path(__file__).resolve().parent


type WebView = dict[str, object]


router = APIRouter()


def get_store(request: Request) -> Store:
    """Return the process-wide store."""
    return _state(request).store


_SESSION_SEARCH_MAX_LIMIT: Final = 200
_SESSION_SEARCH_MAPPER: Final = FootprintMapper().name


class _QueryEmbedder(Protocol):
    """A session embedder that can embed a query with its instruction prefix."""

    name: str
    dim: int

    async def embed_query(self, text: str) -> list[float]:
        """Embed a search query on the query-side manifold."""
        ...


# See ``_AppRoute``: shared caches must never store an /app/ response.
_APP_CACHE_CONTROL: Final = "private, no-cache"


# -- Read routes -------------------------------------------------------------


@router.get("/search")
async def web_search(
    request: Request,
    q: str,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    kind: Inquiry.InquiryKind | None = None,
    limit: int = 50,
) -> list[WebView]:
    """Cross-kind search.

    Grammar: whitespace separates terms and every term must match. A bare term
    matches ``title`` or ``description`` as a case-insensitive substring;
    ``title:RE`` / ``description:RE`` match a case-insensitive regex. Only ``"``
    groups a phrase: ``'`` and backslashes are ordinary characters.

    Args:
      request: FastAPI request object for middleware access.
      q: Bare-token and field-scoped regex search string.
      identity: Authenticated user, validated to have viewer role.
      kind: Filter to one inquiry kind; None means search all kinds.
      limit: Maximum results, 1-1000.

    Returns:
      matches: Matching inquiries, each as ``self`` in ``/get`` renders it.

    """
    del identity
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be in [1, 1000]")
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
    return [_row_to_dict(r) for r in rows]


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
        "edges": edges,
        "backlinks": backlinks,
        "changes": [_change_to_dict(c) for c in changes],
    }


_GRAPH_NODE_COLS: Final = (
    "id, kind, seq, title, status, created, belief_judgement, belief_confidence"
)


@router.get("/graph")
async def web_graph(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    limit: int = 1000,
) -> WebView:
    """Return the inquiry graph as typed nodes + directed edges, for the SPA.

    ``limit`` caps the graph to the most-recently-created ``limit`` nodes, then
    EDGE-CLOSES that set: any older node referenced by an edge to a recent node
    is pulled back in, so an old-but-still-cited node (e.g. a foundational paper
    a new belief proves) stays visible and no edge is left dangling. The
    returned node count is therefore the recent N plus their older neighbours.
    ``limit`` is 1 to 5000: the whole graph is never one response.

    Nodes are the light projection (id, kind, seq, title, status, created,
    belief judgement/confidence), ordered by ``created`` ascending so the replay
    animation adds them in authoring order; edges carry ``from_id`` -> ``to_id``,
    ``edge_kind``, and ``valence`` when present. The detail view
    (:func:`web_get`) serves the full per-kind fields for one node on demand.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user, validated to have viewer role.
      limit: Cap to N most-recent nodes, 1 to 5000.

    Returns:
      graph: Nodes and edges for replay visualization, ordered by created time.

    """
    del identity
    # Any viewer may call this, and the whole graph measured 92k nodes, 29 MB and
    # 8.8 s on production. 5000 is the largest cap ``graph.html`` offers.
    if limit < 1 or limit > 5_000:
        raise HTTPException(status_code=400, detail="limit must be in [1, 5000]")
    async with get_store(request).engine.acquire() as conn:
        # 1. The most-recent ``limit`` node ids.
        recent = await conn.fetch(
            "SELECT id FROM inquiries ORDER BY created DESC, id DESC LIMIT $1",
            limit,
        )
        recent_ids = [r["id"] for r in recent]
        # 2. Every edge touching a recent node (in either direction).
        edge_rows = await conn.fetch(
            "SELECT from_id, to_id, edge_kind, valence FROM edges "
            "WHERE from_id = ANY($1) OR to_id = ANY($1)",
            recent_ids,
        )
        # 3. Edge-close: the recent set plus any neighbour the edges reach.
        keep = set(recent_ids)
        for e in edge_rows:
            keep.add(e["from_id"])
            keep.add(e["to_id"])
        # 4. Full light rows for the kept set, in replay (created ASC) order.
        node_rows = await conn.fetch(
            vetted_sql(
                "SELECT ",
                _GRAPH_NODE_COLS,
                " FROM inquiries WHERE id = ANY($1) ORDER BY created ASC, id ASC",
            ),
            list(keep),
        )
    return {
        "nodes": [_graph_node(r) for r in node_rows],
        "edges": [_graph_edge(r) for r in edge_rows],
    }


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


@router.get("/feed")
async def web_feed(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    after_created: datetime | None = None,
    after_session: UUID | None = None,
    after_part: int | None = None,
    after_seq: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    room: str | None = None,
    actor: str | None = None,
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
    records -- position restarts within each source file). ``since`` / ``until`` bound a fixed historical window;
    ``room`` / ``actor`` filter by routing identity. ``tail=true`` returns the
    newest page (the live console's first load) so a backlog does not force a
    replay from the beginning. The response carries ``next_after`` (a composite
    cursor) for the next poll; an empty page echoes the supplied cursor so the
    tail does not rewind.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user, validated to have viewer role.
      after_created: Resume cursor: creation timestamp of last-seen turn.
      after_session: Resume cursor: session id of last-seen turn.
      after_part: Resume cursor: part index within the session.
      after_seq: Resume cursor: sequence number within the part.
      since: Absolute window start (inclusive).
      until: Absolute window end (exclusive).
      room: Filter turns by job/room routing label.
      actor: Filter turns by actor/agent name.
      limit: Maximum turns to return.
      tail: Return newest page first (true) or oldest available (false).

    Returns:
      feed: Turns across all sessions, pagination cursor for next poll.

    """
    del identity
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be in [1, 1000]")
    after = _feed_cursor(after_created, after_session, after_part, after_seq)
    events = await get_store(request).read_feed(
        after=after,
        since=since,
        until=until,
        room=room,
        actor=actor,
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


def graph_legend() -> dict[str, list[str]]:
    """Return the node-kind and edge-kind sets the graph view colors by.

    The single source the SPA reads to build its color legend, derived from the
    domain enums so it cannot drift: a new ``Inquiry`` subclass or ``Edge.Kind``
    appears here automatically (pinned by ``web_test.py``).

    Returns:
      legend: Enum values for node_kinds and edge_kinds; keyed by name.

    """
    return {
        "node_kinds": list(get_args(Inquiry.InquiryKind)),
        "edge_kinds": list(get_args(Edge.Kind)),
    }


# -- Search query parsing ----------------------------------------------------


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
    """Mount the read-API router, the SPA at ``/``, ``/static/*`` and ``/app/*``.

    ``/static`` is served from ``static_dir`` when given, else from the SPA's
    own ``assets/static`` -- the runtime override lets an operator serve files
    written after deploy (e.g. a generated report) without copying them into
    the source tree, while the default keeps the SPA's bundled assets working.

    ``/app/`` serves a separately built web app from ``app_dir``, only to
    signed-in callers, with ``index.html`` at ``/app/``. The directory is
    resolved on every request, so it may be missing at startup (``/app/``
    answers 404) or be a symlink swapped to a new build without a restart.

    Idempotent: a second call is a no-op, so the first call's directories win.
    ``server._configure_app`` attaches the module-global app, and a test that
    reuses it must not stack duplicate routes (TRK-SRV-002).

    Args:
      app: FastAPI instance to mount routes on.
      assets_dir: Directory holding the SPA's HTML pages (overrides bundled SPA).
      static_dir: Directory to serve at /static (overrides bundled default).
      app_dir: Directory of a built web app to serve at /app/; unset mounts none.

    """
    if getattr(app.state, "web_attached", False):
        return
    app.state.web_attached = True
    assets = assets_dir or (_CWD / "assets")
    app.include_router(router, prefix="/api/web")

    static = static_dir or (assets / "static")
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=str(static)), name="static")

    if app_dir is not None:
        # ``check_dir=False`` lets the server start before a build exists.
        app.add_api_route(
            "/app/{path:path}",
            _AppRoute(files=StaticFiles(directory=app_dir, check_dir=False)),
            methods=["GET"],
            include_in_schema=False,
        )

    _add_page_route(app, "/", assets / "index.html")
    _add_page_route(app, "/graph", assets / "graph.html")
    _add_page_route(app, "/console", assets / "console.html")
    _add_page_route(app, "/me", assets / "me.html")
    _add_page_route(app, "/admin", assets / "admin.html", admin_only=True)
    _add_login_route(app, assets / "login.html")


@dataclass(frozen=True, slots=True, kw_only=True)
class _PageRoute:
    page_path: Path
    admin_only: bool = False

    async def __call__(
        self,
        request: Request,
        identity: Annotated[AuthIdentity | None, Depends(optional_identity)],
    ) -> Response:
        if self.admin_only:
            return _serve_admin_page(
                request,
                identity=identity,
                page_path=self.page_path,
            )
        return _serve_if_authed(request, identity=identity, page_path=self.page_path)


# Files are served through ``get_response`` from a route, not by mounting the
# ``StaticFiles`` app: a mount has no dependency injection for the session check, and
# it answers 500 to every request while the directory is missing (its ``check_config``
# raises), where this answers 404. ``lookup_path`` re-resolves the directory's
# ``realpath`` per request and refuses any path that leaves it, symlinks included.
#
# Every response, errors included, carries ``_APP_CACHE_CONTROL``. A shared cache in
# front of the server may store responses by file extension (Cloudflare does for
# ``.js``, 404s included; measured on production), which would hand one user's copy
# of the app to anyone and pin a 401 or 404 for everyone. ``private`` keeps shared
# caches out, and ``no-cache`` makes browsers revalidate, so a new build's entry page
# is seen as soon as it lands.
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
        response.headers["Cache-Control"] = _APP_CACHE_CONTROL
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
        # Only the entry page redirects: a script or stylesheet cannot use a login page.
        # ``_redirect_when_unauthed`` also returns ``None`` when session login is not
        # configured, where the old pages are served to anyone; the app is not.
        redirect = _redirect_when_unauthed(request, identity=identity)
        if redirect is None or path != "index.html":
            raise HTTPException(status_code=401, detail="not authenticated")
        return redirect


@dataclass(frozen=True, slots=True, kw_only=True)
class _LoginPageRoute:
    page_path: Path

    async def __call__(self) -> FileResponse:
        return FileResponse(self.page_path)


def _add_page_route(
    app: FastAPI,
    path: str,
    page_path: Path,
    *,
    admin_only: bool = False,
) -> None:
    """Mount one HTML page when its asset exists."""
    if page_path.is_file():
        app.add_api_route(
            path,
            _PageRoute(page_path=page_path, admin_only=admin_only),
            methods=["GET"],
            include_in_schema=False,
        )


def _add_login_route(app: FastAPI, page_path: Path) -> None:
    """Mount the login page when its asset exists."""
    if page_path.is_file():
        app.add_api_route(
            "/auth/login_page",
            _LoginPageRoute(page_path=page_path),
            methods=["GET"],
            include_in_schema=False,
        )


def _serve_admin_page(
    request: Request,
    identity: AuthIdentity | None,
    page_path: Path,
) -> Response:
    """Return the admin page for admins; redirect/403 otherwise."""
    redirect = _redirect_when_unauthed(request, identity=identity)
    if redirect is not None:
        return redirect
    if identity is None or identity.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return FileResponse(page_path)


# When session auth is unconfigured (no ``session_secret`` on ``app.state.config``) the
# redirect is suppressed -- the deployment isn't running OAuth and gating the SPA behind
# a login that doesn't exist would lock everyone out.
def _serve_if_authed(
    request: Request,
    identity: AuthIdentity | None,
    page_path: Path,
) -> Response:
    """Return ``page_path`` when signed in, else a 302 to the login page."""
    redirect = _redirect_when_unauthed(request, identity=identity)
    if redirect is not None:
        return redirect
    return FileResponse(page_path)


# Returns ``None`` when the caller is authed *or* when session auth is not configured on
# this deployment (in which case there is no login page to redirect to and
# ``current_user`` couldn't have resolved a session cookie anyway).
def _redirect_when_unauthed(
    request: Request,
    identity: AuthIdentity | None,
) -> RedirectResponse | None:
    """Return a 302 to ``/auth/login_page`` when the request is unauthed."""
    if identity is not None:
        return None
    config: object = getattr(_state(request), "config", None)
    session_secret: object = getattr(config, "session_secret", None)
    if not session_secret:
        return None
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
    return (created, session_id, IntCodec.coerce(part, 0), seq)


# Fields: ``title``, ``description``. Bare tokens search both. ``shlex.split`` reads
# the query as a shell line, which is wrong twice: ``\`` escapes the next character,
# so ``title:\d+`` reached Postgres as ``d+``, and ``'`` opens a quote, so ``don't``
# failed as unterminated. Only ``"`` groups, the grammar the search box's
# placeholder in ``assets/index.html`` states. ``#`` is not a comment either.
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
    relations = _relation_fields(type(inquiry))
    return {
        key: value for key, value in tag_row(inquiry).items() if key not in relations
    }


@cache
def _relation_fields(cls: type[Inquiry]) -> frozenset[str]:
    """Fields typed as edge endpoints, which only the ``edges`` table fills."""
    hints = get_type_hints(cls)
    relations: set[str] = set()
    for f in fields(cls):
        hint = cast(object, hints[f.name])
        peer: object = get_args(hint)[0] if get_origin(hint) is tuple else None
        if isinstance(peer, type) and issubclass(peer, InquiryEdge):
            relations.add(f.name)
    return frozenset(relations)


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
                "agent_usd": FloatCodec.coerce(
                    row[prefix + "marginal_cost_agent_usd"],
                ),
                "resource_usd": FloatCodec.coerce(
                    row[prefix + "marginal_cost_resource_usd"],
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
            out[column] = ListCodec.coerce(value, str)
        elif column == "experiment_codechanges":
            out[column] = [str(uid) for uid in ListCodec.coerce(value, UUID)]
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
        ref["labels"] = ListCodec.coerce(row["labels"], str)


class _AppState(Protocol):
    store: Store
    engine: DatabaseEngine
    # Populated lazily by ``_session_embedder``; ``config`` is set by the app
    # lifespan (absent in the duck-typed test apps, hence the getattr reads).
    config: object
    session_embedder: _QueryEmbedder | None
    # A/B override embedders, cached per stored name by ``_override_embedder`` so
    # repeated ``?model=`` queries reuse one loaded model.
    session_embedder_overrides: dict[str, _QueryEmbedder]


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
def _session_embedder(request: Request) -> _QueryEmbedder | None:
    """Return the process's default session-search embedder, or ``None``."""
    state = _state(request)
    cached = getattr(state, "session_embedder", ABSENT)
    if cached is not ABSENT:
        return cast("_QueryEmbedder | None", cached)
    # ``isinstance`` narrowing, not ``getattr(config, ...)``: the lifespan
    # stores a real ``Config`` (``api/app.py``), so a typed read means a field
    # rename breaks type-checking here instead of silently disabling the
    # semantic arm. Duck-typed test apps carry no config and read as None.
    config: object = getattr(state, "config", None)
    name = config.session_embedder if isinstance(config, Config) else ""
    dim = config.session_embedder_dim if isinstance(config, Config) else None
    embedder = cast(
        "_QueryEmbedder | None",
        registry.build_session_embedder(name, dim=dim),
    )
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
) -> _QueryEmbedder:
    """Return the cached A/B override embedder for ``(name, dim)``; 400 if unknown."""
    state = _state(request)
    cache = getattr(state, "session_embedder_overrides", ABSENT)
    if cache is ABSENT:
        cache = {}
        state.session_embedder_overrides = cache
    overrides = cast("dict[tuple[str, int | None], _QueryEmbedder]", cache)
    key = (name, dim)
    cached = overrides.get(key)
    if cached is not None:
        return cached
    try:
        embedder = registry.build_session_embedder(name, dim=dim)
    except ConfigError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    if embedder is None:
        raise HTTPException(status_code=400, detail="model must be non-empty")
    typed = cast("_QueryEmbedder", embedder)
    overrides[key] = typed
    return typed


def _search_embedder(
    request: Request,
    *,
    semantic: bool,
    model: str,
    dim: int | None,
) -> _QueryEmbedder | None:
    """Select the query embedder: the ``model``/``dim`` override, else the default."""
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
