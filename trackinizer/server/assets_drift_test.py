"""The SPA's hardcoded API paths must match registered server routes.

``server/assets/index.html`` is a fourth consumer of the HTTP contract
(alongside the FastAPI server, the Python client, and the doc) but, being
static HTML, cannot import :mod:`wire.routes`. It spells every ``/api/...``
path as a literal ``fetch`` template. This test extracts those literals
and asserts each one matches a route registered on the full app (API
routers + the ``--web`` SPA surface), so a route rename that forgets the
SPA -- exactly the drift that 404'd ``#/list/Issue`` -- fails here instead
of in the browser.

Limitation: only paths spelled as a single ``/api/...`` literal are
checked. A path assembled by string concatenation (e.g. the edge-field
``${base}/${field}`` in ``annotateEdge``) is invisible to a static scan;
its prefix ``base`` is still covered via the bare edge literal.
"""

from __future__ import annotations

from pathlib import Path

import re

from fastapi import FastAPI

from trackinizer.server import web
from trackinizer.server.api.app import app
from trackinizer.server.route_iter import iter_routes


_ASSETS = Path(__file__).resolve().parent / "assets"
_INDEX_HTML = _ASSETS / "index.html"
_CONSOLE_HTML = _ASSETS / "console.html"
_ADMIN_HTML = _ASSETS / "admin.html"
_ME_HTML = _ASSETS / "me.html"
_GRAPH_HTML = _ASSETS / "graph.html"
# Every SPA page that issues ``/api/...`` literals must be drift-checked, not
# just index.html -- console / admin / me are each consumers of the contract.
_SPA_PAGES = (_INDEX_HTML, _CONSOLE_HTML, _ADMIN_HTML, _ME_HTML, _GRAPH_HTML)

# ``api(`/api/...`)`` and bare ``fetch("/api/...")`` / ``new EventSource(
# "/api/...")`` calls. Capture the path literal up to the first query (?),
# template-literal interpolation boundary, or closing quote/backtick.
_API_PATH_RE = re.compile(r"""["'`](/api/[^"'`?]*)["'`?]""")

# A path literal immediately followed (within the same ``api(...)`` / ``fetch``
# options object) by a ``method: "VERB"`` -- the (path, method) PAIR the SPA
# issues. A path with no nearby ``method`` defaults to GET. This catches a
# verb/route mismatch (e.g. SPA POSTs to a PUT route -> 405) that the
# path-only scan above misses.
_API_PATH_METHOD_RE = re.compile(
    r"""["'`](/api/[^"'`?]*)["'`?][\s\S]{0,160}?method\s*:\s*["'](\w+)["']""",
)


def _template_to_route(path: str) -> str:
    """Turn a JS template-literal path into a FastAPI route template.

    ``/api/inquiries/${id}/${field}`` -> ``/api/inquiries/{id}/{field}``;
    a trailing ``${...}`` segment becomes a single ``{param}``. Static
    paths pass through unchanged.
    """
    return re.sub(r"\$\{[^}]*\}", "{param}", path)


def _registered_path_templates(application: FastAPI) -> set[str]:
    """All route path templates on ``application``, params renamed to ``{param}``."""
    return {
        re.sub(r"\{[^}]*\}", "{param}", path)
        for path, _ in iter_routes(application)
        if path.startswith("/api/")
    }


def _spa_api_paths() -> set[str]:
    """Every distinct ``/api/...`` literal the SPA issues, as route templates.

    ``fieldPath`` builds a kind-scoped base fragment ``/api/${owner}`` that
    is concatenated with ``/${id}/${field}`` before any fetch -- it is
    never requested on its own. A bare ``/api/{param}`` fragment is such a
    base, not a fetched path, so it is excluded (the same concatenation
    limitation already documented for the edge-field ``${base}/${field}``).
    """
    raw: set[str] = set()
    for page in _SPA_PAGES:
        if page.is_file():
            raw |= {m.group(1) for m in _API_PATH_RE.finditer(page.read_text())}
    paths = {_template_to_route(p.rstrip("/")) for p in raw if p != "/api/"}
    return paths - {"/api/{param}"}


