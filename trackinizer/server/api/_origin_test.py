"""Same-origin: permissive for a client with no browser headers, strict for a shell."""

from __future__ import annotations

from fastapi import HTTPException
from starlette.requests import HTTPConnection, Request

import pytest

from trackinizer.server.api._origin import (
    is_own_page_write,
    is_same_origin,
    is_strictly_same_origin,
    require_own_page_write,
)


def _request(*, method: str = "GET", **headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        },
    )


_SIBLING = {"host": "a.test", "origin": "https://b.a.test"}


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_a_read_from_another_page_is_not_refused(method: str) -> None:
    require_own_page_write(_request(method=method, **_SIBLING))


def test_a_socket_handshake_has_no_method_and_is_not_refused() -> None:
    require_own_page_write(HTTPConnection({"type": "websocket", "headers": []}))


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_write_from_another_page_is_refused_and_one_from_this_site_is_not(
    method: str,
) -> None:
    with pytest.raises(HTTPException) as refused:
        require_own_page_write(_request(method=method, **_SIBLING))
    require_own_page_write(
        _request(method=method, host="a.test", origin="https://a.test"),
    )

    assert (refused.value.status_code, refused.value.detail) == (
        403,
        "A write signed in by a session cookie must come from this site's pages.",
    )


@pytest.mark.parametrize(
    ("headers", "permissive", "strict"),
    [
        ({"host": "a.test"}, True, False),
        ({"host": "a.test", "origin": "https://a.test"}, True, True),
        ({"host": "a.test", "origin": "https://evil.test"}, False, False),
        ({"host": "a.test", "origin": "https://["}, False, False),
        ({"host": "a.test", "referer": "https://a.test/x"}, True, False),
        ({"host": "a.test", "referer": "https://evil.test/x"}, False, False),
        ({"origin": "https://a.test"}, False, False),
    ],
)
def test_origin_rules(
    headers: dict[str, str],
    permissive: bool,
    strict: bool,
) -> None:
    request = _request(**headers)

    assert is_same_origin(request) is permissive
    assert is_strictly_same_origin(request) is strict


@pytest.mark.parametrize(
    ("headers", "own"),
    [
        ({"host": "a.test", "origin": "https://a.test"}, True),
        (
            {
                "host": "a.test",
                "origin": "https://a.test",
                "sec-fetch-site": "same-origin",
            },
            True,
        ),
        # A sibling host of the same site: ``SameSite=Lax`` sent its form the cookie.
        ({"host": "a.test", "origin": "https://b.a.test"}, False),
        (
            {
                "host": "a.test",
                "origin": "https://a.test",
                "sec-fetch-site": "same-site",
            },
            False,
        ),
        (
            {
                "host": "a.test",
                "origin": "https://a.test",
                "sec-fetch-site": "cross-site",
            },
            False,
        ),
        (
            {"host": "a.test", "origin": "https://a.test", "sec-fetch-site": "none"},
            False,
        ),
        ({"host": "a.test", "referer": "https://a.test/x"}, False),
        ({"host": "a.test"}, False),
    ],
)
def test_a_write_is_the_own_pages_only_by_its_origin_and_fetch_site(
    headers: dict[str, str],
    own: bool,
) -> None:
    assert is_own_page_write(_request(**headers)) is own


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
