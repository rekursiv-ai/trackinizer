"""``GET /api/export`` serializes the store's rows into the wire's lines."""

from __future__ import annotations

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
    EXPORT_FORMAT,
    EXPORT_MEDIA_TYPE,
    EXPORT_VERSION,
)


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


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

    async def export_graph() -> GraphExport:
        return _graph()

    monkeypatch.setattr(store, "export_graph", export_graph)
    return client


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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
