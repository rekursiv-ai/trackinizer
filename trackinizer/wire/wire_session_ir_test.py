"""Tests for the session IR wire bodies."""

from __future__ import annotations

from typing import Final
from uuid import uuid4

from pydantic import ValidationError
from treekle import immutable

import pytest

from trackinizer.sagent.session.types import Thinking, UserMessage
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_session_ir import (
    AppendRecordsRequest,
    ManifestBody,
    PartBody,
    RecordBody,
    session_parts_path,
    session_records_path,
)


_PLAIN: Final[dict[str, object]] = {"a": [1, {"b": [2]}], "c": {}}
_RECORD: Final[dict[str, object]] = {"idx": 0, "kind": "UserMessage"}
_MANIFEST: Final[dict[str, object]] = {
    "name": "s.jsonl",
    "ir_id": "0b3c2f7e-1d4a-4f5e-9a1b-2c3d4e5f6a7b",
}


def test_frozen_json_serializes_as_plain_json() -> None:
    """Stored rows hold frozen JSON; every wire body must still dump it."""
    frozen = immutable(_PLAIN)
    record = RecordBody(idx=0, kind="UserMessage", payload=frozen)
    manifest = ManifestBody(name="s.jsonl", metadata=frozen, ir_id=uuid4())
    part = PartBody(part=0, name="s.jsonl", format="claude", records=1, metadata=frozen)

    assert record.model_dump(mode="json")["payload"] == _PLAIN
    assert manifest.model_dump(mode="json")["metadata"] == _PLAIN
    assert part.model_dump(mode="json")["metadata"] == _PLAIN


@pytest.mark.parametrize(
    ("record", "kind"),
    [
        (UserMessage(content="hello"), "UserMessage"),
        (
            Thinking(
                context_id=3,
                timestamp="2026-11-07T12:00:00Z",
                content="reasoning",
                encrypted="c2VhbGVk",
            ),
            "Thinking",
        ),
    ],
)
def test_a_stored_row_crosses_the_wire_and_back_unchanged(
    record: UserMessage | Thinking,
    kind: str,
) -> None:
    session_id = uuid4()
    row = SessionRecordRow.of(
        session_id=session_id,
        part=2,
        idx=5,
        record=record,
        model="acme/reasoner",
    )

    body = RecordBody.model_validate_json(RecordBody.of(row).model_dump_json())

    assert (body.idx, body.kind, body.text) == (5, kind, row.text)
    assert body.row(session_id, 2) == row


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"records": [_RECORD]}, "records require 'name' and 'manifest'"),
        ({"name": "s.jsonl", "records": [_RECORD]}, "records require"),
        ({"name": "s.jsonl"}, "given together or not at all"),
        ({"manifest": _MANIFEST}, "given together or not at all"),
    ],
    ids=["records-alone", "no-manifest", "name-alone", "manifest-alone"],
)
def test_records_need_a_named_file_and_its_manifest(
    body: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        AppendRecordsRequest.model_validate(body)


def test_a_slash_command_alone_names_no_file() -> None:
    request = AppendRecordsRequest.model_validate(
        {"slash_commands": [{"timestamp": "2026-11-06T12:00:00Z", "command": "exit"}]},
    )
    named = AppendRecordsRequest.model_validate(
        {"name": "s.jsonl", "manifest": _MANIFEST, "records": [_RECORD]},
    )

    assert (request.name, request.manifest, len(request.slash_commands)) == (
        "",
        None,
        1,
    )
    assert named.manifest is not None
    assert named.manifest.name == "s.jsonl"


def test_a_sessions_records_and_parts_have_paths_of_its_own() -> None:
    session_id = uuid4()

    assert session_records_path(session_id) == f"/api/sessions/{session_id}/records"
    assert session_parts_path(session_id) == f"/api/sessions/{session_id}/parts"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
