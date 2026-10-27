"""Which sessions a run places on a canvas, and the workspace edge a move leaves."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import asyncio
import uuid

import pytest

from trackinizer.conftest import FakeEngine
from trackinizer.server.visuals import session_placement
from trackinizer.server.visuals.session_placement import (
    is_placed,
    move_canvas_sessions,
)
from trackinizer.types.inquiries import AgentSession


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.store.core import Store


type _Write = tuple[str, uuid.UUID, uuid.UUID, str, uuid.UUID | None, str]


class _EdgeStore:
    """The store's edge writes, recorded, over a scripted engine."""

    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        self.writes: list[_Write] = []

    async def remove_edge(
        self,
        *,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        api_key_id: uuid.UUID | None,
        actor: str,
    ) -> None:
        self.writes.append(("remove", from_id, to_id, edge_kind, api_key_id, actor))

    async def add_edge(
        self,
        *,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        api_key_id: uuid.UUID | None,
        actor: str,
    ) -> None:
        self.writes.append(("add", from_id, to_id, edge_kind, api_key_id, actor))


@pytest.mark.parametrize(
    ("account", "cli_session_id", "placed"),
    [
        ("ada@example.com", "claude-1", True),
        ("ada@example.com", None, True),
        ("ada@example.com", "chat:00000000-0000-0000-0000-000000000001", False),
        ("bot@service.invalid", "claude-1", False),
    ],
    ids=["own-run", "own-run-without-id", "science-chat", "service-account"],
)
def test_a_persons_run_is_placed_and_a_chat_or_service_account_is_not(
    account: str,
    cli_session_id: str | None,
    placed: bool,
) -> None:
    session = AgentSession(
        title="run",
        account=account,
        cli="claude",
        cli_session_id=cli_session_id,
    )

    assert is_placed(session) is placed


def test_a_canvas_move_leaves_each_session_one_edge_to_the_new_workspace() -> None:
    session, old, new, key = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = FakeEngine()
    engine.conn.fetch.side_effect = [
        [{"id": session}],
        [{"to_id": str(old)}, {"to_id": str(new)}],
    ]
    store = _EdgeStore(engine)

    asyncio.run(
        move_canvas_sessions(
            cast("DatabaseEngine", engine),
            store=cast("Store", store),
            canvas_id=uuid.uuid4(),
            email="ada@example.com",
            api_key_id=key,
            workspace_id=new,
        ),
    )

    sql, *named = engine.conn.fetch.await_args_list[1].args
    assert "'produced_by'" in cast(str, sql)
    assert named == [session]
    assert store.writes == [
        ("remove", session, old, "produced_by", key, "ada@example.com"),
        ("add", session, new, "produced_by", key, "ada@example.com"),
    ]


@pytest.mark.parametrize("owned", [True, False], ids=["own", "another-users"])
def test_a_placed_sessions_workspace_is_kept_only_when_the_caller_owns_it(
    owned: bool,
) -> None:
    """An edge to a workspace is checked against the caller's email before it is kept."""
    workspace = uuid.uuid4()
    engine = FakeEngine()
    engine.conn.fetchval.side_effect = [workspace, None, workspace if owned else None]
    session = AgentSession(title="run", account="ada@example.com", cli="claude")

    decided = asyncio.run(
        session_placement._decide(
            cast("Conn", engine.conn),
            session=session,
            user_id=uuid.uuid4(),
            email="ada@example.com",
            canvas=None,
            git=None,
        ),
    )

    assert decided == (workspace if owned else None, None)
    _, *checked = engine.conn.fetchval.await_args_list[2].args
    assert checked == [str(workspace), "ada@example.com"]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
