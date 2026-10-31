"""Hand a picture of the canvas from the open page to the agent that asked for it.

An agent cannot see pixels, and the server holds none: the page draws the canvas, so
the page takes the picture. The agent's request waits here, in memory like the event
hub, until the first open page of the canvas's owner answers. The picture is passed
on and never stored. The server runs single-process, as the hub already requires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self
from uuid import UUID, uuid4

import asyncio
import base64
import binascii

from pydantic import BaseModel, ConfigDict, Field, model_validator


if TYPE_CHECKING:
    from collections.abc import Callable


__all__ = [
    "MAX_ANSWER_BYTES",
    "CaptureAnswer",
    "CaptureBox",
    "CaptureImage",
    "CaptureRelay",
    "CaptureTile",
]


MAX_ANSWER_BYTES: Final = 2_200_000
"""The most a page may send: a 1.5 MB picture as base64 and its few notes."""

_PNG: Final = b"\x89PNG\r\n\x1a\n"
_JPEG: Final = b"\xff\xd8\xff"


class CaptureBox(BaseModel):
    """A red box that stood on the canvas when the picture was taken."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    author: Literal["person", "agent"]


class CaptureTile(BaseModel):
    """A visual on the stage, with where its body stood in the picture."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    left: Annotated[float, Field(ge=0, le=1)]
    top: Annotated[float, Field(ge=0, le=1)]
    right: Annotated[float, Field(ge=0, le=1)]
    bottom: Annotated[float, Field(ge=0, le=1)]
    """The body's edges, as fractions of the picture's width and height."""


class CaptureImage(BaseModel):
    """The canvas as the person sees it."""

    model_config = ConfigDict(extra="forbid")

    width: Annotated[int, Field(ge=1, le=4096)]
    height: Annotated[int, Field(ge=1, le=4096)]
    mime: Literal["image/png", "image/jpeg"]
    data: str
    """The picture, base64."""

    not_captured: Annotated[
        list[Annotated[str, Field(max_length=80)]],
        Field(max_length=20),
    ] = Field(default_factory=list)
    """Names of visuals that stayed blank."""

    boxes: Annotated[list[CaptureBox], Field(max_length=50)] = Field(
        default_factory=list,
    )

    tiles: Annotated[list[CaptureTile], Field(max_length=50)] = Field(
        default_factory=list,
    )
    """Every visual on the stage in paint order, the topmost last."""

    @model_validator(mode="after")
    def _is_a_picture(self) -> Self:
        """Refuse bytes that are not the picture they claim to be."""
        try:
            raw = base64.b64decode(self.data, validate=True)
        except binascii.Error as error:
            raise ValueError("data is not base64") from error
        if not raw.startswith(_PNG if self.mime == "image/png" else _JPEG):
            raise ValueError(f"data is not a {self.mime} picture")
        return self


class CaptureAnswer(BaseModel):
    """A page's reply: the picture, or why it has none."""

    model_config = ConfigDict(extra="forbid")

    image: CaptureImage | None = None
    error: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        """Require exactly one of the picture and the reason."""
        if (self.image is None) == (self.error is None):
            raise ValueError("send an image or an error, not both or neither")
        return self


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
