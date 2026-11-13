"""The session relay's calls: a person's, an agent process's, and a daemon's."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qsl
from uuid import UUID

import json
import time

from treekle import from_plain

import httpx2
import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.wire.wire_session_relay import (
    LINES_PING_SEC,
    AgentProcess,
    AgentState,
    EventLine,
    InputSent,
    NewInput,
    Opened,
    ProcessEnded,
    ProcessSpec,
    ServeSession,
    StateLine,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


_SESSION = UUID(int=1)
_PROCESS = UUID(int=2)
_HOST = UUID(int=3)
_INSTANCE = UUID(int=4)
_SINCE = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_SPEC = ProcessSpec(host="lab-box", cwd="~/work", args=("--model", "opus"))
_SERVING = AgentProcess(
    session_id=_SESSION,
    process_id=_PROCESS,
    host="lab-box",
    state="starting",
    since=_SINCE,
)


_CALL_IDS: Final = ("start", "read", "stop", "send", "wait", "ended")
_CALLS: Final[tuple[tuple[str, str, Callable[[Client], object]], ...]] = (
    (
        "PUT",
        f"/api/sessions/{_SESSION}/process",
        lambda found: found.start_process(_SESSION, _SPEC),
    ),
    (
        "GET",
        f"/api/sessions/{_SESSION}/process",
        lambda found: found.read_process(_SESSION),
    ),
    (
        "DELETE",
        f"/api/sessions/{_SESSION}/process",
        lambda found: found.stop_process(_SESSION),
    ),
    (
        "POST",
        f"/api/sessions/{_SESSION}/inputs",
        lambda found: found.send_input(_SESSION, NewInput(kind="Halt", payload={})),
    ),
    (
        "GET",
        f"/api/hosts/{_HOST}/sessions/wait",
        lambda found: found.wait_session(_HOST, instance=_INSTANCE, wait_sec=0),
    ),
    (
        "POST",
        f"/api/hosts/{_HOST}/sessions/ended",
        lambda found: found.report_process_ended(
            _HOST,
            ProcessEnded(process_id=_PROCESS, exit_code=1, reason=""),
        ),
    ),
)


def _client(answer: Callable[[httpx2.Request], httpx2.Response]) -> Client:
    found = Client("https://relay.test")
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


def _sent(request: httpx2.Request) -> tuple[str, str, object]:
    return (
        request.method,
        request.url.path,
        json.loads(request.content) if request.content else None,
    )


def _read_timeout(request: httpx2.Request) -> float | None:
    return from_plain(request.extensions["timeout"], dict[str, float | None])["read"]


def test_starting_a_process_puts_the_spec_and_returns_the_process() -> None:
    recorder = _Recorder(httpx2.Response(200, json=_SERVING.model_dump(mode="json")))

    with _client(recorder) as found:
        started = found.start_process(_SESSION, _SPEC)

    assert started == _SERVING
    assert _sent(recorder.requests[0]) == (
        "PUT",
        f"/api/sessions/{_SESSION}/process",
        {"host": "lab-box", "cwd": "~/work", "args": ["--model", "opus"]},
    )


def test_a_start_sends_only_the_places_that_were_named() -> None:
    """An older server refuses a key it does not know, so a default is not sent."""
    elsewhere = _SERVING.model_copy(update={"login_host": "laptop"})
    recorder = _Recorder(
        httpx2.Response(200, json=_SERVING.model_dump(mode="json")),
        httpx2.Response(200, json=elsewhere.model_dump(mode="json")),
    )

    with _client(recorder) as found:
        started = (
            found.start_process(_SESSION, ProcessSpec()),
            found.start_process(_SESSION, ProcessSpec(login_host="laptop")),
        )

    assert started == (_SERVING, elsewhere)
    assert [_sent(each)[2] for each in recorder.requests] == [
        {},
        {"login_host": "laptop"},
    ]


def test_reading_and_stopping_a_process_return_it_or_none() -> None:
    recorder = _Recorder(
        httpx2.Response(200, json=_SERVING.model_dump(mode="json")),
        httpx2.Response(200, json=None),
        httpx2.Response(200, json=_SERVING.model_dump(mode="json")),
        httpx2.Response(200, json=None),
    )

    with _client(recorder) as found:
        answers = (
            found.read_process(_SESSION),
            found.read_process(_SESSION),
            found.stop_process(_SESSION),
            found.stop_process(_SESSION),
        )

    assert answers == (_SERVING, None, _SERVING, None)
    assert [(each.method, each.url.path) for each in recorder.requests] == [
        ("GET", f"/api/sessions/{_SESSION}/process"),
        ("GET", f"/api/sessions/{_SESSION}/process"),
        ("DELETE", f"/api/sessions/{_SESSION}/process"),
        ("DELETE", f"/api/sessions/{_SESSION}/process"),
    ]


def test_sending_an_input_posts_it_once_and_returns_its_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An input is delivered at most once, so a lost answer is not sent again."""
    monkeypatch.setattr(time, "sleep", _no_sleep)
    new = NewInput(id=UUID(int=7), kind="Command", payload={"line": "/model opus"})
    answered = _Recorder(httpx2.Response(200, json={"id": str(UUID(int=7))}))
    lost = _Recorder(httpx2.ReadError("response lost"))

    with _client(answered) as found:
        sent = found.send_input(_SESSION, new)
    with _client(lost) as found, pytest.raises(ClientError, match="response lost"):
        found.send_input(_SESSION, new)

    assert sent == InputSent(id=UUID(int=7)).id
    assert _sent(answered.requests[0]) == (
        "POST",
        f"/api/sessions/{_SESSION}/inputs",
        {
            "id": str(UUID(int=7)),
            "kind": "Command",
            "payload": {"line": "/model opus"},
        },
    )
    assert len(lost.requests) == 1


