"""``GET /api/export`` serializes the store's rows into the wire's lines."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from trackinizer.lib.custom_json import DictCodec, loads
from trackinizer.server.api.conftest import (
    clear_identity_override,
    install_identity,
    make_test_identity,
)
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
    from trackinizer.server.store.core import Store
    from trackinizer.wire.filters import Filter


_ISSUE: UUID = UUID("33333333-3333-3333-3333-333333333333")
_RUN: UUID = UUID("44444444-4444-4444-4444-444444444444")


def _graph() -> GraphExport:
    """One inquiry and one audit row, in the value types asyncpg returns."""
    created = datetime(2026, 9, 19, 12, 30, tzinfo=UTC)
    return GraphExport(
        migrations=("schema.sql", "schema.019.sql"),
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

    async def export_graph(*, selector: Sequence[Filter] = ()) -> GraphExport:
        # Echoed back rather than applied: what the route owns is parsing the
        # selector and putting it in the header, and the store's own tests
        # cover the scoping it does with it.
        return replace(_graph(), selector=tuple(selector))

    monkeypatch.setattr(store, "export_graph", export_graph)
    return client


def _selector_of(raw: str) -> dict[str, str]:
    """One ``filter=`` param value as the route's query string spells it."""
    return {EXPORT_FILTER_PARAM: raw}


def test_the_header_comes_first_then_one_line_per_row(
    export_client: TestClient,
) -> None:
    r = export_client.get(EXPORT_API_PATH)

    assert r.status_code == 200
    assert r.headers["content-type"].startswith(EXPORT_MEDIA_TYPE)
    header, *rows = [DictCodec.coerce(loads(line)) for line in r.text.splitlines()]
    assert header == {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "migrations": ["schema.sql", "schema.019.sql"],
    }
    assert [row["table"] for row in rows] == ["inquiries", "change_log"]


def test_uuids_and_timestamps_become_strings(export_client: TestClient) -> None:
    r = export_client.get(EXPORT_API_PATH)

    inquiry = DictCodec.coerce(DictCodec.coerce(loads(r.text.splitlines()[1]))["row"])
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
        params=_selector_of('{"field":"labels","op":"is","value":"org:rekursiv"}'),
    )

    assert r.status_code == 200
    header = DictCodec.coerce(loads(r.text.splitlines()[0]))
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

    header = DictCodec.coerce(loads(r.text.splitlines()[0]))
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
        params=_selector_of('{"field":"title","op":"is","value":"anything"}'),
    )

    assert r.status_code == 400
    assert "labels" in str(DictCodec.coerce(r.json())["detail"])


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
    r = export_client.get(EXPORT_API_PATH, params=_selector_of(raw))

    assert r.status_code == 400


def test_no_selector_leaves_the_key_out(export_client: TestClient) -> None:
    """A whole-graph export is unchanged by this feature, header included."""
    header = DictCodec.coerce(
        loads(export_client.get(EXPORT_API_PATH).text.splitlines()[0]),
    )

    assert "selector" not in header


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
