"""A file's records as record bodies, sent in batches of one part."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.client.session_upload import (
    append_batch,
    record_bodies,
    record_body,
    send_batches,
)
from trackinizer.runs.redact import Redactor
from trackinizer.sagent.session.types import (
    AssistantMessage,
    IncompleteRecord,
    Thinking,
    UserMessage,
)
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody


if TYPE_CHECKING:
    from collections.abc import Sequence


_MASKED = "hunter2-hunter2"


def _same(body: RecordBody) -> RecordBody:
    return body


@dataclass(slots=True, kw_only=True)
class _Appender:
    """Takes each append as the server would, refusing what it is told to."""

    appended: list[tuple[UUID, str, ManifestBody, list[RecordBody]]] = field(
        default_factory=list[tuple[UUID, str, ManifestBody, list[RecordBody]]],
    )
    refusing: frozenset[int] = frozenset()
    """The positions of the records the server cannot store."""

    down: bool = False

    def append_records(
        self,
        session_id: UUID,
        *,
        name: str,
        manifest: ManifestBody,
        records: Sequence[RecordBody],
    ) -> object:
        if self.down:
            raise ClientError("server unreachable")
        if any(
            r.idx in self.refusing and r.kind != "UncategorizedRecord" for r in records
        ):
            raise ClientError("cannot store a NUL byte", status_code=422)
        self.appended.append((session_id, name, manifest, list(records)))
        return None


def test_each_record_is_its_body_at_its_position_in_the_file() -> None:
    records = [
        UserMessage(content="hi"),
        AssistantMessage(content="hello"),
        IncompleteRecord(text='{"type": "us'),
    ]

    bodies = record_bodies(records, start=7, redactor=None, bounded=_same)

    assert [(b.idx, b.kind, b.text) for b in bodies] == [
        (7, "UserMessage", "hi"),
        (8, "AssistantMessage", "hello"),
        (9, IncompleteRecord.__name__, '{"type": "us'),
    ]


def test_a_secret_is_masked_before_the_body_is_bounded() -> None:
    seen: list[RecordBody] = []

    def bounded(body: RecordBody) -> RecordBody:
        seen.append(body)
        return body.model_copy(update={"text": "cut"})

    bodies = record_bodies(
        [UserMessage(content=f"the key is {_MASKED}")],
        start=0,
        redactor=Redactor({"KEY": _MASKED}),
        bounded=bounded,
    )

    assert _MASKED not in seen[0].model_dump_json()
    assert "[redacted:KEY]" in seen[0].text
    assert [b.text for b in bodies] == ["cut"]


def test_a_sealed_thought_carries_its_ciphertext_beside_the_masked_payload() -> None:
    body = record_body(
        Thinking(content=f"recall {_MASKED}", encrypted="c2VhbGVk"),
        idx=3,
        redactor=Redactor({"KEY": _MASKED}),
    )

    assert (body.idx, body.ciphertext) == (3, "c2VhbGVk")
    assert _MASKED not in body.model_dump_json()


def test_a_batch_states_its_part_and_how_far_the_part_reaches() -> None:
    client = _Appender()
    session, ir_id = UUID(int=1), UUID(int=2)
    batch = [
        RecordBody(idx=4, kind="UserMessage"),
        RecordBody(idx=5, kind="UserMessage"),
    ]

    append_batch(
        client,
        session=session,
        name="part.jsonl",
        ir_id=ir_id,
        encoding={"newline_terminated": True},
        batch=batch,
        format="sagent",
    )

    assert client.appended == [
        (
            session,
            "part.jsonl",
            ManifestBody(
                name="part.jsonl",
                metadata={"newline_terminated": True},
                ir_id=ir_id,
                format="sagent",
                records=6,
            ),
            batch,
        ),
    ]


def _send(client: _Appender, batch: list[RecordBody]) -> None:
    append_batch(
        client,
        session=UUID(int=1),
        name="part.jsonl",
        ir_id=UUID(int=2),
        encoding={},
        batch=batch,
        format="sagent",
    )


def test_records_go_in_batches_each_taken_off_once_sent() -> None:
    client = _Appender()
    pending = [RecordBody(idx=i, kind="UserMessage") for i in range(5)]

    refused = send_batches(pending, send=partial(_send, client), batch_size=2)

    assert refused == []
    assert pending == []
    assert [[r.idx for r in sent] for _, _, _, sent in client.appended] == [
        [0, 1],
        [2, 3],
        [4],
    ]


def test_a_refused_record_goes_as_its_stand_in_and_is_named() -> None:
    client = _Appender(refusing=frozenset({1}))
    pending = [RecordBody(idx=i, kind="UserMessage") for i in range(3)]

    refused = send_batches(pending, send=partial(_send, client), batch_size=3)

    assert [r.idx for r in refused] == [1]
    sent = [r for _, _, _, batch in client.appended for r in batch]
    assert sorted((r.idx, r.kind) for r in sent) == [
        (0, "UserMessage"),
        (1, "UncategorizedRecord"),
        (2, "UserMessage"),
    ]


def test_an_outage_leaves_the_batch_it_failed_on_and_every_later_one_pending() -> None:
    client = _Appender()
    pending = [RecordBody(idx=i, kind="UserMessage") for i in range(4)]
    sends = 0

    def send(batch: list[RecordBody]) -> None:
        nonlocal sends
        sends += 1
        client.down = sends > 1
        _send(client, batch)

    with pytest.raises(ClientError):
        _ = send_batches(pending, send=send, batch_size=2)

    assert [r.idx for r in pending] == [2, 3]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
