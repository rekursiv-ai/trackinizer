"""The chat upload wire contract: what an ask, its grant and a line's files may carry."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from trackinizer.wire.wire_chat_uploads import (
    ChatUploadAsk,
    ChatUploadGrant,
    UploadAnswer,
    box_path,
)


_CANVAS = uuid.UUID(int=1)
_SLOT = {
    "name": "shot.png",
    "path": "/home/user/uploads/shot.png",
    "url": "https://49983-sbx.e2b.app/files?path=%2Fhome%2Fuser%2Fuploads%2Fshot.png",
}


def test_an_ask_names_the_canvas_and_each_file_by_name_and_size() -> None:
    ask = ChatUploadAsk.model_validate(
        {
            "canvas_id": str(_CANVAS),
            "files": [{"name": "shot.png", "size": 0}, {"name": "data.zip", "size": 9}],
        },
    )

    assert ask.conversation_id is None
    assert [(each.name, each.size) for each in ask.files] == [
        ("shot.png", 0),
        ("data.zip", 9),
    ]


@pytest.mark.parametrize(
    "files",
    [
        [],
        [{"name": "a", "size": 1}] * 21,
        [{"name": "a.txt", "size": 1}, {"name": "a.txt", "size": 2}],
        [{"name": "", "size": 1}],
        [{"name": "x" * 256, "size": 1}],
        [{"name": "dir/a.txt", "size": 1}],
        [{"name": "dir\\a.txt", "size": 1}],
        [{"name": ".", "size": 1}],
        [{"name": "..", "size": 1}],
        [{"name": "a\x00b", "size": 1}],
        [{"name": "a\nb", "size": 1}],
        [{"name": "a.txt", "size": -1}],
    ],
)
def test_an_ask_refuses_no_files_too_many_a_repeat_and_a_name_that_is_no_name(
    files: list[dict[str, object]],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        ChatUploadAsk.model_validate({"canvas_id": str(_CANVAS), "files": files})


def test_an_answer_is_slots_or_a_reason_never_both_or_neither() -> None:
    assert UploadAnswer.model_validate({"files": [_SLOT]}).files is not None
    assert UploadAnswer.model_validate({"error": "no box"}).error == "no box"

    refused: tuple[dict[str, object], ...] = ({}, {"files": [_SLOT], "error": "no box"})
    for neither_or_both in refused:
        with pytest.raises(pydantic.ValidationError):
            UploadAnswer.model_validate(neither_or_both)


@pytest.mark.parametrize(
    "change",
    [
        {"path": "home/user/a"},
        {"path": "/home/user/../.ssh/id"},
        {"url": "http://49983-sbx.e2b.app/files"},
        {"url": "javascript:alert(1)"},
    ],
)
def test_a_slot_is_an_absolute_box_path_and_an_https_link(
    change: dict[str, object],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        ChatUploadGrant.model_validate({"files": [_SLOT | change]})


@pytest.mark.parametrize(
    ("path", "kept"),
    [
        ("/home/user/uploads/a.png", True),
        ("/home/user/uploads/My File (1).md", True),
        ("home/user/a.png", False),
        ("/home/user/../a", False),
        ("/home/user/./a", False),
        ("/home/user//a", False),
        ("/home/user/a\x00", False),
        ("/home/user/a\nb", False),
        ("/" + "a" * 512, False),
    ],
)
def test_a_box_path_is_absolute_normal_and_one_line(path: str, kept: bool) -> None:
    if kept:
        assert box_path(path) == path
    else:
        with pytest.raises(ValueError, match="box path"):
            box_path(path)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
