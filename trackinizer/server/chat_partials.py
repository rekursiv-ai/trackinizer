"""The answers science chats' assistants are writing, held in memory for their readers.

While a turn runs, the assistant puts the answer so far here, a few times a second,
and each tab that shows the chat holds a read open until it changes (see
:mod:`trackinizer.server.api.chat_partial_routes`). Nothing here reaches the
database, the change log or NOTIFY: the stored answer is the record, and replaces
the partial. One partial is held per chat session, for ``ttl_sec`` after its last
put, so a turn that crashed leaves nothing behind for long; past ``most`` partials,
the one put longest ago goes.

A partial's ``seq`` starts at a random number and rises by one with each put of its
chat, so a reader learns nothing of how often other chats, private or staff ones
among them, are written; a partial put after one went starts at a new number, which
no reader saw before.

The server runs single-process, as the inbound queue and the Chat hub already need,
and every caller is on its one event loop: no mutation awaits, so none needs a lock.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import asyncio
import contextlib
import secrets
import time

from trackinizer.wire.wire_science_chat import ChatPartial, ChatPartialState


if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID


__all__ = ["ChatPartials"]


# A browser reads ``seq`` as a double, which holds an integer exactly only below
# 2**53: past it, the ``after`` a reader sends back would differ from the seq it read,
# and each read would be answered at once.
def _first_seq() -> int:
    """Draw the seq of a chat's first partial since it held none."""
    return secrets.randbelow(2**52) + 1


class ChatPartials:
    """Each chat session's answer in writing, by session, the least recently put first.

    Args:
      ttl_sec: How long a partial is held after its last put.
      most: The most partials held; a put past it drops the one put longest ago.
      clock: The monotonic clock expiry is read on; tests inject theirs.
      first_seq: Draws the ``seq`` of a put to a chat that holds no partial; tests
        inject theirs.

    """

    def __init__(
        self,
        *,
        ttl_sec: float = 60.0,
        most: int = 256,
        clock: Callable[[], float] = time.monotonic,
        first_seq: Callable[[], int] = _first_seq,
    ) -> None:
        self._ttl_sec = ttl_sec
        self._most = most
        self._clock = clock
        self._first_seq = first_seq
        self._held: OrderedDict[UUID, _Held] = OrderedDict()
        self._waits: dict[UUID, _Waiting] = {}

    def put(self, session_id: UUID, partial: ChatPartial) -> int:
        """Hold ``partial`` as the session's, in place of any, and wake its readers.

        Args:
          session_id: The chat's session.
          partial: The answer so far.

        Returns:
          seq: The number that names it: one past the partial it replaces, or a new
            draw when the session held none.

        """
        self._sweep()
        held = self._held.get(session_id)
        seq = self._first_seq() if held is None else held.seq + 1
        self._held[session_id] = _Held(
            partial=partial,
            seq=seq,
            expires=self._clock() + self._ttl_sec,
        )
        self._held.move_to_end(session_id)
        self._wake(session_id)
        while len(self._held) > self._most:
            evicted, _ = self._held.popitem(last=False)
            self._wake(evicted)
        return seq

    def clear(self, session_id: UUID, *, turn: str) -> None:
        """Drop the session's partial if it is of ``turn``, and wake its readers.

        A turn's late delete leaves the next turn's partial held.

        Args:
          session_id: The chat's session.
          turn: The turn whose partial ends.

        """
        held = self._held.get(session_id)
        if held is not None and held.partial.turn == turn:
            del self._held[session_id]
            self._wake(session_id)

    def read(self, session_id: UUID) -> ChatPartialState:
        """Return the session's partial now; an expired one is none.

        Args:
          session_id: The chat's session.

        Returns:
          state: Its partial and seq, or ``seq`` 0 and no partial.

        """
        self._sweep()
        held = self._held.get(session_id)
        if held is None:
            return ChatPartialState(seq=0, partial=None)
        return ChatPartialState(seq=held.seq, partial=held.partial)

    async def wait(
        self,
        session_id: UUID,
        *,
        after: int,
        wait_sec: float,
    ) -> ChatPartialState:
        """Return the session's partial once its seq is not ``after``, or at a timeout.

        Registers before it compares, so a put between the two still wakes it. A
        partial held at ``after`` ends the wait when it expires.

        Args:
          session_id: The chat's session.
          after: The seq the caller last saw.
          wait_sec: The longest to wait for a change.

        Returns:
          state: The session's partial and seq; unchanged when nothing changed.

        """
        waiting = self._waits.get(session_id)
        if waiting is None:
            waiting = self._waits[session_id] = _Waiting()
        waiting.readers += 1
        try:
            state = self.read(session_id)
            held = self._held.get(session_id)
            hold = min(
                wait_sec,
                wait_sec if held is None else held.expires - self._clock(),
            )
            if state.seq != after or hold <= 0:
                return state
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(waiting.changed.wait(), hold)
        finally:
            waiting.readers -= 1
            if waiting.readers == 0 and self._waits.get(session_id) is waiting:
                del self._waits[session_id]
        return self.read(session_id)

    def _sweep(self) -> None:
        """Drop the partials past their expiry, the least recently put first."""
        now = self._clock()
        while self._held:
            session_id, held = next(iter(self._held.items()))
            if held.expires > now:
                return
            del self._held[session_id]
            self._wake(session_id)

    # Each change sets the event its waiters hold and takes it away, so a read that
    # registers after the change waits for the next one.
    def _wake(self, session_id: UUID) -> None:
        """Wake every read waiting on the session."""
        waiting = self._waits.pop(session_id, None)
        if waiting is not None:
            waiting.changed.set()


@dataclass(frozen=True, slots=True, kw_only=True)
class _Held:
    """One session's partial, its seq, and when it expires on the clock."""

    partial: ChatPartial
    seq: int
    expires: float


@dataclass(slots=True, kw_only=True)
class _Waiting:
    """The reads waiting on one session, and the event the next change sets."""

    changed: asyncio.Event = field(default_factory=asyncio.Event)
    readers: int = 0