def test_spa_api_paths_are_registered_routes() -> None:
    spa_app = FastAPI()
    for route in app.routes:
        spa_app.router.routes.append(route)
    web.attach(spa_app)
    registered = _registered_path_templates(spa_app)
    spa_paths = _spa_api_paths()
    missing = sorted(p for p in spa_paths if p not in registered)
    assert not missing, (
        "an SPA page issues /api paths with no matching server route:\n"
        + "\n".join(missing)
        + "\n\nregistered:\n"
        + "\n".join(sorted(registered))
    )


def _registered_path_methods(application: FastAPI) -> set[tuple[str, str]]:
    """All ``(path_template, method)`` pairs on ``application``."""
    out: set[tuple[str, str]] = set()
    for path, methods in iter_routes(application):
        if not path.startswith("/api/"):
            continue
        template = re.sub(r"\{[^}]*\}", "{param}", path)
        for method in methods:
            out.add((template, method))
    return out


def _spa_api_path_methods() -> set[tuple[str, str]]:
    """Every ``(path, method)`` the SPA issues with an explicit ``method:``.

    Only pairs where the SPA spells both a ``/api/...`` literal and an adjacent
    ``method: "VERB"`` are returned; a bare path (GET) is covered by the
    path-only test. ``{param}`` bases (concatenated fragments) are excluded.
    """
    pairs: set[tuple[str, str]] = set()
    for page in _SPA_PAGES:
        if not page.is_file():
            continue
        for m in _API_PATH_METHOD_RE.finditer(page.read_text()):
            path = _template_to_route(m.group(1).rstrip("/"))
            if path in ("/api/", "/api/{param}"):
                continue
            # The edge-annotation base ``/api/edges/{from}/{kind}/{to}`` is
            # concatenated with ``/{field}`` before the fetch, so the literal is a
            # 3-param prefix of the real 4-param route -- the documented
            # concatenation limitation. Its verb cannot be matched against the
            # full route, so skip it (the POST create-edge sibling at the same
            # 3-param shape is covered by the path-only test).
            if path == "/api/edges/{param}/{param}/{param}":
                continue
            pairs.add((path, m.group(2).upper()))
    return pairs


def test_spa_api_path_methods_match_registered_verbs() -> None:
    """Every (path, method) the SPA issues must be a registered route verb.

    Path-only coverage cannot catch a SPA that POSTs to a PUT-only route (the
    role-change 405 class): the path exists but the verb does not. This pairs
    each spelled ``method:`` with its path and asserts the (path, verb) is
    registered, so verb drift fails here instead of as a 405 in the browser.
    """
    spa_app = FastAPI()
    for route in app.routes:
        spa_app.router.routes.append(route)
    web.attach(spa_app)
    registered = _registered_path_methods(spa_app)
    registered_paths = {path for path, _ in registered}
    # Only check verbs for paths the static scan resolved to a real route. A
    # path the scan saw only as a concatenation prefix (e.g. the edge-annotation
    # ``${base}/${field}`` builds one extra segment the literal misses) is not a
    # fetched path; the path-only test documents the same limitation. Checking
    # its verb would be a false positive, not a real mismatch.
    mismatched = sorted(
        f"{method} {path}"
        for path, method in _spa_api_path_methods()
        if path in registered_paths and (path, method) not in registered
    )
    assert not mismatched, (
        "an SPA page issues an (path, method) pair with no matching route verb "
        "(e.g. POST to a PUT-only route -> 405):\n" + "\n".join(mismatched)
    )


def test_spa_edge_display_names_derive_from_topology() -> None:
    """``edgeDisplayName`` reads labels from ``EDGE_TOPOLOGY``, not a hard copy.

    The old hand-typed ``names`` object fell through to the raw storage name for
    any unmapped kind (``cites_paper`` -> ``cites_paper``). Labels now come from
    ``/api/meta/edges`` (server-side ``EDGE_POLICIES.forward_label`` /
    ``inverse_label``), so the SPA can never hold a stale per-kind label table.
    Guard that the function derives from ``EDGE_TOPOLOGY`` and carries no
    reintroduced hardcoded ``names`` map.
    """
    html = _INDEX_HTML.read_text()
    block = html[
        html.index("function edgeDisplayName") : html.index(
            "}", html.index("function edgeDisplayName")
        )
    ]
    assert "EDGE_TOPOLOGY[edgeKind]" in block, (
        "edgeDisplayName must derive labels from EDGE_TOPOLOGY (/api/meta/edges)"
    )
    assert "topo.forward" in block
    assert "topo.inverse" in block
    assert "names = {" not in block, (
        "a hardcoded per-kind label map was reintroduced; derive from "
        "EDGE_TOPOLOGY instead"
    )


