"""Optional SPA + read endpoints for the trackinizer FastAPI app.

Mounted by ``trackinizer --web`` (or programmatically via :func:`attach`).
Adds:

- ``GET /`` -- the single-page SPA (``assets/index.html``).
- ``GET /static/*`` -- the SPA's static assets (when present).
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

from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, cast, get_args
from urllib.parse import quote
from uuid import UUID

import os
import re
import shlex
import uuid

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

import asyncpg

from trackinizer.lib.postgres import Conn, DatabaseEngine
from trackinizer.server.auth import (
    AuthIdentity,
    current_user,
    require_role,
)
from trackinizer.server.notify import iter_sse_events, tx
from trackinizer.server.store.core import Store
from trackinizer.server.values import vetted_sql
from trackinizer.types.change_log import Snapshot
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.wire_sessions import FeedCursor, FeedResponse


type WebView = dict[str, object]


router = APIRouter()


def _search_statement_timeout_ms() -> int:
    """Per-query search timeout in milliseconds (default 5s, env-overridable).

    Bounds the DB cost of one ``/api/web/search`` so a pathological POSIX
    regex cannot pin a backend. ``TRACKINIZER_SEARCH_TIMEOUT_MS`` overrides the
    5000ms default; a non-positive or non-integer value is an operator typo and
    raises rather than silently disabling the guard (``0`` means "no timeout"
    in Postgres, which is exactly the DoS this closes).
    """
    raw = os.environ.get("TRACKINIZER_SEARCH_TIMEOUT_MS", "").strip()
    if not raw:
        return 5_000
    timeout_ms = int(raw)
    if timeout_ms < 1:
        raise ValueError(
            f"TRACKINIZER_SEARCH_TIMEOUT_MS must be a positive integer, got {raw!r}"
        )
    return timeout_ms


_SEARCH_STATEMENT_TIMEOUT_MS = _search_statement_timeout_ms()
"""Resolved once at import; ``SET LOCAL statement_timeout`` bound for search."""


def get_store(request: Request) -> Store:
    return cast(Store, request.app.state.store)


# -- Read routes -------------------------------------------------------------


@router.get("/search")
async def web_search(
    request: Request,
    q: str,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    kind: Inquiry.InquiryKind | None = None,
    limit: int = 50,
) -> list[WebView]:
    """Cross-kind search. Grammar: every bare token must match across
    ``title``/``description``; ``title:re`` / ``description:re`` =
    field-scoped regex.
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
        await conn.execute(
            f"SET LOCAL statement_timeout = {_SEARCH_STATEMENT_TIMEOUT_MS}"
        )
        try:
            rows = await conn.fetch(sql, *params)
        except asyncpg.PostgresSyntaxError as exc:
            # ``_build_term_clause`` validates regex with Python's ``re``,
            # but Postgres ``~*`` runs POSIX -- patterns valid in Python
            # but not POSIX (``(?P<name>...)``, ``\d``, possessive
            # quantifiers, ...) trip Postgres at query time. Surface a
            # 400 rather than letting it leak as a 500.
            raise HTTPException(
                status_code=400, detail=f"invalid search pattern: {exc!s}"
            ) from exc
        except asyncpg.QueryCanceledError as exc:
            # The statement timeout fired: the regex was too expensive to run
            # within the per-query budget. A 400 (not 500) tells the caller the
            # pattern is the problem, mirroring the invalid-pattern path.
            raise HTTPException(
                status_code=400,
                detail=(
                    "search pattern exceeded the time budget; "
                    "narrow the regex or add more specific terms"
                ),
            ) from exc
    return [_row_to_dict(r) for r in rows]


