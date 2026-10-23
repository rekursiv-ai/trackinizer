"""Device login from the terminal, against a scripted server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import socket

from treekle import json_loads

import httpx2
import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax.login import LoginResult, login
from trackinizer.trax.profile import (
    Profile,
    current_profile,
    list_profiles,
    read_profile,
    save_profile,
)
from trackinizer.wire.wire_device import (
    DEVICE_CODE_PATH,
    DEVICE_POLL_PATH,
    DeviceCode,
    DeviceToken,
    IssuedKey,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


_KEY: Final = "trax_" + "k" * 43
_ENROLLMENT: Final = "enr_" + "0" * 32 + "_" + "e" * 43
_CODE: Final = DeviceCode(
    device_code="d" * 43,
    user_code="BCDF-GHJK",
    verification_uri="https://trackinizer.example/auth/device",
    verification_uri_complete="https://trackinizer.example/auth/device?user_code=BCDF-GHJK",
    expires_in=600,
    interval=5,
)


class _Server:
    """A server that answers the code request, then each poll from a script."""

    def __init__(self, polls: Sequence[DeviceToken | int]) -> None:
        self.polls = list(polls)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == DEVICE_CODE_PATH:
            return httpx2.Response(201, content=_CODE.model_dump_json())
        assert request.url.path == DEVICE_POLL_PATH
        poll = self.polls.pop(0)
        if isinstance(poll, int):
            return httpx2.Response(poll, json={"detail": "refused"})
        return httpx2.Response(200, content=poll.model_dump_json())

    def bodies(self) -> list[dict[str, object]]:
        return [
            json_loads(request.content, dict[str, object]) for request in self.requests
        ]


class _Time:
    """A clock the fake sleep advances, so a login that never ends times out."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _approved(*, role: str = "writer", machine_token: str = "") -> DeviceToken:
    return DeviceToken(
        status="approved",
        issued=IssuedKey.model_validate(
            {
                "api_key": _KEY,
                "role": role,
                "email": "jp@example.com",
                "machine_token": machine_token,
            },
        ),
    )


def _transport_of(
    server: Callable[[httpx2.Request], httpx2.Response],
) -> Callable[..., httpx2.MockTransport]:
    """Return what stands in for ``httpx2.HTTPTransport``: it answers from ``server``."""

    def make(**options: object) -> httpx2.MockTransport:
        del options
        return httpx2.MockTransport(server)

    return make


def _log_in(
    server: Callable[[httpx2.Request], httpx2.Response],
    clock: _Time,
    *,
    url: str = "https://trackinizer.example",
    profile: str = "",
    machine: str | None = None,
    client: str = "trax",
) -> LoginResult:
    # The client builds its transport from httpx2's, so the server answers there.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx2, "HTTPTransport", _transport_of(server))
        return login(
            url,
            profile=profile,
            machine=machine,
            client=client,
            sleep=clock.sleep,
            clock=clock.clock,
        )


@pytest.fixture(autouse=True)
def _no_pinned_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "Dev_box.local")


def test_a_login_saves_the_key_under_the_servers_host_and_makes_it_current(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _Server([DeviceToken(status="pending"), _approved()])
    clock = _Time()

    result = _log_in(server, clock)

    assert result == LoginResult(profile="trackinizer.example", role="writer")
    assert read_profile("trackinizer.example") == Profile(
        url="https://trackinizer.example",
        api_key=_KEY,
    )
    assert current_profile() == "trackinizer.example"
    assert clock.slept == [5, 5]
    out = capsys.readouterr().out
    assert "https://trackinizer.example/auth/device?user_code=BCDF-GHJK" in out
    assert "BCDF-GHJK" in out
    assert _KEY not in out


def test_the_request_names_the_program_and_the_computer_and_carries_no_credential() -> (
    None
):
    server = _Server([_approved()])

    _log_in(server, _Time(), client="machine-host")

    first, second = server.requests
    assert "authorization" not in first.headers
    assert server.bodies() == [
        {"client": "machine-host", "host": "Dev_box", "machine": None},
        {"device_code": _CODE.device_code},
    ]
    assert second.method == "POST"


def test_a_slow_down_widens_every_later_wait() -> None:
    server = _Server([DeviceToken(status="slow_down"), _approved()])
    clock = _Time()

    _log_in(server, clock)

    assert clock.slept == [5, 10]


def test_a_profile_name_the_caller_gives_is_used() -> None:
    result = _log_in(_Server([_approved()]), _Time(), profile="work")

    assert result.profile == "work"
    assert [name for name, _ in list_profiles()] == ["work"]


def test_an_existing_profile_keeps_its_actor_when_the_key_is_renewed() -> None:
    save_profile(
        "trackinizer.example",
        Profile(url="https://old.example", author="jp", api_key="trax_" + "o" * 43),
    )

    _log_in(_Server([_approved()]), _Time())

    assert read_profile("trackinizer.example") == Profile(
        url="https://trackinizer.example",
        author="jp",
        api_key=_KEY,
    )


@pytest.mark.parametrize(
    ("hostname", "host"),
    [
        ("_build-7.corp", "build-7"),
        ("--ci-box", "ci-box"),
        ("Dev_box.local", "Dev_box"),
    ],
)
def test_a_host_name_the_server_would_refuse_is_cleaned_before_it_is_sent(
    monkeypatch: pytest.MonkeyPatch,
    hostname: str,
    host: str,
) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: hostname)
    server = _Server([_approved()])

    _log_in(server, _Time())

    assert server.bodies()[0]["host"] == host


