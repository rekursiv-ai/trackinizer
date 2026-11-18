"""The exec wire contract: what a command, its offer and its result may carry."""

from __future__ import annotations

import base64
import uuid

import pydantic
import pytest

from trackinizer.wire import wire_exec
from trackinizer.wire.wire_exec import ExecRequest, ExecResult, ExecWait


_REQUEST = {"command": "ls", "cwd": "~/work", "timeout_sec": 5}
_RESULT = {
    "exit_code": 0,
    "stdout_b64": "",
    "stderr_b64": "",
    "cwd": "/home/ada",
    "truncated": False,
    "duration_ms": 3,
}


def test_a_request_needs_only_a_command_a_folder_and_a_timeout() -> None:
    request = ExecRequest.model_validate(_REQUEST)

    assert (request.stdin_b64, request.env) == (None, None)


def test_a_request_carries_stdin_as_base64_and_an_environment() -> None:
    stdin = base64.b64encode(b"hello\n").decode()

    request = ExecRequest.model_validate(
        _REQUEST | {"stdin_b64": stdin, "env": {"PATH_EXTRA": "/opt/bin"}},
    )

    assert (request.stdin_b64, request.env) == (stdin, {"PATH_EXTRA": "/opt/bin"})


@pytest.mark.parametrize(
    "change",
    [
        {"command": ""},
        {"command": "a\x00b"},
        {"command": "x" * 65_537},
        {"cwd": "relative/dir"},
        {"cwd": "/a\nb"},
        {"timeout_sec": 0},
        {"timeout_sec": 601},
        {"stdin_b64": "not base64!"},
        {"stdin_b64": "A"},
        {"stdin_b64": "AAAAA"},
        {"stdin_b64": "AA="},
        {"stdin_b64": "A==="},
        {"stdin_b64": "AAAA=AAA"},
        {"stdin_b64": base64.b64encode(bytes(wire_exec.MAX_STREAM_BYTES + 1)).decode()},
        {"stdin_b64": "A" * (wire_exec.MAX_STREAM_BYTES * 2)},
        {"env": {"1BAD": "x"}},
        {"env": {"OK": "a\x00b"}},
        {"env": {"OK": "x" * 8_193}},
        {"env": {f"K{n}": "v" for n in range(wire_exec.MAX_ENV_VARS + 1)}},
        {"host": "other"},
    ],
    ids=lambda change: next(iter(change)),
)
def test_a_request_outside_the_limits_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        ExecRequest.model_validate(_REQUEST | change)


@pytest.mark.parametrize("missing", ["command", "cwd", "timeout_sec"])
def test_a_request_without_a_required_field_is_refused(missing: str) -> None:
    body = {key: value for key, value in _REQUEST.items() if key != missing}

    with pytest.raises(pydantic.ValidationError):
        ExecRequest.model_validate(body)


def test_the_offer_is_the_request_with_the_id_the_result_is_reported_under() -> None:
    exec_id = uuid.UUID(int=7)

    offer = ExecWait.model_validate(
        _REQUEST | {"exec_id": str(exec_id), "asker": "ada@x.y"},
    )

    assert (offer.exec_id, offer.asker, offer.command) == (exec_id, "ada@x.y", "ls")


def test_a_full_size_stream_fits_a_result() -> None:
    full = base64.b64encode(bytes(wire_exec.MAX_STREAM_BYTES)).decode()

    result = ExecResult.model_validate(_RESULT | {"stdout_b64": full})

    assert len(base64.b64decode(result.stdout_b64)) == wire_exec.MAX_STREAM_BYTES


@pytest.mark.parametrize(
    "change",
    [
        {"exit_code": 256},
        {"duration_ms": -1},
        {"stdout_b64": "A" * (wire_exec.MAX_STREAM_BYTES * 2)},
        {"stderr_b64": "***"},
        {"cwd": "relative"},
        {"truncated": "maybe"},
        {"extra": 1},
    ],
    ids=lambda change: next(iter(change)),
)
def test_a_result_outside_the_limits_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        ExecResult.model_validate(_RESULT | change)


def test_a_timeout_reports_the_exit_code_timeout_gives() -> None:
    assert wire_exec.TIMEOUT_EXIT_CODE == 124


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
