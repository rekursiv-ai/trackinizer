"""A session record stores through the codec, with its ``JSON`` fields plain."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from trackinizer.lib.agent.types.sessions import (
    Attachment,
    ShellCommandResult,
    UserMessage,
)
from trackinizer.lib.agent.types.stored import from_stored, to_stored
from trackinizer.lib.codec import ReadError, immutable


_MESSAGE = UserMessage(
    content="hi",
    attachments=(Attachment(mime_descriptor="image/png", data=b"image"),),
    extra=immutable({"z": [1, {"k": "v"}], "a": None}),
)


def test_tuples_and_bytes_are_tagged() -> None:
    """The codec's tags carry what plain JSON cannot say."""
    stored = to_stored(_MESSAGE)

    assert stored["attachments"] == {
        "py/tuple": [
            {
                "py/object": "trackinizer.lib.agent.types.sessions.Attachment",
                "mime_descriptor": "image/png",
                "data": {"py/b64": "aW1hZ2U="},
            },
        ],
    }


def test_json_fields_store_as_the_plain_objects_the_provider_wrote() -> None:
    """``extra`` is provider JSON: plain, key order kept, no codec wrapper."""
    extra = to_stored(_MESSAGE)["extra"]

    assert isinstance(extra, dict)
    assert extra == {"z": [1, {"k": "v"}], "a": None}
    assert list(extra) == ["z", "a"]


def test_a_stored_record_reads_back_equal_with_json_fields_frozen() -> None:
    """Reading freezes each ``JSON`` field as the provider readers build it."""
    record = from_stored(to_stored(_MESSAGE), UserMessage)

    assert record == _MESSAGE
    assert isinstance(record.extra, MappingProxyType)
    assert record.extra["z"] == (1, MappingProxyType({"k": "v"}))


def test_a_record_stored_before_the_codec_reads_back_equal() -> None:
    """The plain shape, a tuple as an array and bytes as base64, still reads."""
    plain = {
        "py/object": "trackinizer.lib.agent.types.sessions.ShellCommandResult",
        "context_id": None,
        "timestamp": None,
        "call_id": "c1",
        "extra": {"z": [1]},
        "command": ["ls", "-la"],
        "stdout": "ok",
        "stderr": "",
        "exit_code": 0,
    }

    record = from_stored(plain, ShellCommandResult)

    assert record == ShellCommandResult(
        call_id="c1",
        extra=immutable({"z": [1]}),
        command=("ls", "-la"),
        stdout="ok",
        exit_code=0,
    )


def test_data_that_is_not_the_target_raises() -> None:
    """A payload of another record type is refused, not coerced."""
    with pytest.raises(ReadError, match="unknown field"):
        _ = from_stored(to_stored(_MESSAGE), ShellCommandResult)


def test_a_value_that_is_not_a_record_passes_through_each_way() -> None:
    """Only records' ``JSON`` fields change; any other value is the codec's."""
    assert to_stored((1, "a")) == {"py/tuple": [1, "a"]}
    assert from_stored({"py/tuple": [1, "a"]}, tuple[int, str]) == (1, "a")


def test_a_value_that_is_no_json_object_cannot_be_stored() -> None:
    """A payload column holds an object; a bare scalar is refused."""
    with pytest.raises(TypeError, match="int does not store as a JSON object"):
        _ = to_stored(5)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
