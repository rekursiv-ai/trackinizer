"""The workspace event stream: frames, rechecks, comments, and slow subscribers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncio
import uuid

import pytest

from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.chat_hub import (
    ChangedFrame,
    ChatHub,
    DeliveredFrame,
    HighlightFrame,
    MessageFrame,
    NavigateFrame,
    StatusFrame,
    iter_workspace_events,
)
from trackinizer.server.visuals.workspaces import WorkspacePartner, WorkspaceState
from trackinizer.wire.wire_chats import ChatMessage


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


_WORKSPACE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_CONVERSATION = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _partner(status: str) -> WorkspacePartner:
    return WorkspacePartner.model_validate(
        {
            "session_id": None,
            "actor": "scout",
            "cli": None,
            "status": status,
        },
    )


class _Canvas:
    """A canvas whose partner a test can change between reads."""

    def __init__(self) -> None:
        self.partner = _partner("live")
        self.reads = 0
        self.is_active = True

    async def state(self) -> WorkspaceState:
        self.reads += 1
        return WorkspaceState(
            id=_WORKSPACE,
            revision=3,
            visuals=[],
            partner=self.partner,
        )

    async def active(self) -> bool:
        return self.is_active

    async def conversations(self) -> list[uuid.UUID]:
        return [_CONVERSATION]


def _frame(chunk: bytes) -> dict[str, object]:
    text = chunk.decode()
    assert text.startswith("data: ")
    assert text.endswith("\n\n")
    return from_plain(loads(text.removeprefix("data: ")), dict[str, object])


def _message() -> ChatMessage:
    return ChatMessage(
        id=uuid.uuid4(),
        seq=1,
        role="assistant",
        author="scout",
        text="hi",
        created=datetime.now(UTC),
    )


def _events(
    hub: ChatHub,
    *,
    canvas: _Canvas,
    changes: AsyncGenerator[str] | None = None,
    keepalive_sec: float = 60,
) -> AsyncGenerator[bytes]:
    return iter_workspace_events(
        hub,
        workspace_id=_WORKSPACE,
        read_state=canvas.state,
        read_conversations=canvas.conversations,
        is_active=canvas.active,
        changes=changes,
        keepalive_sec=keepalive_sec,
    )


async def _ids(*ids: str) -> AsyncGenerator[str]:
    for changed in ids:
        yield changed
        await asyncio.sleep(0)
    await asyncio.sleep(3600)


async def _partner_status(events: AsyncGenerator[bytes]) -> str:
    changed = _frame(await asyncio.wait_for(anext(events), 2))
    assert changed["type"] == "workspace"
    partner = from_plain(
        from_plain(changed["state"], dict[str, object])["partner"],
        dict[str, object],
    )
    return str(partner["status"])


@pytest.mark.asyncio
async def test_stream_opens_with_a_comment_then_the_state_then_every_frame() -> None:
    """Open comment, workspace state, then published frames, each stamped with `t`."""
    hub = ChatHub()
    events = _events(hub, canvas=_Canvas())
    assert await anext(events) == b": open\n\n"
    opened = _frame(await anext(events))
    assert opened["type"] == "workspace"
    assert from_plain(opened["state"], dict[str, object])["revision"] == 3

    hub.publish(
        _WORKSPACE,
        frame=MessageFrame(conversation_id=_CONVERSATION, message=_message()),
    )
    hub.publish(
        _WORKSPACE,
        frame=StatusFrame(conversation_id=_CONVERSATION, text="thinking"),
    )
    hub.publish(_WORKSPACE, frame=DeliveredFrame(conversation_id=_CONVERSATION, seq=4))
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/graph"))
    hub.publish(uuid.uuid4(), frame=NavigateFrame(route="#/elsewhere"))
    hub.publish(_WORKSPACE, frame=HighlightFrame(ids=[_CONVERSATION]))
    frames = [_frame(await anext(events)) for _ in range(5)]
    assert [f["type"] for f in frames] == [
        "message",
        "status",
        "delivered",
        "navigate",
        "highlight",
    ]
    assert frames[4]["ids"] == [str(_CONVERSATION)]
    assert all(isinstance(f["t"], int) and f["t"] > 0 for f in frames)
    assert frames[0]["conversation_id"] == str(_CONVERSATION)
    assert from_plain(frames[0]["message"], dict[str, object])["text"] == "hi"
    assert frames[1]["text"] == "thinking"
    assert frames[2]["seq"] == 4
    assert frames[3]["route"] == "#/graph"
    await events.aclose()


@pytest.mark.asyncio
async def test_open_replays_each_conversations_status_and_delivered_seq() -> None:
    """A reload mid-turn shows what the partner is doing."""
    hub = ChatHub()
    hub.set_status(_CONVERSATION, text="reading the graph")
    hub.set_delivered(_CONVERSATION, seq=7)
    events = _events(hub, canvas=_Canvas())
    assert await anext(events) == b": open\n\n"
    assert _frame(await anext(events))["type"] == "workspace"
    status = _frame(await anext(events))
    delivered = _frame(await anext(events))
    assert (status["type"], status["text"]) == ("status", "reading the graph")
    assert (delivered["type"], delivered["seq"]) == ("delivered", 7)
    await events.aclose()


@pytest.mark.asyncio
async def test_open_with_nothing_to_replay_sends_only_the_canvas() -> None:
    """No status and no delivered seq means no frame for them."""
    hub = ChatHub()
    events = _events(hub, canvas=_Canvas())
    await anext(events)
    await anext(events)
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/x"))
    assert _frame(await anext(events))["type"] == "navigate"
    await events.aclose()


@pytest.mark.asyncio
async def test_idle_stream_sends_keepalive_comments() -> None:
    """A quiet stream says so, so a proxy never sees a long silence."""
    hub = ChatHub()
    events = _events(hub, canvas=_Canvas(), keepalive_sec=0.01)
    assert await anext(events) == b": open\n\n"
    await anext(events)
    assert await anext(events) == b": keepalive\n\n"
    await events.aclose()


@pytest.mark.asyncio
async def test_the_stream_ends_at_a_keepalive_once_the_owner_is_no_longer_active() -> (
    None
):
    """A user disabled mid-stream does not keep reading."""
    hub = ChatHub()
    canvas = _Canvas()
    events = _events(hub, canvas=canvas, keepalive_sec=0.01)
    await anext(events)
    await anext(events)
    assert await anext(events) == b": keepalive\n\n"
    canvas.is_active = False
    chunks = [chunk async for chunk in events]
    assert chunks == []


@pytest.mark.asyncio
async def test_keepalive_rechecks_the_partner_and_sends_the_canvas_when_it_changed() -> (
    None
):
    """A partner that went away reaches the browser without any operation."""
    hub = ChatHub()
    canvas = _Canvas()
    events = _events(hub, canvas=canvas, keepalive_sec=0.01)
    await anext(events)
    await anext(events)
    assert await anext(events) == b": keepalive\n\n"
    assert await anext(events) == b": keepalive\n\n"
    assert canvas.reads >= 2
    canvas.partner = _partner("unavailable")
    chunk = await anext(events)
    while chunk == b": keepalive\n\n":
        chunk = await anext(events)
    changed = _frame(chunk)
    assert changed["type"] == "workspace"
    partner = from_plain(
        from_plain(changed["state"], dict[str, object])["partner"],
        dict[str, object],
    )
    assert partner["status"] == "unavailable"
    await events.aclose()


@pytest.mark.asyncio
async def test_a_nudge_rechecks_the_partner_at_once() -> None:
    """A session that starts or ends shows up without waiting for the keepalive."""
    hub = ChatHub()
    canvas = _Canvas()
    events = _events(hub, canvas=canvas)
    await anext(events)
    await anext(events)
    canvas.partner = _partner("unavailable")
    hub.nudge()
    assert await _partner_status(events) == "unavailable"
    await events.aclose()


@pytest.mark.asyncio
async def test_a_nudge_with_the_same_partner_sends_nothing() -> None:
    """Only a change is news."""
    hub = ChatHub()
    canvas = _Canvas()
    events = _events(hub, canvas=canvas)
    await anext(events)
    await anext(events)
    hub.nudge()
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/after"))
    assert _frame(await asyncio.wait_for(anext(events), 2))["route"] == "#/after"
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/again"))
    assert _frame(await asyncio.wait_for(anext(events), 2))["route"] == "#/again"
    assert canvas.reads >= 2
    await events.aclose()


@pytest.mark.asyncio
async def test_changed_inquiry_ids_are_relayed_as_frames() -> None:
    """One stream also carries what `/api/web/subscribe` carries."""
    hub = ChatHub()
    events = _events(hub, canvas=_Canvas(), changes=_ids("a-1", "b-2"))
    await anext(events)
    await anext(events)
    first = _frame(await asyncio.wait_for(anext(events), 2))
    second = _frame(await asyncio.wait_for(anext(events), 2))
    assert (first["type"], first["id"]) == ("changed", "a-1")
    assert second["id"] == "b-2"
    assert isinstance(first["t"], int)
    await events.aclose()


@pytest.mark.asyncio
async def test_a_finished_change_source_does_not_end_the_stream() -> None:
    """The ids ending leaves the other frames flowing."""

    async def none() -> AsyncGenerator[str]:
        for changed in ():
            yield changed

    hub = ChatHub()
    events = _events(hub, canvas=_Canvas(), changes=none())
    await anext(events)
    await anext(events)
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/still"))
    assert _frame(await asyncio.wait_for(anext(events), 2))["route"] == "#/still"
    await events.aclose()


@pytest.mark.asyncio
async def test_subscriber_more_than_256_frames_behind_is_dropped() -> None:
    """The stream of an overflowing subscriber ends; the others are unharmed."""
    hub = ChatHub()
    slow = _events(hub, canvas=_Canvas())
    await anext(slow)
    await anext(slow)
    with hub.subscribe(_WORKSPACE) as healthy:
        for _ in range(257):
            hub.publish(
                _WORKSPACE,
                frame=DeliveredFrame(conversation_id=_CONVERSATION, seq=1),
            )
            healthy.queue.get_nowait()
        hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/after"))
        assert healthy.queue.qsize() == 1
    assert [chunk async for chunk in slow] == []


def test_status_is_kept_until_cleared_and_forgotten_with_its_conversation() -> None:
    """A status is the conversation's current one until the next, or empty."""
    hub = ChatHub()
    assert hub.status_of(_CONVERSATION) == ""
    hub.set_status(_CONVERSATION, text="thinking")
    hub.set_status(_CONVERSATION, text="writing")
    assert hub.status_of(_CONVERSATION) == "writing"
    hub.set_status(_CONVERSATION, text="")
    assert hub.status_of(_CONVERSATION) == ""
    hub.set_status(_CONVERSATION, text="again")
    hub.set_delivered(_CONVERSATION, seq=3)
    hub.forget(_CONVERSATION)
    assert (hub.status_of(_CONVERSATION), hub.delivered_of(_CONVERSATION)) == ("", 0)


def test_delivered_never_moves_back() -> None:
    """A late, lower seq does not un-deliver a message."""
    hub = ChatHub()
    hub.set_delivered(_CONVERSATION, seq=5)
    hub.set_delivered(_CONVERSATION, seq=2)
    assert hub.delivered_of(_CONVERSATION) == 5


def test_changed_frame_names_its_inquiry() -> None:
    """The frame model carries the id and a stamp."""
    frame = ChangedFrame(id="x")
    assert (frame.type, frame.id) == ("changed", "x")
    assert frame.t > 0


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
