"""A Console relay reaches the canvas's chat partner once, or says it did not.

The database reads (the canvas with its partner, whether its owner may post into its
chat, and the chat's open session) are replaced; the queue is real, so what the partner
would drain is what is asserted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import uuid

import pytest

from trackinizer.conftest import FakeEngine
from trackinizer.server.audience import SessionReader
from trackinizer.server.config import Assistant
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.visuals import console_relay
from trackinizer.server.visuals.canvas_store import ProviderOfflineError
from trackinizer.server.visuals.canvases import (
    CanvasPartner,
    CanvasState,
)
from trackinizer.server.visuals.console_relay import relay_console_line
from trackinizer.wire.wire_sessions import ConsoleRelay, ConsoleTarget


if TYPE_CHECKING:
    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.wire.wire_science_chat import ChatProvider


_USER = uuid.UUID(int=1)
_CANVAS = uuid.UUID(int=2)
_CONVERSATION = uuid.UUID(int=3)
_SERVICE = uuid.UUID(int=4)
_CHAT = uuid.UUID(int=5)
_KEY = uuid.UUID(int=6)
_SCOUT = Assistant(actor="scout", email="scout@example.com")
_READER = SessionReader(audiences=frozenset({"open"}), email="alice@example.com")
_LIVE = CanvasPartner(
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
    partner: CanvasPartner | None = _LIVE,
) -> CanvasState:
    """Return a canvas holding ``conversation`` whose partner is ``partner``."""
    return CanvasState(
        id=_CANVAS,
        revision=0,
        name="unnamed-canvas#1",
        conversation_id=conversation,
        page=None,
        visuals=[],
        partner=partner,
    )


class _Canvas:
    """Stands in for the two database reads; records what they were asked."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        state: CanvasState | None = None,
        chat: uuid.UUID | None = None,
        may_post: bool = True,
        deleted: bool = False,
        provider: ChatProvider = "assistant",
        offline: bool = False,
    ) -> None:
        self.engine = FakeEngine()
        self.routed: list[tuple[uuid.UUID, ChatProvider]] = []
        self.answerers_asked: list[tuple[str, uuid.UUID]] = []
        self.asked_to_post: list[tuple[str, uuid.UUID]] = []
        self.locked: list[uuid.UUID] = []
        self.inbound = InboundQueue()

        async def read_canvas(
            engine: DatabaseEngine,
            *,
            user_id: uuid.UUID,
            canvas_id: uuid.UUID,
            inbound: InboundQueue,
            assistant: Assistant | None,
        ) -> CanvasState | None:
            assert engine is self.engine
            assert (user_id, canvas_id) == (_USER, _CANVAS)
            assert inbound is self.inbound
            assert assistant == _SCOUT
            return state or _state()

        async def chat_answerer(
            conn: object,
            *,
            state: CanvasState,
            conversation_id: uuid.UUID,
            poster: str,
            assistant: Assistant | None,
        ) -> ChatProvider:
            assert conn is self.engine.conn
            assert (state.id, assistant) == (_CANVAS, _SCOUT)
            self.answerers_asked.append((poster, conversation_id))
            return provider

        async def chat_line_target(
            conn: object,
            *,
            inbound: InboundQueue,
            assistant: Assistant | None,
            provider: ChatProvider,
            conversation_id: uuid.UUID,
        ) -> uuid.UUID | None:
            assert conn is self.engine.conn
            assert (inbound, assistant) == (self.inbound, _SCOUT)
            self.routed.append((conversation_id, provider))
            if offline:
                raise ProviderOfflineError(f"No {provider} run holds this chat")
            return chat

        async def lock_chat_id(conn: object, *, conversation_id: uuid.UUID) -> None:
            assert conn is self.engine.conn
            self.locked.append(conversation_id)

        async def chat_was_deleted(conn: object, *, conversation_id: uuid.UUID) -> bool:
            assert conn is self.engine.conn
            assert self.locked[-1] == conversation_id
            return deleted

        async def may_post_as_told(
            conn: object,
            *,
            poster: str,
            reader: SessionReader,
            conversation_id: uuid.UUID,
            assistant: Assistant | None,
            inbound: InboundQueue,
        ) -> bool:
            assert conn is self.engine.conn
            assert (assistant, reader, inbound) == (_SCOUT, _READER, self.inbound)
            self.asked_to_post.append((poster, conversation_id))
            return may_post

        monkeypatch.setattr(console_relay, "read_canvas", read_canvas)
        monkeypatch.setattr(console_relay, "chat_answerer", chat_answerer)
        monkeypatch.setattr(console_relay, "chat_line_target", chat_line_target)
        monkeypatch.setattr(console_relay, "may_post", may_post_as_told)
        monkeypatch.setattr(console_relay, "lock_chat_id", lock_chat_id)
        monkeypatch.setattr(console_relay, "chat_was_deleted", chat_was_deleted)

    async def relay(self, *, key: uuid.UUID = _KEY) -> bool | None:
        """Relay ``_RELAY`` from the user's canvas as the user."""
        return await relay_console_line(
            cast("DatabaseEngine", self.engine),
            user_id=_USER,
            canvas_id=_CANVAS,
            relay=_RELAY,
            key=key,
            source="alice@example.com",
            source_role="writer",
            inbound=self.inbound,
            assistant=_SCOUT,
            reader=_READER,
        )


