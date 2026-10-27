"""Hand a page's ask for upload links to the assistant, and its answer back.

The assistant signs the links, since only it reaches the workspace boxes; the server
only knows who may post. A page's ask waits here, in memory like the capture relay,
until the assistant's held wait takes it and its answer arrives. Nothing is stored,
and the server runs single-process, as the event hub already requires. An ask no
assistant answers in time ends the page's wait, and one a page leaves goes with it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import asyncio
import time

from trackinizer.wire.wire_chat_uploads import UploadAnswer, UploadAsk


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.wire.wire_chat_uploads import ChatUploadFile


__all__ = ["UploadRelay", "UploadsBusyError"]


class UploadsBusyError(Exception):
    """As many asks as the relay holds are already waiting for the assistant."""


@dataclass(slots=True, kw_only=True)
class UploadRelay:
    """Asks waiting for the assistant, oldest first, and the answers they wait for.

    Args:
      timeout_sec: How long a page waits for the assistant to take and answer an ask.
      max_waiting: The most asks held at once; each holds a request open, so a page
        asking past it is refused at once rather than queued.

    """

    timeout_sec: float = 15.0
    max_waiting: int = 64

    _pending: deque[UploadAsk] = field(default_factory=deque)
    _answers: dict[UUID, asyncio.Future[UploadAnswer]] = field(default_factory=dict)
    _arrived: asyncio.Event = field(default_factory=asyncio.Event)

    async def ask(
        self,
        *,
        canvas_id: UUID,
        conversation_id: UUID | None,
        workspace_id: UUID,
        asker: str,
        asker_role: str,
        files: Sequence[ChatUploadFile],
    ) -> UploadAnswer:
        """Queue an ask for the assistant and wait for its answer.

        Args:
          canvas_id: The asker's canvas.
          conversation_id: The chat, once it has begun.
          workspace_id: The workspace that names the asker's box.
          asker: The asker's attested email.
          asker_role: The asker's attested role.
          files: The files asked for.

        Returns:
          answer: The assistant's slots, or its reason for none.

        Raises:
          TimeoutError: The assistant did not take and answer it in time.
          UploadsBusyError: ``max_waiting`` asks are already waiting.

        """
        if len(self._answers) >= self.max_waiting:
            raise UploadsBusyError
        ask = UploadAsk(
            request_id=uuid4(),
            canvas_id=canvas_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            asker=asker,
            asker_role=asker_role,
            files=list(files),
        )
        answered = asyncio.Future[UploadAnswer]()
        self._answers[ask.request_id] = answered
        self._pending.append(ask)
        self._arrived.set()
        try:
            return await asyncio.wait_for(answered, self.timeout_sec)
        finally:
            del self._answers[ask.request_id]
            if ask in self._pending:
                self._pending.remove(ask)

    async def wait(self, *, wait_sec: float) -> UploadAsk | None:
        """Take the oldest ask, waiting up to ``wait_sec`` for one to arrive.

        Args:
          wait_sec: The longest to wait.

        Returns:
          ask: The ask, taken from the queue; ``None`` when none came in time.

        """
        deadline = time.monotonic() + wait_sec
        while not self._pending:
            self._arrived.clear()
            try:
                await asyncio.wait_for(
                    self._arrived.wait(),
                    max(deadline - time.monotonic(), 0),
                )
            except TimeoutError:
                return None
        return self._pending.popleft()

    def answer(self, request_id: UUID, answer: UploadAnswer) -> bool:
        """Give an ask its answer; only the first for an ask still waiting is taken.

        Args:
          request_id: The ask's id, as the assistant was handed it.
          answer: The assistant's answer.

        Returns:
          taken: False when the ask is unknown, answered, or its page gave up.

        """
        answered = self._answers.get(request_id)
        if answered is None or answered.done():
            return False
        answered.set_result(answer)
        return True
