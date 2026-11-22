"""Tests for the helpers every API route module shares."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import uuid

from fastapi import HTTPException

import pytest

from trackinizer.conftest import FakeEngine, make_conn
from trackinizer.server.api._routes_shared import (
    idempotency_key,
    iso_format,
    parse_fields,
    parse_seq_ranges,
    refusal,
    request_engine,
    require_browser,
    require_instance,
)
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.wire.seq_ranges import SeqRange


if TYPE_CHECKING:
    from fastapi import Request


def test_a_refusal_carries_its_code_and_message() -> None:
    refused = refusal(409, code="tools_not_enabled", message="it is off")

    assert (refused.status_code, refused.detail) == (
        409,
        {"code": "tools_not_enabled", "message": "it is off"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [True, False, None])
async def test_daemon_instance_requires_an_explicit_match(current: bool | None) -> None:
    conn = make_conn()
    conn.fetchval.return_value = current
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(engine=FakeEngine(conn))),
    )
    host, instance = uuid.uuid4(), uuid.uuid4()
    if current is True:
        await require_instance(cast("Request", request), host, instance=instance)
    else:
        with pytest.raises(HTTPException) as refused:
            await require_instance(cast("Request", request), host, instance=instance)
        assert (refused.value.status_code, refused.value.detail) == (
            403,
            "stale host instance",
        )
    assert conn.fetchval.call_args is not None
    assert conn.fetchval.call_args.args == (
        "SELECT daemon_instance = $2 FROM hosts WHERE id = $1",
        host,
        instance,
    )


def test_request_engine_is_the_engine_on_app_state() -> None:
    engine = FakeEngine()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))

    assert request_engine(cast("Request", request)) is engine


def test_seq_ranges_decode_each_param_into_one_union() -> None:
    assert parse_seq_ranges(["1..3", "7.."], min_seq=1) == (
        SeqRange(start=1, stop=3),
        SeqRange(start=7),
    )


def test_absent_seq_ranges_are_no_ranges() -> None:
    assert parse_seq_ranges(None, min_seq=0) == ()


@pytest.mark.parametrize("raw", ["0..3", "nonsense"], ids=["below min", "malformed"])
def test_a_bad_seq_range_is_a_400_naming_the_fault(raw: str) -> None:
    with pytest.raises(HTTPException) as caught:
        _ = parse_seq_ranges([raw], min_seq=1)

    assert caught.value.status_code == 400
    assert isinstance(caught.value.detail, str)
    assert caught.value.detail


def test_fields_keep_the_named_row_keys() -> None:
    assert parse_fields(["kind", "title", "kind"]) == frozenset({"kind", "title"})


def test_absent_fields_send_the_whole_row() -> None:
    assert parse_fields(None) is None


def test_an_unknown_field_is_a_400_naming_it() -> None:
    with pytest.raises(HTTPException) as caught:
        _ = parse_fields(["title", "colour"])

    assert (caught.value.status_code, caught.value.detail) == (
        400,
        "unknown field 'colour'",
    )


def test_iso_format_renders_a_datetime_and_passes_none() -> None:
    at = datetime(2026, 11, 6, 13, 5, tzinfo=UTC)

    assert iso_format(at) == "2026-11-06T13:05:00+00:00"
    assert iso_format(None) is None


@pytest.mark.parametrize("key", [uuid.UUID(int=5), None])
def test_idempotency_key_is_what_the_middleware_parsed(key: uuid.UUID | None) -> None:
    request = SimpleNamespace(state=SimpleNamespace(idempotency_key=key))

    assert idempotency_key(cast("Request", request)) == key


def test_a_request_that_skipped_the_middleware_has_no_key() -> None:
    request = SimpleNamespace(state=SimpleNamespace())

    assert idempotency_key(cast("Request", request)) is None


def test_a_key_that_is_not_a_uuid_is_refused() -> None:
    request = SimpleNamespace(state=SimpleNamespace(idempotency_key="not-a-uuid"))

    with pytest.raises(ValueError, match="isinstance"):
        _ = idempotency_key(cast("Request", request))


def test_a_browser_session_passes_and_an_api_key_is_403() -> None:
    require_browser(make_test_identity(api_key_id=None))

    with pytest.raises(HTTPException) as caught:
        require_browser(make_test_identity())

    assert (caught.value.status_code, caught.value.detail) == (
        403,
        "Browser session required",
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
