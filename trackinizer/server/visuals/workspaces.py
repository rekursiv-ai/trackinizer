"""Per-user workspaces backed by owned Trackinizer Issues.

One of a user's workspaces is their default: a chat or a session with no other home
lands there. It is an ordinary workspace otherwise, and renaming it makes it one.
"""

from __future__ import annotations

from datetime import datetime
from itertools import count
from typing import TYPE_CHECKING, Final, Literal

import re
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from treekle import from_plain

from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.server.preferences import (
    WorkspacesPreference,
    stored_preferences,
    write_preferences,
)
from trackinizer.server.visuals.canvases import CanvasMetadata
from trackinizer.server.visuals.science_chats import read_chat_sessions
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.store.core import Store


_DEFAULT_LABEL: Final = "scratch"
"""The label beside ``workspace`` that marks a user's default workspace.

The default was called Scratch, and every one made then carries this label; a new
name for it would leave each of them unmarked, and a build that still calls the
default Scratch reads the same mark.
"""

_UNTITLED: Final = re.compile(r"Untitled workspace(?: ([0-9]+))?")


class WorkspaceSummary(BaseModel):
    """One workspace in the user's bar."""

    id: uuid.UUID
    seq: int
    title: str
    scratch: bool = Field(
        description=(
            "Whether this is your default workspace, where a chat or a session with "
            "no other home lands. Renaming it makes it an ordinary workspace."
        ),
    )
    hidden: bool
    pinned: bool
    last_canvas_id: str | None = Field(
        default=None,
        description=(
            "The canvas the user opened last here, which switching to the workspace "
            "opens; it may since be hidden, moved or gone."
        ),
    )


class WorkspaceCanvas(BaseModel):
    """A canvas attached to one workspace."""

    id: uuid.UUID
    name: str
    hidden: bool
    pinned: bool
    conversation_id: uuid.UUID | None
    session_ids: list[uuid.UUID]


class WorkspaceInfo(WorkspaceSummary):
    """A workspace and its attached canvases."""

    canvases: list[WorkspaceCanvas]


class CreateWorkspace(BaseModel):
    """A new workspace's title."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def _trim_title(cls, title: str) -> str:
        title = title.strip()
        if not title:
            raise ValueError("A workspace title is 1 to 120 characters.")
        return title


class UpdateWorkspace(BaseModel):
    """Fields a user may change on a workspace."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)
    hidden: bool | None = None
    pinned: bool | None = None

    @field_validator("title")
    @classmethod
    def _trim_title(cls, title: str | None) -> str | None:
        if title is None:
            return None
        title = title.strip()
        if not title:
            raise ValueError("A workspace title is 1 to 120 characters.")
        return title

    @model_validator(mode="after")
    def _requires_change(self) -> UpdateWorkspace:
        if self.title is None and self.hidden is None and self.pinned is None:
            raise ValueError("At least one workspace field is required.")
        return self


