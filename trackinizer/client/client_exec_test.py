"""The client's three exec calls: the assistant's, and the host's wait and result."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

import json

from treekle import from_plain

import httpx2
import pytest

from trackinizer.client import client
from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.wire import wire_exec
from trackinizer.wire.wire_exec import ExecRequest, ExecResult, ExecWait


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


_MACHINE = UUID(int=1)
_INSTANCE = UUID(int=2)
_RESULT = ExecResult(
    exit_code=3,
    stdout_b64="aGk=",
    stderr_b64="",
    cwd="/home/ada",
    truncated=False,
    duration_ms=9,
)


def _client(
    answer: Callable[[httpx2.Request], httpx2.Response],
) -> Client:
    found = Client("http://exec.test")
    found._http.close()
    found._http = httpx2.Client(
        base_url=found.base_url,
        transport=httpx2.MockTransport(answer),
    )
    return found


def _no_sleep(seconds: float) -> None:
    del seconds


class _Recorder:
    def __init__(self, *replies: httpx2.Response | Exception) -> None:
        self.requests: list[httpx2.Request] = []
        self._replies: Iterator[httpx2.Response | Exception] = iter(replies)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = next(self._replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_a_command_goes_to_the_machines_exec_route_for_the_asker() -> None:
    recorder = _Recorder(httpx2.Response(200, json=_RESULT.model_dump(mode="json")))
    key = uuid4()

    with _client(recorder) as found:
        result = found.exec_command(
            "gx10",
            ExecRequest(command="ls", cwd="~", timeout_sec=9),
            asker="ada@x.y",
            change_id=key,
        )

    assert result == _RESULT
    (sent,) = recorder.requests
    assert (sent.method, sent.url.path, dict(parse_qsl(sent.url.query.decode()))) == (
        "POST",
        "/api/machines/gx10/exec",
        {"asker": "ada@x.y"},
    )
    assert from_plain(json.loads(sent.content), dict[str, object]) == {
        "command": "ls",
        "cwd": "~",
        "timeout_sec": 9,
    }
    assert sent.headers["Idempotency-Key"] == str(key)


def test_a_command_gets_a_key_of_its_own_when_the_caller_names_none() -> None:
    recorder = _Recorder(httpx2.Response(200, json=_RESULT.model_dump(mode="json")))

    with _client(recorder) as found:
        _ = found.exec_command(
            "gx10",
            ExecRequest(command="ls", cwd="~", timeout_sec=1),
            asker="ada@x.y",
        )

    assert UUID(recorder.requests[0].headers["Idempotency-Key"])


def test_the_read_timeout_outlasts_the_command_its_offer_window_and_the_grace() -> None:
    recorder = _Recorder(httpx2.Response(200, json=_RESULT.model_dump(mode="json")))

    with _client(recorder) as found:
        _ = found.exec_command(
            "gx10",
            ExecRequest(command="ls", cwd="~", timeout_sec=600),
            asker="ada@x.y",
        )

    read = from_plain(recorder.requests[0].extensions["timeout"], dict[str, float])[
        "read"
    ]
    assert read >= 600 + wire_exec.OFFER_TTL_SEC + wire_exec.RESULT_GRACE_SEC


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.Response(
            504,
            json={"detail": {"code": "exec_lost", "message": "the host did not take"}},
        ),
        httpx2.ReadError("response lost"),
    ],
    ids=["gateway-timeout", "lost-response"],
)
def test_a_command_that_may_have_run_is_not_sent_again_on_its_own(
    failure: httpx2.Response | Exception,
) -> None:
    recorder = _Recorder(failure)

    with _client(recorder) as found, pytest.raises(ClientError) as refused:
        _ = found.exec_command(
            "gx10",
            ExecRequest(command="rm -rf build", cwd="~", timeout_sec=5),
            asker="ada@x.y",
        )

    assert len(recorder.requests) == 1
    if isinstance(failure, httpx2.Response):
        assert (refused.value.status_code, refused.value.code) == (504, "exec_lost")


def test_a_malformed_result_is_a_client_error() -> None:
    recorder = _Recorder(httpx2.Response(200, json={"exit_code": "zero"}))

    with _client(recorder) as found, pytest.raises(ClientError, match="malformed"):
        _ = found.exec_command(
            "gx10",
            ExecRequest(command="ls", cwd="~", timeout_sec=5),
            asker="ada@x.y",
        )


def test_the_hosts_wait_returns_the_next_command_or_none() -> None:
    offer = ExecWait(
        exec_id=uuid4(),
        asker="ada@x.y",
        command="ls",
        cwd="~",
        timeout_sec=5,
    )
    recorder = _Recorder(
        httpx2.Response(200, json=offer.model_dump(mode="json", exclude_none=True)),
        httpx2.Response(200, json=None),
    )

    with _client(recorder) as found:
        first = found.wait_exec(_MACHINE, instance=_INSTANCE, wait_sec=25)
        second = found.wait_exec(_MACHINE, instance=_INSTANCE, wait_sec=0)

    assert (first, second) == (offer, None)
    sent = recorder.requests[0]
    assert (sent.method, sent.url.path, dict(parse_qsl(sent.url.query.decode()))) == (
        "GET",
        f"/api/machines/{_MACHINE}/exec/wait",
        {"instance": str(_INSTANCE), "wait_sec": "25"},
    )


def test_a_wait_whose_response_was_lost_is_retried_under_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client.time, "sleep", _no_sleep)
    recorder = _Recorder(
        httpx2.ReadError("response lost"),
        httpx2.Response(200, json=None),
    )

    with _client(recorder) as found:
        assert found.wait_exec(_MACHINE, instance=_INSTANCE, wait_sec=0) is None

    keys = {each.headers["Idempotency-Key"] for each in recorder.requests}
    assert (len(recorder.requests), len(keys)) == (2, 1)


def test_the_host_posts_a_result_under_the_commands_id() -> None:
    recorder = _Recorder(httpx2.Response(204))
    exec_id = uuid4()

    with _client(recorder) as found:
        found.post_exec_result(_MACHINE, exec_id, _RESULT)

    (sent,) = recorder.requests
    assert (sent.method, sent.url.path) == (
        "POST",
        f"/api/machines/{_MACHINE}/exec/{exec_id}/result",
    )
    assert ExecResult.model_validate_json(sent.content) == _RESULT


def test_a_result_for_a_command_that_ended_is_a_client_error_the_host_can_read() -> (
    None
):
    recorder = _Recorder(httpx2.Response(404, json={"detail": "no such command"}))

    with _client(recorder) as found, pytest.raises(ClientError) as ended:
        found.post_exec_result(_MACHINE, uuid4(), _RESULT)

    assert ended.value.status_code == 404


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