@router.get("/recent_changes")
async def web_recent_changes(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    limit: int = 50,
) -> list[WebView]:
    """Most-recent ``change_log`` rows, snapshot deltas flattened."""
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
    """Resolve a UUID to its kind; 404 when not found."""
    del identity
    async with get_store(request).engine.acquire() as conn:
        kind = await conn.fetchval(
            "SELECT kind FROM inquiries WHERE id = $1", target_id
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
    """Single inquiry + edges + backlinks + recent changes, for the SPA."""
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
            _CHANGE_SELECT
            + " WHERE c.subject_id = $1 ORDER BY c.created DESC LIMIT 50",
            target_id,
        )
    return {
        "self": _row_to_dict(row),
        "edges": edges,
        "backlinks": backlinks,
        "changes": [_change_to_dict(c) for c in changes],
    }


_GRAPH_NODE_COLS = (
    "id, kind, seq, title, status, created, belief_judgement, belief_confidence"
)


@router.get("/graph")
async def web_graph(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    limit: int = 1000,
) -> WebView:
    """The inquiry graph as typed nodes + directed edges, for the SPA.

    ``limit`` caps the graph to the most-recently-created ``limit`` nodes, then
    EDGE-CLOSES that set: any older node referenced by an edge to a recent node
    is pulled back in, so an old-but-still-cited node (e.g. a foundational paper
    a new belief proves) stays visible and no edge is left dangling. The
    returned node count is therefore the recent N plus their older neighbours.
    ``limit=0`` returns the whole graph (unbounded -- only for small datasets).

    Nodes are the light projection (id, kind, seq, title, status, created,
    belief judgement/confidence), ordered by ``created`` ascending so the replay
    animation adds them in authoring order; edges carry ``from_id`` -> ``to_id``,
    ``edge_kind``, and ``valence`` when present. The detail view
    (:func:`web_get`) serves the full per-kind fields for one node on demand.
    """
    del identity
    if limit < 0 or limit > 50_000:
        raise HTTPException(status_code=400, detail="limit must be in [0, 50000]")
    async with get_store(request).engine.acquire() as conn:
        if limit == 0:
            node_rows = await conn.fetch(
                vetted_sql(
                    "SELECT ",
                    _GRAPH_NODE_COLS,
                    " FROM inquiries ORDER BY created ASC, id ASC",
                )
            )
            edge_rows = await conn.fetch(
                "SELECT from_id, to_id, edge_kind, valence FROM edges"
            )
            return {
                "nodes": [_graph_node(r) for r in node_rows],
                "edges": [_graph_edge(r) for r in edge_rows],
            }
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
    """
    del identity
    engine = cast(DatabaseEngine, request.app.state.engine)
    return StreamingResponse(iter_sse_events(engine), media_type="text/event-stream")


@router.get("/feed")
async def web_feed(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    after_created: datetime | None = None,
    after_session: UUID | None = None,
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

    ``after_created`` / ``after_session`` / ``after_seq`` together form the
    composite keyset cursor a poll resumes past (all three are required to
    resume; the order key is composite so a bare ``created`` resume would skip
    same-instant ties). ``since`` / ``until`` bound a fixed historical window;
    ``room`` / ``actor`` filter by routing identity. ``tail=true`` returns the
    newest page (the live console's first load) so a backlog does not force a
    replay from the beginning. The response carries ``next_after`` (a composite
    cursor) for the next poll; an empty page echoes the supplied cursor so the
    tail does not rewind.
    """
    del identity
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be in [1, 1000]")
    after = _feed_cursor(after_created, after_session, after_seq)
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
            created=last.created, session_id=last.session_id, seq=last.seq
        )
    elif after is not None:
        next_after = FeedCursor(created=after[0], session_id=after[1], seq=after[2])
    else:
        next_after = None
    return FeedResponse(events=events, next_after=next_after)


def _feed_cursor(
    created: datetime | None,
    session_id: UUID | None,
    seq: int | None,
) -> tuple[datetime, UUID, int] | None:
    """Assemble the composite feed cursor, or ``None`` when unset.

    All three parts must be present together: a partial cursor cannot resume
    the ``(created, session_id, seq)`` order and is a client error.
    """
    parts = (created, session_id, seq)
    if all(p is None for p in parts):
        return None
    if any(p is None for p in parts):
        raise HTTPException(
            status_code=400,
            detail="after_created, after_session, after_seq must be given together",
        )
    return (cast(datetime, created), cast(UUID, session_id), cast(int, seq))