def test_an_input_to_a_session_no_process_serves_names_the_refusal() -> None:
    refusal = {"detail": {"code": "not_live", "message": "No process serves it."}}
    recorder = _Recorder(httpx2.Response(409, json=refusal))

    with _client(recorder) as found, pytest.raises(ClientError) as raised:
        found.send_input(_SESSION, NewInput(kind="Halt", payload={}))

    assert (raised.value.status_code, raised.value.code) == (409, "not_live")


def test_open_lines_yields_each_frame_and_skips_what_is_not_one() -> None:
    opened = Opened(
        state=AgentState(line="[1s]", busy=True, since=_SINCE),
        process=_SERVING,
    )
    event = EventLine(kind="ModelResponsePartial", payload={"text": "Hi"})
    state = StateLine(state=AgentState(line="[2s]", busy=False, since=_SINCE))
    stream = "".join(
        [
            ": open\n\n",
            f"data: {opened.model_dump_json()}\n\n",
            "event: ping\ndata: {}\n\n",
            f"data: {event.model_dump_json()}\n\n",
            f"data:{state.model_dump_json()}\n\n",
        ],
    )
    recorder = _Recorder(httpx2.Response(200, text=stream))

    with _client(recorder) as found:
        frames = list(found.open_lines(_SESSION))

    assert frames == [opened, event, state]
    assert _sent(recorder.requests[0]) == (
        "GET",
        f"/api/sessions/{_SESSION}/lines",
        None,
    )
    # A quiet session must not end the read, so the read outlasts a keep-alive.
    assert _read_timeout(recorder.requests[0]) == 2.5 * LINES_PING_SEC


