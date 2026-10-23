"""Place an agent run's session in a workspace and a canvas.

A placed session has exactly one ``produced_by`` edge to a workspace Issue, one
``canvas:<canvas uuid>`` label and, on a git branch, one ``git:<origin>@<branch>``
label. A canvas holds many sessions; one made for a session also remembers it as
``CanvasMetadata.session_id``, which never changes who Chat talks to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit, urlunsplit

import uuid

from treekle import from_plain

from trackinizer.server.notify import tx
from trackinizer.server.visuals.canvas_store import new_session_canvas
from trackinizer.server.visuals.workspaces import check_workspace, open_scratch
from trackinizer.wire.wire_science_chat import CHAT_SESSION_PREFIX
from trackinizer.wire.wire_service_accounts import is_service_address
from trackinizer.wire.wire_sessions import (
    CANVAS_LABEL_PREFIX,
    GIT_LABEL_PREFIX,
    GitRef,
    PlaceSession,
    SessionPlacement,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.store.core import Store
    from trackinizer.server.visuals.catalog import VisualCatalogBody
    from trackinizer.types.inquiries import AgentSession


_CANVAS_SESSIONS: Final = 200
"""The most sessions one canvas move rewrites."""

_WORKSPACE_EDGES: Final = (
    "SELECT e.to_id FROM edges AS e JOIN inquiries AS issue ON issue.id = e.to_id "
    "WHERE e.from_id = $1 AND e.edge_kind = 'produced_by' AND e.to_kind = 'Issue' "
    "AND issue.labels @> ARRAY['workspace']::text[]"
)


def git_label(git: GitRef) -> str:
    """Return the ``git:`` label of a checkout, its origin URL without credentials.

    Args:
      git: The origin URL and branch the client sent.

    Returns:
      label: ``git:<origin>@<branch>``.

    """
    origin = git.origin.strip()
    parts = urlsplit(origin)
    if parts.scheme and "@" in parts.netloc:
        origin = urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
    return f"{GIT_LABEL_PREFIX}{origin}@{git.branch.strip()}"


def is_placed(session: AgentSession) -> bool:
    """Say whether a session is one a run places: a person's own agent run.

    Args:
      session: The session.

    Returns:
      placed: False for a service account and a science chat.

    """
    return not (
        is_service_address(session.account)
        or (session.cli_session_id or "").startswith(CHAT_SESSION_PREFIX)
    )


async def place_session(
    engine: DatabaseEngine,
    *,
    store: Store,
    session: AgentSession,
    user_id: uuid.UUID,
    email: str,
    api_key_id: uuid.UUID | None,
    body: PlaceSession,
    catalog: VisualCatalogBody,
) -> SessionPlacement:
    """Place ``session`` by the first rule that matches, and record it.

    1. ``body.canvas`` is the caller's canvas: that canvas and its workspace.
    2. The session is already placed, or has a canvas made for it: keep them.
    3. The caller's newest earlier session on the same git branch has a workspace:
       that workspace, in a new canvas.
    4. The caller's Scratch workspace, in a new canvas.

    Placing a placed session returns its placement. The labels and the edge follow
    the canvas's commit, so a retry converges.

    Args:
      engine: Database connection source.
      store: Store that writes the labels and the edge.
      session: The session, opened by the caller's own key.
      user_id: The caller.
      email: The caller's account.
      api_key_id: The key that opened the session.
      body: The canvas the run started from and its checkout.
      catalog: Trusted visual definitions for a new canvas.

    Returns:
      placement: The workspace and canvas; both ``None`` for a session not placed.

    """
    if not is_placed(session):
        return SessionPlacement()
    label = None if body.git is None else git_label(body.git)
    scratch = await open_scratch(engine, store=store, user_id=user_id, email=email)
    async with engine.acquire() as conn, tx(conn):
        # Held to the commit, so two placements of one session make one canvas.
        await conn.execute(
            "SELECT 1 FROM inquiries WHERE id = $1 FOR UPDATE",
            session.id,
        )
        workspace_id, canvas_id = await _decide(
            conn,
            session=session,
            user_id=user_id,
            email=email,
            canvas=body.canvas,
            git=label,
            scratch=scratch,
        )
        if canvas_id is None:
            canvas_id = await new_session_canvas(
                conn,
                user_id=user_id,
                name=f"{session.owner or email} {session.cli or 'session'}",
                catalog=catalog,
                workspace_id=workspace_id,
                session_id=session.id,
            )
    await _record(
        store,
        current=tuple(session.labels or ()),
        session=session,
        api_key_id=api_key_id,
        email=email,
        workspace_id=workspace_id,
        wanted=[
            f"{CANVAS_LABEL_PREFIX}{canvas_id}",
            *([] if label is None else [label]),
        ],
    )
    return SessionPlacement(workspace_id=workspace_id, canvas_id=canvas_id)


async def move_session(
    engine: DatabaseEngine,
    *,
    store: Store,
    session: AgentSession,
    email: str,
    api_key_id: uuid.UUID | None,
    workspace_id: uuid.UUID,
) -> bool:
    """Move one session to an owned workspace, and its made-for canvas if it is alone.

    Args:
      engine: Database connection source.
      store: Store that writes the edge.
      session: The session to move.
      email: The caller's account, which owns the workspace.
      api_key_id: The calling key, if any.
      workspace_id: The destination.

    Returns:
      moved: False when the destination is not the caller's workspace.

    """
    if not await check_workspace(engine, email=email, workspace_id=workspace_id):
        return False
    await _set_workspace(
        store,
        session_id=session.id,
        workspace_id=workspace_id,
        api_key_id=api_key_id,
        actor=email,
    )
    async with engine.acquire() as conn:
        # The canvas made for it moves only if no other session sits on it.
        await conn.execute(
            "UPDATE visual_canvases AS w SET metadata = (coalesce(w.metadata, "
            "'{}'::jsonb) - 'suggestion') || jsonb_build_object('workspace_id', "
            "$2::text), modified_at = clock_timestamp() "
            "WHERE w.metadata->>'session_id' = $3 AND NOT EXISTS ("
            "SELECT 1 FROM inquiries AS other WHERE other.kind = 'AgentSession' "
            "AND other.id <> $1 AND other.labels @> ARRAY[$4 || w.id::text])",
            session.id,
            str(workspace_id),
            str(session.id),
            CANVAS_LABEL_PREFIX,
        )
    return True


async def move_canvas_sessions(
    engine: DatabaseEngine,
    *,
    store: Store,
    canvas_id: uuid.UUID,
    email: str,
    api_key_id: uuid.UUID | None,
    workspace_id: uuid.UUID,
) -> None:
    """Move the workspace edge of every session placed on a canvas.

    Args:
      engine: Database connection source.
      store: Store that writes the edges.
      canvas_id: The canvas that moved.
      email: The caller's account; only its sessions move.
      api_key_id: The calling key, if any.
      workspace_id: The canvas's new workspace.

    """
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM inquiries WHERE kind = 'AgentSession' AND account = $1 "
            "AND $2 = ANY(labels) ORDER BY created DESC, id LIMIT $3",
            email,
            f"{CANVAS_LABEL_PREFIX}{canvas_id}",
            _CANVAS_SESSIONS,
        )
    for row in rows:
        await _set_workspace(
            store,
            session_id=from_plain(row["id"], uuid.UUID),
            workspace_id=workspace_id,
            api_key_id=api_key_id,
            actor=email,
        )


async def _decide(
    conn: Conn,
    *,
    session: AgentSession,
    user_id: uuid.UUID,
    email: str,
    canvas: uuid.UUID | None,
    git: str | None,
    scratch: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Apply the four rules; the canvas is ``None`` when a new one is to be made."""
    if canvas is not None:
        row = await conn.fetchrow(
            "SELECT metadata->>'workspace_id' AS workspace FROM visual_canvases "
            "WHERE id = $1 AND user_id = $2",
            canvas,
            user_id,
        )
        if row is not None:
            owned = await _owned(conn, email=email, workspace=row["workspace"])
            return owned or scratch, canvas
    edge = await conn.fetchval(_WORKSPACE_EDGES, session.id)
    made = await conn.fetchval(
        "SELECT id FROM visual_canvases WHERE user_id = $1 "
        "AND (id::text = $3 OR metadata->>'session_id' = $2) ORDER BY created_at, id LIMIT 1",
        user_id,
        str(session.id),
        next(
            (
                label.removeprefix(CANVAS_LABEL_PREFIX)
                for label in session.labels or ()
                if label.startswith(CANVAS_LABEL_PREFIX)
            ),
            "-",
        ),
    )
    if edge is not None or made is not None:
        workspace = from_plain(edge, uuid.UUID) if edge is not None else scratch
        return workspace, None if made is None else from_plain(made, uuid.UUID)
    if git is not None:
        earlier = await conn.fetchval(
            "SELECT e.to_id FROM inquiries AS past JOIN edges AS e "
            "ON e.from_id = past.id AND e.edge_kind = 'produced_by' "
            "AND e.to_kind = 'Issue' JOIN inquiries AS issue ON issue.id = e.to_id "
            "WHERE past.kind = 'AgentSession' AND past.account = $1 "
            "AND $2 = ANY(past.labels) AND past.id <> $3 AND issue.owner = $1 "
            "AND past.created <= $4 "
            "AND issue.labels @> ARRAY['workspace']::text[] "
            "ORDER BY past.created DESC, past.id DESC LIMIT 1",
            email,
            git,
            session.id,
            session.created,
        )
        if earlier is not None:
            return from_plain(earlier, uuid.UUID), None
    return scratch, None


