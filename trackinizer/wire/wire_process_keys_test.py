"""The routes a process key may call are spelt as the wire's own path constants are."""

from __future__ import annotations

from uuid import UUID

from trackinizer.wire.wire_process_keys import PROCESS_KEY_ROUTES, WEBSOCKET
from trackinizer.wire.wire_session_ir import (
    session_parts_path,
    session_records_path,
)
from trackinizer.wire.wire_session_relay import SESSION_PROCESS_WS_PATH
from trackinizer.wire.wire_terminals import (
    TERMINAL_HOSTS_CHANGES_PATH,
    TERMINAL_HOSTS_PATH,
)
from trackinizer.wire.wire_tools import TOOLS_WS_PATH


def test_each_route_is_the_one_its_client_dials() -> None:
    """Written out so ``server.auth`` imports no session wire; a drift fails here."""
    session = UUID(int=7)
    filled = {
        (method, path.format(session_id=session))
        for method, path in PROCESS_KEY_ROUTES
        if "{session_id}" in path
    }

    assert filled == {
        ("GET", session_parts_path(session)),
        ("GET", session_records_path(session)),
        ("POST", session_records_path(session)),
        (WEBSOCKET, SESSION_PROCESS_WS_PATH.format(session_id=session)),
    }
    assert {
        ("GET", TERMINAL_HOSTS_PATH),
        ("GET", TERMINAL_HOSTS_CHANGES_PATH),
        (WEBSOCKET, TOOLS_WS_PATH),
        ("POST", "/api/messages"),
    } == PROCESS_KEY_ROUTES - {
        (method, path) for method, path in PROCESS_KEY_ROUTES if "{session_id}" in path
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
