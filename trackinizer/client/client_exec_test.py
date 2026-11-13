"""The host's calls: its wait for a tool connection, and the old exec wait and result."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

import time

import httpx2
import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.wire.wire_exec import ExecResult, ExecWait
from trackinizer.wire.wire_tools import ToolOffer


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


_HOST = UUID(int=1)
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
    found = Client("https://exec.test")
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


def test_the_hosts_tool_wait_returns_the_next_offer_or_none() -> None:
    offer = ToolOffer(connection_id=uuid4())
    recorder = _Recorder(
        httpx2.Response(200, json=offer.model_dump(mode="json")),
        httpx2.Response(200, json=None),
    )

    with _client(recorder) as found:
        first = found.wait_tools(_HOST, instance=_INSTANCE, wait_sec=25)
        second = found.wait_tools(_HOST, instance=_INSTANCE, wait_sec=0)

    assert (first, second) == (offer, None)
    sent = recorder.requests[0]
    assert (sent.method, sent.url.path, dict(parse_qsl(sent.url.query.decode()))) == (
        "GET",
        f"/api/hosts/{_HOST}/tools/wait",
        {"instance": str(_INSTANCE), "wait_sec": "25"},
    )


def test_a_tool_wait_whose_response_was_lost_is_asked_again_with_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server keeps no receipt of a wait: an offer lost on its way is lost."""
    monkeypatch.setattr(time, "sleep", _no_sleep)
    recorder = _Recorder(
        httpx2.ReadError("response lost"),
        httpx2.Response(200, json=None),
    )

    with _client(recorder) as found:
        assert found.wait_tools(_HOST, instance=_INSTANCE, wait_sec=0) is None

    assert len(recorder.requests) == 2
    assert not any("Idempotency-Key" in each.headers for each in recorder.requests)


def test_a_malformed_offer_is_a_client_error() -> None:
    recorder = _Recorder(httpx2.Response(200, json={"connection_id": "not a uuid"}))

    with _client(recorder) as found, pytest.raises(ClientError, match="malformed"):
        _ = found.wait_tools(_HOST, instance=_INSTANCE, wait_sec=0)


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
        first = found.wait_exec(_HOST, instance=_INSTANCE, wait_sec=25)
        second = found.wait_exec(_HOST, instance=_INSTANCE, wait_sec=0)

    assert (first, second) == (offer, None)
    sent = recorder.requests[0]
    assert (sent.method, sent.url.path, dict(parse_qsl(sent.url.query.decode()))) == (
        "GET",
        f"/api/hosts/{_HOST}/exec/wait",
        {"instance": str(_INSTANCE), "wait_sec": "25"},
    )


def test_a_wait_whose_response_was_lost_is_retried_under_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", _no_sleep)
    recorder = _Recorder(
        httpx2.ReadError("response lost"),
        httpx2.Response(200, json=None),
    )

    with _client(recorder) as found:
        assert found.wait_exec(_HOST, instance=_INSTANCE, wait_sec=0) is None

    keys = {each.headers["Idempotency-Key"] for each in recorder.requests}
    assert (len(recorder.requests), len(keys)) == (2, 1)


def test_the_host_posts_a_result_under_the_commands_id() -> None:
    recorder = _Recorder(httpx2.Response(204))
    exec_id = uuid4()

    with _client(recorder) as found:
        found.post_exec_result(_HOST, exec_id, _RESULT)

    (sent,) = recorder.requests
    assert (sent.method, sent.url.path) == (
        "POST",
        f"/api/hosts/{_HOST}/exec/{exec_id}/result",
    )
    assert ExecResult.model_validate_json(sent.content) == _RESULT


def test_a_result_for_a_command_that_ended_is_a_client_error_the_host_can_read() -> (
    None
):
    recorder = _Recorder(httpx2.Response(404, json={"detail": "no such command"}))

    with _client(recorder) as found, pytest.raises(ClientError) as ended:
        found.post_exec_result(_HOST, uuid4(), _RESULT)

    assert ended.value.status_code == 404


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