def graph_legend() -> dict[str, list[str]]:
    """The node-kind and edge-kind sets the graph view colors by.

    The single source the SPA reads to build its color legend, derived from the
    domain enums so it cannot drift: a new ``Inquiry`` subclass or ``Edge.Kind``
    appears here automatically (pinned by ``web_test.py``).
    """
    return {
        "node_kinds": list(get_args(Inquiry.InquiryKind.__value__)),
        "edge_kinds": list(get_args(Edge.Kind.__value__)),
    }


# -- Search query parsing ----------------------------------------------------


def _parse_query(q: str) -> list[tuple[str | None, str]]:
    """Tokenize a search query into ``(field, pattern)`` terms.

    Fields: ``title``, ``description``. Bare tokens search both.
    """
    tokens = shlex.split(q)
    out: list[tuple[str | None, str]] = []
    for tok in tokens:
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


def _build_term_clause(
    terms: Sequence[tuple[str | None, str]],
) -> tuple[str, list[object]]:
    """Render ``terms`` as a SQL AND clause + bind params.

    Multiple bare tokens (``foo bar``) intersect: a row must match every
    token. Field-qualified tokens (``title:^x$``) compose with the
    same AND semantics. Field-qualified regexes are validated client-
    side via :func:`re.compile` so a malformed pattern surfaces as a
    :class:`ValueError` -- the route turns that into 400, not 500.
    """
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
                f"(title ILIKE {ph} ESCAPE '\\' OR description ILIKE {ph} ESCAPE '\\')"
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


# -- JSON serialization ------------------------------------------------------


def _graph_node(row: asyncpg.Record) -> WebView:
    """Light node projection for the graph view.

    Carries id, kind, seq, title, status (so the view can dim retired
    ``abandoned`` / ``invalid`` nodes), created (the replay order key), and --
    for Beliefs -- judgement + confidence so the view can encode a claim's
    verdict and certainty. The two belief fields are emitted only when present,
    so a non-Belief row stays clean.
    """
    out: WebView = {
        "id": str(row["id"]),
        "kind": row["kind"],
        "seq": row["seq"],
        "title": row["title"] or "",
        "status": row["status"],
        "created": _isoformat(row["created"]),
    }
    if row["belief_judgement"] is not None:
        out["judgement"] = row["belief_judgement"]
    if row["belief_confidence"] is not None:
        out["confidence"] = row["belief_confidence"]
    return out


def _graph_edge(row: asyncpg.Record) -> WebView:
    """Typed directed link for the graph view.

    ``valence`` is present only on ``proves`` / ``favors`` citations (NULL on
    structural edges); the key is omitted when absent so the SPA never has to
    special-case a null.
    """
    out: WebView = {
        "from_id": str(row["from_id"]),
        "to_id": str(row["to_id"]),
        "edge_kind": row["edge_kind"],
    }
    if row["valence"] is not None:
        out["valence"] = row["valence"]
    return out


