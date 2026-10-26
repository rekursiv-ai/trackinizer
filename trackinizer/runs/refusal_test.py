"""Tests for standing in for the records the server refuses."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.runs.refusal import (
    REFUSED_KIND,
    is_refusal,
    send_isolating,
    stand_in,
)
from trackinizer.sagent.session.types import UncategorizedRecord, UserMessage
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_session_ir import RecordBody


class _Server:
    """Stores what it is sent; refuses every batch that holds a ``refuse`` text."""

    def __init__(self, *, refuse: str = "\x00", status: int = 422) -> None:
        self.refuse = refuse
        self.status = status
        self.stored: list[RecordBody] = []
        self.requests = 0

    def send(self, records: list[RecordBody]) -> None:
        self.requests += 1
        if any(self.refuse in record.text for record in records):
            raise ClientError("refused", status_code=self.status)
        self.stored.extend(records)


def _body(idx: int, text: str) -> RecordBody:
    row = SessionRecordRow.of(
        session_id=UUID(int=0),
        part=0,
        idx=idx,
        record=UserMessage(content=text),
    )
    return RecordBody.of(row)


@pytest.mark.parametrize("status", [400, 413, 422])
def test_a_refusal_of_the_content_or_its_size_is_a_refusal(status: int) -> None:
    assert is_refusal(ClientError("x", status_code=status))


@pytest.mark.parametrize("status", [404, 408, 409, 429, 500, 503])
def test_no_other_answer_is_a_refusal(status: int) -> None:
    assert not is_refusal(ClientError("x", status_code=status))


def test_an_error_without_a_status_is_no_refusal() -> None:
    assert not is_refusal(ClientError("connection refused"))
    assert not is_refusal(ConnectionError("down"))


def test_a_batch_the_server_takes_is_sent_once() -> None:
    server = _Server()
    records = [_body(idx, f"turn {idx}") for idx in range(4)]

    refused = send_isolating(server.send, records=records)

    assert refused == []
    assert server.requests == 1
    assert server.stored == records


def test_each_refused_record_is_stood_in_at_its_place_and_the_rest_are_sent() -> None:
    server = _Server()
    records = [
        _body(idx, "a\x00b" if idx in {2, 5} else f"turn {idx}") for idx in range(8)
    ]

    refused = send_isolating(server.send, records=records)

    assert [record.idx for record in refused] == [2, 5]
    assert sorted(record.idx for record in server.stored) == list(range(8))
    sent = {record.idx: record for record in server.stored}
    assert all(sent[idx] == records[idx] for idx in (0, 1, 3, 4, 6, 7))
    assert {sent[2].kind, sent[5].kind} == {"UncategorizedRecord"}


@pytest.mark.parametrize("status", [409, 503])
def test_a_failure_that_is_no_refusal_reaches_the_caller(status: int) -> None:
    server = _Server(status=status)

    with pytest.raises(ClientError):
        _ = send_isolating(
            server.send,
            records=[_body(0, "turn 0"), _body(1, "a\x00b")],
        )

    assert server.requests == 1
    assert server.stored == []


def test_the_stand_in_keeps_the_place_and_says_what_was_refused_and_why() -> None:
    when = datetime(2026, 10, 13, 6, 47, tzinfo=UTC)
    body = _body(3, "a\x00b").model_copy(
        update={"context_id": 1, "timestamp": when, "model": "m"},
    )

    standing = stand_in(body, reason="POST /records -> 422")
    record = standing.row(UUID(int=0), 0).record()

    assert (standing.idx, standing.context_id, standing.timestamp, standing.model) == (
        3,
        1,
        when,
        "m",
    )
    assert isinstance(record, UncategorizedRecord)
    assert record.kind == REFUSED_KIND
    assert record.payload == {
        "refused_kind": "UserMessage",
        "reason": "POST /records -> 422",
    }
    assert "\x00" not in standing.text


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
