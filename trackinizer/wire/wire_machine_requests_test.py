"""The request models refuse what the table cannot hold, before any write."""

from __future__ import annotations

import pydantic
import pytest

from trackinizer.wire.wire_machine_requests import (
    MAX_PAYLOAD_BYTES,
    MachineRequestCreate,
    MachineRequestResult,
)
from trackinizer.wire.wire_machines import (
    MAX_SPEC_BYTES,
    MachineProvision,
)


def test_a_request_defaults_to_an_empty_body() -> None:
    assert MachineRequestCreate(kind="push").body == {}


@pytest.mark.parametrize("kind", ["Push", "1push", "p" * 33, "", "push now"])
def test_a_kind_outside_the_pattern_is_refused(kind: str) -> None:
    with pytest.raises(pydantic.ValidationError, match="kind"):
        MachineRequestCreate(kind=kind)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"ref": "a\x00b"}, "NUL"),
        ({"a\x00": 1}, "NUL"),
        ({"refs": [{"sha": "\x00"}]}, "NUL"),
        ({"n": float("nan")}, "not finite"),
        ({"n": [float("inf")]}, "not finite"),
        ({"k": "v" * MAX_PAYLOAD_BYTES}, "exceeds 65536 bytes"),
    ],
    ids=["nul-value", "nul-key", "nul-nested", "nan", "inf", "too-large"],
)
def test_a_body_the_table_cannot_hold_is_refused(
    body: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(pydantic.ValidationError, match=reason):
        MachineRequestCreate.model_validate({"kind": "push", "body": body})
    with pytest.raises(pydantic.ValidationError, match=reason):
        MachineRequestResult.model_validate({"status": "done", "result": body})


def test_the_largest_body_is_measured_as_compact_utf8() -> None:
    # `{"k":"` and `"}` are eight bytes; two-byte characters fill the rest exactly.
    fits = {"k": "é" * ((MAX_PAYLOAD_BYTES - 8) // 2)}
    assert MachineRequestCreate(kind="push", body=fits).body == fits
    with pytest.raises(pydantic.ValidationError, match="exceeds"):
        MachineRequestCreate(kind="push", body={"k": f"{fits['k']}éé"})


def test_a_backslash_u_text_is_not_mistaken_for_a_nul() -> None:
    body = {"text": "\\u0000"}
    assert MachineRequestCreate(kind="push", body=body).body == body


@pytest.mark.parametrize("status", ["pending", "running", ""])
def test_a_result_is_only_done_or_failed(status: str) -> None:
    with pytest.raises(pydantic.ValidationError, match="status"):
        MachineRequestResult.model_validate({"status": status})


def test_a_spec_is_bounded_like_a_body_at_its_own_limit() -> None:
    with pytest.raises(pydantic.ValidationError, match=f"exceeds {MAX_SPEC_BYTES}"):
        MachineProvision(provider="cloud", spec={"k": "v" * MAX_SPEC_BYTES})
    with pytest.raises(pydantic.ValidationError, match="NUL"):
        MachineProvision(provider="cloud", spec={"k": "\x00"})


@pytest.mark.parametrize(
    "fields",
    [
        {"provider": ""},
        {"provider": "Cloud"},
        {"provider": "c" * 33},
        {"provider": "cloud", "ttl_sec": 59},
        {"provider": "cloud", "spec": []},
        {"provider": "cloud", "extra": 1},
    ],
    ids=["empty", "upper", "long", "short-ttl", "spec-list", "extra"],
)
def test_a_provision_outside_the_contract_is_refused(fields: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        MachineProvision.model_validate(fields)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