def _row_to_dict(row: asyncpg.Record) -> WebView:
    """Flatten an ``inquiries`` row to JSON for the SPA.

    Kind-specific columns surface only when populated on this row;
    NULL columns are omitted so the JSON doesn't carry irrelevant
    nulls per kind.
    """
    out: WebView = {
        "id": str(row["id"]),
        "kind": row["kind"],
        "seq": row["seq"],
        "owner": row["owner"] or "",
        "account": row["account"],
        "status": row["status"],
        "title": row["title"] or "",
        "description": row["description"] or "",
        "labels": list(row["labels"] or []),
        "subscribers": list(row["subscribers"] or []),
        "marginal_cost": {
            "agent_usd": float(row["marginal_cost_agent_usd"]),
            "resource_usd": float(row["marginal_cost_resource_usd"]),
        },
        "created": _isoformat(row["created"]),
        "modified": _isoformat(row["modified"]),
    }
    # (bare wire key, prefixed storage column). The SPA detail view is
    # kind-scoped, so it speaks the bare field name; the column is the
    # flat storage name.
    for key, column in (
        ("judgement", "belief_judgement"),
        ("confidence", "belief_confidence"),
        ("issue_kind", "issue_kind"),
        ("validation", "issue_validation"),
        ("priority", "issue_priority"),
        ("outcome", "experiment_outcome"),
        ("abstract", "paper_abstract"),
        ("publication_type", "paper_publication_type"),
        ("venue", "paper_venue"),
        ("subvenue", "paper_subvenue"),
        ("source", "paper_source"),
        ("google_scholar_cluster_id", "paper_google_scholar_cluster_id"),
        ("google_scholar_cites_id", "paper_google_scholar_cites_id"),
        ("sha", "codechange_sha"),
        ("url", "webresult_url"),
        ("query", "websearch_query"),
        ("provider", "websearch_provider"),
        ("cli", "agentsession_cli"),
        ("cli_session_id", "agentsession_cli_session_id"),
    ):
        if column in row and row[column] is not None:
            out[key] = row[column]
    # Timestamps are TIMESTAMPTZ; ISO-format them like ``created`` / ``modified``
    # rather than leaking raw datetimes.
    for key, column in (
        ("started", "agentsession_started"),
        ("ended", "agentsession_ended"),
        ("publish_date", "paper_publish_date"),
    ):
        if column in row and row[column] is not None:
            out[key] = _isoformat(row[column])
    if "agentsession_rooms" in row and row["agentsession_rooms"] is not None:
        out["rooms"] = list(row["agentsession_rooms"])
    if "paper_authors" in row and row["paper_authors"] is not None:
        out["authors"] = list(row["paper_authors"])
    if "experiment_codechanges" in row and row["experiment_codechanges"] is not None:
        out["codechanges"] = [str(uid) for uid in row["experiment_codechanges"]]
    # ``experiment_config`` is JSONB -> the registered codec already decoded it
    # to a dict; surface it verbatim (unlike the scalar columns above, it is not
    # ISO-formatted). Only present on Experiment rows.
    if "experiment_config" in row and row["experiment_config"] is not None:
        out["config"] = row["experiment_config"]
    return out


_SNAPSHOT_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(Snapshot))

_CHANGE_SELECT = (
    "SELECT c.*, COALESCE(k_user.email, actor_user.email) AS principal "
    "FROM change_log c "
    "LEFT JOIN api_keys k ON k.id = c.api_key_id "
    "LEFT JOIN users k_user ON k_user.id = k.user_id "
    "LEFT JOIN users actor_user ON actor_user.email = c.actor AND c.api_key_id IS NULL"
)


def _change_to_dict(row: asyncpg.Record) -> WebView:
    """Flatten a ``change_log`` row to JSON.

    Identity columns surface at top level; the flat ``old_*`` / ``new_*``
    snapshot columns gather into nested ``old`` / ``new`` objects with
    only-populated keys present.
    """
    api_key_id = row.get("api_key_id")
    out: WebView = {
        "id": str(row["id"]),
        "created": _isoformat(row["created"]),
        "actor": row["actor"],
        "principal": row.get("principal") or "",
        "api_key_id": (None if api_key_id is None else str(api_key_id)),
        "subject_id": str(row["subject_id"]),
        "subject_kind": row["subject_kind"],
        "kind": row["kind"],
        "caused_by": str(row["caused_by"]) if row["caused_by"] else None,
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
                "agent_usd": float(row[prefix + "marginal_cost_agent_usd"] or 0),
                "resource_usd": float(row[prefix + "marginal_cost_resource_usd"] or 0),
            }
            continue
        row_column = prefix + column
        if row_column not in row:
            continue
        value = row[row_column]
        if value is None:
            continue
        if column in ("labels", "subscribers", "issue_kind"):
            out[column] = list(cast(list[str], value))
        elif column == "experiment_codechanges":
            out[column] = [str(uid) for uid in cast(list[UUID], value)]
        elif isinstance(value, UUID):
            out[column] = str(value)
        else:
            out[column] = value
    return out


