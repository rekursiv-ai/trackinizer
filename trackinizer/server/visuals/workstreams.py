"""Per-user workstreams backed by owned Trackinizer Issues."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from treekle import from_plain

from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.server.preferences import (
    WorkstreamsPreference,
    stored_preferences,
    write_preferences,
)
from trackinizer.server.visuals.science_chats import read_chat_sessions
from trackinizer.server.visuals.workspaces import StationData
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.store.core import Store


class WorkstreamSummary(BaseModel):
    """One workstream in the user's bar."""

    id: uuid.UUID
    seq: int
    title: str
    scratch: bool
    hidden: bool
    pinned: bool


class WorkstreamStation(BaseModel):
    """A station attached to one workstream."""

    id: uuid.UUID
    name: str
    hidden: bool
    pinned: bool
    conversation_id: uuid.UUID | None
    session_ids: list[uuid.UUID]


class WorkstreamInfo(WorkstreamSummary):
    """A workstream and its attached stations."""

    stations: list[WorkstreamStation]


class CreateWorkstream(BaseModel):
    """A new workstream's title."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def _trim_title(cls, title: str) -> str:
        title = title.strip()
        if not title:
            raise ValueError("A workstream title is 1 to 120 characters.")
        return title


class UpdateWorkstream(BaseModel):
    """Fields a user may change on a workstream."""

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
            raise ValueError("A workstream title is 1 to 120 characters.")
        return title

    @model_validator(mode="after")
    def _requires_change(self) -> UpdateWorkstream:
        if self.title is None and self.hidden is None and self.pinned is None:
            raise ValueError("At least one workstream field is required.")
        return self


class WorkstreamOrder(BaseModel):
    """The ids in their chosen workstream bar order."""

    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] = Field(max_length=500)

    @field_validator("ids")
    @classmethod
    def _distinct_ids(cls, ids: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(ids) != len(set(ids)):
            raise ValueError("Workstream ids must be distinct.")
        return ids


class SetStationWorkstream(BaseModel):
    """The workstream a station is moved to."""

    model_config = ConfigDict(extra="forbid")

    workstream_id: uuid.UUID


class ScratchWorkstreamError(Exception):
    """Scratch is not a workstream a user may hide."""


async def open_scratch(
    engine: DatabaseEngine,
    *,
    store: Store,
    user_id: uuid.UUID,
    email: str,
) -> uuid.UUID:
    """Find or create the user's Scratch Issue.

    Args:
      engine: Database connection source.
      store: Store used to submit a Scratch Issue in the locked transaction.
      user_id: The owner of the users row to lock during creation.
      email: The user's account and Issue owner.

    Returns:
      issue_id: The Scratch Issue id.

    """
    async with engine.acquire() as conn:
        row = await _scratch_row(conn, email=email)
    if row is not None:
        return from_plain(row["id"], uuid.UUID)

    async with notify_after_commit(), engine.acquire() as conn, tx(conn):
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE id = $1 FOR UPDATE",
            user_id,
        )
        if user is None:
            raise ValueError("User not found")
        row = await _scratch_row(conn, email=email)
        if row is None:
            return await store.submit_issue(
                SubmitIssue(
                    title="Scratch",
                    account=email,
                    owner=email,
                    actor=email,
                    labels=["workstream", "scratch"],
                    status="active",
                ),
                actor=email,
                conn=conn,
            )
        return from_plain(row["id"], uuid.UUID)


async def create_workstream(
    engine: DatabaseEngine,
    *,
    store: Store,
    user_id: uuid.UUID,
    email: str,
    title: str,
) -> WorkstreamSummary:
    """Submit an owned workstream Issue and return its bar entry.

    Args:
      engine: Database connection source.
      store: Store that submits the Issue.
      user_id: The owner of the workstream preferences.
      email: The user's account and Issue owner.
      title: The workstream's title.

    Returns:
      summary: The created workstream.

    """
    checked = CreateWorkstream(title=title)
    issue_id = await store.submit_issue(
        SubmitIssue(
            title=checked.title,
            account=email,
            owner=email,
            actor=email,
            labels=["workstream"],
            status="active",
        ),
        actor=email,
    )
    entries = await list_workstreams(
        engine,
        user_id=user_id,
        email=email,
        include_hidden=True,
    )
    return next(entry for entry in entries if entry.id == issue_id)


async def list_workstreams(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    q: str | None = None,
    include_hidden: bool = False,
) -> list[WorkstreamSummary]:
    """List owned workstream Issues in pin, saved-order, then newest order.

    Args:
      engine: Database connection source.
      user_id: The preference owner.
      email: The Issue owner to query.
      q: Optional title substring.
      include_hidden: Whether to include hidden workstreams.

    Returns:
      workstreams: Matching Issues in bar order.

    """
    async with engine.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT preferences FROM users WHERE id = $1",
            user_id,
        )
        if user is None:
            return []
        preference = stored_preferences(user["preferences"]).workstreams
        rows = await conn.fetch(
            "SELECT id, seq, title, modified FROM inquiries WHERE kind = 'Issue' "
            "AND owner = $1 AND labels @> ARRAY['workstream']::text[]",
            email,
        )
        scratch = await _scratch_row(conn, email=email)
    scratch_id = None if scratch is None else from_plain(scratch["id"], uuid.UUID)
    hidden_ids = set(preference.hidden)
    pinned_ids = set(preference.pinned)
    order_index = {
        workstream_id: index for index, workstream_id in enumerate(preference.order)
    }
    summaries = [
        _summary(
            row,
            scratch_id=scratch_id,
            hidden_ids=hidden_ids,
            pinned_ids=pinned_ids,
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
            not item.scratch,
            order_index.get(str(item.id), len(order_index)),
            -modified_by_id[item.id].timestamp(),
        ),
    )
    return found


async def update_workstream(
    engine: DatabaseEngine,
    *,
    store: Store,
    user_id: uuid.UUID,
    email: str,
    workstream_id: uuid.UUID,
    title: str | None = None,
    hidden: bool | None = None,
    pinned: bool | None = None,
) -> WorkstreamSummary | None:
    """Update a workstream title or its per-user hidden and pinned flags.

    Args:
      engine: Database connection source.
      store: Store used to record a title change.
      user_id: The preference owner.
      email: The user's Issue owner.
      workstream_id: Workstream to update.
      title: New title, or None to leave it unchanged.
      hidden: New hidden state, or None to leave it unchanged.
      pinned: New pinned state, or None to leave it unchanged.

    Returns:
      summary: The changed workstream, or None when it is foreign or absent.

    """
    checked = UpdateWorkstream(title=title, hidden=hidden, pinned=pinned)
    if not await check_workstream(engine, email=email, workstream_id=workstream_id):
        return None
    async with engine.acquire() as conn, tx(conn):
        scratch = await _scratch_row(conn, email=email)
        scratch_id = None if scratch is None else from_plain(scratch["id"], uuid.UUID)
        workstreams = await _lock_preferences(conn, user_id=user_id)
        if workstream_id == scratch_id and checked.hidden is True:
            raise ScratchWorkstreamError
        if checked.hidden is not None:
            workstreams = _toggle_id(
                workstreams,
                "hidden",
                workstream_id,
                checked.hidden,
            )
        if checked.pinned is not None and workstream_id != scratch_id:
            workstreams = _toggle_id(
                workstreams,
                "pinned",
                workstream_id,
                checked.pinned,
            )
        await _save_preferences(
            conn,
            user_id=user_id,
            workstreams=workstreams,
        )
    if checked.title is not None:
        await store.set_title(workstream_id, checked.title, actor=email)
    entries = await list_workstreams(
        engine,
        user_id=user_id,
        email=email,
        include_hidden=True,
    )
    return next((entry for entry in entries if entry.id == workstream_id), None)


async def set_workstream_order(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    ids: list[uuid.UUID],
) -> list[WorkstreamSummary] | None:
    """Save the user's workstream bar order and return the visible entries.

    Args:
      engine: Database connection source.
      user_id: The preference owner.
      email: The user's Issue owner.
      ids: Workstream ids in their requested order.

    Returns:
      workstreams: The updated list, or None if an id is foreign or absent.

    """
    checked = WorkstreamOrder(ids=ids)
    async with engine.acquire() as conn, tx(conn):
        if checked.ids:
            rows = await conn.fetch(
                "SELECT id FROM inquiries WHERE kind = 'Issue' AND owner = $1 "
                "AND labels @> ARRAY['workstream']::text[] AND id = ANY($2::uuid[])",
                email,
                checked.ids,
            )
            if len(rows) != len(checked.ids):
                return None
        workstreams = await _lock_preferences(conn, user_id=user_id)
        await _save_preferences(
            conn,
            user_id=user_id,
            workstreams=workstreams.model_copy(
                update={"order": [str(workstream_id) for workstream_id in checked.ids]},
            ),
        )
    return await list_workstreams(
        engine,
        user_id=user_id,
        email=email,
        include_hidden=True,
    )


async def check_workstream(
    engine: DatabaseEngine,
    *,
    email: str,
    workstream_id: uuid.UUID,
) -> bool:
    """Say whether the Issue exists, belongs to ``email``, and has its label.

    Args:
      engine: Database connection source.
      email: The expected Issue owner.
      workstream_id: Issue to check.

    Returns:
      found: Whether this is an owned workstream Issue.

    """
    async with engine.acquire() as conn:
        return await _check_workstream(
            conn,
            email=email,
            workstream_id=workstream_id,
        )


async def workstream_info(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    workstream_id: uuid.UUID,
) -> WorkstreamInfo | None:
    """Return a workstream and its stations with every session under each chat.

    Args:
      engine: Database connection source.
      user_id: The preference owner.
      email: The user's Issue owner.
      workstream_id: Workstream to read.

    Returns:
      info: The workstream and its stations, or None when it is foreign or absent.

    """
    entries = await list_workstreams(
        engine,
        user_id=user_id,
        email=email,
        include_hidden=True,
    )
    summary = next((entry for entry in entries if entry.id == workstream_id), None)
    if summary is None:
        return None
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, station FROM visual_workspaces WHERE user_id = $1 "
            "AND coalesce(station->>'workstream_id', $2) = $3 ORDER BY modified_at DESC",
            user_id,
            str(_uuid_or_none_from_summary(summary)),
            str(workstream_id),
        )
        stations: list[WorkstreamStation] = []
        for row in rows:
            station = StationData.model_validate(
                from_plain(row["station"], dict[str, object]),
            )
            session_ids: list[uuid.UUID] = []
            if station.conversation_id is not None:
                session_ids = await read_chat_sessions(
                    conn,
                    conversation_id=station.conversation_id,
                )
            stations.append(
                WorkstreamStation(
                    id=from_plain(row["id"], uuid.UUID),
                    name=station.name,
                    hidden=station.hidden,
                    pinned=station.pinned,
                    conversation_id=station.conversation_id,
                    session_ids=session_ids,
                ),
            )
    return WorkstreamInfo(
        id=summary.id,
        seq=summary.seq,
        title=summary.title,
        scratch=summary.scratch,
        hidden=summary.hidden,
        pinned=summary.pinned,
        stations=stations,
    )


async def _scratch_row(
    conn: Conn,
    *,
    email: str,
) -> Record | None:
    """Read the user's first Issue labeled as Scratch and a workstream."""
    return await conn.fetchrow(
        "SELECT id FROM inquiries WHERE kind = 'Issue' AND owner = $1 "
        "AND account = $1 AND labels @> ARRAY['workstream', 'scratch']::text[] "
        "ORDER BY seq LIMIT 1",
        email,
    )


