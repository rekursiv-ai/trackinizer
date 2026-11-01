"""``GET /api/export`` serializes the store's rows into the wire's lines."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from treekle import from_plain, json_dumps, json_loads

import asyncpg
import pytest

from trackinizer.server.api.conftest import (
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.api.export_routes import export_lines
from trackinizer.server.store.export import GraphExport
from trackinizer.wire.wire_export import (
    EXPORT_API_PATH,
    EXPORT_FILTER_PARAM,
    EXPORT_FORMAT,
    EXPORT_MEDIA_TYPE,
    EXPORT_VERSION,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.access_policy import Principal
    from trackinizer.server.audience import SessionReader
    from trackinizer.server.store.core import Store
    from trackinizer.wire.filters import Filter, FilterOp


_ISSUE: UUID = UUID("33333333-3333-3333-3333-333333333333")
_RUN: UUID = UUID("44444444-4444-4444-4444-444444444444")


def _graph() -> GraphExport:
    """One inquiry and one audit row, in the value types asyncpg returns."""
    created = datetime(2026, 9, 19, 12, 30, tzinfo=UTC)
    return GraphExport(
        migrations=("schema.sql", "schema.019.sql"),
        audiences=("open",),
        withheld_sessions=2,
        tables=(
            (
                "inquiries",
                (
                    {
                        "id": _ISSUE,
                        "title": "Broad question",
                        "labels": ["triage"],
                        "description": None,
                        "created": created,
                        "experiment_codechanges": [_RUN],
                    },
                ),
            ),
            ("edges", ()),
            (
                "change_log",
                ({"id": _RUN, "subject_id": _ISSUE, "reason": ""},),
            ),
        ),
    )


@pytest.fixture
def export_client(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Return a route client whose store exports :func:`_graph`."""
    client, store, _engine = route_client

    async def export_graph(
        *,
        reader: SessionReader,
        selector: Sequence[Filter] = (),
        principal: Principal | None = None,
    ) -> GraphExport:
        # Echoed back rather than applied: what the route owns is parsing the
        # selector and putting it in the header, and the store's own tests
        # cover the scoping it does with it.
        del reader, principal
        return replace(_graph(), selector=tuple(selector))

    monkeypatch.setattr(store, "export_graph", export_graph)
    return client


def _filter_selector(raw: str) -> dict[str, str]:
    """One ``filter=`` param value as the route's query string spells it."""
    return {EXPORT_FILTER_PARAM: raw}


def test_the_header_comes_first_then_one_line_per_row(
    export_client: TestClient,
) -> None:
    r = export_client.get(EXPORT_API_PATH)

    assert r.status_code == 200
    assert r.headers["content-type"].startswith(EXPORT_MEDIA_TYPE)
    header, *rows = [
        json_loads(line, dict[str, object]) for line in r.text.splitlines()
    ]
    assert header == {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "migrations": ["schema.sql", "schema.019.sql"],
        "audiences": ["open"],
        "withheld_sessions": 2,
    }
    assert [row["table"] for row in rows] == ["inquiries", "change_log"]


def test_uuids_and_timestamps_become_strings(export_client: TestClient) -> None:
    r = export_client.get(EXPORT_API_PATH)

    inquiry = from_plain(
        json_loads(r.text.splitlines()[1], dict[str, object])["row"],
        dict[str, object],
    )
    assert inquiry == {
        "id": str(_ISSUE),
        "title": "Broad question",
        "labels": ["triage"],
        "description": None,
        "created": "2026-09-19T12:30:00+00:00",
        "experiment_codechanges": [str(_RUN)],
    }


def test_every_line_ends_in_a_newline(export_client: TestClient) -> None:
    r = export_client.get(EXPORT_API_PATH)

    assert r.text.endswith("\n")
    assert r.text.count("\n") == 3


def test_a_row_line_is_compact_utf8() -> None:
    graph = replace(_graph(), tables=(("inquiries", ({"title": "café", "n": 1},)),))

    _header, line = export_lines(graph)

    assert line == '{"table":"inquiries","row":{"title":"café","n":1}}\n'.encode()


def test_a_non_finite_float_is_refused() -> None:
    graph = replace(_graph(), tables=(("inquiries", ({"n": float("nan")},)),))

    with pytest.raises(ValueError, match="not JSON compliant"):
        list(export_lines(graph))


def test_a_viewer_may_export(export_client: TestClient) -> None:
    install_identity(make_test_identity(role="viewer"))

    assert export_client.get(EXPORT_API_PATH).status_code == 200


def test_export_without_auth_is_401(export_client: TestClient) -> None:
    clear_identity_override()

    assert export_client.get(EXPORT_API_PATH).status_code == 401