def test_open_lines_reads_a_refusals_body_before_it_names_the_refusal() -> None:
    # Made here, not shared: a stream's body is unread until asked for, and once read
    # stays read, so a shared one would pass a client that never reads it.
    refused = httpx2.Response(400, content=iter([b"no such session"]))

    with _client(_Recorder(refused)) as found, pytest.raises(ClientError) as raised:
        _ = list(found.open_lines(_SESSION))

    assert (str(raised.value), raised.value.status_code) == (
        f"GET /api/sessions/{_SESSION}/lines -> 400: no such session",
        400,
    )


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (
            httpx2.ConnectError("refused"),
            f"GET /api/sessions/{_SESSION}/lines failed: refused",
        ),
        (
            httpx2.Response(200, text='data: {"type": "nonsense"}\n\n'),
            f"/api/sessions/{_SESSION}/lines returned a malformed payload: ",
        ),
    ],
    ids=["never reached it", "a frame of no known type"],
)
def test_open_lines_raises_a_client_error_naming_the_stream_it_cannot_read(
    reply: httpx2.Response | Exception,
    message: str,
) -> None:
    with _client(_Recorder(reply)) as found, pytest.raises(ClientError) as raised:
        _ = list(found.open_lines(_SESSION))

    assert str(raised.value).startswith(message)
    assert raised.value.status_code is None


def test_the_daemons_session_wait_returns_the_next_start_or_none() -> None:
    serve = ServeSession(
        session_id=_SESSION,
        process_id=_PROCESS,
        token=f"key-{_PROCESS.hex}",
        spec=_SPEC,
    )
    recorder = _Recorder(
        httpx2.Response(200, json=serve.model_dump(mode="json")),
        httpx2.Response(200, json=None),
    )

    with _client(recorder) as found:
        first = found.wait_session(_HOST, instance=_INSTANCE, wait_sec=25)
        second = found.wait_session(_HOST, instance=_INSTANCE, wait_sec=0)

    assert (first, second) == (serve, None)
    sent = recorder.requests[0]
    assert (sent.method, sent.url.path, dict(parse_qsl(sent.url.query.decode()))) == (
        "GET",
        f"/api/hosts/{_HOST}/sessions/wait",
        {"instance": str(_INSTANCE), "wait_sec": "25"},
    )
    # The read outlasts the hold it asked for, or the client would end its own wait.
    assert (_read_timeout(sent) or 0) > 25
    assert not any("Idempotency-Key" in each.headers for each in recorder.requests)


def test_the_daemon_reports_how_a_process_it_started_ended() -> None:
    ended = ProcessEnded(
        process_id=_PROCESS,
        exit_code=3,
        reason="exited with code 3",
        output="Traceback",
    )
    recorder = _Recorder(httpx2.Response(204))

    with _client(recorder) as found:
        assert found.report_process_ended(_HOST, ended) is None

    assert _sent(recorder.requests[0]) == (
        "POST",
        f"/api/hosts/{_HOST}/sessions/ended",
        {
            "process_id": str(_PROCESS),
            "exit_code": 3,
            "reason": "exited with code 3",
            "output": "Traceback",
        },
    )


@pytest.mark.parametrize(("method", "path", "call"), _CALLS, ids=_CALL_IDS)
def test_a_refused_call_names_its_request(
    method: str,
    path: str,
    call: Callable[[Client], object],
) -> None:
    recorder = _Recorder(httpx2.Response(403, text="not yours"))

    with _client(recorder) as found, pytest.raises(ClientError) as raised:
        _ = call(found)

    assert str(raised.value) == f"{method} {path} -> 403: not yours"


# The end report is answered with nothing, so it has no payload to misread.
@pytest.mark.parametrize(("path", "call"), [each[1:] for each in _CALLS[:-1]])
def test_an_answer_that_is_not_what_the_call_returns_names_its_path(
    path: str,
    call: Callable[[Client], object],
) -> None:
    recorder = _Recorder(httpx2.Response(200, json={"nonsense": True}))

    with _client(recorder) as found, pytest.raises(ClientError) as raised:
        _ = call(found)

    assert str(raised.value).startswith(f"{path} returned a malformed payload: ")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
