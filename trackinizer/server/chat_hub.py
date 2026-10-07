"""In-process fan-out of canvas and Chat changes to the owner's open browsers.

The server runs single-process, as the inbound queue already requires, so one
broker keeps every subscriber's queue, and what the partner is doing in each
conversation: its current status and how far it has read. Callers publish only
after the database commit, so a frame never names state a reader cannot yet
fetch.

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

from trackinizer.server.visuals.workspaces import WorkspaceState
from trackinizer.wire.wire_chats import ChatMessage


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Generator


__all__ = [
    "ChangedFrame",
    "ChatHub",
    "DeletedFrame",
    "DeliveredFrame",
    "Frame",
    "HighlightFrame",
    "MessageFrame",
    "NavigateFrame",
    "StatusFrame",
    "Subscription",
    "WorkspaceFrame",
    "iter_workspace_events",
]


SUBSCRIBER_QUEUE_SIZE: Final = 256
"""Frames one subscriber may fall behind before its stream is ended."""


class _Stamped(BaseModel):
    """A frame, stamped with the server's epoch milliseconds when it was made."""

    t: int = Field(default_factory=lambda: time.time_ns() // 1_000_000)


class WorkspaceFrame(_Stamped):
    """The canvas as it stands: on open, after every change, and on a new partner."""

    type: Literal["workspace"] = "workspace"
    state: WorkspaceState


class NavigateFrame(_Stamped):
    """An agent moved the page. Not replayed to a tab that was not listening."""

    type: Literal["navigate"] = "navigate"
    route: str


class HighlightFrame(_Stamped):
    """An agent marked inquiries on the page; empty clears. Not replayed."""

    type: Literal["highlight"] = "highlight"
    ids: list[UUID]


class MessageFrame(_Stamped):
    """One stored user, assistant or attributed message."""

    type: Literal["message"] = "message"
    conversation_id: UUID
    message: ChatMessage


class StatusFrame(_Stamped):
    """An assistant's status for a conversation; empty clears it."""

    type: Literal["status"] = "status"
    conversation_id: UUID
    text: str


class DeliveredFrame(_Stamped):
    """The partner drained the conversation's messages through ``seq``."""

    type: Literal["delivered"] = "delivered"
    conversation_id: UUID
    seq: int


class DeletedFrame(_Stamped):
    """A conversation was deleted."""

    type: Literal["deleted"] = "deleted"
    conversation_id: UUID


class ChangedFrame(_Stamped):
    """An inquiry changed, as ``/api/web/subscribe`` reports it."""

    type: Literal["changed"] = "changed"
    id: str


type Frame = (
    WorkspaceFrame
    | NavigateFrame
    | HighlightFrame
    | MessageFrame
    | StatusFrame
    | DeliveredFrame
    | DeletedFrame
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
    """Per-workspace subscribers and what each conversation's partner is doing."""

    queue_size: int = SUBSCRIBER_QUEUE_SIZE

    _subscribers: dict[UUID, set[Subscription]] = field(default_factory=dict)

    _statuses: dict[UUID, str] = field(default_factory=dict)

    _delivered: dict[UUID, int] = field(default_factory=dict)

    @contextmanager
    def subscribe(self, workspace_id: UUID) -> Generator[Subscription]:
        """Receive every frame published for a workspace until the block ends.

        Args:
          workspace_id: Canvas whose frames to receive.

        Yields:
          subscription: The queue frames arrive on.

        """
        subscription = Subscription(queue=asyncio.Queue(maxsize=self.queue_size))
        self._subscribers.setdefault(workspace_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            self._drop(workspace_id, subscription=subscription)

    def publish(self, workspace_id: UUID, *, frame: Frame) -> None:
        """Queue a frame for every subscriber, dropping any that is too far behind.

        Args:
          workspace_id: Canvas the frame is about.
          frame: What happened.

        """
        for subscription in tuple(self._subscribers.get(workspace_id, ())):
            try:
                subscription.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # It reconnects, and starts again from the canvas frame.
                self._drop(workspace_id, subscription=subscription)
                while not subscription.queue.empty():
                    subscription.queue.get_nowait()
                subscription.queue.put_nowait(None)

    def nudge(self) -> None:
        """Ask every open stream to recheck its canvas's partner.

        Called when a session starts or ends, or a poller takes a lease.
        """
        for subscribers in self._subscribers.values():
            for subscription in subscribers:
                subscription.nudged.set()

    def set_status(self, conversation_id: UUID, *, text: str) -> None:
        """Keep a conversation's current status; empty clears it."""
        if text:
            self._statuses[conversation_id] = text
        else:
            self._statuses.pop(conversation_id, None)

    def status_of(self, conversation_id: UUID) -> str:
        """Return a conversation's current status, empty when it has none."""
        return self._statuses.get(conversation_id, "")

    def set_delivered(self, conversation_id: UUID, *, seq: int) -> None:
        """Keep how far the partner has read a conversation, never moving back."""
        self._delivered[conversation_id] = max(
            seq,
            self._delivered.get(conversation_id, 0),
        )

    def delivered_of(self, conversation_id: UUID) -> int:
        """Return the newest message the partner has read, 0 when none."""
        return self._delivered.get(conversation_id, 0)

    def forget(self, conversation_id: UUID) -> None:
        """Drop what is kept for a conversation that was deleted."""
        self._statuses.pop(conversation_id, None)
        self._delivered.pop(conversation_id, None)

    def _drop(self, workspace_id: UUID, *, subscription: Subscription) -> None:
        """Remove a subscriber, and its workspace's entry once it is the last."""
        subscribers = self._subscribers.get(workspace_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            del self._subscribers[workspace_id]


async def iter_workspace_events(
    hub: ChatHub,
    *,
    workspace_id: UUID,
    read_state: Callable[[], Awaitable[WorkspaceState]],
    read_conversations: Callable[[], Awaitable[list[UUID]]],
    is_active: Callable[[], Awaitable[bool]],
    changes: AsyncGenerator[str] | None = None,
    keepalive_sec: float = 25.0,
) -> AsyncGenerator[bytes]:
    r"""Stream a canvas's frames as SSE, as ``/api/web/subscribe`` streams ids.

    Opens with a comment, because a proxy in front of production holds the
    response headers until the first body byte, then the canvas as it stands and
    each conversation's current status and delivered ``seq``, read after
    subscribing so no change falls between the read and the first frame. The
    canvas is read again, and sent when its partner differs, whenever the broker
    is nudged and after each ``keepalive_sec`` without a frame, which also gets a
    comment. Inquiry ids from ``changes`` pass through as ``changed`` frames. The
    stream ends when the subscriber falls too far behind, or when its owner is no
    longer active; it reconnects only in the first case.

    Args:
      hub: Broker to subscribe on.
      workspace_id: Canvas to follow.
      read_state: Reads the canvas, with its partner.
      read_conversations: Lists the canvas's conversation ids.
      is_active: Whether the canvas's owner may still use the app; asked on each
        keepalive, and the stream ends when it says no.
      changes: Ids of changed inquiries, or None to relay none.
      keepalive_sec: Longest silence before a comment; must stay well under the
        edge's 125 s idle cutoff.

    Yields:
      item: SSE comments and ``data:`` frames.

    """
    with hub.subscribe(workspace_id) as stream:
        yield b": open\n\n"
        state = await read_state()
        yield _sse(WorkspaceFrame(state=state))
        partner = state.partner
        for conversation_id in await read_conversations():
            if status := hub.status_of(conversation_id):
                yield _sse(StatusFrame(conversation_id=conversation_id, text=status))
            if seq := hub.delivered_of(conversation_id):
                yield _sse(DeliveredFrame(conversation_id=conversation_id, seq=seq))
        frame_wait = asyncio.ensure_future(stream.queue.get())
        nudge_wait = asyncio.ensure_future(stream.nudged.wait())
        change_wait = None if changes is None else asyncio.ensure_future(anext(changes))
        try:
            while True:
                waits = {w for w in (frame_wait, nudge_wait, change_wait) if w}
                done, _ = await asyncio.wait(
                    waits,
                    timeout=keepalive_sec,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    yield b": keepalive\n\n"
                    if not await is_active():
                        return
                recheck = not done
                if frame_wait in done:
                    frame = frame_wait.result()
                    frame_wait = asyncio.ensure_future(stream.queue.get())
                    if frame is None:
                        return
                    if isinstance(frame, WorkspaceFrame):
                        partner = frame.state.partner
                    yield _sse(frame)
                if changes is not None and change_wait in done:
                    try:
                        changed = change_wait.result()
                    except StopAsyncIteration:
                        change_wait = None
                    else:
                        change_wait = asyncio.ensure_future(anext(changes))
                        yield _sse(ChangedFrame(id=changed))
                if nudge_wait in done:
                    stream.nudged.clear()
                    nudge_wait = asyncio.ensure_future(stream.nudged.wait())
                    recheck = True
                if recheck:
                    state = await read_state()
                    if state.partner != partner:
                        partner = state.partner
                        yield _sse(WorkspaceFrame(state=state))
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