def test_a_selector_reaches_the_store_and_the_header(
    export_client: TestClient,
) -> None:
    """The clause the caller sent is what the store is asked for, and what is said."""
    r = export_client.get(
        EXPORT_API_PATH,
        params=_filter_selector('{"field":"labels","op":"is","value":"org:rekursiv"}'),
    )

    assert r.status_code == 200
    header = json_loads(r.text.splitlines()[0], dict[str, object])
    assert header["selector"] == [
        {"field": "labels", "op": "is", "value": "org:rekursiv"},
    ]


def test_clauses_arrive_in_the_order_they_were_sent(
    export_client: TestClient,
) -> None:
    """Repeated params AND together, which is how the request spells a partition."""
    r = export_client.get(
        EXPORT_API_PATH,
        params=[
            (
                EXPORT_FILTER_PARAM,
                '{"field":"labels","op":"is","value":"org:rekursiv"}',
            ),
            (EXPORT_FILTER_PARAM, '{"field":"labels","op":"nre","value":"^machine:"}'),
        ],
    )

    header = json_loads(r.text.splitlines()[0], dict[str, object])
    assert header["selector"] == [
        {"field": "labels", "op": "is", "value": "org:rekursiv"},
        {"field": "labels", "op": "nre", "value": "^machine:"},
    ]


def test_an_unselectable_field_is_400_and_names_what_is_allowed(
    export_client: TestClient,
) -> None:
    """A subgraph is carved on labels; ``title`` would be a different feature."""
    r = export_client.get(
        EXPORT_API_PATH,
        params=_filter_selector('{"field":"title","op":"is","value":"anything"}'),
    )

    assert r.status_code == 400
    assert "labels" in str(from_plain(r.json(), dict[str, object])["detail"])


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json at all", id="not-json"),
        pytest.param('["labels","is","x"]', id="not-an-object"),
        pytest.param('{"field":"labels","op":"is"}', id="no-value"),
        pytest.param('{"field":"labels","op":"wat","value":"x"}', id="unknown-op"),
        pytest.param('{"field":"labels","op":"re","value":"("}', id="bad-regex"),
    ],
)
def test_a_malformed_clause_is_400_not_a_whole_graph(
    export_client: TestClient,
    raw: str,
) -> None:
    """The dangerous failure is a refused selector quietly exporting everything."""
    r = export_client.get(EXPORT_API_PATH, params=_filter_selector(raw))

    assert r.status_code == 400


def test_no_selector_leaves_the_key_out(export_client: TestClient) -> None:
    """A whole-graph export is unchanged by this feature, header included."""
    header = json_loads(
        export_client.get(EXPORT_API_PATH).text.splitlines()[0],
        dict[str, object],
    )

    assert "selector" not in header


@pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
def test_label_ordering_is_a_client_error(
    route_client: tuple[TestClient, Store, FakeEngine],
    op: FilterOp,
) -> None:
    """The real store rejects unsupported operators before opening a connection."""
    client, _store, engine = route_client
    raw = json_dumps({"field": "labels", "op": op, "value": "x"})

    response = client.get(EXPORT_API_PATH, params=_filter_selector(raw))

    assert response.status_code == 422
    body = from_plain(response.json(), dict[str, object])
    assert body["code"] == "validation"
    assert "labels" in from_plain(body["detail"], str)
    engine.conn.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "error_type",
    [asyncpg.QueryCanceledError, asyncpg.InvalidRegularExpressionError],
)
def test_database_selector_failures_are_client_errors(
    route_client: tuple[TestClient, Store, FakeEngine],
    error_type: type[asyncpg.PostgresError],
) -> None:
    """Database rejection and cancellation cross the real store and HTTP guard."""
    client, _store, engine = route_client
    engine.conn.fetch.side_effect = [
        error_type("query refused"),
        list[dict[str, object]](),
    ]

    response = client.get(
        EXPORT_API_PATH,
        params=_filter_selector('{"field":"labels","op":"re","value":"x"}'),
    )

    assert response.status_code == 400


def test_the_export_is_read_for_the_audiences_of_its_caller(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _engine = route_client
    asked: list[frozenset[str]] = []

    async def export_graph(
        *,
        reader: SessionReader,
        selector: Sequence[Filter] = (),
        principal: Principal | None = None,
    ) -> GraphExport:
        del selector, principal
        asked.append(reader.audiences)
        return _graph()

    monkeypatch.setattr(store, "export_graph", export_graph)

    install_identity(make_test_identity())
    assert client.get(EXPORT_API_PATH).status_code == 200
    install_identity(
        replace(make_test_identity(), read_audiences=frozenset({"open", "staff"})),
    )
    assert client.get(EXPORT_API_PATH).status_code == 200

    assert asked == [frozenset({"open", "private"}), frozenset({"open", "staff"})]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
