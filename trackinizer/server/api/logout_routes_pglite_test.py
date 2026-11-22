"""A logout ends the session over PGlite: a copy of its cookie opens nothing after.

The HTTP routes are driven through the whole app. A WebSocket is resolved as its route
resolves it, by ``request_user`` over a Starlette ``WebSocket``, since the test client
cannot share the session event loop that owns the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException
from starlette.websockets import WebSocket

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import TERMINAL_HOST
from trackinizer.server.auth import request_user


if TYPE_CHECKING:
    from starlette.types import Message

    from trackinizer.server.api.conftest import TerminalWorld


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("cheap_scrypt"),
]

_PROFILE = "/api/me/profile"


async def test_a_copied_cookie_opens_no_route_or_socket_after_logout(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@example.com")
    cookie = world.cookie(ada)
    other = world.cookie(await world.person("bob@example.com"))
    assert (await world.call("GET", _PROFILE, headers=world.browser(cookie))).is_success

    logged_out = await world.call("POST", "/auth/logout", headers=world.browser(cookie))

    assert logged_out.status_code == 302
    after = await world.call("GET", _PROFILE, headers=world.browser(cookie))
    assert after.status_code == 401
    with pytest.raises(HTTPException) as refused:
        await request_user(_socket(cookie))
    assert refused.value.status_code == 401
    # Another person's session is theirs: one logout ends one person's.
    assert (await world.call("GET", _PROFILE, headers=world.browser(other))).is_success


async def test_a_sign_in_after_logout_opens_and_a_stale_copy_cannot_end_it(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@example.com")
    old = world.cookie(ada)
    await world.call("POST", "/auth/logout", headers=world.browser(old))
    new = world.cookie(ada, generation=1)

    again = await world.call("POST", "/auth/logout", headers=world.browser(old))

    assert again.status_code == 302
    assert (await world.call("GET", _PROFILE, headers=world.browser(new))).is_success
    assert (await request_user(_socket(new))).user_id == ada


async def test_a_logout_leaves_the_persons_api_keys_live(world: TerminalWorld) -> None:
    ada = await world.person("ada@example.com")
    key = await world.key(ada)
    cookie = world.cookie(ada)

    await world.call("POST", "/auth/logout", headers=world.browser(cookie))

    keyed = await world.call(
        "GET",
        _PROFILE,
        headers={"Authorization": f"Bearer {key}"},
    )
    assert keyed.is_success


def _socket(cookie: str) -> WebSocket:
    """Return a WebSocket that a signed-in browser opens with ``cookie``."""

    async def nothing() -> Message:
        return {"type": "websocket.disconnect"}

    async def drop(message: Message) -> None:
        del message

    return WebSocket(
        {
            "type": "websocket",
            "app": app,
            "path": "/ws",
            "query_string": b"",
            "headers": [
                (b"host", TERMINAL_HOST.encode()),
                (b"cookie", cookie.encode()),
            ],
        },
        nothing,
        drop,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
