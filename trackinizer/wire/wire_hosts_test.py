from __future__ import annotations

from datetime import UTC, datetime

import uuid

from pydantic import TypeAdapter, ValidationError

import pytest

from trackinizer.wire.wire_hosts import (
    MAX_FACTS_BYTES,
    MAX_HOW_CHARS,
    Facts,
    Host,
    HostDetail,
    HostPut,
    HostRun,
    HostRuns,
)


def _host(**over: object) -> dict[str, object]:
    return {
        "name": "gpu-box",
        "role": "",
        "how": "",
        "labels": list[str](),
        "updated_by": "admin@example.com",
        "updated": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
    } | over


@pytest.mark.parametrize(
    "over",
    [
        {"name": "Bad"},
        {"name": "-lead"},
        {"role": "9role"},
        {"how": "x" * (MAX_HOW_CHARS + 1)},
        {"extra": 1},
    ],
)
def test_host_refuses_bad_fields(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Host.model_validate(_host(**over))


def test_detail_facts_validate_and_bound_size() -> None:
    detail = HostDetail.model_validate(
        _host(facts={"os": "linux", "cpus": 8, "gpu": True, "clis": ["claude"]}),
    )
    assert detail.facts == {"os": "linux", "cpus": 8, "gpu": True, "clis": ["claude"]}
    assert HostDetail.model_validate(_host()).facts == {}
    too_big = {f"k{i}": "x" * 200 for i in range(MAX_FACTS_BYTES // 200 + 1)}
    with pytest.raises(ValidationError, match=f"facts exceed {MAX_FACTS_BYTES} bytes"):
        HostDetail.model_validate(_host(facts=too_big))
    with pytest.raises(ValidationError):
        HostDetail.model_validate(_host(facts={"Upper": "x"}))


def test_facts_accept_the_exact_byte_limit_but_refuse_one_more() -> None:
    values = ["x" * 255] * 15 + [""]
    facts: Facts = {"a": values}
    adapter = TypeAdapter(Facts)
    padding = MAX_FACTS_BYTES - len(adapter.dump_json(facts))
    values[-1] = "x" * padding

    assert len(adapter.dump_json(facts)) == MAX_FACTS_BYTES
    assert HostDetail.model_validate(_host(facts=facts)).facts == facts

    values[-1] += "x"
    with pytest.raises(ValidationError, match="facts exceed"):
        HostDetail.model_validate(_host(facts=facts))


def test_put_refuses_nul_and_keeps_omitted_fields_none() -> None:
    put = HostPut.model_validate({"how": "ssh gpu-box"})
    assert (put.role, put.how) == (None, "ssh gpu-box")
    assert HostPut.model_validate({}).how is None
    with pytest.raises(ValidationError, match="NUL"):
        HostPut.model_validate({"how": "a\x00b"})


def test_run_ignores_fields_a_newer_server_added() -> None:
    sid = uuid.uuid4()
    run = HostRun.model_validate({"session_id": str(sid), "cli": "claude", "x": 1})
    assert run.session_id == sid
    assert HostRuns.model_validate({"runs": [run.model_dump()]}).past == []


def test_a_listed_host_defaults_to_never_connected() -> None:
    detail = HostDetail.model_validate(
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
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