async def _owned(
    conn: Conn,
    *,
    email: str,
    workspace: object,
) -> uuid.UUID | None:
    """Return a canvas's stored workspace id if it is an owned workspace Issue."""
    if workspace is None:
        return None
    found = await conn.fetchval(
        "SELECT id FROM inquiries WHERE id = $1::uuid AND kind = 'Issue' "
        "AND owner = $2 AND labels @> ARRAY['workspace']::text[]",
        from_plain(workspace, str),
        email,
    )
    return None if found is None else from_plain(found, uuid.UUID)


async def _record(
    store: Store,
    *,
    current: tuple[str, ...],
    session: AgentSession,
    api_key_id: uuid.UUID | None,
    email: str,
    workspace_id: uuid.UUID,
    wanted: list[str],
) -> None:
    """Make the session's edge and its canvas and git labels exactly the wanted."""
    await _set_workspace(
        store,
        session_id=session.id,
        workspace_id=workspace_id,
        api_key_id=api_key_id,
        actor=email,
    )
    families = (CANVAS_LABEL_PREFIX, GIT_LABEL_PREFIX)
    for old in current:
        if old.startswith(families) and old not in wanted:
            _ = await store.remove_label(
                session.id,
                old,
                api_key_id=api_key_id,
                actor=email,
            )
    for label in wanted:
        if label not in current:
            _ = await store.add_label(
                session.id,
                label,
                api_key_id=api_key_id,
                actor=email,
            )


async def _set_workspace(
    store: Store,
    *,
    session_id: uuid.UUID,
    workspace_id: uuid.UUID,
    api_key_id: uuid.UUID | None,
    actor: str,
) -> None:
    """Leave one ``produced_by`` edge from the session to a workspace: the given."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(_WORKSPACE_EDGES, session_id)
    for row in rows:
        old = from_plain(row["to_id"], uuid.UUID)
        if old != workspace_id:
            _ = await store.remove_edge(
                from_id=session_id,
                to_id=old,
                edge_kind="produced_by",
                api_key_id=api_key_id,
                actor=actor,
            )
    _ = await store.add_edge(
        from_id=session_id,
        to_id=workspace_id,
        edge_kind="produced_by",
        api_key_id=api_key_id,
        actor=actor,
    )