@pytest.mark.parametrize(
    "url",
    ["http://trackinizer.example", "http://10.0.0.5:8765", "http://[2001:db8::1]"],
)
def test_plain_http_beyond_this_computer_is_refused_before_any_request(
    url: str,
) -> None:
    server = _Server([_approved()])

    with pytest.raises(ClientError, match="plain http"):
        _log_in(server, _Time(), url=url)

    assert server.requests == []
    assert list_profiles() == []


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"],
)
def test_plain_http_to_this_computer_is_allowed(url: str) -> None:
    result = _log_in(_Server([_approved()]), _Time(), url=url)

    assert result.role == "writer"


def test_a_login_without_a_program_name_is_trax_logging_in() -> None:
    # What ``trax profile NAME login URL`` sends: it names no program of its own.
    server = _Server([_approved()])

    _log_in(server, _Time())

    assert server.bodies()[0]["client"] == "trax"


def test_a_server_with_a_port_is_named_with_it() -> None:
    result = _log_in(_Server([_approved()]), _Time(), url="http://127.0.0.1:8765/")

    assert result.profile == "127.0.0.1-8765"
    assert read_profile("127.0.0.1-8765").url == "http://127.0.0.1:8765"


def test_a_saved_default_profile_stays_current() -> None:
    save_profile("default", Profile(url="http://127.0.0.1:8765"))

    _log_in(_Server([_approved()]), _Time())

    assert current_profile() == "default"
    assert {name for name, _ in list_profiles()} == {"default", "trackinizer.example"}


def test_a_pinned_profile_stays_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACKINIZER_PROFILE", "lab")

    _log_in(_Server([_approved()]), _Time())

    assert current_profile() == "lab"


def test_a_login_that_names_a_machine_returns_its_enrollment_token() -> None:
    server = _Server([_approved(machine_token=_ENROLLMENT)])

    result = _log_in(server, _Time(), machine="dev-box")

    assert result.machine_token == _ENROLLMENT
    assert server.bodies()[0]["machine"] == "dev-box"
    assert _ENROLLMENT not in repr(result)


def test_a_machine_the_server_did_not_enroll_saves_nothing() -> None:
    server = _Server([_approved()])

    with pytest.raises(ClientError, match="enrolled no token for machine 'devbox'"):
        _log_in(server, _Time(), machine="devbox")

    assert list_profiles() == []


def test_a_key_below_writer_is_reported_as_it_is() -> None:
    result = _log_in(_Server([_approved(role="viewer")]), _Time())

    assert result.role == "viewer"


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("denied", "denied in the browser"),
        ("expired_token", "expired before it was approved"),
    ],
)
def test_a_denied_or_expired_code_ends_the_login_and_saves_nothing(
    status: str,
    message: str,
) -> None:
    server = _Server([DeviceToken.model_validate({"status": status})])

    with pytest.raises(ClientError, match=message):
        _log_in(server, _Time())

    assert list_profiles() == []


def test_a_code_nobody_approves_times_out() -> None:
    server = _Server([DeviceToken(status="pending")] * 200)
    clock = _Time()

    with pytest.raises(ClientError, match="not approved in time"):
        _log_in(server, clock)

    assert sum(clock.slept) >= _CODE.expires_in
    assert list_profiles() == []


def test_a_refusal_from_the_server_passes_through() -> None:
    with pytest.raises(ClientError, match="429"):
        _log_in(_Server([429]), _Time())


def test_a_malformed_answer_is_reported_without_quoting_it() -> None:
    def garbled(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(201, json={"device_code": "SECRETMARKER"})

    with pytest.raises(ClientError, match="malformed payload") as raised:
        _log_in(garbled, _Time())

    assert "SECRETMARKER" not in str(raised.value)


@pytest.mark.parametrize("name", ["a/b", "..", "a b"])
def test_a_bad_name_is_refused_before_any_request(name: str) -> None:
    server = _Server([_approved()])

    with pytest.raises(ClientError, match="invalid profile name"):
        _log_in(server, _Time(), url="https://trackinizer.example", profile=name)

    assert server.requests == []


def test_an_unusable_url_is_refused_before_any_request() -> None:
    server = _Server([_approved()])

    with pytest.raises(ClientError, match="invalid URL"):
        _log_in(server, _Time(), url="trackinizer.example")

    assert server.requests == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
