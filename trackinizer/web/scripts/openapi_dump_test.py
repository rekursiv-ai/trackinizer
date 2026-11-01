"""What the dump leaves out of a served schema: a deployment's addons, nothing else."""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.web.scripts.openapi_dump import core_schema, render


if TYPE_CHECKING:
    from treekle import MutablePlainTree


def test_a_servers_own_schema_comes_back_unchanged() -> None:
    assert core_schema(_server()) == _server()


def test_an_addons_routes_and_the_components_only_they_use_are_left_out() -> None:
    # ``Row`` is the server's too, so it stays; ``Board`` and what it reaches go.
    served = _schema(
        {
            **_paths(),
            "/api/addons/board/leaderboards": _route("Board"),
            "/api/addons/board/results": _route("Row"),
        },
        schemas={
            **_schemas(),
            "Board": {"properties": {"metric": _ref("Metric")}},
            "Metric": {"type": "object"},
        },
    )

    assert core_schema(served) == _server()


def test_a_route_of_the_servers_own_is_kept_whatever_it_uses() -> None:
    changed = _schema(
        {**_paths(), "/api/inquiries/{id}": _route("Row")},
        schemas=_schemas(),
    )

    assert core_schema(changed) == changed


def test_operations_left_out_go_with_the_components_only_they_use() -> None:
    # A path keeps the methods not left out; ``Board`` goes, ``Row`` is the server's.
    served = _schema(
        {
            **_paths(),
            "/api/boards": _route("Board"),
            "/api/inquiries": {**_route("Page"), "post": _route("Row")["get"]},
        },
        schemas={**_schemas(), "Board": {"properties": {"row": _ref("Row")}}},
    )

    core = core_schema(
        served,
        left_out=frozenset({("/api/boards", "get"), ("/api/inquiries", "post")}),
    )

    assert core == _server()


def test_a_schema_without_components_gains_none() -> None:
    bare: dict[str, MutablePlainTree] = {
        "paths": {"/api/inquiries": {}, "/api/addons/board/x": {}},
    }

    assert core_schema(bare) == {"paths": {"/api/inquiries": {}}}


def test_a_schema_renders_with_sorted_keys_one_space_indent_and_a_final_newline() -> (
    None
):
    # The build's hash and the live server's are of this text, so both sides must
    # write it byte for byte alike.
    assert render({"b": 1, "a": {"c": [2]}}) == (
        '{\n "a": {\n  "c": [\n   2\n  ]\n },\n "b": 1\n}\n'
    )


def _server() -> dict[str, MutablePlainTree]:
    """Return a server's own schema: no addon routes."""
    return _schema(_paths(), schemas=_schemas())


def _paths() -> dict[str, MutablePlainTree]:
    """Return the server's routes; the listing of addons is one of them."""
    return {"/api/addons": _route("Addons"), "/api/inquiries": _route("Page")}


def _schemas() -> dict[str, MutablePlainTree]:
    """Return the server's components; one nothing uses."""
    return {
        "Addons": {"type": "object"},
        "Page": {"properties": {"row": _ref("Row")}},
        "Row": {"type": "object"},
        "Unused": {"type": "object"},
    }


def _schema(
    paths: dict[str, MutablePlainTree],
    *,
    schemas: dict[str, MutablePlainTree],
) -> dict[str, MutablePlainTree]:
    return {
        "info": {"title": "trackinizer"},
        "paths": paths,
        "components": {"schemas": schemas},
    }


def _route(name: str) -> dict[str, MutablePlainTree]:
    return {"get": {"responses": {"200": {"content": {"j": {"schema": _ref(name)}}}}}}


def _ref(name: str) -> dict[str, MutablePlainTree]:
    return {"$ref": f"#/components/schemas/{name}"}


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
