"""Tests for ``POST /api/inquiries/{kind}`` and ``/api/inquiries/batch`` routes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import uuid

import asyncpg
import pytest

from trackinizer.conftest import executed_sql


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


def _is_uuid(value: str) -> bool:
    """True iff ``value`` parses as a UUID."""
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class TestRoutes:
    def test_submit_issue(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        r = client.post("/api/inquiries/issue", json={"title": "t"})
        assert r.status_code == 201
        assert "id" in r.json()

    def test_submit_codechange_allows_unset_sha(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        r = client.post("/api/inquiries/codechange", json={"title": "c"})
        assert r.status_code == 201

    def test_submit_batch_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        first_key = "11111111-1111-1111-1111-111111111111"
        second_key = "22222222-2222-2222-2222-222222222222"
        r = client.post(
            "/api/inquiries/batch",
            json={
                "items": [
                    {
                        "kind": "Issue",
                        "title": "i1",
                        "idempotency_key": first_key,
                    },
                    {
                        "kind": "Artifact",
                        "title": "a1",
                        "idempotency_key": second_key,
                    },
                ]
            },
        )
        assert r.status_code == 200
        body = r.json()
        # Inquiry ids are server-minted; assert structure, not equality
        # against the sent idempotency_keys.
        assert list(body) == ["ids"]
        assert len(body["ids"]) == 2
        assert all(_is_uuid(s) for s in body["ids"])

    def test_submit_batch_forwards_per_item_actor(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The batch route must forward each item's ``actor`` intact (the
        # store applies ``item.actor or <fallback>`` per item), so a
        # per-item actor is honored exactly as single-submit honors it
        # (API-23). The route must not flatten every item to the caller's
        # email.
        client, store, _engine = route_client
        captured = AsyncMock(return_value=[uuid.uuid4(), uuid.uuid4()])
        monkeypatch.setattr(store, "submit_batch", captured)
        r = client.post(
            "/api/inquiries/batch",
            json={
                "items": [
                    {
                        "kind": "Issue",
                        "title": "i1",
                        "actor": "alice",
                        "idempotency_key": "11111111-1111-1111-1111-111111111111",
                    },
                    {
                        "kind": "Artifact",
                        "title": "a1",
                        "idempotency_key": "22222222-2222-2222-2222-222222222222",
                    },
                ]
            },
        )
        assert r.status_code == 200, r.text
        forwarded_items = captured.call_args.args[0]
        # item[0] keeps its explicit actor; item[1] (no actor) falls back.
        assert forwarded_items[0].actor == "alice"
        assert not forwarded_items[1].actor

    def test_submit_batch_validates_each_distinct_account_once(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Many items sharing one account must validate that account once, not
        # per item -- a per-item probe is a needless N-connection fan-out.
        client, store, engine = route_client
        monkeypatch.setattr(
            store, "submit_batch", AsyncMock(return_value=[uuid.uuid4()] * 20)
        )
        probe_calls = 0
        base = engine.conn.fetchval

        async def fetchval(sql: str, *args: object) -> object:
            nonlocal probe_calls
            if "FROM users" in sql and "status = 'active'" in sql:
                probe_calls += 1
                return 1
            return await base(sql, *args)

        engine.conn.fetchval = AsyncMock(side_effect=fetchval)
        items = [
            {
                "kind": "Issue",
                "title": f"i{n}",
                "account": "shared@example.com",
                "idempotency_key": str(uuid.uuid4()),
            }
            for n in range(20)
        ]
        r = client.post("/api/inquiries/batch", json={"items": items})
        assert r.status_code == 200, r.text
        # One distinct account -> exactly one active-user probe.
        assert probe_calls == 1

    def test_submit_batch_route_validates_discriminator(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        r = client.post(
            "/api/inquiries/batch",
            json={"items": [{"kind": "Bogus", "title": "x"}]},
        )
        assert r.status_code == 422

    def test_submit_batch_rolls_back_whole_batch_on_failure(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """An item failure rolls the whole batch back: one BEGIN, one ROLLBACK.

        The shared connection fails on the second item's inquiry INSERT.
        Atomicity means the batch runs in ONE transaction (a single
        ``BEGIN``) that ``ROLLBACK``s without ``COMMIT`` -- no earlier item
        is left committed.
        """
        client, _store, engine = route_client
        conn = engine.conn
        inserts = {"n": 0}
        real_execute = conn.execute.side_effect

        async def execute(sql: str, *args: object) -> object:
            if "INSERT INTO inquiries" in sql:
                inserts["n"] += 1
                if inserts["n"] == 2:
                    raise RuntimeError("boom")
            if real_execute is not None:
                return await real_execute(sql, *args)
            return "OK"

        conn.execute.side_effect = execute
        # TestClient re-raises an unhandled server exception; the contract
        # under test is the transaction shape, asserted on the conn below.
        with pytest.raises(RuntimeError, match="boom"):
            client.post(
                "/api/inquiries/batch",
                json={
                    "items": [
                        {
                            "kind": "Issue",
                            "title": "i1",
                            "idempotency_key": "11111111-1111-1111-1111-111111111111",
                        },
                        {
                            "kind": "Artifact",
                            "title": "a1",
                            "idempotency_key": "22222222-2222-2222-2222-222222222222",
                        },
                    ]
                },
            )
        verbs = [s for s in executed_sql(conn) if s in ("BEGIN", "COMMIT", "ROLLBACK")]
        assert verbs.count("BEGIN") == 1
        assert verbs.count("ROLLBACK") == 1
        assert "COMMIT" not in verbs

    def test_submit_batch_requires_per_item_idempotency(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        r = client.post(
            "/api/inquiries/batch",
            json={"items": [{"kind": "Issue", "title": "i1"}]},
        )
        assert r.status_code == 422
        assert "idempotency_key" in r.text

    def test_submit_batch_commits_once_for_all_items(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A successful multi-kind batch commits in exactly one transaction."""
        client, _store, engine = route_client
        conn = engine.conn
        item_keys = [f"11111111-1111-1111-1111-{i:012d}" for i in range(8)]
        kinds = [
            {"kind": "Issue", "title": "i"},
            {"kind": "Artifact", "title": "a"},
            {"kind": "Experiment", "title": "e"},
            {"kind": "Paper", "title": "p", "source": "https://x"},
            {"kind": "Belief", "title": "c"},
            {"kind": "CodeChange", "title": "cc", "sha": "abc"},
            {"kind": "WebResult", "title": "w", "url": "https://x"},
            {"kind": "WebSearch", "title": "ws", "query": "q"},
        ]
        items = [
            {**body, "idempotency_key": item_keys[i]} for i, body in enumerate(kinds)
        ]
        r = client.post("/api/inquiries/batch", json={"items": items})
        assert r.status_code == 200
        body = r.json()
        assert len(body["ids"]) == len(item_keys)
        assert all(_is_uuid(s) for s in body["ids"])
        verbs = [s for s in executed_sql(conn) if s in ("BEGIN", "COMMIT", "ROLLBACK")]
        assert verbs.count("BEGIN") == 1
        assert verbs.count("COMMIT") == 1
        assert "ROLLBACK" not in verbs

    def test_submit_batch_unique_violation_rolls_back_whole_batch(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A change_log PK collision mid-batch rolls the whole batch back.

        A concurrent racer can take an item's idempotency_key after its
        probe said it was free, so the insert raises UniqueViolation. In a
        batch this must propagate and roll every item back -- never recover
        one item on the now-poisoned shared transaction.
        """
        client, _store, engine = route_client
        conn = engine.conn
        inserts = {"n": 0}
        real_execute = conn.execute.side_effect

        async def execute(sql: str, *args: object) -> object:
            if "INSERT INTO change_log" in sql:
                inserts["n"] += 1
                if inserts["n"] == 2:
                    raise asyncpg.UniqueViolationError("dup change_log.id")
            if real_execute is not None:
                return await real_execute(sql, *args)
            return "OK"

        conn.execute.side_effect = execute
        r = client.post(
            "/api/inquiries/batch",
            json={
                "items": [
                    {
                        "kind": "Issue",
                        "title": "i1",
                        "idempotency_key": "11111111-1111-1111-1111-111111111111",
                    },
                    {
                        "kind": "Artifact",
                        "title": "a1",
                        "idempotency_key": "22222222-2222-2222-2222-222222222222",
                    },
                ]
            },
        )
        # The app maps a UniqueViolation to 409; the batch's single tx rolls
        # back first, so nothing committed.
        assert r.status_code == 409
        verbs = [s for s in executed_sql(conn) if s in ("BEGIN", "COMMIT", "ROLLBACK")]
        assert verbs.count("BEGIN") == 1
        assert verbs.count("ROLLBACK") == 1
        assert "COMMIT" not in verbs

    def test_submit_batch_retry_returns_existing_ids_without_reinserting(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Re-running a committed batch returns original ids, inserts nothing.

        Each item's idempotency probe finds its prior ``created`` change and
        short-circuits, so a retried batch is duplicate-free.
        """
        client, _store, engine = route_client
        conn = engine.conn
        existing = {
            "11111111-1111-1111-1111-111111111111": uuid.uuid4(),
            "22222222-2222-2222-2222-222222222222": uuid.uuid4(),
        }
        real_fetchrow = conn.fetchrow.side_effect

        async def fetchrow(sql: str, *args: object) -> object:
            if "FROM change_log c WHERE c.id" in sql:
                row_id = existing.get(str(args[0]))
                if row_id is not None:
                    return {
                        "subject_id": row_id,
                        "change_kind": "created",
                        "subject_kind": "Issue"
                        if str(args[0]).startswith("1")
                        else "Artifact",
                    }
            if real_fetchrow is not None:
                return await real_fetchrow(sql, *args)
            return None

        conn.fetchrow.side_effect = fetchrow
        r = client.post(
            "/api/inquiries/batch",
            json={
                "items": [
                    {
                        "kind": "Issue",
                        "title": "i1",
                        "idempotency_key": "11111111-1111-1111-1111-111111111111",
                    },
                    {
                        "kind": "Artifact",
                        "title": "a1",
                        "idempotency_key": "22222222-2222-2222-2222-222222222222",
                    },
                ]
            },
        )
        assert r.status_code == 200
        assert r.json()["ids"] == [str(v) for v in existing.values()]
        inserts = [
            c
            for c in conn.execute.call_args_list
            if c.args and "INSERT INTO inquiries" in c.args[0]
        ]
        assert inserts == []
