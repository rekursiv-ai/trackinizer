"""Tests for records named by id or by kind and number."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import uuid

import pytest

from trackinizer.conftest import RecordingPolicy, make_conn
from trackinizer.server.access_policy import Principal, inquiry_read_filter
from trackinizer.server.store.named import parse_named, read_named
from trackinizer.wire.refs import SeqRef


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


_ID = uuid.UUID("889ffcb2-cf44-43e7-9806-eb08428c6203")


@pytest.mark.parametrize(
    ("text", "named"),
    [
        (str(_ID), _ID),
        (str(_ID).upper(), _ID),
        ("issue/11", SeqRef(kind="Issue", seq=11)),
        ("Belief/3", SeqRef(kind="Belief", seq=3)),
    ],
)
def test_an_id_or_a_kind_and_number_names_a_record(
    text: str,
    named: uuid.UUID | SeqRef,
) -> None:
    assert parse_named(text) == named


@pytest.mark.parametrize(
    "text",
    ["", "Issue", "Issue/", "Issue/11/12", "Nothing/11", "Issue/-1", "not-a-uuid"],
)
def test_anything_else_names_none(text: str) -> None:
    assert parse_named(text) is None


@pytest.mark.asyncio
async def test_nothing_wanted_reads_nothing() -> None:
    conn = make_conn()
    assert await read_named(cast("Conn", conn), wanted=[]) == {}
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [False, True])
async def test_a_record_read_is_found_by_its_id_and_its_number_through_access(
    monkeypatch: pytest.MonkeyPatch,
    restricted: bool,
) -> None:
    conn = make_conn()
    conn.fetch.return_value = [
        {"id": _ID, "kind": "Issue", "seq": 12, "title": "x" * 513},
    ]
    monkeypatch.setattr(
        RecordingPolicy,
        "read_filter",
        Mock(return_value="FALSE" if restricted else "TRUE"),
    )

    reader = Principal(email="ada@example.com", role="writer")

    found = await read_named(
        cast("Conn", conn),
        wanted=[_ID, SeqRef(kind="Issue", seq=12)],
        policy=RecordingPolicy(),
        principal=reader,
    )

    assert found[_ID] is found[SeqRef(kind="Issue", seq=12)]
    assert found[_ID].title == "x" * 512
    sent = conn.fetch.await_args
    assert sent is not None
    assert sent.args[1:4] == ([_ID], ["Issue"], [12])
    gate = inquiry_read_filter(
        "inquiries.id",
        policy=RecordingPolicy(),
        principal=reader,
        params=[[_ID], ["Issue"], [12]],
    )
    assert ("(FALSE)" in gate) is restricted
    if gate != "TRUE":
        assert str(sent.args[0]).endswith(f" AND ({gate})")


@pytest.mark.asyncio
@pytest.mark.parametrize("gated", [False, True])
async def test_the_records_are_read_in_one_query_gated_by_the_readers_access(
    monkeypatch: pytest.MonkeyPatch,
    gated: bool,
) -> None:
    conn = make_conn()
    conn.fetch.return_value = []
    # A gate of its own, so every build has one to send: the export has no chat filing.
    monkeypatch.setattr(RecordingPolicy, "read_filter", Mock(return_value="FALSE"))
    policy = RecordingPolicy() if gated else None
    reader = Principal(email="ada@example.com", role="writer")

    await read_named(
        cast("Conn", conn),
        wanted=[_ID, SeqRef(kind="Issue", seq=12)],
        policy=policy,
        principal=reader,
    )

    sent = conn.fetch.await_args
    assert sent is not None
    gate = inquiry_read_filter(
        "inquiries.id",
        policy=policy,
        principal=reader,
        params=[[_ID], ["Issue"], [12]],
    )
    start = (
        "SELECT id, kind, seq, title FROM inquiries "
        "WHERE (id = ANY($1::uuid[]) "
        "OR (kind, seq) IN (SELECT * FROM unnest($2::text[], $3::bigint[])))"
    )
    readable = f" AND ({gate})" if gated else ""
    assert str(sent.args[0]) == start + readable
    if gated:
        assert "readable_inquiry.id = inquiries.id AND (" in gate


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