@pytest.mark.asyncio
async def test_a_relay_goes_to_the_chat_session_the_assistant_has_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canvas = _Canvas(monkeypatch, chat=_CHAT)

    assert await canvas.relay() is True

    assert canvas.locked == [_CONVERSATION]
    assert canvas.routed == [(_CONVERSATION, "assistant")]
    assert canvas.answerers_asked == [("alice@example.com", _CONVERSATION)]
    assert canvas.asked_to_post == [("alice@example.com", _CONVERSATION)]
    assert canvas.inbound.pending(_SERVICE) == 0
    (message,) = canvas.inbound.drain(_CHAT)
    assert (message.text, message.source, message.source_role) == (
        "rerun the suite",
        "alice@example.com",
        "writer",
    )
    context = message.context
    assert context is not None
    assert context.relay == _RELAY
    assert (context.canvas_id, context.conversation_id) == (_CANVAS, _CONVERSATION)
    assert context.visible_visuals == []


@pytest.mark.asyncio
async def test_a_chat_the_assistant_has_not_open_hears_it_on_the_service_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canvas = _Canvas(monkeypatch)

    assert await canvas.relay() is True

    assert canvas.inbound.pending(_CHAT) == 0
    (message,) = canvas.inbound.drain(_SERVICE)
    assert message.context is not None
    assert message.context.relay == _RELAY


@pytest.mark.asyncio
async def test_a_relay_is_queued_once_however_often_it_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canvas = _Canvas(monkeypatch, chat=_CHAT)

    assert [await canvas.relay(), await canvas.relay()] == [True, True]
    assert canvas.inbound.pending(_CHAT) == 1
    assert await canvas.relay(key=uuid.UUID(int=7)) is True
    assert canvas.inbound.pending(_CHAT) == 2


@pytest.mark.asyncio
async def test_a_relay_into_a_cli_chat_joins_the_queue_its_run_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canvas = _Canvas(monkeypatch, chat=_CHAT, provider="claude")

    assert await canvas.relay() is True

    assert canvas.routed == [(_CONVERSATION, "claude")]
    assert canvas.inbound.pending(_SERVICE) == 0
    (message,) = canvas.inbound.drain(_CHAT)
    assert message.text == "rerun the suite"


@pytest.mark.asyncio
async def test_a_relay_into_a_cli_chat_no_run_holds_is_not_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canvas = _Canvas(monkeypatch, chat=_CHAT, provider="codex", offline=True)

    assert await canvas.relay() is False
    assert canvas.routed == [(_CONVERSATION, "codex")]
    assert canvas.inbound.pending(_CHAT) == canvas.inbound.pending(_SERVICE) == 0


@pytest.mark.asyncio
async def test_a_canvas_that_is_not_the_callers_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canvas = _Canvas(monkeypatch, chat=_CHAT)

    async def not_theirs(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(console_relay, "read_canvas", not_theirs)

    assert await canvas.relay() is None
    assert canvas.inbound.pending(_CHAT) == canvas.inbound.pending(_SERVICE) == 0


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
    state: CanvasState,
) -> None:
    canvas = _Canvas(monkeypatch, state=state, chat=_CHAT)

    assert await canvas.relay() is False
    assert canvas.inbound.pending(_CHAT) == canvas.inbound.pending(_SERVICE) == 0


@pytest.mark.asyncio
async def test_a_sender_who_may_not_post_into_the_chat_relays_nothing_into_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canvas = _Canvas(monkeypatch, chat=_CHAT, may_post=False)

    assert await canvas.relay() is False
    assert canvas.asked_to_post == [("alice@example.com", _CONVERSATION)]
    assert canvas.routed == []
    assert canvas.inbound.pending(_CHAT) == canvas.inbound.pending(_SERVICE) == 0


@pytest.mark.asyncio
async def test_a_deleted_chat_hears_no_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canvas = _Canvas(monkeypatch, chat=_CHAT, deleted=True)

    assert await canvas.relay() is False
    assert canvas.locked == [_CONVERSATION]
    assert canvas.asked_to_post == canvas.routed == []
    assert canvas.inbound.pending(_CHAT) == canvas.inbound.pending(_SERVICE) == 0


@pytest.mark.asyncio
async def test_a_relay_does_not_evict_a_line_the_partner_has_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canvas = _Canvas(monkeypatch, chat=_CHAT)
    canvas.inbound = InboundQueue(max_per_session=1)

    assert await canvas.relay(key=uuid.UUID(int=8)) is True
    assert await canvas.relay() is False
    assert canvas.inbound.pending(_CHAT) == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
