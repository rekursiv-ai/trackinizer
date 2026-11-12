"""Hand a picture of the canvas from the open page to the agent that asked for it.

An agent cannot see pixels, and the server holds none: the page draws the canvas, so
the page takes the picture. The agent's request waits here, in memory like the event
hub, until the first open page of the canvas's owner answers. The picture is passed
on and never stored. The server runs single-process, as the hub already requires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import asyncio

from trackinizer.wire.wire_capture import CaptureAnswer


if TYPE_CHECKING:
    from collections.abc import Callable


__all__ = ["MAX_ANSWER_BYTES", "CaptureRelay"]


MAX_ANSWER_BYTES: Final = 2_200_000
"""The most a page may send: a 1.5 MB picture as base64 and its few notes."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CaptureRelay:
    """Requests waiting for a page, by request id.

    Args:
      timeout_sec: How long a request waits for any page to answer.

    """

    timeout_sec: float = 8.0

    _waiting: dict[UUID, tuple[UUID, asyncio.Future[CaptureAnswer]]] = field(
        default_factory=dict,
    )

    async def ask(
        self,
        canvas_id: UUID,
        *,
        publish: Callable[[UUID], None],
    ) -> CaptureAnswer:
        """Send a request to the canvas's open pages and wait for the first answer.

        Args:
          canvas_id: Canvas whose pages are asked.
          publish: Tells the pages the request id.

        Returns:
          answer: What the first page sent.

        Raises:
          TimeoutError: No page answered in time.

        """
        request = uuid4()
        answered = asyncio.Future[CaptureAnswer]()
        self._waiting[request] = (canvas_id, answered)
        try:
            publish(request)
            return await asyncio.wait_for(answered, self.timeout_sec)
        finally:
            del self._waiting[request]

    def answer(
        self,
        canvas_id: UUID,
        *,
        request: UUID,
        answer: CaptureAnswer,
    ) -> bool:
        """Give a request its answer; only the first for a request is taken.

        Args:
          canvas_id: Canvas the answering page belongs to.
          request: The id the page was sent.
          answer: What the page sent.

        Returns:
          taken: False when the request is unknown, finished, or another canvas's.

        """
        waiting = self._waiting.get(request)
        if waiting is None or waiting[0] != canvas_id or waiting[1].done():
            return False
        waiting[1].set_result(answer)
        return True
