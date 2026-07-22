"""Tests for ``/api/edges/<from>/<kind>/<to>`` routes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import asyncio
import uuid

from fastapi import HTTPException

import asyncpg
import pytest

from trackinizer.conftest import (
    make_store,
    new_uuid,
    queue_field_rows,
    set_field_row,
)
from trackinizer.server.api.app import app
from trackinizer.server.api.edge import _set_edge_annotation
from trackinizer.types.edges import Edge
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import Inquiry, Issue


def test_set_edge_annotation_rejects_unknown_field() -> None:
    """An unknown annotation field raises, not a silent labels-fallthrough.

    Drift defense: the if/elif ladder maps each ``edge_field_routes`` column
    to one ``set_edge_annotation`` kwarg; a new column with no branch must
    fail loudly rather than be written as ``labels``.
    """
    store, _engine = make_store()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            _set_edge_annotation(
                store,
                "weight",
                0.5,
                from_id=new_uuid(),
                to_id=new_uuid(),
                edge_kind="narrows",
                api_key_id=None,
                actor="u",
            )
        )
    assert exc.value.status_code == 500
    assert "edge-annotation setter" in exc.value.detail


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


def _edge_row(
    *,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    priority: int | None = None,
    note: str | None = "",
    valence: float | None = None,
    labels: list[str] | None = None,
) -> dict[str, object]:
    """One ``edges`` row as ``conn.fetchrow`` would return it.

    Mirrors the column set selected by ``get_edge`` and the
    ``FOR UPDATE`` SELECT in ``set_edge_annotation`` / ``remove_edge``;
    the union of those two column sets is a superset, so one shape
    serves every edge read the mock has to satisfy.
    """
    return {
        "from_id": from_id,
        "from_kind": "Issue",
        "to_id": to_id,
        "to_kind": "Issue",
        "edge_kind": "narrows",
        "priority": priority,
        "note": note,
        "valence": valence,
        "labels": labels,
    }


async def _non_session_authz(target_id: uuid.UUID) -> tuple[str, uuid.UUID | None]:
    """Partial-store stand-in: every endpoint is a shared (non-session) row."""
    del target_id
    return ("Issue", None)


class _PartialEdgeStore:
    def __init__(self) -> None:
        self.calls = 0

    session_authz = staticmethod(_non_session_authz)

    async def add_edge(
        self,
        *,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: Edge.Kind,
        priority: Issue.Priority | None = None,
        note: str = "",
        valence: float | None = None,
        labels: Sequence[str] = (),
        reason: str = "",
        api_key_id: uuid.UUID | None = None,
        actor: Inquiry.Actor,
    ) -> tuple[uuid.UUID | None, bool]:
        del (
            from_id,
            to_id,
            edge_kind,
            priority,
            note,
            valence,
            labels,
            reason,
            api_key_id,
            actor,
        )
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("boom")
        return uuid.uuid4(), True


class _ConflictEdgeStore:
    """Edge store whose ``add_edge`` raises a client-safe ConflictError.

    Edge creation is an upsert, so a duplicate no longer errors; but a cycle /
    self-loop / bad target kind still raises a ``ConflictError`` whose
    author-written message must reach the batch caller verbatim.
    """

    session_authz = staticmethod(_non_session_authz)

    async def add_edge(
        self,
        *,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: Edge.Kind,
        priority: Issue.Priority | None = None,
        note: str = "",
        valence: float | None = None,
        labels: Sequence[str] = (),
        reason: str = "",
        api_key_id: uuid.UUID | None = None,
        actor: Inquiry.Actor,
    ) -> tuple[uuid.UUID | None, bool]:
        del from_id, to_id, priority, note, valence, labels, reason, api_key_id, actor
        raise ConflictError(f"{edge_kind} edge would close a cycle")


class _LeakyEdgeStore:
    """Edge store whose ``add_edge`` raises a raw asyncpg violation.

    The DETAIL carries internal constraint / column names that must never
    reach the wire.
    """

    session_authz = staticmethod(_non_session_authz)

    async def add_edge(
        self,
        *,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: Edge.Kind,
        priority: Issue.Priority | None = None,
        note: str = "",
        valence: float | None = None,
        labels: Sequence[str] = (),
        reason: str = "",
        api_key_id: uuid.UUID | None = None,
        actor: Inquiry.Actor,
    ) -> tuple[uuid.UUID | None, bool]:
        del (
            from_id,
            to_id,
            edge_kind,
            priority,
            note,
            valence,
            labels,
            reason,
            api_key_id,
            actor,
        )
        err = asyncpg.ForeignKeyViolationError(
            'insert violates foreign key "secret_constraint" on column from_id'
        )
        raise err


class TestCreate:
    def test_create_edge(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        # ``add_edge``: lookup_kind(from) + insert_edge[lookup_kind(to),
        # cycle-check, INSERT RETURNING]. A ``proves`` citation (Paper -> Belief)
        # is the only edge family that carries a ``valence``.
        engine.conn.fetchval.side_effect = ["Paper", "Belief", False, new_uuid()]
        r = client.post(
            f"/api/edges/{new_uuid()}/proves/{new_uuid()}",
            json={"actor": "u", "note": "load-bearing", "valence": 0.5},
        )
        body = r.json()
        assert r.status_code == 200
        assert "change_id" in body
        assert body["change_id"] is not None

    def test_create_edge_bare_body(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        engine.conn.fetchval.side_effect = ["Issue", "Issue", False, new_uuid()]
        r = client.post(
            f"/api/edges/{new_uuid()}/narrows/{new_uuid()}",
            json={},
        )
        body = r.json()
        assert r.status_code == 200
        assert "change_id" in body
        assert body["change_id"] is not None


class TestBatch:
    def test_create_edge_batch(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        # Two edges, each: lookup_kind(from) + lookup_kind(to) + cycle +
        # INSERT RETURNING.
        engine.conn.fetchval.side_effect = [
            "Issue",
            "Issue",
            False,
            new_uuid(),  # first edge
            "Issue",
            "Issue",
            False,
            new_uuid(),  # second edge
        ]
        r = client.post(
            "/api/edges/batch",
            json={
                "items": [
                    {
                        "from_id": str(new_uuid()),
                        "to_id": str(new_uuid()),
                        "edge_kind": "narrows",
                        "actor": "u",
                    },
                    {
                        "from_id": str(new_uuid()),
                        "to_id": str(new_uuid()),
                        "edge_kind": "narrows",
                        "actor": "u",
                    },
                ]
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "ok": True,
            "items": [{"ok": True}, {"ok": True}],
        }

    def test_create_edge_batch_reports_partial_success(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        app.state.store = _PartialEdgeStore()
        r = client.post(
            "/api/edges/batch",
            json={
                "items": [
                    {
                        "from_id": str(new_uuid()),
                        "to_id": str(new_uuid()),
                        "edge_kind": "narrows",
                        "actor": "u",
                    },
                    {
                        "from_id": str(new_uuid()),
                        "to_id": str(new_uuid()),
                        "edge_kind": "narrows",
                        "actor": "u",
                    },
                    {
                        "from_id": str(new_uuid()),
                        "to_id": str(new_uuid()),
                        "edge_kind": "narrows",
                        "actor": "u",
                    },
                ]
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "ok": False,
            "items": [
                {"ok": True},
                # The raw exception message is replaced with a generic,
                # leak-free message (API-11): only ConflictError-family
                # carries an author-written, client-safe string.
                {"ok": False, "error": "edge could not be created", "index": 1},
                {"ok": False, "error": "skipped after earlier failure", "index": 2},
            ],
        }
        # Fail-stop: item[0] was attempted (and, on a real store, committed
        # in its own tx) before item[1] failed; item[2] was never attempted.
        store = app.state.store
        assert isinstance(store, _PartialEdgeStore)
        assert store.calls == 2

    def test_edge_batch_conflict_error_message_is_preserved(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # A ConflictError (e.g. a cycle / self-loop) carries an author-written,
        # client-safe message, so it is surfaced verbatim rather than replaced
        # by the generic fallback.
        client, _store, _engine = route_client
        app.state.store = _ConflictEdgeStore()
        r = client.post(
            "/api/edges/batch",
            json={
                "items": [
                    {
                        "from_id": str(new_uuid()),
                        "to_id": str(new_uuid()),
                        "edge_kind": "narrows",
                        "actor": "u",
                    },
                ]
            },
        )
        assert r.status_code == 200
        assert r.json()["items"][0]["error"] == "narrows edge would close a cycle"

    def test_edge_batch_does_not_leak_db_detail(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # A raw asyncpg violation carries DETAIL (column/constraint names);
        # the batch response must not echo it. The error is the generic,
        # leak-free message instead.
        client, _store, _engine = route_client
        app.state.store = _LeakyEdgeStore()
        r = client.post(
            "/api/edges/batch",
            json={
                "items": [
                    {
                        "from_id": str(new_uuid()),
                        "to_id": str(new_uuid()),
                        "edge_kind": "narrows",
                        "actor": "u",
                    },
                ]
            },
        )
        assert r.status_code == 200
        error = r.json()["items"][0]["error"]
        assert error == "edge could not be created"
        assert "secret_constraint" not in error
        assert "from_id" not in error


class TestRead:
    def test_get_edge(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        from_id, to_id = new_uuid(), new_uuid()
        set_field_row(
            engine.conn,
            _edge_row(
                from_id=from_id,
                to_id=to_id,
                priority=3,
                note="blocks deploy",
                valence=0.5,
                labels=["context"],
            ),
        )
        r = client.get(f"/api/edges/{from_id}/narrows/{to_id}")
        assert r.status_code == 200
        assert r.json() == {
            "from_id": str(from_id),
            "from_kind": "Issue",
            "to_id": str(to_id),
            "to_kind": "Issue",
            "edge_kind": "narrows",
            "priority": 3,
            "note": "blocks deploy",
            "valence": 0.5,
            "labels": ["context"],
        }

    def test_get_edge_missing(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, None)
        r = client.get(f"/api/edges/{new_uuid()}/narrows/{new_uuid()}")
        assert r.status_code == 404


class TestDelete:
    def test_delete_edge(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        from_id, to_id = new_uuid(), new_uuid()
        # ``remove_edge``: FOR UPDATE SELECT (fetchrow) then captured-edge
        # walk (fetch) then DELETE.
        set_field_row(engine.conn, _edge_row(from_id=from_id, to_id=to_id))
        engine.conn.fetch.return_value = []
        r = client.request(
            "DELETE",
            f"/api/edges/{from_id}/narrows/{to_id}",
            json={"actor": "u"},
        )
        body = r.json()
        assert r.status_code == 200
        assert "change_id" in body
        assert body["change_id"] is not None


class TestAnnotate:
    def test_put_scalar_field(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        from_id, to_id = new_uuid(), new_uuid()
        # ``set_edge_annotation`` does one FOR UPDATE fetchrow.
        set_field_row(engine.conn, _edge_row(from_id=from_id, to_id=to_id))
        r = client.put(
            f"/api/edges/{from_id}/narrows/{to_id}/note",
            json={"value": "contextual note", "actor": "u"},
        )
        body = r.json()
        assert r.status_code == 200
        assert "change_id" in body
        assert body["change_id"] is not None

    def test_put_labels_field(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        from_id, to_id = new_uuid(), new_uuid()
        set_field_row(engine.conn, _edge_row(from_id=from_id, to_id=to_id))
        r = client.put(
            f"/api/edges/{from_id}/narrows/{to_id}/labels",
            json={"value": ["context"], "actor": "u"},
        )
        body = r.json()
        assert r.status_code == 200
        assert "change_id" in body
        assert body["change_id"] is not None

    def test_delete_field_clears(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        from_id, to_id = new_uuid(), new_uuid()
        set_field_row(engine.conn, _edge_row(from_id=from_id, to_id=to_id, note="old"))
        r = client.request(
            "DELETE",
            f"/api/edges/{from_id}/narrows/{to_id}/note",
            json={"actor": "u"},
        )
        body = r.json()
        assert r.status_code == 200
        assert "change_id" in body
        assert body["change_id"] is not None

    def test_patch_labels_add(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        from_id, to_id = new_uuid(), new_uuid()
        # ``add_edge_label`` -> get_edge (fetchrow) then
        # set_edge_annotation (FOR UPDATE fetchrow).
        queue_field_rows(
            engine.conn,
            _edge_row(from_id=from_id, to_id=to_id, labels=[]),
            _edge_row(from_id=from_id, to_id=to_id, labels=[]),
        )
        r = client.patch(
            f"/api/edges/{from_id}/narrows/{to_id}/labels",
            json={"op": "add", "value": "context", "actor": "u"},
        )
        body = r.json()
        assert r.status_code == 200
        assert "change_id" in body
        assert body["change_id"] is not None

    def test_patch_labels_sub(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        from_id, to_id = new_uuid(), new_uuid()
        queue_field_rows(
            engine.conn,
            _edge_row(from_id=from_id, to_id=to_id, labels=["context"]),
            _edge_row(from_id=from_id, to_id=to_id, labels=["context"]),
        )
        r = client.patch(
            f"/api/edges/{from_id}/narrows/{to_id}/labels",
            json={"op": "sub", "value": "context", "actor": "u"},
        )
        body = r.json()
        assert r.status_code == 200
        assert "change_id" in body
        assert body["change_id"] is not None

    def test_patch_scalar_field_rejected(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        r = client.patch(
            f"/api/edges/{new_uuid()}/narrows/{new_uuid()}/note",
            json={"op": "add", "value": "x", "actor": "u"},
        )
        assert r.status_code == 405

    def test_put_unknown_field_rejected(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        r = client.put(
            f"/api/edges/{new_uuid()}/narrows/{new_uuid()}/bogus",
            json={"value": 1, "actor": "u"},
        )
        assert r.status_code == 404


class TestCoverageRoutesAndCli:
    def test_edge_routes_and_by_seq_missing(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        source_id = new_uuid()
        target_id = new_uuid()
        queue_field_rows(
            engine.conn,
            # GET /api/inquiries/Issue/999 -> seq lookup, not found.
            None,
            # PUT .../note: set_edge_annotation FOR UPDATE fetchrow.
            _edge_row(from_id=source_id, to_id=target_id),
        )
        engine.conn.fetchval.side_effect = ["Issue", "Issue", False, new_uuid()]
        # A missing short-ref is 404 (API-08/24), consistent with get_edge.
        assert client.get("/api/inquiries/Issue/999").status_code == 404
        assert (
            client.post(
                f"/api/edges/{source_id}/narrows/{target_id}",
                json={},
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/edges/{source_id}/narrows/{target_id}/note",
                json={"value": "contextual note", "actor": "u"},
            ).status_code
            == 200
        )
        set_field_row(engine.conn, _edge_row(from_id=source_id, to_id=target_id))
        engine.conn.fetch.return_value = []
        assert (
            client.request(
                "DELETE",
                f"/api/edges/{source_id}/narrows/{target_id}",
                json={"actor": "u"},
            ).status_code
            == 200
        )