async def _check_workstream(
    conn: Conn,
    *,
    email: str,
    workstream_id: uuid.UUID,
) -> bool:
    """Check workstream ownership and its label on an existing connection."""
    return (
        await conn.fetchval(
            "SELECT 1 FROM inquiries WHERE id = $1 AND kind = 'Issue' AND owner = $2 "
            "AND labels @> ARRAY['workstream']::text[]",
            workstream_id,
            email,
        )
        is not None
    )


async def _lock_preferences(
    conn: Conn,
    *,
    user_id: uuid.UUID,
) -> WorkstreamsPreference:
    """Lock the preference owner before changing their workstream view."""
    row = await conn.fetchrow(
        "SELECT preferences FROM users WHERE id = $1 FOR UPDATE",
        user_id,
    )
    if row is None:
        raise ValueError("User not found")
    return stored_preferences(row["preferences"]).workstreams


async def _save_preferences(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    workstreams: WorkstreamsPreference,
) -> None:
    """Save only the workstreams preference while preserving other keys."""
    written = await write_preferences(
        conn,
        user_id=user_id,
        change={"workstreams": workstreams.model_dump(mode="json")},
    )
    if written is None:
        raise ValueError("User not found")


def _summary(
    row: Record,
    *,
    scratch_id: uuid.UUID | None,
    hidden_ids: set[str],
    pinned_ids: set[str],
    q: str | None,
) -> WorkstreamSummary | None:
    """Build and search one workstream summary row."""
    issue_id = from_plain(row["id"], uuid.UUID)
    title = from_plain(row["title"], str)
    if q is not None and q != "" and q.casefold() not in title.casefold():
        return None
    return WorkstreamSummary(
        id=issue_id,
        seq=from_plain(row["seq"], int),
        title=title,
        scratch=issue_id == scratch_id,
        hidden=str(issue_id) in hidden_ids,
        pinned=str(issue_id) in pinned_ids or issue_id == scratch_id,
    )


def _toggle_id(
    preference: WorkstreamsPreference,
    key: Literal["hidden", "pinned"],
    workstream_id: uuid.UUID,
    enabled: bool,
) -> WorkstreamsPreference:
    """Set one workstream id's membership in hidden or pinned preferences."""
    values = list(preference.hidden if key == "hidden" else preference.pinned)
    text_id = str(workstream_id)
    values = [value for value in values if value != text_id]
    if enabled:
        values.append(text_id)
    return preference.model_copy(update={key: values})


def _uuid_or_none_from_summary(summary: WorkstreamSummary) -> uuid.UUID | None:
    """Return the id only when this summary is Scratch."""
    return summary.id if summary.scratch else None