_PEER_COLUMNS = (
    "t.kind AS peer_kind, t.seq AS peer_seq, t.title AS peer_title, "
    "t.status AS peer_status, t.belief_judgement AS peer_judgement"
)
"""SELECT fragment for the joined inquiry on the far end of an edge."""


def _peer_ref(row: asyncpg.Record, peer_id: UUID) -> WebView:
    """Build a UI ref ``{id, kind, seq, title, status, judgement?}``."""
    out: WebView = {
        "id": str(peer_id),
        "kind": row["peer_kind"],
        "seq": row["peer_seq"],
        "title": row["peer_title"] or "",
        "status": row["peer_status"],
    }
    if row["peer_judgement"] is not None:
        out["judgement"] = row["peer_judgement"]
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
    out: dict[str, list[WebView]] = {}
    for row in rows:
        ref = _peer_ref(row, cast(UUID, row[peer_col]))
        _add_edge_annotation(ref, row)
        out.setdefault(row["edge_kind"], []).append(ref)
    return cast(WebView, out)


def _add_edge_annotation(ref: WebView, row: asyncpg.Record) -> None:
    """Attach edge-local metadata to a peer ref."""
    if row["priority"] is not None:
        ref["priority"] = row["priority"]
    if row["note"]:
        ref["note"] = row["note"]
    if row["valence"] is not None:
        ref["valence"] = row["valence"]
    if row["labels"]:
        ref["labels"] = list(row["labels"])


def _isoformat(value: object) -> str:
    """ISO-format a datetime; fall back to ``str()`` for non-datetimes."""
    return value.isoformat() if isinstance(value, datetime) else str(value)


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
    """
    if not hasattr(request.app.state, "engine"):
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
) -> None:
    """Mount the read-API router, the SPA at ``/``, and ``/static/*``.

    ``/static`` is served from ``static_dir`` when given, else from the SPA's
    own ``assets/static`` -- the runtime override lets an operator serve files
    written after deploy (e.g. a generated report) without copying them into
    the source tree, while the default keeps the SPA's bundled assets working.

    Idempotent: a second call is a no-op. ``server.py`` attaches the
    module-global app at import, so a re-import or test reuse must not stack
    duplicate routes (TRK-SRV-002).
    """
    if getattr(app.state, "web_attached", False):
        return
    app.state.web_attached = True
    assets = assets_dir or (Path(__file__).parent / "assets")
    app.include_router(router, prefix="/api/web")

    static = static_dir or (assets / "static")
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=str(static)), name="static")

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
            return _serve_admin_page(request, identity, self.page_path)
        return _serve_if_authed(request, identity, self.page_path)


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
    redirect = _redirect_when_unauthed(request, identity)
    if redirect is not None:
        return redirect
    if identity is None or identity.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return FileResponse(page_path)


def _serve_if_authed(
    request: Request,
    identity: AuthIdentity | None,
    page_path: Path,
) -> Response:
    """Return ``page_path`` when signed in, else a 302 to the login page.

    When session auth is unconfigured (no ``session_secret`` on
    ``app.state.config``) the redirect is suppressed -- the deployment
    isn't running OAuth and gating the SPA behind a login that doesn't
    exist would lock everyone out.
    """
    redirect = _redirect_when_unauthed(request, identity)
    if redirect is not None:
        return redirect
    return FileResponse(page_path)


def _redirect_when_unauthed(
    request: Request,
    identity: AuthIdentity | None,
) -> RedirectResponse | None:
    """Return a 302 to ``/auth/login_page`` when the request is unauthed.

    Returns ``None`` when the caller is authed *or* when session auth
    is not configured on this deployment (in which case there is no
    login page to redirect to and ``current_user`` couldn't have
    resolved a session cookie anyway).
    """
    if identity is not None:
        return None
    config = getattr(request.app.state, "config", None)
    session_secret = getattr(config, "session_secret", None) if config else None
    if not session_secret:
        return None
    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    return RedirectResponse(
        url=f"/auth/login_page?next={quote(next_url, safe='')}",
        status_code=302,
    )
