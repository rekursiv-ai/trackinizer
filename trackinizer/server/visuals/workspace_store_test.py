"""The canvas store: the Chat partner choice is stored, and a replay never undoes it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import asyncio
import uuid

import pytest

from trackinizer.conftest import FakeEngine, executed_sql, make_conn
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.config import Assistant
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.catalog import default_catalog
from trackinizer.server.visuals.workspace_store import (
    AppliedOperation,
    apply_workspace_operation,
    open_latest_workspace,
    read_workspace,
)
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    WorkspaceData,
)


if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from trackinizer.lib.postgres import DatabaseEngine, PGliteEngine
    from trackinizer.server.visuals.workspaces import WorkspaceState


_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_WORKSPACE: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")


async def _seed(engine: PGliteEngine) -> None:
    await reset_schema(engine)
    await Store(engine, embed=StubEmbedder()).bootstrap()
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'T', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, station, state) "
            "VALUES ($1, $2, $3, $4)",
            _WORKSPACE,
            _USER,
            {"name": "unnamed-workstream#1"},
            {"visuals": []},
        )


async def _apply(
    engine: PGliteEngine,
    body: ApplyWorkspaceOperation,
    *,
    key: uuid.UUID,
    inbound: InboundQueue,
) -> AppliedOperation | None:
    return await apply_workspace_operation(
        engine,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        key=key,
        body=body,
        catalog=default_catalog(),
        inbound=inbound,
        assistant=None,
    )


def _choose(*, revision: int, choice: str) -> ApplyWorkspaceOperation:
    return ApplyWorkspaceOperation.model_validate(
        {"revision": revision, "operation": {"kind": "partner", "choice": choice}},
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_partner_choice_is_stored_and_a_replay_answers_with_the_current_one(
    pglite_engine: PGliteEngine,
) -> None:
    """A retried choice returns its receipt, but the partner follows what is chosen now."""
    await _seed(pglite_engine)
    inbound = InboundQueue()
    first_key, second_key = uuid.uuid4(), uuid.uuid4()

    local = await _apply(
        pglite_engine,
        _choose(revision=0, choice="local"),
        key=first_key,
        inbound=inbound,
    )
    assert local is not None
    assert local.state.partner_choice == "local"
    assert local.state.partner is not None
    assert local.state.partner.kind == "local"
    assert local.state.revision == 1

    shared = await _apply(
        pglite_engine,
        _choose(revision=1, choice="shared"),
        key=second_key,
        inbound=inbound,
    )
    assert shared is not None
    assert shared.state.partner_choice == "shared"
    assert shared.state.partner is None

    replay = await _apply(
        pglite_engine,
        _choose(revision=0, choice="local"),
        key=first_key,
        inbound=inbound,
    )
    assert replay is not None
    assert replay.replayed
    assert replay.state.partner_choice == "shared"

    read = await read_workspace(
        pglite_engine,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        inbound=inbound,
        assistant=None,
    )
    assert read is not None
    assert (read.partner_choice, read.revision) == ("shared", 2)


_STATION: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_LATEST: Final = (
    "SELECT id, revision, state, station FROM visual_workspaces "
    "WHERE user_id = $1 ORDER BY modified_at DESC, id DESC LIMIT 1"
)
_ROW: Final[dict[str, object]] = {
    "id": _STATION,
    "revision": 4,
    "state": {"visuals": []},
    "station": {"name": "tails", "page": "#/issue/7"},
}


def _open(engine: FakeEngine, *, assistant: Assistant | None = None) -> WorkspaceState:
    """Open the latest station through ``engine``."""
    return asyncio.run(
        open_latest_workspace(
            cast("DatabaseEngine", engine),
            user_id=_USER,
            catalog=default_catalog(),
            inbound=InboundQueue(),
            assistant=assistant,
        ),
    )


def _scripted(
    *latest: object,
) -> tuple[FakeEngine, list[tuple[str, tuple[object, ...]]]]:
    """Answer each read of the latest station in turn, and record every fetchrow."""
    conn: AsyncMock = make_conn()
    reads = list(latest)
    asked: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(sql: str, *args: object) -> object:
        asked.append((sql, args))
        if sql.startswith("INSERT"):
            return _ROW
        return reads.pop(0)

    conn.fetchrow.side_effect = fetchrow
    return FakeEngine(conn), asked


def test_the_station_changed_last_is_read_and_nothing_is_written() -> None:
    engine, asked = _scripted(_ROW)

    state = _open(engine, assistant=Assistant(actor="scout", email="kb@example.com"))

    assert asked == [(_LATEST, (_USER,))]
    assert (state.id, state.revision, state.name, state.page) == (
        _STATION,
        4,
        "tails",
        "#/issue/7",
    )
    assert state.conversation_id is None
    assert state.assistant == "scout"
    assert state.partner is not None
    assert state.partner.status == "unavailable"
    assert not any(sql.startswith("INSERT") for sql in executed_sql(engine.conn))


def test_a_user_with_no_station_gets_the_first_one_named_and_read_back() -> None:
    engine, asked = _scripted(None, _ROW)

    state = _open(engine)

    assert [sql for sql, _ in asked] == [
        _LATEST,
        (
            "INSERT INTO visual_workspaces (id, user_id, station, state) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING "
            "RETURNING id, revision, state, station"
        ),
        _LATEST,
    ]
    _, (new_id, owner, station, data) = asked[1]
    assert isinstance(new_id, uuid.UUID)
    # A station's JSON holds only the fields it has: no chat and no page yet.
    assert (owner, station) == (_USER, {"name": "unnamed-workstream#1"})
    assert WorkspaceData.model_validate(data).visuals[0].type == "trax.browse"
    assert asked[2][1] == (_USER,)
    assert state.id == _STATION
    assert state.assistant is None
    assert state.partner is None


def test_a_local_partner_is_looked_for_among_the_owners_own_helpers() -> None:
    local_state: dict[str, object] = {"visuals": [], "partner_choice": "local"}
    engine, _ = _scripted(_ROW | {"state": local_state})

    state = _open(engine)

    assert state.partner is not None
    assert (state.partner.kind, state.partner.status) == ("local", "unavailable")
    assert engine.conn.fetch.call_args.args[2] == _USER


def test_a_station_that_stays_invisible_after_its_insert_is_an_error() -> None:
    engine, _ = _scripted(None, None)

    with pytest.raises(RuntimeError, match=r"^first workspace insert was not visible$"):
        _ = _open(engine)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
