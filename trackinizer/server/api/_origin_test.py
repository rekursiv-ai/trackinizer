"""Same-origin: permissive for a client with no browser headers, strict for a shell."""

from __future__ import annotations

from starlette.requests import Request

import pytest

from trackinizer.server.api._origin import (
    is_same_origin,
    is_strictly_same_origin,
)


def _request(**headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        },
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


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
