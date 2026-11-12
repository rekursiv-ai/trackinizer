"""Wire contract for a picture of a canvas, taken by its owner's open page.

An agent posts to :data:`CAPTURES_PATH` and is answered with a
:class:`CaptureImage`; the page that took the picture puts a
:class:`CaptureAnswer` to :data:`CAPTURE_PATH`. The server reads the page's
answer and the client reads the server's reply through these models, so both
refuse bytes that are not the picture they claim to be.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self
from uuid import UUID

import base64
import binascii

from pydantic import BaseModel, ConfigDict, Field, model_validator


__all__ = [
    "CAPTURES_PATH",
    "CAPTURE_PATH",
    "CaptureAnswer",
    "CaptureBox",
    "CaptureImage",
    "CaptureTile",
]

CAPTURES_PATH: Final = "/api/canvases/{canvas_id}/captures"
"""An agent asks for a picture here."""

CAPTURE_PATH: Final = "/api/canvases/{canvas_id}/captures/{capture_id}"
"""The owner's page answers request ``capture_id`` here."""

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

    @property
    def picture(self) -> bytes:
        """The picture's bytes, decoded from ``data``."""
        return base64.b64decode(self.data, validate=True)

    @model_validator(mode="after")
    def _is_a_picture(self) -> Self:
        """Refuse bytes that are not the picture they claim to be."""
        try:
            raw = self.picture
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
