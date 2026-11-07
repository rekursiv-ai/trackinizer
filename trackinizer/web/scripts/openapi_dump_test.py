"""What the dump leaves out of a served schema, and that the committed dump is current.

The web app is typed against the committed dump, so a server change that alters
the contract (a route, a parameter, a body) fails here, in Python CI, instead of
shipping a client typed against routes that no longer exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import difflib
import hashlib
import json
import sys

import httpx2
import pytest

from trackinizer.web.scripts import openapi_dump
from trackinizer.web.scripts.openapi_dump import (
    OPENAPI_JSON,
    core_schema,
    render,
    web_app_schema,
)


if TYPE_CHECKING:
    from pathlib import Path

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


def test_a_reference_inside_a_list_keeps_its_component() -> None:
    served = _schema(
        {
            **_paths(),
            "/api/either": {
                "get": {"schema": {"anyOf": [_ref("Row"), _ref("Either")]}},
            },
        },
        schemas={**_schemas(), "Either": {"type": "object"}},
    )
    assert core_schema(served) == served


def test_main_dumps_a_running_servers_schema_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[tuple[str, float]] = []

    def get(url: str, *, timeout: float) -> httpx2.Response:
        asked.append((url, timeout))
        return httpx2.Response(
            200,
            json=_server(),
            request=httpx2.Request("GET", url),
        )

    monkeypatch.setattr(httpx2, "get", get)
    monkeypatch.setattr(
        sys,
        "argv",
        ["openapi_dump", "--url", "http://127.0.0.1:8765/", "--output", "-"],
    )

    assert openapi_dump.main() == 0

    out, err = capsys.readouterr()
    assert asked == [("http://127.0.0.1:8765/openapi.json", 30.0)]
    assert json.loads(out) == _server()
    assert err == f"openapi_sha256 {hashlib.sha256(out.encode()).hexdigest()}\n"


# Generating the whole app's schema takes 0.14 s on the dev Mac, over the 100 ms
# unit budget, so this runs in the slow tier, which CI runs on every pull request.
@pytest.mark.compute_large_fixture
def test_main_writes_the_in_repo_apps_schema_as_committed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    written = tmp_path / "openapi.json"
    monkeypatch.setattr(sys, "argv", ["openapi_dump", "--output", str(written)])

    assert openapi_dump.main() == 0

    assert written.read_text() == OPENAPI_JSON.read_text()


@pytest.mark.compute_large_fixture
def test_committed_schema_matches_the_app() -> None:
    committed = OPENAPI_JSON.read_text()
    current = render(web_app_schema())
    diff = difflib.unified_diff(
        committed.splitlines(keepends=True),
        current.splitlines(keepends=True),
        fromfile="committed",
        tofile="app",
    )
    assert committed == current, (
        f"{OPENAPI_JSON} differs from the server's schema. Regenerate it with\n"
        "  trackinizer/web/scripts/openapi_dump.py\n"
        "then run `./npm run typecheck` in trackinizer/web.\n" + "".join(diff)[:4000]
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
