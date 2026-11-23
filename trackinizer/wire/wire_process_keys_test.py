"""The routes a process key may call are spelt as the wire's own path constants are."""

from __future__ import annotations

from uuid import UUID

import pytest

from trackinizer.wire.routes import edge_field_path, inquiry_field_path
from trackinizer.wire.wire_capture import CAPTURES_PATH
from trackinizer.wire.wire_job import JOBS_PATH
from trackinizer.wire.wire_process_keys import (
    PROCESS_KEY_ROUTES,
    RECORD_WRITE_ROUTES,
    RECORDS_ROUTES,
    WEBSOCKET,
    _route_union,
    listed_route,
)
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
        ("GET", "/api/addons/knowspend/campaigns"),
        ("GET", "/api/addons/knowspend/today"),
        ("POST", "/api/addons/campaignscores/iterations/{seq}"),
        ("GET", JOBS_PATH),
        ("GET", "/api/inquiries"),
        ("GET", "/api/inquiries/next_issue"),
        ("GET", "/api/inquiries/{target_id}/cost"),
        ("GET", "/api/inquiries/{target_id}/confidence"),
        ("GET", "/api/inquiries/{target_id}/authority"),
        ("GET", "/api/web/lookup/{target_id}"),
        ("GET", "/api/web/get/{target_id}"),
        ("GET", "/api/web/recent_changes"),
        ("GET", "/api/web/search_sessions"),
        ("GET", "/api/web/search_inquiries"),
        ("GET", "/api/version"),
    } == PROCESS_KEY_ROUTES.keys() - RECORD_WRITE_ROUTES.keys() - {
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


def test_the_records_routes_keep_their_scope_in_the_one_list() -> None:
    """A merge must never widen a key: a session's records stay its own tree's."""
    assert {route: PROCESS_KEY_ROUTES.get(route) for route in RECORDS_ROUTES} == dict(
        RECORDS_ROUTES,
    )
    assert RECORDS_ROUTES[("GET", _records_template())] == "tree"
    assert {method for method, _ in RECORDS_ROUTES} == {"GET"}


def test_the_record_writes_are_the_routes_trax_s_write_verbs_call_and_no_more() -> None:
    """Spelt as the wire's field and edge routes are, each scoped by its own route."""
    edge = "/api/edges/{from_id}/{edge_kind}/{to_id}"

    assert dict(RECORD_WRITE_ROUTES) == dict.fromkeys(
        {
            ("POST", "/api/inquiries/batch"),
            ("POST", "/api/inquiries/lookup"),
            ("POST", "/api/inquiries/next_issue"),
            ("DELETE", "/api/inquiries/{target_id}"),
            *(
                ("PUT", inquiry_field_path(field))
                for field in (
                    "title",
                    "status",
                    "owner",
                    "description",
                    "app",
                    "priority",
                    "validation",
                    "judgement",
                )
            ),
            ("PATCH", inquiry_field_path("labels")),
            ("PATCH", inquiry_field_path("marginal_cost_agent_usd")),
            ("POST", edge),
            ("DELETE", edge),
            ("PUT", edge_field_path("labels")),
        },
        "account",
    )
    assert {route: PROCESS_KEY_ROUTES.get(route) for route in RECORD_WRITE_ROUTES} == (
        dict(RECORD_WRITE_ROUTES)
    )


def test_the_record_writes_reach_no_admin_or_org_route() -> None:
    """A row's account and session id, and the org's settings, stay off."""
    reached = [
        route
        for route in PROCESS_KEY_ROUTES
        if route[1].startswith(("/api/variables", "/api/export", "/api/admin"))
        or route[1].endswith(("/account", "/cli_session_id", "/metrics/write"))
    ]

    assert reached == []


def test_joining_a_list_that_scopes_a_route_apart_raises() -> None:
    route = ("GET", "/api/sessions/{session_id}/records")

    joined = _route_union({route: "tree"}, {route: "tree", ("GET", "/x"): "account"})
    with pytest.raises(
        ValueError,
        match=r"^GET /api/sessions/\{session_id\}/records is listed as tree and as "
        r"account$",
    ):
        _route_union({route: "tree"}, {route: "account"})

    assert dict(joined) == {route: "tree", ("GET", "/x"): "account"}


def test_listed_route_picks_the_most_specific_template() -> None:
    routes = [
        ("GET", "/api/inquiries/{kind}/{seq}"),
        ("GET", "/api/inquiries/{target_id}"),
        ("GET", "/api/inquiries/next_issue"),
        ("GET", "/api/inquiries/{target_id}/cost"),
        ("POST", "/api/inquiries/next_issue"),
    ]
    uuid = "0b4c2c1e-0000-4000-8000-000000000001"

    assert [
        listed_route(method, path, routes)
        for method, path in (
            ("GET", "/api/inquiries/next_issue"),
            ("GET", f"/api/inquiries/{uuid}"),
            ("GET", "/api/inquiries/issue/7"),
            ("GET", f"/api/inquiries/{uuid}/cost"),
            ("POST", "/api/inquiries/next_issue"),
            ("POST", "/api/inquiries/lookup"),
            ("GET", "/api/inquiries"),
            ("GET", "/api/inquiries/issue/7/extra/more"),
        )
    ] == [
        ("GET", "/api/inquiries/next_issue"),
        ("GET", "/api/inquiries/{target_id}"),
        ("GET", "/api/inquiries/{kind}/{seq}"),
        ("GET", "/api/inquiries/{target_id}/cost"),
        ("POST", "/api/inquiries/next_issue"),
        None,
        None,
        None,
    ]


def test_a_parameter_names_no_empty_segment() -> None:
    routes = [("GET", "/api/inquiries/{target_id}")]

    assert [
        listed_route("GET", path, routes)
        for path in ("/api/inquiries/", "/api/inquiries/x")
    ] == [None, ("GET", "/api/inquiries/{target_id}")]


def _records_template() -> str:
    """Return the records route's template, as the session wire spells its path."""
    return session_records_path(UUID(int=0)).replace(str(UUID(int=0)), "{session_id}")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
