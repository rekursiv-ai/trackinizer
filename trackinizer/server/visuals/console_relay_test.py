"""A Console relay reaches the station's chat partner once, or says it did not.

The database reads (the station with its partner, and the chat's open session) are
replaced; the queue is real, so what the partner would drain is what is asserted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import uuid

import pytest

from trackinizer.conftest import FakeEngine
from trackinizer.server.config import Assistant
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.visuals import console_relay
from trackinizer.server.visuals.console_relay import relay_console_line
from trackinizer.server.visuals.workspaces import (
    WorkspacePartner,
    WorkspaceState,
)
from trackinizer.wire.wire_sessions import ConsoleRelay, ConsoleTarget


if TYPE_CHECKING:
    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.server.visuals.workspaces import PartnerChoice


_USER = uuid.UUID(int=1)
_STATION = uuid.UUID(int=2)
_CONVERSATION = uuid.UUID(int=3)
_SERVICE = uuid.UUID(int=4)
_CHAT = uuid.UUID(int=5)
_KEY = uuid.UUID(int=6)
_SCOUT = Assistant(actor="scout", email="scout@example.com")
_LIVE = WorkspacePartner(
    session_id=_SERVICE,
    actor="scout",
    cli="sagent",
    status="live",
)
_RELAY = ConsoleRelay(
    targets=[
        ConsoleTarget(actor="lead-2", room=None),
        ConsoleTarget(actor="tiles-a", room="ops"),
    ],
    text="rerun the suite",
)


def _state(
    *,
    conversation: uuid.UUID | None = _CONVERSATION,
    partner: WorkspacePartner | None = _LIVE,
    choice: PartnerChoice = "shared",
) -> WorkspaceState:
    """Return a station holding ``conversation`` whose partner is ``partner``."""
    return WorkspaceState(
        id=_STATION,
        revision=0,
        name="unnamed-workstream#1",
        conversation_id=conversation,
        page=None,
        visuals=[],
        partner_choice=choice,
        partner=partner,
    )


class _Station:
    """Stands in for the two database reads; records what they were asked."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        state: WorkspaceState | None = None,
        chat: uuid.UUID | None = None,
    ) -> None:
        self.engine = FakeEngine()
        self.opened_for: list[uuid.UUID] = []
        self.inbound = InboundQueue()

        async def read_workspace(
            engine: DatabaseEngine,
            *,
            user_id: uuid.UUID,
            workspace_id: uuid.UUID,
            inbound: InboundQueue,
            assistant: Assistant | None,
        ) -> WorkspaceState | None:
            assert engine is self.engine
            assert (user_id, workspace_id) == (_USER, _STATION)
            assert inbound is self.inbound
            assert assistant == _SCOUT
            return state or _state()

        async def live_chat_session(
            conn: object,
            *,
            inbound: InboundQueue,
            opener: str,
            conversation_id: uuid.UUID,
        ) -> uuid.UUID | None:
            assert conn is self.engine.conn
            assert inbound is self.inbound
            assert opener == _SCOUT.email
            self.opened_for.append(conversation_id)
            return chat

        monkeypatch.setattr(console_relay, "read_workspace", read_workspace)
        monkeypatch.setattr(console_relay, "live_chat_session", live_chat_session)

    async def relay(self, *, key: uuid.UUID = _KEY) -> bool | None:
        """Relay ``_RELAY`` from the user's station as the user."""
        return await relay_console_line(
            cast("DatabaseEngine", self.engine),
            user_id=_USER,
            workspace_id=_STATION,
            relay=_RELAY,
            key=key,
            source="alice@example.com",
            source_role="writer",
            inbound=self.inbound,
            assistant=_SCOUT,
        )


@pytest.mark.asyncio
async def test_a_relay_goes_to_the_chat_session_the_assistant_has_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    station = _Station(monkeypatch, chat=_CHAT)

    assert await station.relay() is True

    assert station.opened_for == [_CONVERSATION]
    assert station.inbound.pending(_SERVICE) == 0
    (message,) = station.inbound.drain(_CHAT)
    assert (message.text, message.source, message.source_role) == (
        "rerun the suite",
        "alice@example.com",
        "writer",
    )
    context = message.context
    assert context is not None
    assert context.relay == _RELAY
    assert (context.workspace_id, context.conversation_id) == (_STATION, _CONVERSATION)
    assert context.visible_visuals == []


@pytest.mark.asyncio
async def test_a_chat_the_assistant_has_not_open_hears_it_on_the_service_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    station = _Station(monkeypatch)

    assert await station.relay() is True

    assert station.inbound.pending(_CHAT) == 0
    (message,) = station.inbound.drain(_SERVICE)
    assert message.context is not None
    assert message.context.relay == _RELAY


@pytest.mark.asyncio
async def test_a_local_helper_is_sent_the_relay_without_asking_for_an_open_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    station = _Station(monkeypatch, state=_state(choice="local"), chat=_CHAT)

    assert await station.relay() is True

    assert station.opened_for == []
    assert station.inbound.pending(_SERVICE) == 1
    assert station.inbound.pending(_CHAT) == 0


@pytest.mark.asyncio
async def test_a_relay_is_queued_once_however_often_it_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    station = _Station(monkeypatch, chat=_CHAT)

    assert [await station.relay(), await station.relay()] == [True, True]
    assert station.inbound.pending(_CHAT) == 1
    assert await station.relay(key=uuid.UUID(int=7)) is True
    assert station.inbound.pending(_CHAT) == 2


@pytest.mark.asyncio
async def test_a_station_that_is_not_the_callers_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    station = _Station(monkeypatch, chat=_CHAT)

    async def not_theirs(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(console_relay, "read_workspace", not_theirs)

    assert await station.relay() is None
    assert station.inbound.pending(_CHAT) == station.inbound.pending(_SERVICE) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        _state(conversation=None),
        _state(partner=None),
        _state(partner=_LIVE.model_copy(update={"session_id": None})),
        _state(partner=_LIVE.model_copy(update={"status": "unavailable"})),
    ],
    ids=["no chat", "no partner", "no partner session", "partner not live"],
)
async def test_a_relay_nobody_can_take_is_not_delivered_and_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
    state: WorkspaceState,
) -> None:
    station = _Station(monkeypatch, state=state, chat=_CHAT)

    assert await station.relay() is False
    assert station.inbound.pending(_CHAT) == station.inbound.pending(_SERVICE) == 0


@pytest.mark.asyncio
async def test_a_relay_does_not_evict_a_line_the_partner_has_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    station = _Station(monkeypatch, chat=_CHAT)
    station.inbound = InboundQueue(max_per_session=1)

    assert await station.relay(key=uuid.UUID(int=8)) is True
    assert await station.relay() is False
    assert station.inbound.pending(_CHAT) == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
