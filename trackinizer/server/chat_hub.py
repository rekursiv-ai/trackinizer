"""In-process fan-out of canvas changes to the owner's open browsers.

The server runs single-process, as the inbound queue already requires, so one
broker keeps every subscriber's queue. A Chat conversation is an AgentSession, so
its lines reach a browser as the session's ``changed`` id, the way every other
record does; the hub carries the canvas and what an agent does to it. Callers
publish only after the database commit, so a frame never names state a reader
cannot yet fetch.

A frame is one SSE ``data:`` line of JSON carrying ``t``, the server's epoch
milliseconds, so a client can tell how long a change took to reach it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

import asyncio
import time

from pydantic import BaseModel, Field

from trackinizer.server.notify import SSE_PING
from trackinizer.server.preferences import Preferences
from trackinizer.server.visuals.canvases import CanvasState


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Generator


__all__ = [
    "CanvasFrame",
    "CaptureFrame",
    "ChangedFrame",
    "ChatFrame",
    "ChatHub",
    "Frame",
    "HighlightFrame",
    "NavigateFrame",
    "PreferencesFrame",
    "Subscription",
    "iter_canvas_events",
]


SUBSCRIBER_QUEUE_SIZE: Final = 256
"""Frames one subscriber may fall behind before its stream is ended."""


class _Stamped(BaseModel):
    """A frame, stamped with the server's epoch milliseconds when it was made."""

    t: int = Field(default_factory=lambda: time.time_ns() // 1_000_000)


class CanvasFrame(_Stamped):
    """The canvas as it stands: on open, after every change, and on a new partner."""

    type: Literal["canvas"] = "canvas"
    state: CanvasState


class NavigateFrame(_Stamped):
    """An agent moved the page. Not replayed to a tab that was not listening."""

    type: Literal["navigate"] = "navigate"
    route: str


class HighlightFrame(_Stamped):
    """An agent marked inquiries on the page; empty clears. Not replayed."""

    type: Literal["highlight"] = "highlight"
    ids: list[UUID]


class ChatFrame(_Stamped):
    """An agent hid or showed the tab's Chat window. Not replayed, nor stored."""

    type: Literal["chat"] = "chat"
    hidden: bool


class CaptureFrame(_Stamped):
    """An agent asks the open page for a picture of the canvas. Not replayed."""

    type: Literal["capture"] = "capture"
    request: UUID


class PreferencesFrame(_Stamped):
    """The canvas owner's preferences changed: by them, or by the canvas's agent.

    Sent to every canvas of theirs, so each of their tabs shows the change. Not
    replayed: a tab that opens reads them from the profile.
    """

    type: Literal["preferences"] = "preferences"
    preferences: Preferences


class ChangedFrame(_Stamped):
    """An inquiry changed, as ``/api/web/subscribe`` reports it."""

    type: Literal["changed"] = "changed"
    id: str


type Frame = (
    CanvasFrame
    | NavigateFrame
    | HighlightFrame
    | ChatFrame
    | CaptureFrame
    | PreferencesFrame
    | ChangedFrame
)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Subscription:
    """One open stream's frames; ``None`` ends the stream."""

    queue: asyncio.Queue[Frame | None]

    nudged: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when something that may change a canvas's partner happened."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatHub:
    """Per-canvas subscribers."""

    queue_size: int = SUBSCRIBER_QUEUE_SIZE

    _subscribers: dict[UUID, set[Subscription]] = field(default_factory=dict)

    @contextmanager
    def subscribe(self, canvas_id: UUID) -> Generator[Subscription]:
        """Receive every frame published for a canvas until the block ends.

        Args:
          canvas_id: Canvas whose frames to receive.

        Yields:
          subscription: The queue frames arrive on.

        """
        subscription = Subscription(queue=asyncio.Queue(maxsize=self.queue_size))
        self._subscribers.setdefault(canvas_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            self._drop(canvas_id, subscription=subscription)

    def publish(self, canvas_id: UUID, *, frame: Frame) -> None:
        """Queue a frame for every subscriber, dropping any that is too far behind.

        Args:
          canvas_id: Canvas the frame is about.
          frame: What happened.

        """
        for subscription in tuple(self._subscribers.get(canvas_id, ())):
            try:
                subscription.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # It reconnects, and starts again from the canvas frame.
                self._drop(canvas_id, subscription=subscription)
                while not subscription.queue.empty():
                    subscription.queue.get_nowait()
                subscription.queue.put_nowait(None)

    def listening(self, canvas_id: UUID) -> bool:
        """Say whether any open stream follows a canvas.

        Args:
          canvas_id: Canvas to ask about.

        Returns:
          listening: True while at least one stream is subscribed.

        """
        return canvas_id in self._subscribers

    def nudge(self) -> None:
        """Ask every open stream to recheck its canvas's partner.

        Called when a session starts or ends, or a poller takes a lease.
        """
        for subscribers in self._subscribers.values():
            for subscription in subscribers:
                subscription.nudged.set()

    def _drop(self, canvas_id: UUID, *, subscription: Subscription) -> None:
        """Remove a subscriber, and its canvas's entry once it is the last."""
        subscribers = self._subscribers.get(canvas_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            del self._subscribers[canvas_id]


async def iter_canvas_events(
    hub: ChatHub,
    *,
    canvas_id: UUID,
    read_state: Callable[[], Awaitable[CanvasState]],
    is_active: Callable[[], Awaitable[bool]],
    changes: AsyncGenerator[str] | None = None,
    keepalive_sec: float = 25.0,
    viewer: bool = False,
) -> AsyncGenerator[bytes]:
    r"""Stream a canvas's frames as SSE, as ``/api/web/subscribe`` streams ids.

    Opens with a comment, because a proxy in front of production holds the
    response headers until the first body byte, then the canvas as it stands, read
    after subscribing so no change falls between the read and the first frame. The
    canvas is read again, and sent when its partner differs, whenever the broker
    is nudged and every ``keepalive_sec``, even while frames arrive. An idle
    interval also gets a ``ping`` event, which the page watches for. Inquiry ids from ``changes`` pass through as
    ``changed`` frames. The
    stream ends when the subscriber falls too far behind, or when its owner is no
    longer active; it reconnects only in the first case.

    Args:
      hub: Broker to subscribe on.
      canvas_id: Canvas to follow.
      read_state: Reads the canvas, with its partner.
      is_active: Whether the canvas's owner may still use the app; asked on each
        interval, even while frames arrive, and the stream ends when it says no.
      changes: Ids of changed inquiries, or None to relay none.
      keepalive_sec: Longest silence before a ping; must stay well under the
        edge's 125 s idle cutoff.
      viewer: The subscriber views another person's canvas. Only its canvas
        frames reach them, each read again by ``read_state``: the others
        (navigate, highlight, chat, capture, preferences) are the owner's tab's,
        and a published canvas carries the owner's preferences.

    Yields:
      item: SSE comments and ``data:`` frames.

    """
    with hub.subscribe(canvas_id) as stream:
        yield b": open\n\n"
        state = await read_state()
        yield _sse(CanvasFrame(state=state))
        partner = state.partner
        frame_wait = asyncio.ensure_future(stream.queue.get())
        nudge_wait = asyncio.ensure_future(stream.nudged.wait())
        change_wait = None if changes is None else asyncio.ensure_future(anext(changes))
        deadline = time.monotonic() + keepalive_sec
        try:
            while True:
                waits = {w for w in (frame_wait, nudge_wait, change_wait) if w}
                done, _ = await asyncio.wait(
                    waits,
                    timeout=max(0.0, deadline - time.monotonic()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    yield SSE_PING
                recheck = not done or time.monotonic() >= deadline
                if recheck:
                    if not await is_active():
                        return
                    deadline = time.monotonic() + keepalive_sec
                if nudge_wait in done:
                    stream.nudged.clear()
                    nudge_wait = asyncio.ensure_future(stream.nudged.wait())
                    recheck = True
                if recheck:
                    state = await read_state()
                    if state.partner != partner:
                        partner = state.partner
                        yield _sse(CanvasFrame(state=state))
                if frame_wait in done:
                    frame = frame_wait.result()
                    frame_wait = asyncio.ensure_future(stream.queue.get())
                    if frame is None:
                        return
                    if isinstance(frame, CanvasFrame):
                        if viewer:
                            frame = CanvasFrame(state=await read_state())
                        partner = frame.state.partner
                        yield _sse(frame)
                    elif not viewer:
                        yield _sse(frame)
                if changes is not None and change_wait in done:
                    try:
                        changed = change_wait.result()
                    except StopAsyncIteration:
                        change_wait = None
                    else:
                        change_wait = asyncio.ensure_future(anext(changes))
                        yield _sse(ChangedFrame(id=changed))
        finally:
            pending = {w for w in (frame_wait, nudge_wait, change_wait) if w}
            for wait in pending:
                wait.cancel()
            await asyncio.wait(pending)
            if changes is not None:
                await changes.aclose()


def _sse(frame: Frame) -> bytes:
    """Encode one frame as an SSE ``data:`` line."""
    return f"data: {frame.model_dump_json()}\n\n".encode()
