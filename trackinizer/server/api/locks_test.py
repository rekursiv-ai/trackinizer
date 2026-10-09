"""Unit tests for the lock guard: who it asks the database about, and what it says."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import uuid

from fastapi import HTTPException

import pytest

from trackinizer.lib.codec import from_plain
from trackinizer.server.api.conftest import install_identity, make_test_identity
from trackinizer.server.api.locks import referenced_ids, require_unlocked
from trackinizer.server.auth import AuthIdentity
from trackinizer.wire.bodies import SubmitBatch, SubmitIssue


if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store

_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_KEY = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


class _Engine:
    """An engine whose one connection answers ``fetch`` with ``rows``."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.conn = AsyncMock()
        self.conn.fetch = AsyncMock(return_value=rows)

    def acquire(self) -> _Engine:
        return self

    async def __aenter__(self) -> AsyncMock:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        del exc


class _Request:
    """Just enough of a request for ``get_store``: an app whose state holds a store."""

    def __init__(self, engine: _Engine) -> None:
        store = type("_Store", (), {"engine": engine})()
        state = type("_State", (), {"store": store})()
        self.app = type("_App", (), {"state": state})()


def _request(rows: list[dict[str, object]]) -> tuple[Request, _Engine]:
    engine = _Engine(rows)
    return cast("Request", _Request(engine)), engine


def _who(role: Role) -> AuthIdentity:
    return AuthIdentity(
        user_id=uuid.uuid4(),
        api_key_id=None,
        email="x@example.com",
        role=role,
    )


@pytest.mark.asyncio
async def test_an_admin_is_never_asked_about() -> None:
    request, engine = _request([{"kind": "Issue", "seq": 1}])
    await require_unlocked(request, _who("admin"), [_A])
    assert not engine.conn.fetch.called


@pytest.mark.asyncio
async def test_no_rows_means_no_question() -> None:
    request, engine = _request([{"kind": "Issue", "seq": 1}])
    await require_unlocked(request, _who("writer"), [])
    assert not engine.conn.fetch.called


@pytest.mark.asyncio
async def test_a_locked_row_refuses_a_writer_by_name() -> None:
    request, _ = _request([{"kind": "Belief", "seq": 7}])
    with pytest.raises(HTTPException) as refused:
        await require_unlocked(request, _who("writer"), [_A, _B])
    assert refused.value.status_code == 403
    assert refused.value.detail == "Belief #7 is locked; only an admin may change it"


@pytest.mark.asyncio
async def test_no_locked_row_lets_a_writer_through() -> None:
    request, engine = _request([])
    await require_unlocked(request, _who("writer"), (_A, _B))
    ids, peers = engine.conn.fetch.call_args.args[1:]
    assert (ids, peers) == ([_A, _B], False)


@pytest.mark.asyncio
async def test_a_purge_asks_about_the_rows_it_is_linked_to() -> None:
    request, engine = _request([])
    await require_unlocked(request, _who("viewer"), [_A], include_peers=True)
    assert engine.conn.fetch.call_args.args[2] is True
    assert "FROM edges" in from_plain(engine.conn.fetch.call_args.args[0], str)


def test_a_submit_names_the_rows_it_links_but_not_its_own_key() -> None:
    issue = SubmitIssue(
        title="t",
        narrows=[(_A, None)],
        requires=[_B],
        idempotency_key=_KEY,
    )
    assert referenced_ids(issue) == {_A, _B}


def test_a_batch_names_every_item_and_edge_row() -> None:
    batch = SubmitBatch.model_validate(
        {
            "items": [
                {
                    "kind": "Issue",
                    "title": "t",
                    "idempotency_key": str(_KEY),
                    "narrows": [[str(_A), None]],
                },
            ],
            "edges": [{"from_index": 0, "to_id": str(_B), "edge_kind": "narrows"}],
        },
    )
    assert referenced_ids(batch) == {_A, _B}


def test_a_body_with_no_rows_names_none() -> None:
    assert referenced_ids(SubmitIssue(title="t", idempotency_key=_KEY)) == set()
    assert referenced_ids("text") == set()
    assert referenced_ids((1, "x")) == set()


class TestSetLock:
    def test_a_missing_row_is_404_and_a_found_one_answers_its_flag(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.fetchval = AsyncMock(return_value=None)
        assert (
            client.put(
                f"/api/admin/inquiries/{_A}/lock",
                json={"locked": True},
            ).status_code
            == 404
        )
        engine.conn.fetchval = AsyncMock(return_value=True)
        r = client.put(f"/api/admin/inquiries/{_A}/lock", json={"locked": True})
        assert from_plain(r.json(), dict[str, object]) == {
            "id": str(_A),
            "locked": True,
        }
        assert engine.conn.fetchval.call_args.args[1:] == (
            _A,
            True,
            make_test_identity().email,
        )

    def test_a_writer_is_refused(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        r = client.put(f"/api/admin/inquiries/{_A}/lock", json={"locked": True})
        assert r.status_code == 403
        assert not engine.conn.fetchval.called


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
