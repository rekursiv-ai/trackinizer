from __future__ import annotations

from typing import cast, get_args

import pytest

from trackinizer.sagent.session.types import SessionRecord
from trackinizer.types.streams import Stderr, Stdin, Stdout, TraxRecord


@pytest.mark.parametrize("stream", [Stdin, Stdout, Stderr])
def test_each_stream_carries_text_and_optional_context(
    stream: type[Stdin | Stdout | Stderr],
) -> None:
    line = stream(text="hi\n")
    assert line.text == "hi\n"
    assert line.context_id is None
    assert line.timestamp is None
    # Only a frozen dataclass with ``eq`` is hashable.
    assert hash(line) == hash(stream(text="hi\n"))


def test_streams_are_distinct_records() -> None:
    assert Stdout(text="x") != Stderr(text="x")
    assert Stdin(text="x", context_id=1) == Stdin(text="x", context_id=1)


def test_trax_record_widens_the_shared_ir() -> None:
    members = get_args(cast(object, TraxRecord.__value__))
    assert members[1:] == (Stdin, Stdout, Stderr)
    assert members[0] == SessionRecord


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
