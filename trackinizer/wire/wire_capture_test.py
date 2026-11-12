"""Tests for the capture wire models: a picture is what it claims, with its notes."""

from __future__ import annotations

from typing import Final

import base64
import uuid

from pydantic import ValidationError

import pytest

from trackinizer.wire.wire_capture import (
    CaptureAnswer,
    CaptureBox,
    CaptureImage,
    CaptureTile,
)


_CANVAS: Final = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PNG_BYTES: Final = b"\x89PNG\r\n\x1a\n-"
_PNG: Final = base64.b64encode(_PNG_BYTES).decode()
_JPEG: Final = base64.b64encode(b"\xff\xd8\xff-").decode()


def _image(*, data: str = _PNG, mime: str = "image/png") -> dict[str, object]:
    return {"width": 40, "height": 30, "mime": mime, "data": data}


def test_an_answer_is_a_picture_or_a_reason_and_never_both_or_neither() -> None:
    image = CaptureImage.model_validate(_image())

    assert CaptureAnswer(image=image).error is None
    for both_or_neither in (dict[str, object](), {"image": _image(), "error": "x"}):
        with pytest.raises(ValidationError):
            CaptureAnswer.model_validate(both_or_neither)


@pytest.mark.parametrize(
    "bad",
    [
        _image(data="not base64!"),
        _image(data=_JPEG),
        _image(data=_PNG, mime="image/jpeg"),
        _image(mime="image/gif"),
        {**_image(), "width": 0},
    ],
)
def test_bytes_that_are_not_the_picture_claimed_are_refused(
    bad: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CaptureImage.model_validate(bad)


_TILE_BODY = {"left": 0.1, "top": 0.2, "right": 0.6, "bottom": 0.9}


@pytest.mark.parametrize(
    "bad",
    [{**_TILE_BODY, "left": 1.5}, {**_TILE_BODY, "top": -0.1}, {**_TILE_BODY, "x": 1}],
)
def test_a_tile_is_an_id_and_edges_that_are_fractions(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CaptureTile.model_validate({"id": str(_CANVAS), **bad})


def test_an_image_keeps_its_tiles_in_order_and_at_most_fifty() -> None:
    tiles = [{"id": str(uuid.uuid4()), **_TILE_BODY} for _ in range(50)]
    image = CaptureImage.model_validate({**_image(), "tiles": tiles})
    assert [str(t.id) for t in image.tiles] == [t["id"] for t in tiles]
    assert CaptureImage.model_validate(_image()).tiles == []
    with pytest.raises(ValidationError):
        CaptureImage.model_validate({**_image(), "tiles": [*tiles, tiles[0]]})


def test_an_image_hands_back_its_picture_as_bytes() -> None:
    assert CaptureImage.model_validate(_image()).picture == _PNG_BYTES
    jpeg = CaptureImage.model_validate(_image(data=_JPEG, mime="image/jpeg"))
    assert jpeg.picture == b"\xff\xd8\xff-"


@pytest.mark.parametrize("author", ["person", "agent"])
def test_a_box_is_drawn_by_the_person_or_the_agent(author: str) -> None:
    box = CaptureBox.model_validate({"id": str(_CANVAS), "author": author})
    assert (box.id, box.author) == (_CANVAS, author)


@pytest.mark.parametrize(
    "bad",
    [{"author": "someone"}, {"author": "person", "x": 1}, {"id": "b1"}],
)
def test_a_box_names_its_author_and_nothing_else(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CaptureBox.model_validate({"id": str(_CANVAS), "author": "person", **bad})


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
