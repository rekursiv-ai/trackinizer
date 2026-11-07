"""``recompute_authority`` against a mocked connection: the exact writes per column."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import uuid

import pytest

from trackinizer.conftest import executed_sql, make_conn, make_store
from trackinizer.server.store.authority import AUTHORITY_RELATIONS


if TYPE_CHECKING:
    from trackinizer.mock_test_support import MockConnection


def _serve_edges(
    conn: MockConnection,
    edges_by_kind: dict[str, list[dict[str, object]]],
) -> None:
    """Answer each relation's edge read from ``edges_by_kind``."""

    async def fetch(sql: str, *args: object) -> list[dict[str, object]]:
        if sql.startswith("SELECT from_id, to_id, valence FROM edges"):
            (kind,) = args
            assert isinstance(kind, str)
            return edges_by_kind.get(kind, [])
        return []

    conn.fetch = AsyncMock(side_effect=fetch)


def _score_writes(conn: MockConnection) -> dict[str, tuple[object, ...]]:
    """Return each column's score UPDATE arguments, keyed by column."""
    writes: dict[str, tuple[object, ...]] = {}
    for call in conn.execute.call_args_list:
        sql = call.args[0]
        assert isinstance(sql, str)
        if "v.score" in sql:
            column = sql.removeprefix("UPDATE inquiries AS i SET ").split(" ")[0]
            writes[column] = call.args[1:]
    return writes


def test_every_relation_is_ranked() -> None:
    columns = {r.column for r in AUTHORITY_RELATIONS}
    assert columns == {
        "proves_authority",
        "favors_authority",
        "cited_by_authority",
        "issue_authority",
    }
    assert {r.edge_kind for r in AUTHORITY_RELATIONS if r.weighted} == {
        "proves",
        "favors",
    }


@pytest.mark.asyncio
async def test_recompute_writes_reached_nodes_inside_one_transaction() -> None:
    paper, claim = uuid.uuid4(), uuid.uuid4()
    child, prereq, parent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn = make_conn()
    _serve_edges(
        conn,
        {
            "proves": [{"from_id": paper, "to_id": claim, "valence": -0.5}],
            "requires": [{"from_id": child, "to_id": prereq, "valence": None}],
            "narrows": [{"from_id": child, "to_id": parent, "valence": None}],
        },
    )
    store, engine = make_store(conn)

    # Reusing the held connection must not acquire again: PGlite would deadlock.
    async with engine.acquire() as held:
        written = await store.recompute_authority(conn=held)

    # One reached node for proves; requires and narrows union into one graph.
    assert written == 3
    sql = executed_sql(conn)
    assert sql[0].startswith("SELECT from_id")
    assert sql.count("BEGIN") == 1
    assert sql[-1] == "COMMIT"
    begin = sql.index("BEGIN")
    assert all(s.startswith("SELECT from_id") for s in sql[:begin])
    for reset in (
        (
            "UPDATE inquiries SET proves_authority = NULL "
            "WHERE proves_authority IS NOT NULL"
        ),
        (
            "UPDATE inquiries SET favors_authority = NULL "
            "WHERE favors_authority IS NOT NULL"
        ),
        (
            "UPDATE inquiries SET cited_by_authority = NULL "
            "WHERE cited_by_authority IS NOT NULL"
        ),
        "UPDATE inquiries SET issue_authority = NULL WHERE issue_authority IS NOT NULL",
    ):
        assert sql.index(reset) > begin
    writes = _score_writes(conn)
    assert set(writes) == {"proves_authority", "issue_authority"}
    ids, scores = writes["proves_authority"]
    assert ids == [claim]
    (score,) = cast(list[float], scores)
    assert score > 0.0
    issue_ids, _ = writes["issue_authority"]
    assert set(cast(list[uuid.UUID], issue_ids)) == {prereq, parent}


@pytest.mark.asyncio
async def test_recompute_without_conn_acquires_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()
    _serve_edges(conn, {})
    store, engine = make_store(conn)
    acquire = MagicMock(wraps=engine.acquire)
    monkeypatch.setattr(engine, "acquire", acquire)

    assert await store.recompute_authority() == 0

    acquire.assert_called_once_with()
    assert conn.fetch.await_count == len(AUTHORITY_RELATIONS)
    assert _score_writes(conn) == {}
    assert executed_sql(conn)[-1] == "COMMIT"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
