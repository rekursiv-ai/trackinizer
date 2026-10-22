"""The workspace event stream: frames, rechecks, comments, and slow subscribers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, TypedDict

import asyncio
import time
import uuid

from treekle import json_loads

import pytest

from trackinizer.server import chat_hub
from trackinizer.server.chat_hub import (
    CaptureFrame,
    ChangedFrame,
    ChatFrame,
    ChatHub,
    HighlightFrame,
    NavigateFrame,
    Subscription,
    iter_workspace_events,
)
from trackinizer.server.visuals.workspaces import WorkspacePartner, WorkspaceState


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


_WORKSPACE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_RECORD = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


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
            name="unnamed-workstream#1",
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


class _WorkspaceFrame(TypedDict):
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
    return iter_workspace_events(
        hub,
        workspace_id=_WORKSPACE,
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
    changed = _frame(await asyncio.wait_for(anext(events), 2), _WorkspaceFrame)
    assert changed["type"] == "workspace"
    return changed["state"]["partner"]["status"]


@pytest.mark.asyncio
async def test_stream_opens_with_a_comment_then_the_state_then_every_frame() -> None:
    """Open comment, workspace state, then published frames, each stamped with `t`."""
    hub = ChatHub()
    events = _events(hub, canvas=_Canvas())
    assert await anext(events) == b": open\n\n"
    opened = _frame(await anext(events), _WorkspaceFrame)
    assert opened["type"] == "workspace"
    assert opened["state"]["revision"] == 3

    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/graph"))
    hub.publish(uuid.uuid4(), frame=NavigateFrame(route="#/elsewhere"))
    hub.publish(_WORKSPACE, frame=HighlightFrame(ids=[_RECORD]))
    hub.publish(_WORKSPACE, frame=ChatFrame(hidden=True))
    frames = [_frame(await anext(events), dict[str, object]) for _ in range(3)]
    assert [f["type"] for f in frames] == ["navigate", "highlight", "chat"]
    assert set(frames[2]) == {"t", "type", "hidden"}
    assert frames[2]["hidden"] is True
    assert frames[1]["ids"] == [str(_RECORD)]
    assert all(isinstance(f["t"], int) and f["t"] > 0 for f in frames)
    assert frames[0]["route"] == "#/graph"
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
    changed = _frame(chunk, _WorkspaceFrame)
    assert changed["type"] == "workspace"
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
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/first"))
    await anext(events)
    now[0] = 61.0
    canvas.is_active = False
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/revoked"))
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
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/first"))
    await anext(events)
    now[0] = 61.0
    canvas.partner = _partner("unavailable")
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/busy"))
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
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/after"))
    assert (
        _frame(await asyncio.wait_for(anext(events), 2), dict[str, object])["route"]
        == "#/after"
    )
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/again"))
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
    hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/still"))
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
    with hub.subscribe(_WORKSPACE) as healthy:
        for _ in range(257):
            hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/busy"))
            healthy.queue.get_nowait()
        hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/after"))
        assert healthy.queue.qsize() == 1
    assert [chunk async for chunk in slow] == []


def test_a_subscriber_that_leaves_hears_no_more_and_the_last_takes_the_entry() -> None:
    """Leaving drops only that subscriber; the workspace's entry goes with the last."""
    held: dict[uuid.UUID, set[Subscription]] = {}
    hub = ChatHub(_subscribers=held)
    other = uuid.UUID(int=7)
    with hub.subscribe(_WORKSPACE) as stays, hub.subscribe(other) as elsewhere:
        with hub.subscribe(_WORKSPACE) as leaves:
            assert len(held[_WORKSPACE]) == 2
        hub.publish(_WORKSPACE, frame=NavigateFrame(route="#/after"))
        assert (stays.queue.qsize(), leaves.queue.qsize()) == (1, 0)
        assert elsewhere.queue.qsize() == 0
        assert set(held) == {_WORKSPACE, other}
    assert held == {}


def test_a_canvas_is_listened_to_only_while_a_stream_follows_it() -> None:
    """``listening`` is how a request for a picture fails at once with no page open."""
    hub = ChatHub()
    assert not hub.listening(_WORKSPACE)
    with hub.subscribe(_WORKSPACE):
        assert hub.listening(_WORKSPACE)
        assert not hub.listening(uuid.UUID(int=7))
    assert not hub.listening(_WORKSPACE)


def test_a_capture_frame_reaches_the_stream_and_is_not_replayed() -> None:
    """A request for a picture is an event: a stream that opens later never hears it."""
    hub = ChatHub()
    request = uuid.UUID(int=9)
    with hub.subscribe(_WORKSPACE) as early:
        hub.publish(_WORKSPACE, frame=CaptureFrame(request=request))
        with hub.subscribe(_WORKSPACE) as late:
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
