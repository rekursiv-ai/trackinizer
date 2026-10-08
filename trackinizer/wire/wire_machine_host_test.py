"""Machine host wire models: what a host may report, and what a rejection shows."""

from __future__ import annotations

from datetime import UTC, datetime

import uuid

import pydantic
import pytest

from trackinizer.wire.wire_machine_host import (
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    JoinRequest,
    JoinResponse,
)
from trackinizer.wire.wire_machines import MachineDetail


_INSTANCE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TOKEN = "enr_" + "0" * 32 + "_" + "SECRETMARKER" * 3 + "x" * 7


def _join(**fields: object) -> JoinRequest:
    return JoinRequest.model_validate(
        {
            "name": "dev-1",
            "token": _TOKEN,
            "instance": _INSTANCE,
            "host_version": "0.1",
            **fields,
        },
    )


def test_a_host_reports_strings_ints_bools_and_lists_of_strings() -> None:
    facts = {
        "os": "linux",
        "cpus": 8,
        "memory_bytes": 2**40,
        "has_gpu": True,
        "clis": ["claude", "codex"],
    }

    joined = _join(facts=facts)
    beat = HeartbeatRequest(instance=_INSTANCE, host_version="0.1", facts=facts)

    assert joined.facts == facts
    assert beat.facts == facts


def test_facts_default_to_none_on_a_heartbeat_and_empty_on_a_join() -> None:
    assert HeartbeatRequest(instance=_INSTANCE, host_version="").facts is None
    assert _join().facts == {}


@pytest.mark.parametrize(
    "facts",
    [
        {"Upper": "x"},
        {"1st": "x"},
        {"k" * 33: "x"},
        {"a": 1.5},
        {"a": None},
        {"a": {"nested": 1}},
        {"a": [1]},
        {"a": 2**63},
        {"a": -(2**63) - 1},
        {"a": "x" * 257},
        {"a": "a\x00b"},
        {"a": ["a\x00b"]},
        {f"k{i}": 1 for i in range(65)},
        {f"k{i}": "v" * 200 for i in range(40)},
    ],
    ids=[
        "upper",
        "digit-first",
        "long-key",
        "float",
        "null",
        "nested",
        "int-list",
        "big",
        "small",
        "long-string",
        "nul",
        "nul-in-list",
        "too-many",
        "too-large",
    ],
)
def test_facts_the_table_cannot_hold_are_refused(facts: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        _join(facts=facts)
    with pytest.raises(pydantic.ValidationError):
        HeartbeatRequest.model_validate(
            {"instance": _INSTANCE, "host_version": "", "facts": facts},
        )


def test_a_bool_is_not_an_int_and_the_largest_int_is_allowed() -> None:
    assert _join(facts={"a": 2**63 - 1, "b": -(2**63)}).facts["a"] == 2**63 - 1
    assert _join(facts={"a": True}).facts == {"a": True}


@pytest.mark.parametrize(
    "fields",
    [
        {"unexpected": "x"},
        {"name": "Upper"},
        {"host_version": "v" * 65},
        {"host_version": "a\x00b"},
        {"token": "t" * 129},
    ],
    ids=["extra", "name", "version", "nul-version", "token"],
)
def test_a_bad_join_field_is_refused(fields: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        _join(**fields)


@pytest.mark.parametrize("host_version", ["v" * 65, "a\x00b"], ids=["long", "nul"])
def test_a_host_version_the_table_cannot_hold_is_refused_in_a_heartbeat(
    host_version: str,
) -> None:
    with pytest.raises(pydantic.ValidationError):
        HeartbeatRequest.model_validate(
            {"instance": _INSTANCE, "host_version": host_version},
        )


def test_a_join_needs_every_field_but_facts() -> None:
    with pytest.raises(pydantic.ValidationError):
        JoinRequest.model_validate({"name": "dev-1", "token": _TOKEN})
    with pytest.raises(pydantic.ValidationError):
        HeartbeatRequest.model_validate({"instance": _INSTANCE})


def test_no_secret_appears_in_a_repr() -> None:
    credential = "trax_machine_" + "1" * 32 + "_" + "CREDENTIALSECRET" * 2 + "x" * 11

    shown = [
        repr(_join()),
        repr(
            EnrollResponse(token=_TOKEN, expires_at=datetime(2026, 10, 7, tzinfo=UTC)),
        ),
        repr(JoinResponse(machine_id=uuid.uuid4(), credential=credential)),
    ]

    assert not any(
        "SECRETMARKER" in text or "CREDENTIALSECRET" in text for text in shown
    )


def test_a_refused_join_does_not_quote_its_token() -> None:
    with pytest.raises(pydantic.ValidationError) as refused:
        _join(token=_TOKEN * 4)

    assert "SECRETMARKER" not in str(refused.value)


def test_an_enroll_name_follows_the_machine_name_pattern() -> None:
    assert EnrollRequest(name="dev-1").name == "dev-1"
    with pytest.raises(pydantic.ValidationError):
        EnrollRequest(name="Dev 1")


def test_a_listed_machine_defaults_to_never_connected() -> None:
    detail = MachineDetail.model_validate(
        {
            "name": "dev-1",
            "role": "",
            "how": "",
            "labels": [],
            "updated_by": "tester",
            "updated": "2026-10-07T12:00:00Z",
        },
    )

    assert (detail.status, detail.last_heartbeat, detail.facts) == ("never", None, {})


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
