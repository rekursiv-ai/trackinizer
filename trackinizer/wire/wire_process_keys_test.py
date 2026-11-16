"""The routes a process key may call are spelt as the wire's own path constants are."""

from __future__ import annotations

from uuid import UUID

from trackinizer.wire.wire_capture import CAPTURES_PATH
from trackinizer.wire.wire_process_keys import PROCESS_KEY_ROUTES, WEBSOCKET
from trackinizer.wire.wire_session_ir import (
    session_parts_path,
    session_records_path,
)
from trackinizer.wire.wire_session_relay import (
    SESSION_LINES_PATH,
    SESSION_PROCESS_PATH,
    SESSION_PROCESS_WS_PATH,
)
from trackinizer.wire.wire_sessions import SESSION_START_PATH
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
        ("GET", SESSION_PROCESS_PATH.format(session_id=session)),
        ("PUT", SESSION_PROCESS_PATH.format(session_id=session)),
        ("DELETE", SESSION_PROCESS_PATH.format(session_id=session)),
        ("GET", SESSION_LINES_PATH.format(session_id=session)),
    }
    assert {
        ("GET", TERMINAL_HOSTS_PATH),
        ("GET", TERMINAL_HOSTS_CHANGES_PATH),
        (WEBSOCKET, TOOLS_WS_PATH),
        ("POST", "/api/messages"),
        ("GET", "/api/canvases/{canvas_id}"),
        ("POST", "/api/canvases/{canvas_id}/operations"),
        ("POST", CAPTURES_PATH),
        ("POST", SESSION_START_PATH),
        ("GET", "/api/inquiries/{kind}/{seq}"),
        ("GET", "/api/inquiries/{target_id}"),
        ("GET", "/api/me/preferences/agent"),
        ("GET", "/api/workspaces"),
        ("PATCH", "/api/workspaces/{workspace_id}"),
        ("GET", "/api/canvases"),
        ("PUT", "/api/canvases/{canvas_id}/name"),
        ("GET", "/api/addons/knowhost/hosts"),
        ("POST", "/api/addons/knowhost/hosts"),
        ("POST", "/api/addons/knowhost/hosts/{host_id}/pause"),
        ("POST", "/api/addons/knowhost/hosts/{host_id}/resume"),
        ("GET", "/api/addons/knowspend/limits"),
        ("PUT", "/api/addons/knowspend/limits"),
    } == PROCESS_KEY_ROUTES.keys() - {
        (method, path) for method, path in PROCESS_KEY_ROUTES if "{session_id}" in path
    }
    assert {
        route for route, scope in PROCESS_KEY_ROUTES.items() if scope == "descendant"
    } == {
        ("GET", SESSION_PROCESS_PATH),
        ("PUT", SESSION_PROCESS_PATH),
        ("DELETE", SESSION_PROCESS_PATH),
        ("GET", SESSION_LINES_PATH),
        ("GET", "/api/inquiries/{target_id}"),
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