class WorkspaceOrder(BaseModel):
    """The ids in their chosen workspace bar order."""

    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] = Field(max_length=500)

    @field_validator("ids")
    @classmethod
    def _distinct_ids(cls, ids: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(ids) != len(set(ids)):
            raise ValueError("Workspace ids must be distinct.")
        return ids


class SetCanvasWorkspace(BaseModel):
    """The workspace a canvas is moved to."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID


async def open_default_workspace(
    engine: DatabaseEngine,
    *,
    store: Store,
    user_id: uuid.UUID,
    email: str,
) -> uuid.UUID:
    """Find or make the user's default workspace, for something with no other home.

    Args:
      engine: Database connection source.
      store: Store used to submit a new default in the locked transaction.
      user_id: The owner of the users row to lock while making one.
      email: The user's account and Issue owner.

    Returns:
      issue_id: The default workspace's Issue id.

    """
    async with engine.acquire() as conn:
        row = await _default_row(conn, email=email)
    if row is not None:
        return from_plain(row["id"], uuid.UUID)
    async with notify_after_commit(), engine.acquire() as conn, tx(conn):
        return await default_workspace_in(
            conn,
            store=store,
            user_id=user_id,
            email=email,
        )


async def default_workspace_in(
    conn: Conn,
    *,
    store: Store,
    user_id: uuid.UUID,
    email: str,
) -> uuid.UUID:
    """Find or make the user's default workspace inside the caller's transaction.

    The users row is locked before one is made, so two callers make one. A new one
    is titled with :func:`next_untitled_title` over the user's own workspaces.

    Args:
      conn: A connection inside the caller's transaction.
      store: Store used to submit a new default on ``conn``.
      user_id: The owner of the users row to lock while making one.
      email: The user's account and Issue owner.

    Returns:
      issue_id: The default workspace's Issue id.

    Raises:
      ValueError: The user is gone.

    """
    row = await _default_row(conn, email=email)
    if row is not None:
        return from_plain(row["id"], uuid.UUID)
    user = await conn.fetchrow("SELECT id FROM users WHERE id = $1 FOR UPDATE", user_id)
    if user is None:
        raise ValueError("User not found")
    row = await _default_row(conn, email=email)
    if row is not None:
        return from_plain(row["id"], uuid.UUID)
    titles = await conn.fetch(
        "SELECT title FROM inquiries WHERE kind = 'Issue' AND owner = $1 "
        "AND labels @> ARRAY['workspace']::text[]",
        email,
    )
    return await store.submit_issue(
        SubmitIssue(
            title=next_untitled_title(from_plain(r["title"], str) for r in titles),
            account=email,
            owner=email,
            actor=email,
            labels=["workspace", _DEFAULT_LABEL],
            status="active",
        ),
        actor=email,
        conn=conn,
    )


def next_untitled_title(titles: Iterable[str]) -> str:
    """Title a workspace ``Untitled workspace N`` with the lowest N no title holds.

    ``Untitled workspace`` holds 1, so the first is that and the next
    ``Untitled workspace 2``.

    Args:
      titles: The titles of the user's own workspaces.

    Returns:
      title: The lowest free title.

    """
    taken = {
        int(match[1] or 1) for title in titles if (match := _UNTITLED.fullmatch(title))
    }
    number = next(n for n in count(1) if n not in taken)
    return "Untitled workspace" if number == 1 else f"Untitled workspace {number}"


async def create_workspace(
    engine: DatabaseEngine,
    *,
    store: Store,
    user_id: uuid.UUID,
    email: str,
    title: str,
) -> WorkspaceSummary:
    """Submit an owned workspace Issue and return its bar entry.

    Args:
      engine: Database connection source.
      store: Store that submits the Issue.
      user_id: The owner of the workspace preferences.
      email: The user's account and Issue owner.
      title: The workspace's title.

    Returns:
      summary: The created workspace.

    """
    checked = CreateWorkspace(title=title)
    issue_id = await store.submit_issue(
        SubmitIssue(
            title=checked.title,
            account=email,
            owner=email,
            actor=email,
            labels=["workspace"],
            status="active",
        ),
        actor=email,
    )
    entries = await list_workspaces(
        engine,
        user_id=user_id,
        email=email,
        include_hidden=True,
    )
    return next(entry for entry in entries if entry.id == issue_id)


async def list_workspaces(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    q: str | None = None,
    include_hidden: bool = False,
) -> list[WorkspaceSummary]:
    """List owned workspace Issues in pin, saved-order, then newest order.

    Args:
      engine: Database connection source.
      user_id: The preference owner.
      email: The Issue owner to query.
      q: Optional title substring.
      include_hidden: Whether to include hidden workspaces.

    Returns:
      workspaces: Matching Issues in bar order.

    """
    async with engine.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT preferences FROM users WHERE id = $1",
            user_id,
        )
        if user is None:
            return []
        preference = stored_preferences(user["preferences"]).workspaces
        rows = await conn.fetch(
            "SELECT id, seq, title, modified FROM inquiries WHERE kind = 'Issue' "
            "AND owner = $1 AND labels @> ARRAY['workspace']::text[]",
            email,
        )
        default = await _default_row(conn, email=email)
    default_id = None if default is None else from_plain(default["id"], uuid.UUID)
    hidden_ids = set(preference.hidden)
    pinned_ids = set(preference.pinned)
    order_index = {
        workspace_id: index for index, workspace_id in enumerate(preference.order)
    }
    summaries = [
        _summary(
            row,
            default_id=default_id,
            hidden_ids=hidden_ids,
            pinned_ids=pinned_ids,
            last_canvas=preference.last_canvas,
            q=q,
        )
        for row in rows
    ]
    found = [
        summary
        for summary in summaries
        if summary is not None and (include_hidden or not summary.hidden)
    ]
    modified_by_id = {
        from_plain(row["id"], uuid.UUID): from_plain(row["modified"], datetime)
        for row in rows
    }
    found.sort(
        key=lambda item: (
            not item.pinned,
            order_index.get(str(item.id), len(order_index)),
            -modified_by_id[item.id].timestamp(),
        ),
    )
    return found


async def update_workspace(
    engine: DatabaseEngine,
    *,
    store: Store,
    user_id: uuid.UUID,
    email: str,
    workspace_id: uuid.UUID,
    title: str | None = None,
    hidden: bool | None = None,
    pinned: bool | None = None,
) -> WorkspaceSummary | None:
    """Update a workspace title or its per-user hidden and pinned flags.

    A title given for the default makes it an ordinary workspace: the canvases it
    holds by having no workspace are given its id, and it loses its mark, so the next
    thing with no other home makes a new default.

    Args:
      engine: Database connection source.
      store: Store used to record a title change.
      user_id: The preference owner.
      email: The user's Issue owner.
      workspace_id: Workspace to update.
      title: New title, or None to leave it unchanged.
      hidden: New hidden state, or None to leave it unchanged.
      pinned: New pinned state, or None to leave it unchanged.

    Returns:
      summary: The changed workspace, or None when it is foreign or absent.

    """
    checked = UpdateWorkspace(title=title, hidden=hidden, pinned=pinned)
    if not await check_workspace(engine, email=email, workspace_id=workspace_id):
        return None
    async with engine.acquire() as conn, tx(conn):
        default = await _default_row(conn, email=email)
        promoted = (
            checked.title is not None
            and default is not None
            and from_plain(default["id"], uuid.UUID) == workspace_id
        )
        workspaces = await _lock_preferences(conn, user_id=user_id)
        if checked.hidden is not None:
            workspaces = _toggle_id(
                workspaces,
                "hidden",
                workspace_id,
                checked.hidden,
            )
        if checked.pinned is not None:
            workspaces = _toggle_id(
                workspaces,
                "pinned",
                workspace_id,
                checked.pinned,
            )
        await _save_preferences(
            conn,
            user_id=user_id,
            workspaces=workspaces,
        )
        if promoted:
            await _keep_loose_canvases(conn, user_id=user_id, workspace_id=workspace_id)
    if checked.title is not None:
        await store.set_title(workspace_id, checked.title, actor=email)
    if promoted:
        await store.remove_label(workspace_id, _DEFAULT_LABEL, actor=email)
    entries = await list_workspaces(
        engine,
        user_id=user_id,
        email=email,
        include_hidden=True,
    )
    return next((entry for entry in entries if entry.id == workspace_id), None)


async def set_workspace_order(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    ids: list[uuid.UUID],
) -> list[WorkspaceSummary] | None:
    """Save the user's workspace bar order and return the visible entries.

    Args:
      engine: Database connection source.
      user_id: The preference owner.
      email: The user's Issue owner.
      ids: Workspace ids in their requested order.

    Returns:
      workspaces: The updated list, or None if an id is foreign or absent.

    """
    checked = WorkspaceOrder(ids=ids)
    async with engine.acquire() as conn, tx(conn):
        if checked.ids:
            rows = await conn.fetch(
                "SELECT id FROM inquiries WHERE kind = 'Issue' AND owner = $1 "
                "AND labels @> ARRAY['workspace']::text[] AND id = ANY($2::uuid[])",
                email,
                checked.ids,
            )
            if len(rows) != len(checked.ids):
                return None
        workspaces = await _lock_preferences(conn, user_id=user_id)
        await _save_preferences(
            conn,
            user_id=user_id,
            workspaces=workspaces.model_copy(
                update={"order": [str(workspace_id) for workspace_id in checked.ids]},
            ),
        )
    return await list_workspaces(
        engine,
        user_id=user_id,
        email=email,
        include_hidden=True,
    )


async def remember_opened_canvas(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    canvas_id: uuid.UUID,
) -> bool:
    """Remember a canvas as the one the user opened last in its workspace.

    Switching to the workspace opens it again, in any browser. A canvas with no
    workspace is the default workspace's. Only that workspace's entry changes.

    Args:
      engine: Database connection source.
      user_id: The owner of the canvas and of the workspace preferences.
      email: The user's Issue owner, whose default holds a canvas with no workspace.
      canvas_id: The canvas opened.

    Returns:
      found: Whether the user has such a canvas.

    """
    async with engine.acquire() as conn, tx(conn):
        canvas = await conn.fetchrow(
            "SELECT metadata->>'workspace_id' AS workspace_id FROM visual_canvases "
            "WHERE id = $1 AND user_id = $2",
            canvas_id,
            user_id,
        )
        if canvas is None:
            return False
        workspace_id = from_plain(canvas["workspace_id"], str | None)
        if workspace_id is None:
            default = await _default_row(conn, email=email)
            # The default is made when something lands in it; until then no bar
            # shows a canvas with no workspace.
            if default is None:
                return True
            workspace_id = str(from_plain(default["id"], uuid.UUID))
        workspaces = await _lock_preferences(conn, user_id=user_id)
        if workspaces.last_canvas.get(workspace_id) != str(canvas_id):
            await _save_preferences(
                conn,
                user_id=user_id,
                workspaces=workspaces.model_copy(
                    update={
                        "last_canvas": {
                            **workspaces.last_canvas,
                            workspace_id: str(canvas_id),
                        },
                    },
                ),
            )
    return True


async def check_workspace(
    engine: DatabaseEngine,
    *,
    email: str,
    workspace_id: uuid.UUID,
) -> bool:
    """Say whether the Issue exists, belongs to ``email``, and has its label.

    Args:
      engine: Database connection source.
      email: The expected Issue owner.
      workspace_id: Issue to check.

    Returns:
      found: Whether this is an owned workspace Issue.

    """
    async with engine.acquire() as conn:
        return await _check_workspace(
            conn,
            email=email,
            workspace_id=workspace_id,
        )


async def workspace_info(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    workspace_id: uuid.UUID,
) -> WorkspaceInfo | None:
    """Return a workspace and its canvases with every session under each chat.

    Args:
      engine: Database connection source.
      user_id: The preference owner.
      email: The user's Issue owner.
      workspace_id: Workspace to read.

    Returns:
      info: The workspace and its canvases, or None when it is foreign or absent.

    """
    entries = await list_workspaces(
        engine,
        user_id=user_id,
        email=email,
        include_hidden=True,
    )
    summary = next((entry for entry in entries if entry.id == workspace_id), None)
    if summary is None:
        return None
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, metadata FROM visual_canvases WHERE user_id = $1 "
            "AND coalesce(metadata->>'workspace_id', $2) = $3 ORDER BY modified_at DESC",
            user_id,
            str(workspace_id) if summary.scratch else None,
            str(workspace_id),
        )
        canvases: list[WorkspaceCanvas] = []
        for row in rows:
            canvas = CanvasMetadata.model_validate(
                from_plain(row["metadata"], dict[str, object]),
            )
            session_ids: list[uuid.UUID] = []
            if canvas.conversation_id is not None:
                session_ids = await read_chat_sessions(
                    conn,
                    conversation_id=canvas.conversation_id,
                )
            canvases.append(
                WorkspaceCanvas(
                    id=from_plain(row["id"], uuid.UUID),
                    name=canvas.name,
                    hidden=canvas.hidden,
                    pinned=canvas.pinned,
                    conversation_id=canvas.conversation_id,
                    session_ids=session_ids,
                ),
            )
    return WorkspaceInfo(
        id=summary.id,
        seq=summary.seq,
        title=summary.title,
        scratch=summary.scratch,
        hidden=summary.hidden,
        pinned=summary.pinned,
        last_canvas_id=summary.last_canvas_id,
        canvases=canvases,
    )


async def show_workspace(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Take a workspace off the user's hidden list, as a canvas made in it lands there.

    One statement, so it holds against a concurrent change to the user's bar.

    Args:
      conn: The connection to write on.
      user_id: The owner of the workspace preferences.
      workspace_id: The workspace a canvas was made in.

    """
    await conn.execute(
        "UPDATE users SET preferences = jsonb_set(preferences, '{workspaces,hidden}', "
        "(preferences->'workspaces'->'hidden') - $2::text) "
        "WHERE id = $1 AND preferences->'workspaces'->'hidden' ? $2::text",
        user_id,
        str(workspace_id),
    )


async def _default_row(
    conn: Conn,
    *,
    email: str,
) -> Record | None:
    """Read the user's first Issue marked as their default workspace."""
    return await conn.fetchrow(
        "SELECT id FROM inquiries WHERE kind = 'Issue' AND owner = $1 "
        "AND account = $1 AND labels @> ARRAY['workspace', $2::text] "
        "ORDER BY seq LIMIT 1",
        email,
        _DEFAULT_LABEL,
    )


async def _keep_loose_canvases(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Give the user's canvases in no workspace the id of the default they sit in."""
    # ``modified_at`` stays: nothing a person sees changed, and the canvas changed
    # last is the one a new tab opens.
    await conn.execute(
        "UPDATE visual_canvases SET metadata = metadata || "
        "jsonb_build_object('workspace_id', $2::text) "
        "WHERE user_id = $1 AND NOT metadata ? 'workspace_id'",
        user_id,
        str(workspace_id),
    )


async def _check_workspace(
    conn: Conn,
    *,
    email: str,
    workspace_id: uuid.UUID,
) -> bool:
    """Check workspace ownership and its label on an existing connection."""
    return (
        await conn.fetchval(
            "SELECT 1 FROM inquiries WHERE id = $1 AND kind = 'Issue' AND owner = $2 "
            "AND labels @> ARRAY['workspace']::text[]",
            workspace_id,
            email,
        )
        is not None
    )


async def _lock_preferences(
    conn: Conn,
    *,
    user_id: uuid.UUID,
) -> WorkspacesPreference:
    """Lock the preference owner before changing their workspace view."""
    row = await conn.fetchrow(
        "SELECT preferences FROM users WHERE id = $1 FOR UPDATE",
        user_id,
    )
    if row is None:
        raise ValueError("User not found")
    return stored_preferences(row["preferences"]).workspaces


async def _save_preferences(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    workspaces: WorkspacesPreference,
) -> None:
    """Save only the workspaces preference while preserving other keys."""
    written = await write_preferences(
        conn,
        user_id=user_id,
        change={"workspaces": workspaces.model_dump(mode="json")},
    )
    if written is None:
        raise ValueError("User not found")


def _summary(
    row: Record,
    *,
    default_id: uuid.UUID | None,
    hidden_ids: set[str],
    pinned_ids: set[str],
    last_canvas: Mapping[str, str],
    q: str | None,
) -> WorkspaceSummary | None:
    """Build and search one workspace summary row."""
    issue_id = from_plain(row["id"], uuid.UUID)
    title = from_plain(row["title"], str)
    if q is not None and q != "" and q.casefold() not in title.casefold():
        return None
    return WorkspaceSummary(
        id=issue_id,
        seq=from_plain(row["seq"], int),
        title=title,
        scratch=issue_id == default_id,
        hidden=str(issue_id) in hidden_ids,
        pinned=str(issue_id) in pinned_ids,
        last_canvas_id=last_canvas.get(str(issue_id)),
    )


def _toggle_id(
    preference: WorkspacesPreference,
    key: Literal["hidden", "pinned"],
    workspace_id: uuid.UUID,
    enabled: bool,
) -> WorkspacesPreference:
    """Set one workspace id's membership in hidden or pinned preferences."""
    values = list(preference.hidden if key == "hidden" else preference.pinned)
    text_id = str(workspace_id)
    values = [value for value in values if value != text_id]
    if enabled:
        values.append(text_id)
    return preference.model_copy(update={key: values})
