"""Server routes must match the wire route table exactly.

``wire/routes.py`` is the single API definition; ``api/edit.py`` registers
handlers by iterating it. This test fails if the two ever diverge -- if a
handler is registered for a (field, verb) the table does not declare, or
the table declares one the server did not register. It is the guard that
makes server/table drift impossible, so the same table can drive the
client and the generated doc.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from fastapi import FastAPI

from trackinizer.server import web
from trackinizer.server.api.app import app
from trackinizer.server.api.submit import SUBMIT_BODY
from trackinizer.server.route_iter import (
    iter_routes,
    registered_paths,
)
from trackinizer.server.store.core import Store
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.routes import (
    edge_field_path,
    edge_field_routes,
    inquiry_field_path,
    inquiry_field_routes,
)
from trackinizer.wire.wire_metrics import METRICS_API_PATHS
from trackinizer.wire.wire_metrics_query import (
    METRICS_QUERY_API_PATHS,
)
from trackinizer.wire.wire_sessions import SESSION_API_PATHS


# Field-mutation routes are exactly the PUT/PATCH/DELETE verbs under the
# per-field prefix. GET reads under the same prefix (``/cost``,
# ``/proves_belief``) are query routes, not part of the field-mutation
# table, so they are excluded here.
_MUTATING = frozenset({"PUT", "PATCH", "DELETE"})


def _registered_mutations_under(prefix: str) -> set[tuple[str, str]]:
    """``(path, method)`` for every registered mutation under ``prefix``."""
    out: set[tuple[str, str]] = set()
    for path, methods in iter_routes(app):
        if not path.startswith(prefix) or path.count("/") != prefix.count("/"):
            continue
        out.update((path, method) for method in methods & _MUTATING)
    return out


def _registered_field_verbs() -> set[tuple[str, str]]:
    """``(path, method)`` for every registered inquiry-field mutation.

    Kind-specific fields route under their owning kind
    (``/api/<kind>/{target_id}/<field>``); base fields and cost axes stay
    under ``/api/inquiries``. Scan every prefix the wire table declares so
    the registered set spans all kind-scoped routes.
    """
    prefixes = {
        inquiry_field_path(route.column).rsplit("/", 1)[0] + "/"
        for route in inquiry_field_routes()
    }
    out: set[tuple[str, str]] = set()
    for prefix in prefixes:
        out |= _registered_mutations_under(prefix)
    return out


def _expected_field_verbs() -> set[tuple[str, str]]:
    """``(path, method)`` the wire table declares for inquiry fields."""
    out: set[tuple[str, str]] = set()
    for route in inquiry_field_routes():
        path = inquiry_field_path(route.column)
        if route.put:
            out.add((path, "PUT"))
        if route.patch:
            out.add((path, "PATCH"))
        if route.delete:
            out.add((path, "DELETE"))
    return out


def test_server_field_routes_match_wire_table() -> None:
    registered = _registered_field_verbs()
    expected = _expected_field_verbs()
    assert registered == expected, (
        "server inquiry-field routes drifted from wire/routes.py:\n"
        f"  registered but not in table: {sorted(registered - expected)}\n"
        f"  in table but not registered: {sorted(expected - registered)}"
    )


def test_every_field_route_method_resolves_on_store() -> None:
    """Each generated setter name must exist as a callable on ``Store``.

    The wire table derives ``set_<column>`` / ``add_<stem>`` /
    ``remove_<stem>`` names; ``edit.py`` dispatches them via ``getattr``.
    A declared field with no backing method 500s at request time (the
    rooms-field regression). This binds the table to the implementation
    so a missing setter fails at import-time test, not in production.
    """
    missing: list[str] = []
    for route in inquiry_field_routes():
        for method_name in (route.set_method, route.add_method, route.sub_method):
            if method_name is None:
                continue
            if not callable(getattr(Store, method_name, None)):
                missing.append(f"{route.column} -> Store.{method_name}")
    assert not missing, f"field routes with no backing Store method: {sorted(missing)}"


def test_required_fields_have_no_delete() -> None:
    """Required columns (title, status) expose no DELETE -- regression F8."""
    delete_paths = {p for p, m in _expected_field_verbs() if m == "DELETE"}
    for required in ("title", "status"):
        assert inquiry_field_path(required) not in delete_paths


def test_cost_axes_expose_put_patch_delete() -> None:
    """Both cost axes expose all three mutating verbs -- regression TAPI-001."""
    expected = _expected_field_verbs()
    for axis in ("marginal_cost_agent_usd", "marginal_cost_resource_usd"):
        path = inquiry_field_path(axis)
        assert {(path, "PUT"), (path, "PATCH"), (path, "DELETE")} <= expected


def _expected_edge_field_verbs() -> set[tuple[str, str]]:
    """``(path, method)`` the wire table declares for edge annotations."""
    out: set[tuple[str, str]] = set()
    for route in edge_field_routes():
        path = edge_field_path(route.column)
        out.add((path, "PUT"))
        out.add((path, "DELETE"))
        if route.patch:
            out.add((path, "PATCH"))
    return out


def test_server_edge_field_routes_match_wire_table() -> None:
    registered = _registered_mutations_under(
        "/api/edges/{from_id}/{edge_kind}/{to_id}/"
    )
    expected = _expected_edge_field_verbs()
    assert registered == expected, (
        "server edge-field routes drifted from wire/routes.py:\n"
        f"  registered but not in table: {sorted(registered - expected)}\n"
        f"  in table but not registered: {sorted(expected - registered)}"
    )


def test_session_api_paths_are_registered_routes() -> None:
    """Every hand-registered session/messaging/feed/version route exists.

    These routes are not derived from the inquiry-field table, so a rename
    would silently break the client / SPA / deploy probe with no drift signal.
    ``SESSION_API_PATHS`` is their single registry; this asserts each appears
    on the live app, the analogue of the field-route drift test for the
    unmanaged route family.
    """
    # The feed route lives on the ``--web`` SPA surface, the others on the API
    # app; attach the SPA so the registered set spans both, as the live server
    # does (mirrors ``assets_drift_test``).
    full_app = FastAPI()
    for route in app.routes:
        full_app.router.routes.append(route)
    web.attach(full_app)
    registered = registered_paths(full_app)
    missing = sorted(p for p in SESSION_API_PATHS if p not in registered)
    assert not missing, (
        "session-family routes in SESSION_API_PATHS with no registered route:\n"
        + "\n".join(missing)
    )


def test_metrics_api_paths_are_registered_routes() -> None:
    """Every hand-registered experiment-metric route exists on the live app.

    Like ``SESSION_API_PATHS``, ``METRICS_API_PATHS`` is a hand-maintained
    registry (the metrics routes are not derived from the inquiry-field
    table), so this is its drift guard: a rename that misses one surface
    would otherwise break the client / SPA silently.
    """
    registered = registered_paths(app)
    all_metric_paths = (*METRICS_API_PATHS, *METRICS_QUERY_API_PATHS)
    missing = sorted(p for p in all_metric_paths if p not in registered)
    assert not missing, "metrics-family routes with no registered route:\n" + "\n".join(
        missing
    )


def test_session_api_paths_are_documented() -> None:
    """Every session-family route appears in ``docs/api.md``.

    The doc is a hand-maintained contract consumer (api.md:1 claims to be
    canonical). Without this gate the session/messaging/feed/version routes
    drifted out of the doc entirely. The literal-segment check tolerates the
    doc's ``<uuid>`` spelling vs. the route template's ``{session_id}``.
    """
    api_md = (Path(__file__).resolve().parents[2] / "docs" / "api.md").read_text()
    # Reduce each route template to the literal segments around the path
    # parameter, then assert each survives in the doc text.
    missing: list[str] = []
    for path in SESSION_API_PATHS:
        segments = [seg for seg in path.split("/") if seg and not seg.startswith("{")]
        if not all(seg in api_md for seg in segments):
            missing.append(path)
    assert not missing, f"routes absent from docs/api.md: {sorted(missing)}"


def test_submit_tokens_are_kind_lowercased() -> None:
    """The submit URL token is exactly ``kind.lower()`` for every kind.

    One canonical lowercase spelling per kind across the URL, the CLI
    verb, and the storage prefix -- no snake_case split, no hand table.
    """
    kinds = set(get_args(Inquiry.InquiryKind.__value__))
    assert set(SUBMIT_BODY) == {kind.lower() for kind in kinds}


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