def test_graph_live_adds_are_seeded_before_reheat() -> None:
    """Live graph inserts should seed new nodes before force relaxation."""
    html = _GRAPH_HTML.read_text()
    commit = html[html.index("function commit()") : html.index("function fitView()")]
    ordered_steps = [
        "flushPendingEdges();",
        "seedNewNodePositions();",
        "dampExistingVelocities();",
        "Graph.graphData(data);",
        "warmSimulation();",
        "newNodeIds.clear();",
    ]
    positions = [commit.index(step) for step in ordered_steps]
    assert positions == sorted(positions)
    assert 'Graph.d3Force("charge").strength(-70).distanceMax(280);' in html
    assert 'Graph.d3Force("gravity", radialGravity(0.02));' in html
    assert "Graph.d3VelocityDecay(0.58);" in html
    assert "const pendingEdges = new Map();" in html
    assert "function flushPendingEdges()" in html
    assert "function relaxNewEdges()" not in html


def test_search_box_routes_exact_refs_before_text_search() -> None:
    """The existing search control doubles as the exact-reference control."""
    html = _INDEX_HTML.read_text()
    assert 'placeholder="search title/description or enter Kind#seq..."' in html
    assert 'id="exact-seq"' not in html

    destination = html[
        html.index("function searchDestination") : html.index(
            "function buildSearch()",
        )
    ]
    ordered_routes = [
        "UUID_RE.test(trimmed)",
        "`#/lookup/${trimmed}`",
        "SHORT_RE.exec(trimmed)",
        "`#/ref/${encodeURIComponent(short[1])}/${short[2]}`",
        "`#/search?q=${encodeURIComponent(trimmed)}`",
    ]
    positions = [destination.index(route) for route in ordered_routes]
    assert positions == sorted(positions)

    search = html[
        html.index("function buildSearch()") : html.index(
            "const RERENDER_MS",
        )
    ]
    assert "location.hash = searchDestination(search.value);" in search


def _render_turn_block() -> str:
    """The body of ``renderTurn``, up to the next top-level function."""
    html = _INDEX_HTML.read_text()
    start = html.index("function renderTurn(ev)")
    return html[start : html.index("\nfunction ", start + 1)]


def test_every_turn_shows_its_structure_and_its_raw_record() -> None:
    """Structured rendering AND the record's JSON, on every kind.

    The per-kind arms each append ONE body form, so a kind with a structured
    arm showed no JSON and a kind without one showed only JSON -- the reader
    could never see both. The raw block is appended after the dispatch, outside
    every arm, so it cannot be skipped by adding a twenty-second ``else if``.
    """
    block = _render_turn_block()
    assert "function rawRecord(" in _INDEX_HTML.read_text()
    assert block.count("rawRecord(ev)") == 1, (
        "the raw record must be appended exactly once, from outside the "
        "per-kind dispatch"
    )
    # After the last arm's closing brace, and before the return: unconditional.
    assert block.index("rawRecord(ev)") > block.rindex("} else {")
    assert block.index("rawRecord(ev)") < block.index('return el("div"')


def test_the_raw_record_omits_ciphertext() -> None:
    """A sealed ``Thinking`` block is base64 megabytes, and unreadable.

    ``rawRecord`` must strip it rather than paste it into every reasoning turn;
    the transcript read also asks the route for plaintext only, which is what
    ``read_session_records_route`` documents a viewer wants.
    """
    html = _INDEX_HTML.read_text()
    start = html.index("function rawRecord(")
    block = html[start : html.index("\nfunction ", start + 1)]
    assert "ciphertext" in block, "rawRecord must drop the ciphertext key"
    assert "plaintext_only=true" in html, (
        "the transcript read must not pull ciphertext it never renders"
    )


def test_a_context_clear_shows_what_the_fresh_context_was_given() -> None:
    """The record that delineates a session must render what it states.

    ``ContextClear`` carries the system prompt as the agent saw it and the
    summary a compaction carried in; rendering only the words "context
    cleared" threw both away.
    """
    block = _render_turn_block()
    arm = block[block.index('ev.kind === "ContextClear"') :]
    arm = arm[: arm.index("} else ")]
    assert "m.system_prompt" in arm
    assert "m.summary" in arm


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
