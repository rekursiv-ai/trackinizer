"""Canvas presets: a preset is a layout, so opening one keeps the owner's settings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import uuid

import pytest

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.presets import (
    OpenPreset,
    _preset_from_row,
    open_preset,
)
from trackinizer.server.visuals.workspaces import WorkspaceData


if TYPE_CHECKING:
    from trackinizer.lib.postgres import PGliteEngine


_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_WORKSPACE: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")
_PRESET: Final = uuid.UUID("66666666-6666-6666-6666-666666666666")
_RECORD: Final = uuid.UUID("77777777-7777-7777-7777-777777777777")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_opening_a_preset_keeps_the_canvas_chat_partner(
    pglite_engine: PGliteEngine,
) -> None:
    """Restoring a layout does not undo the owner's choice of local helper."""
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    async with pglite_engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, "
            "visual_workspace_enabled) "
            "VALUES ($1, 'user@example.com', 'T', 'writer', 'active', TRUE)",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, state) VALUES ($1, $2, $3)",
            _WORKSPACE,
            _USER,
            {"visuals": [], "partner_choice": "local"},
        )
        await conn.execute(
            "INSERT INTO visual_workspace_presets (id, user_id, name, state) "
            "VALUES ($1, $2, 'view', $3)",
            _PRESET,
            _USER,
            {"visuals": []},
        )
    opened = await open_preset(
        pglite_engine,
        user_id=_USER,
        preset_id=_PRESET,
        body=OpenPreset(workspace_id=_WORKSPACE, revision=0),
        key=uuid.uuid4(),
        inbound=InboundQueue(),
        assistant=None,
    )
    assert opened is not None
    assert opened.state.partner_choice == "local"
    async with pglite_engine.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT state->>'partner_choice' FROM visual_workspaces WHERE id = $1",
            _WORKSPACE,
        )
    assert stored == "local"


def test_a_stored_preset_reads_back_with_its_settings_and_times() -> None:
    """A row's settings come from its stored state, and its times from the row."""
    created = datetime(2026, 1, 1, tzinfo=UTC)
    modified = datetime(2026, 1, 2, tzinfo=UTC)
    state: dict[str, object] = {
        "visuals": [],
        "agent_instructions": "Be brief.",
        "continuation_record_id": str(_RECORD),
    }

    preset = _preset_from_row(
        {
            "id": _PRESET,
            "name": "view",
            "state": state,
            "created_at": created,
            "modified_at": modified,
        },
    )

    assert preset.model_dump() == {
        "id": _PRESET,
        "name": "view",
        "agent_instructions": "Be brief.",
        "continuation_record_id": _RECORD,
        "state": WorkspaceData.model_validate(state).model_dump(),
        "created_at": created,
        "modified_at": modified,
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
