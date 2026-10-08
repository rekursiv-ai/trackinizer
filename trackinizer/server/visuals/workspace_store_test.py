"""The canvas store: the Chat partner choice is stored, and a replay never undoes it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import uuid

import pytest

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.catalog import default_catalog
from trackinizer.server.visuals.workspace_store import (
    AppliedOperation,
    apply_workspace_operation,
    read_workspace,
)
from trackinizer.server.visuals.workspaces import ApplyWorkspaceOperation


if TYPE_CHECKING:
    from trackinizer.lib.postgres import PGliteEngine


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
            "INSERT INTO visual_workspaces (id, user_id, state) VALUES ($1, $2, $3)",
            _WORKSPACE,
            _USER,
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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
