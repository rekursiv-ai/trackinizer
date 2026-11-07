from __future__ import annotations

import uuid

from trackinizer.wire.refs import SeqRef, UuidRef


def test_seq_ref_renders_kind_hash_seq() -> None:
    assert str(SeqRef(kind="Issue", seq=7)) == "Issue#7"


def test_uuid_ref_renders_the_uuid() -> None:
    target = uuid.uuid4()
    ref = UuidRef(uuid=target)
    assert str(ref) == str(target)
    assert ref.expected_kind is None
    assert UuidRef(uuid=target, expected_kind="Belief").expected_kind == "Belief"


def test_refs_are_frozen_values() -> None:
    ref = SeqRef(kind="Paper", seq=1)
    assert ref == SeqRef(kind="Paper", seq=1)
    # Only a frozen dataclass with ``eq`` is hashable, so refs can key a dict.
    assert {ref: 1}[SeqRef(kind="Paper", seq=1)] == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
