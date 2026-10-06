"""Signed-cookie payload validation at the authentication boundary."""

from uuid import uuid4

from itsdangerous import URLSafeTimedSerializer

import pytest

from trackinizer.server.session import (
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    read_oauth_state_cookie,
    read_session_cookie,
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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
