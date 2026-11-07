"""Signed-cookie payload validation at the authentication boundary."""

from collections.abc import Callable
from typing import Literal
from uuid import uuid4

from fastapi import Response
from itsdangerous import URLSafeTimedSerializer

import pytest

from trackinizer.server.session import (
    OAUTH_STATE_COOKIE_NAME,
    OAUTH_STATE_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
    clear_oauth_state_cookie,
    clear_session_cookie,
    issue_oauth_state,
    read_oauth_state_cookie,
    read_session_cookie,
    set_session_cookie,
)


@pytest.mark.parametrize("payload", [{}, [], {"user_id": 3}, None])
def test_session_rejects_unknown_signed_payload(payload: object) -> None:
    secret = uuid4().hex
    signed = URLSafeTimedSerializer(secret, salt="trackinizer.session.v1").dumps(
        payload,
    )
    assert (
        read_session_cookie(
            {SESSION_COOKIE_NAME: signed},
            secret=secret,
            max_age_seconds=60,
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [{}, [], {"state": 3, "next": "/"}, {"state": "s", "next": 3}, None],
)
def test_oauth_rejects_unknown_signed_payload(payload: object) -> None:
    secret = uuid4().hex
    signed = URLSafeTimedSerializer(secret, salt="trackinizer.oauth_state.v1").dumps(
        payload,
    )
    assert (
        read_oauth_state_cookie({OAUTH_STATE_COOKIE_NAME: signed}, secret=secret)
        is None
    )


_ATTRIBUTES = {"httponly": True, "secure": True, "samesite": "lax", "path": "/"}


class _Response:
    """Keeps every cookie set and deleted, as a browser would read the headers."""

    def __init__(self) -> None:
        self.set: dict[str, tuple[str, dict[str, object]]] = {}
        self.deleted: dict[str, dict[str, object]] = {}

    def set_cookie(
        self,
        key: str,
        value: str = "",
        *,
        max_age: int | None = None,
        httponly: bool = False,
        secure: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
        path: str | None = "/",
    ) -> None:
        self.set[key] = (
            value,
            {
                "max_age": max_age,
                "httponly": httponly,
                "secure": secure,
                "samesite": samesite,
                "path": path,
            },
        )

    def delete_cookie(
        self,
        key: str,
        *,
        path: str = "/",
        httponly: bool = False,
        secure: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
    ) -> None:
        self.deleted[key] = {
            "httponly": httponly,
            "secure": secure,
            "samesite": samesite,
            "path": path,
        }

    def jar(self) -> dict[str, str]:
        return {key: value for key, (value, _) in self.set.items()}


class TestSessionCookie:
    def test_a_set_cookie_reads_back_its_user_under_its_secret_only(self) -> None:
        secret = uuid4().hex
        response = _Response()

        set_session_cookie(response, user_id="u-1", secret=secret, max_age_seconds=60)

        _, attributes = response.set[SESSION_COOKIE_NAME]
        assert attributes == {"max_age": 60, **_ATTRIBUTES}
        jar = response.jar()
        assert read_session_cookie(jar, secret=secret, max_age_seconds=60) == "u-1"
        assert read_session_cookie(jar, secret=uuid4().hex, max_age_seconds=60) is None
        replayed = {OAUTH_STATE_COOKIE_NAME: jar[SESSION_COOKIE_NAME]}
        assert read_oauth_state_cookie(replayed, secret=secret) is None

    def test_no_cookie_is_no_session(self) -> None:
        assert read_session_cookie({}, secret=uuid4().hex, max_age_seconds=60) is None
        assert (
            read_session_cookie(
                {SESSION_COOKIE_NAME: ""},
                secret=uuid4().hex,
                max_age_seconds=60,
            )
            is None
        )

    def test_clearing_expires_it_with_the_attributes_it_was_set_with(self) -> None:
        response = _Response()

        clear_session_cookie(response)

        assert response.deleted == {SESSION_COOKIE_NAME: _ATTRIBUTES}


class TestOAuthStateCookie:
    def test_an_issued_state_reads_back_its_state_and_next_url(self) -> None:
        secret = uuid4().hex
        response = _Response()

        issue_oauth_state(response, state="nonce", next_url="/inbox", secret=secret)

        _, attributes = response.set[OAUTH_STATE_COOKIE_NAME]
        assert attributes == {"max_age": OAUTH_STATE_MAX_AGE_SECONDS, **_ATTRIBUTES}
        jar = response.jar()
        assert read_oauth_state_cookie(jar, secret=secret) == ("nonce", "/inbox")
        assert read_oauth_state_cookie(jar, secret=uuid4().hex) is None

    @pytest.mark.parametrize(("state", "next_url"), [("", "/inbox"), ("nonce", "")])
    def test_a_blank_state_or_next_url_is_no_state(
        self,
        state: str,
        next_url: str,
    ) -> None:
        secret = uuid4().hex
        response = _Response()

        issue_oauth_state(response, state=state, next_url=next_url, secret=secret)

        assert read_oauth_state_cookie(response.jar(), secret=secret) is None

    def test_no_cookie_is_no_state(self) -> None:
        assert read_oauth_state_cookie({}, secret=uuid4().hex) is None

    def test_clearing_expires_it(self) -> None:
        response = _Response()

        clear_oauth_state_cookie(response)

        assert response.deleted == {OAUTH_STATE_COOKIE_NAME: _ATTRIBUTES}


def test_issued_session_cookie_round_trips_with_secure_attributes() -> None:
    response = Response()
    secret = uuid4().hex
    user = str(uuid4())
    set_session_cookie(response, user_id=user, secret=secret, max_age_seconds=120)
    header = response.headers["set-cookie"]
    raw = header.split(";", 1)[0].split("=", 1)[1]
    assert (
        read_session_cookie(
            {SESSION_COOKIE_NAME: raw},
            secret=secret,
            max_age_seconds=120,
        )
        == user
    )
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=120"):
        assert attribute in header
    assert read_session_cookie({}, secret=secret, max_age_seconds=120) is None


def test_issued_oauth_state_round_trips_without_becoming_a_session() -> None:
    response = Response()
    secret = uuid4().hex
    issue_oauth_state(response, state="nonce", next_url="/trax/?q=x", secret=secret)
    header = response.headers["set-cookie"]
    raw = header.split(";", 1)[0].split("=", 1)[1]
    assert read_oauth_state_cookie(
        {OAUTH_STATE_COOKIE_NAME: raw},
        secret=secret,
    ) == ("nonce", "/trax/?q=x")
    assert (
        read_session_cookie(
            {SESSION_COOKIE_NAME: raw},
            secret=secret,
            max_age_seconds=120,
        )
        is None
    )
    for attribute in (
        "HttpOnly",
        "Secure",
        "SameSite=lax",
        "Path=/",
        f"Max-Age={OAUTH_STATE_MAX_AGE_SECONDS}",
    ):
        assert attribute in header
    assert read_oauth_state_cookie({}, secret=secret) is None


@pytest.mark.parametrize("clear", [clear_session_cookie, clear_oauth_state_cookie])
def test_cleared_cookie_expires_with_the_same_security_attributes(
    clear: Callable[[Response], None],
) -> None:
    response = Response()
    clear(response)
    header = response.headers["set-cookie"]
    name = (
        SESSION_COOKIE_NAME
        if clear is clear_session_cookie
        else OAUTH_STATE_COOKIE_NAME
    )
    assert header.startswith(f'{name}="";')
    for attribute in ("Max-Age=0", "HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert attribute in header


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
