"""Direct GPU wire contract: keys, command lines, node names and certificates."""

from __future__ import annotations

import base64
import hashlib
import re

import pydantic
import pytest

from trackinizer.wire.wire_job import (
    GATE_RO_VERBS,
    GATE_RW_VERBS,
    MAX_ARGV_CHARS,
    NODE_PATTERN,
    TERMINAL_STATES,
    Grant,
    GrantStatus,
    NodeAddress,
    ReadGrantBody,
    SubmitBody,
    ed25519_public_key,
    key_sha256,
)


_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIPtWmP3ce7m3hrwpxvm9x5s3WnCClGsA5R5nJ9J25ir1"
_KEY = f"ssh-ed25519 {_BLOB}"
_RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDOMCaToQLzDaKUozWG6BTWw x"
_COMMIT = "a" * 40
_HOST_KEY = f"ssh-ed25519 {_BLOB}"
_CERT = "ssh-ed25519-cert-v01@openssh.com AAAAIHNzaC1lZDI1NTE5LWNlcnQtdjAx"


def _submit(**fields: object) -> SubmitBody:
    return SubmitBody.model_validate(
        {
            "public_key": _KEY,
            "gpus": 1,
            "minutes": 5,
            "argv": ["python", "x.py"],
            "commit": _COMMIT,
            **fields,
        },
    )


def _node(**fields: object) -> NodeAddress:
    return NodeAddress.model_validate(
        {
            "name": "gpu-node-b",
            "host": "203.0.113.7",
            "port": 22,
            "user": "gj7",
            "host_key": _HOST_KEY,
            **fields,
        },
    )


def test_an_ed25519_key_is_kept_and_its_comment_dropped() -> None:
    assert ed25519_public_key(f"{_KEY} trax-job") == _KEY
    assert ed25519_public_key(_KEY) == _KEY


