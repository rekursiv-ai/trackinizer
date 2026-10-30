"""The canvas event stream: frames, rechecks, comments, and slow subscribers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, TypedDict

import asyncio
import time
import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server import chat_hub
from trackinizer.server.chat_hub import (
    CanvasFrame,
    CaptureFrame,
    ChangedFrame,
    ChatFrame,
    ChatHub,
    HighlightFrame,
    NavigateFrame,
    Subscription,
    iter_canvas_events,
)
from trackinizer.server.visuals.canvases import CanvasPartner, CanvasState


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


_CANVAS = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_RECORD = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _partner(status: str) -> CanvasPartner:
    return CanvasPartner.model_validate(
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

    async def state(self) -> CanvasState:
        self.reads += 1
        return CanvasState(
            id=_CANVAS,
            revision=3,
            name="unnamed-canvas#1",
            conversation_id=None,
            page=None,
            visuals=[],
            partner=self.partner,
        )

    async def active(self) -> bool:
        return self.is_active


class _Partner(TypedDict):
    status: str


class _State(TypedDict):
    revision: int
    partner: _Partner


class _CanvasFrame(TypedDict):
    type: str
    state: _State


def _frame[T](chunk: bytes, schema: type[T]) -> T:
    text = chunk.decode()
    assert text.startswith("data: ")
    assert text.endswith("\n\n")
    return json_loads(text.removeprefix("data: "), schema)


def _events(
    hub: ChatHub,
    *,
    canvas: _Canvas,
    changes: AsyncGenerator[str] | None = None,
    keepalive_sec: float = 60,
) -> AsyncGenerator[bytes]:
    return iter_canvas_events(
        hub,
        canvas_id=_CANVAS,
        read_state=canvas.state,
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
    changed = _frame(await asyncio.wait_for(anext(events), 2), _CanvasFrame)
    assert changed["type"] == "canvas"
    return changed["state"]["partner"]["status"]


@pytest.mark.asyncio
async def test_stream_opens_with_a_comment_then_the_state_then_every_frame() -> None:
    """Open comment, canvas state, then published frames, each stamped with `t`."""
    hub = ChatHub()
    events = _events(hub, canvas=_Canvas())
    assert await anext(events) == b": open\n\n"
    opened = _frame(await anext(events), _CanvasFrame)
    assert opened["type"] == "canvas"
    assert opened["state"]["revision"] == 3

    hub.publish(_CANVAS, frame=NavigateFrame(route="#/graph"))
    hub.publish(uuid.uuid4(), frame=NavigateFrame(route="#/elsewhere"))
    hub.publish(_CANVAS, frame=HighlightFrame(ids=[_RECORD]))
    hub.publish(_CANVAS, frame=ChatFrame(hidden=True))
    frames = [_frame(await anext(events), dict[str, object]) for _ in range(3)]
    assert [f["type"] for f in frames] == ["navigate", "highlight", "chat"]
    assert set(frames[2]) == {"t", "type", "hidden"}
    assert frames[2]["hidden"] is True
    assert frames[1]["ids"] == [str(_RECORD)]
    assert all(isinstance(f["t"], int) and f["t"] > 0 for f in frames)
    assert frames[0]["route"] == "#/graph"
    await events.aclose()


@pytest.mark.asyncio
async def test_a_viewer_hears_the_canvas_alone_each_time_read_as_they_read_it() -> None:
    """The owner's tab's own frames are not a viewer's; a broadcast canvas holds the owner's preferences."""
    hub = ChatHub()
    canvas = _Canvas()
    events = iter_canvas_events(
        hub,
        canvas_id=_CANVAS,
        read_state=canvas.state,
        is_active=canvas.active,
        viewer=True,
    )
    await anext(events)
    await anext(events)
    owners = (await canvas.state()).model_copy(
        update={"preferences": {"theme": "light"}},
    )
    reads = canvas.reads
    hub.publish(_CANVAS, frame=NavigateFrame(route="#/graph"))
    hub.publish(_CANVAS, frame=HighlightFrame(ids=[_RECORD]))
    hub.publish(_CANVAS, frame=ChatFrame(hidden=True))
    hub.publish(_CANVAS, frame=CaptureFrame(request=_RECORD))
    hub.publish(_CANVAS, frame=CanvasFrame(state=owners))
    frame = _frame(await asyncio.wait_for(anext(events), 2), dict[str, object])
    assert frame["type"] == "canvas"
    assert from_plain(frame["state"], dict[str, object])["preferences"] is None
    assert canvas.reads == reads + 1
    await events.aclose()


@pytest.mark.asyncio
async def test_the_stream_carries_no_chat_frames_of_its_own() -> None:
    """A conversation's lines are its session's records, so they arrive as `changed` ids."""
    hub = ChatHub()
    events = _events(hub, canvas=_Canvas(), changes=_ids("session-1"))
    await anext(events)
    await anext(events)
    frame = _frame(await asyncio.wait_for(anext(events), 2), dict[str, object])
    assert (frame["type"], frame["id"]) == ("changed", "session-1")
    await events.aclose()


@pytest.mark.asyncio
async def test_idle_stream_sends_ping_events() -> None:
    """A quiet stream pings, so a proxy never sees a long silence and the page can tell a dead stream."""
    hub = ChatHub()
    events = _events(hub, canvas=_Canvas(), keepalive_sec=0.01)
    assert await anext(events) == b": open\n\n"
    await anext(events)
    assert await anext(events) == b"event: ping\ndata: {}\n\n"
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
    assert await anext(events) == b"event: ping\ndata: {}\n\n"
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
    assert await anext(events) == b"event: ping\ndata: {}\n\n"
    assert await anext(events) == b"event: ping\ndata: {}\n\n"
    assert canvas.reads >= 2
    canvas.partner = _partner("unavailable")
    chunk = await anext(events)
    while chunk == b"event: ping\ndata: {}\n\n":
        chunk = await anext(events)
    changed = _frame(chunk, _CanvasFrame)
    assert changed["type"] == "canvas"
    assert changed["state"]["partner"]["status"] == "unavailable"
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
async def test_busy_stream_checks_revocation_on_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(
        chat_hub,
        "time",
        SimpleNamespace(monotonic=lambda: now[0], time_ns=time.time_ns),
    )
    hub = ChatHub()
    canvas = _Canvas()
    events = _events(hub, canvas=canvas)
    await anext(events)
    await anext(events)
    hub.publish(_CANVAS, frame=NavigateFrame(route="#/first"))
    await anext(events)
    now[0] = 61.0
    canvas.is_active = False
    hub.publish(_CANVAS, frame=NavigateFrame(route="#/revoked"))
    try:
        with pytest.raises(StopAsyncIteration):
            await anext(events)
    finally:
        await events.aclose()


@pytest.mark.asyncio
async def test_busy_stream_rechecks_partner_on_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(
        chat_hub,
        "time",
        SimpleNamespace(monotonic=lambda: now[0], time_ns=time.time_ns),
    )
    hub = ChatHub()
    canvas = _Canvas()
    events = _events(hub, canvas=canvas)
    await anext(events)
    await anext(events)
    hub.publish(_CANVAS, frame=NavigateFrame(route="#/first"))
    await anext(events)
    now[0] = 61.0
    canvas.partner = _partner("unavailable")
    hub.publish(_CANVAS, frame=NavigateFrame(route="#/busy"))
    try:
        assert await _partner_status(events) == "unavailable"
    finally:
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
    hub.publish(_CANVAS, frame=NavigateFrame(route="#/after"))
    assert (
        _frame(await asyncio.wait_for(anext(events), 2), dict[str, object])["route"]
        == "#/after"
    )
    hub.publish(_CANVAS, frame=NavigateFrame(route="#/again"))
    assert (
        _frame(await asyncio.wait_for(anext(events), 2), dict[str, object])["route"]
        == "#/again"
    )
    assert canvas.reads >= 2
    await events.aclose()


@pytest.mark.asyncio
async def test_changed_inquiry_ids_are_relayed_as_frames() -> None:
    """One stream also carries what `/api/web/subscribe` carries."""
    hub = ChatHub()
    events = _events(hub, canvas=_Canvas(), changes=_ids("a-1", "b-2"))
    await anext(events)
    await anext(events)
    first = _frame(await asyncio.wait_for(anext(events), 2), dict[str, object])
    second = _frame(await asyncio.wait_for(anext(events), 2), dict[str, object])
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
    hub.publish(_CANVAS, frame=NavigateFrame(route="#/still"))
    assert (
        _frame(await asyncio.wait_for(anext(events), 2), dict[str, object])["route"]
        == "#/still"
    )
    await events.aclose()


@pytest.mark.asyncio
async def test_subscriber_more_than_256_frames_behind_is_dropped() -> None:
    """The stream of an overflowing subscriber ends; the others are unharmed."""
    hub = ChatHub()
    slow = _events(hub, canvas=_Canvas())
    await anext(slow)
    await anext(slow)
    with hub.subscribe(_CANVAS) as healthy:
        for _ in range(257):
            hub.publish(_CANVAS, frame=NavigateFrame(route="#/busy"))
            healthy.queue.get_nowait()
        hub.publish(_CANVAS, frame=NavigateFrame(route="#/after"))
        assert healthy.queue.qsize() == 1
    assert [chunk async for chunk in slow] == []


def test_a_subscriber_that_leaves_hears_no_more_and_the_last_takes_the_entry() -> None:
    """Leaving drops only that subscriber; the canvas's entry goes with the last."""
    held: dict[uuid.UUID, set[Subscription]] = {}
    hub = ChatHub(_subscribers=held)
    other = uuid.UUID(int=7)
    with hub.subscribe(_CANVAS) as stays, hub.subscribe(other) as elsewhere:
        with hub.subscribe(_CANVAS) as leaves:
            assert len(held[_CANVAS]) == 2
        hub.publish(_CANVAS, frame=NavigateFrame(route="#/after"))
        assert (stays.queue.qsize(), leaves.queue.qsize()) == (1, 0)
        assert elsewhere.queue.qsize() == 0
        assert set(held) == {_CANVAS, other}
    assert held == {}


def test_a_canvas_is_listened_to_only_while_a_stream_follows_it() -> None:
    """``listening`` is how a request for a picture fails at once with no page open."""
    hub = ChatHub()
    assert not hub.listening(_CANVAS)
    with hub.subscribe(_CANVAS):
        assert hub.listening(_CANVAS)
        assert not hub.listening(uuid.UUID(int=7))
    assert not hub.listening(_CANVAS)


def test_a_capture_frame_reaches_the_stream_and_is_not_replayed() -> None:
    """A request for a picture is an event: a stream that opens later never hears it."""
    hub = ChatHub()
    request = uuid.UUID(int=9)
    with hub.subscribe(_CANVAS) as early:
        hub.publish(_CANVAS, frame=CaptureFrame(request=request))
        with hub.subscribe(_CANVAS) as late:
            assert late.queue.empty()
        frame = early.queue.get_nowait()
    assert isinstance(frame, CaptureFrame)
    assert json_loads(frame.model_dump_json(), dict[str, object])["request"] == str(
        request,
    )


def test_changed_frame_names_its_inquiry() -> None:
    """The frame model carries the id and a stamp."""
    frame = ChangedFrame(id="x")
    assert (frame.type, frame.id) == ("changed", "x")
    assert frame.t > 0


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
