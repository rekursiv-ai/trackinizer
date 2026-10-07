"""Tests for the session IR wire bodies."""

from __future__ import annotations

from typing import Final
from uuid import uuid4

from trackinizer.lib.codec import immutable
from trackinizer.wire.wire_session_ir import ManifestBody, PartBody, RecordBody


_PLAIN: Final[dict[str, object]] = {"a": [1, {"b": [2]}], "c": {}}


def test_frozen_json_serializes_as_plain_json() -> None:
    """Stored rows hold frozen JSON; every wire body must still dump it."""
    frozen = immutable(_PLAIN)
    record = RecordBody(idx=0, kind="UserMessage", payload=frozen)
    manifest = ManifestBody(name="s.jsonl", metadata=frozen, ir_id=uuid4())
    part = PartBody(part=0, name="s.jsonl", format="claude", records=1, metadata=frozen)

    assert record.model_dump(mode="json")["payload"] == _PLAIN
    assert manifest.model_dump(mode="json")["metadata"] == _PLAIN
    assert part.model_dump(mode="json")["metadata"] == _PLAIN


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