_NOT_ED25519 = r"^the public key must be one ssh-ed25519 key$"
_NOT_BASE64 = r"^the public key is not base64$"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (_RSA, _NOT_ED25519),
        ("garbage", _NOT_ED25519),
        ("", _NOT_ED25519),
        ("ssh-ed25519", _NOT_ED25519),
        ("ssh-ed25519 !!!not-base64!!!", _NOT_BASE64),
        (f"ssh-ed25519 {_BLOB[:10]}!{_BLOB[10:]}", _NOT_BASE64),
        (f"ssh-ed25519 {_BLOB[:-4]}", _NOT_ED25519),
        (f"ssh-ed25519 {base64.b64encode(b'x' * 51).decode()}", _NOT_ED25519),
        (f"ssh-ed25519 {base64.b64encode(b'x' * 60).decode()}", _NOT_ED25519),
    ],
    ids=[
        "rsa",
        "garbage",
        "empty",
        "no-blob",
        "bad-base64",
        "stray-character",
        "short",
        "wrong-type",
        "long",
    ],
)
def test_anything_but_one_ed25519_key_is_refused(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ed25519_public_key(value)


def test_key_sha256_is_the_digest_of_the_decoded_blob() -> None:
    expected = hashlib.sha256(base64.b64decode(_BLOB)).hexdigest()

    assert key_sha256(f"{_KEY} a-comment") == expected
    assert re.fullmatch(r"[0-9a-f]{64}", expected)


def test_key_sha256_refuses_a_key_that_is_not_ed25519() -> None:
    with pytest.raises(ValueError, match=_NOT_ED25519):
        key_sha256(_RSA)


def test_a_submit_holds_a_clean_key_and_the_command() -> None:
    body = _submit(public_key=f"{_KEY} trax-job", label="try 1.a")

    assert body.public_key == _KEY
    assert body.argv == ["python", "x.py"]
    assert body.label == "try 1.a"


@pytest.mark.parametrize(
    "fields",
    [
        {"gpus": 0},
        {"gpus": 9},
        {"minutes": 0},
        {"minutes": 361},
        {"argv": []},
        {"argv": ["a\x00b"]},
        {"label": "x" * 81},
        {"label": "bad;label"},
        {"commit": "A" * 40},
        {"commit": "a" * 39},
        {"public_key": _RSA},
        {"extra": 1},
    ],
    ids=[
        "gpus-low",
        "gpus-high",
        "minutes-low",
        "minutes-high",
        "no-argv",
        "nul",
        "long-label",
        "label-chars",
        "upper-commit",
        "short-commit",
        "rsa",
        "extra",
    ],
)
def test_a_submit_refuses_a_bad_field(fields: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        _submit(**fields)


def test_the_command_line_is_capped_with_one_separator_per_word() -> None:
    longest = "a" * (MAX_ARGV_CHARS - 1)

    assert _submit(argv=[longest]).argv == [longest]
    with pytest.raises(pydantic.ValidationError, match="too long"):
        _submit(argv=[longest, ""])
    with pytest.raises(pydantic.ValidationError, match="too long"):
        _submit(argv=["a" * MAX_ARGV_CHARS])


@pytest.mark.parametrize("name", ["a", "gpu-node-b", "n0de-7", "a" * 63])
def test_a_node_name_is_a_lowercase_label(name: str) -> None:
    assert re.fullmatch(NODE_PATTERN, name)
    assert _node(name=name).name == name


@pytest.mark.parametrize(
    "name",
    ["", "-a", "A", "a_b", "a.b", "a" * 64, "a b", "a\n"],
)
def test_a_node_name_outside_the_pattern_is_refused(name: str) -> None:
    assert re.fullmatch(NODE_PATTERN, name) is None
    with pytest.raises(pydantic.ValidationError):
        _node(name=name)


@pytest.mark.parametrize(
    "fields",
    [
        {"user": "root"},
        {"user": "gj"},
        {"user": "gj1234567890123"},
        {"port": 0},
        {"port": 65_536},
        {"host": ""},
        {"host_key": "ssh-rsa AAAA"},
    ],
)
def test_a_node_address_refuses_a_bad_field(fields: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        _node(**fields)


def _grant(**fields: object) -> dict[str, object]:
    return {
        "job_id": 7,
        "serial": 3,
        "role": "rw",
        "node": _node().model_dump(),
        "certificate": _CERT,
        "valid_before": "2026-10-21T12:00:00Z",
        "max_bundle_bytes": 1_000_000,
        **fields,
    }


def test_a_grant_status_carries_a_grant_only_when_ready() -> None:
    ready = GrantStatus.model_validate({"state": "ready", "grant": _grant()})
    pending = GrantStatus.model_validate({"state": "pending"})
    refused = GrantStatus.model_validate({"state": "refused", "reason": "no credit"})

    assert ready.grant is not None
    assert ready.grant.node.user == "gj7"
    assert ready.grant.valid_before.year == 2026
    assert (pending.grant, pending.reason) == (None, "")
    assert refused.reason == "no credit"


@pytest.mark.parametrize(
    "fields",
    [
        {"certificate": "ssh-rsa AAAA"},
        {"certificate": "ssh-ed25519-cert-v01@openssh.com"},
        {"role": "admin"},
    ],
)
def test_a_grant_refuses_a_certificate_or_role_it_does_not_know(
    fields: dict[str, object],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        Grant.model_validate(_grant(**fields))


def test_a_read_grant_body_validates_the_key_like_a_submit() -> None:
    assert ReadGrantBody(public_key=f"{_KEY} c").public_key == _KEY
    with pytest.raises(pydantic.ValidationError):
        ReadGrantBody(public_key=_RSA)


def test_the_read_verbs_are_a_subset_of_the_upload_verbs() -> None:
    assert set(GATE_RO_VERBS) < set(GATE_RW_VERBS)
    assert "upload" not in GATE_RO_VERBS
    assert "completed" in TERMINAL_STATES
    assert "running" not in TERMINAL_STATES


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
