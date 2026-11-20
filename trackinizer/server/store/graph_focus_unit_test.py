"""Unit tests of breadth-first traversal and its query bounds."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from trackinizer.conftest import (
    RecordingPolicy,
    make_conn,
    make_store,
    new_uuid,
)
from trackinizer.server.access_policy import Principal, inquiry_read_filter
from trackinizer.server.store.graph_focus import read_neighbourhood


@pytest.mark.asyncio
async def test_missing_focus_stops_before_any_hop() -> None:
    conn = make_conn()
    conn.fetchval.return_value = []
    store, _ = make_store(conn)
    focus = new_uuid()
    async with store.engine.acquire() as held:
        assert await read_neighbourhood(held, focus=[focus], hops=3, limit=4) == {}
    assert conn.fetchval.await_count == 1
    call = conn.fetchval.call_args
    assert call is not None
    assert call.args[1:] == ([focus],)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 3, 4])
async def test_hops_carry_frontier_seen_and_remaining_room(limit: int) -> None:
    conn = make_conn()
    focus, first, second = new_uuid(), new_uuid(), new_uuid()
    conn.fetchval.side_effect = [[focus], [first, second], []]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        found = await read_neighbourhood(held, focus=[focus], hops=4, limit=limit)
    assert found == ({focus: 0} if limit == 1 else {focus: 0, first: 1, second: 1})
    calls = conn.fetchval.call_args_list
    assert len(calls) == (1 if limit == 1 else 2 if limit == 3 else 3)
    if limit > 1:
        assert calls[1].args[1:] == ([focus], [focus], limit - 1)
    if limit == 4:
        assert calls[2].args[1:] == ([first, second], [focus, first, second], 1)


@pytest.mark.asyncio
async def test_hop_count_stops_a_nonempty_frontier() -> None:
    conn = make_conn()
    focus, peer = new_uuid(), new_uuid()
    conn.fetchval.side_effect = [[focus], [peer]]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        found = await read_neighbourhood(held, focus=[focus], hops=1, limit=3)
    assert found == {focus: 0, peer: 1}
    assert conn.fetchval.await_count == 2


@pytest.mark.asyncio
async def test_each_focus_takes_a_share_of_hop_one_then_the_rest_tops_it_up() -> None:
    conn = make_conn()
    a, b, x, y = new_uuid(), new_uuid(), new_uuid(), new_uuid()
    # The read answers in its own order; the walk keeps the order asked.
    conn.fetchval.side_effect = [[b, a], [x], [y], []]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        found = await read_neighbourhood(held, focus=[a, b], hops=1, limit=5)
    assert list(found.items()) == [(a, 0), (b, 0), (x, 1), (y, 1)]
    sent = [call.args[1:] for call in conn.fetchval.call_args_list]
    assert sent == [
        ([a, b],),
        # Three places left over two foci: a takes at most two.
        ([a], [a, b], 2),
        # What a took is out of b's reach, and b may have both places left.
        ([b], [a, b, x], 2),
        ([a, b], [a, b, x, y], 1),
    ]


@pytest.mark.asyncio
async def test_no_focus_reads_once_the_shares_fill_the_limit() -> None:
    conn = make_conn()
    a, b, x = new_uuid(), new_uuid(), new_uuid()
    conn.fetchval.side_effect = [[a, b], [x]]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        found = await read_neighbourhood(held, focus=[a, b], hops=2, limit=3)
    assert found == {a: 0, b: 0, x: 1}
    # The one place left is a's share; b and the top-up have none, so read nothing.
    assert [call.args[1:] for call in conn.fetchval.call_args_list] == [
        ([a, b],),
        ([a], [a, b], 1),
    ]


@pytest.mark.asyncio
async def test_the_next_hop_walks_on_from_what_every_share_took() -> None:
    conn = make_conn()
    a, b, x, y = new_uuid(), new_uuid(), new_uuid(), new_uuid()
    conn.fetchval.side_effect = [[a, b], [x], [y], [], []]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        await read_neighbourhood(held, focus=[a, b], hops=2, limit=9)
    hop_two = conn.fetchval.call_args_list[-1]
    assert hop_two.args[1:] == ([x, y], [a, b, x, y], 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("gated", [False, True])
async def test_the_foci_are_read_in_one_query_gated_by_the_readers_access(
    gated: bool,
) -> None:
    conn = make_conn()
    conn.fetchval.return_value = []
    store, _ = make_store(conn)
    focus = new_uuid()
    policy = RecordingPolicy() if gated else None
    reader = Principal(email="ada@example.com", role="writer")
    async with store.engine.acquire() as held:
        await read_neighbourhood(
            held,
            focus=[focus],
            hops=1,
            limit=5,
            policy=policy,
            principal=reader,
        )
    call = conn.fetchval.call_args
    assert call is not None
    start = "SELECT ARRAY(SELECT id FROM inquiries WHERE id = ANY($1::uuid[])"
    gate = inquiry_read_filter(
        "inquiries.id",
        policy=policy,
        principal=reader,
        params=[[focus]],
    )
    assert str(call.args[0]) == (f"{start} AND ({gate}))" if gated else f"{start})")
    if gated:
        assert "readable_inquiry.id = inquiries.id AND (" in gate


@pytest.mark.asyncio
@pytest.mark.parametrize("gated", [False, True])
async def test_a_hop_reads_its_neighbours_gated_by_the_readers_access(
    gated: bool,
) -> None:
    conn = make_conn()
    focus = new_uuid()
    conn.fetchval.side_effect = [[focus], []]
    store, _ = make_store(conn)
    policy = RecordingPolicy() if gated else None
    reader = Principal(email="ada@example.com", role="writer")
    async with store.engine.acquire() as held:
        await read_neighbourhood(
            held,
            focus=[focus],
            hops=1,
            limit=5,
            policy=policy,
            principal=reader,
        )
    call = conn.fetchval.call_args
    assert call is not None
    gate = inquiry_read_filter(
        "n.id",
        policy=policy,
        principal=reader,
        params=[[focus], [focus], 4],
    )
    start = (
        "SELECT ARRAY(SELECT n.id FROM ("
        " SELECT to_id AS id FROM edges WHERE from_id = ANY($1::uuid[])"
        " UNION SELECT from_id FROM edges WHERE to_id = ANY($1::uuid[])"
        ") AS n WHERE n.id <> ALL($2::uuid[])"
    )
    end = (
        " ORDER BY (SELECT created FROM inquiries WHERE id = n.id) DESC, n.id DESC"
        " LIMIT $3)"
    )
    readable = f" AND ({gate})" if gated else ""
    assert str(call.args[0]) == start + readable + end
    if gated:
        assert "readable_inquiry.id = n.id AND (" in gate


@pytest.mark.asyncio
async def test_every_share_reads_through_the_access_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()
    a, b = new_uuid(), new_uuid()
    conn.fetchval.side_effect = [[a, b], [], [], []]
    monkeypatch.setattr(RecordingPolicy, "read_filter", Mock(return_value="FALSE"))
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        await read_neighbourhood(
            held,
            focus=[a, b],
            hops=1,
            limit=5,
            policy=RecordingPolicy(),
            principal=Principal(email="ada@example.com", role="writer"),
        )
    sql = [str(call.args[0]) for call in conn.fetchval.call_args_list]
    assert len(sql) == 4
    assert all("(FALSE)" in each for each in sql)


@pytest.mark.asyncio
async def test_an_unreadable_focus_is_dropped_and_the_rest_walk_alone() -> None:
    conn = make_conn()
    hidden, shown, peer = new_uuid(), new_uuid(), new_uuid()
    conn.fetchval.side_effect = [[shown], [peer]]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        found = await read_neighbourhood(held, focus=[hidden, shown], hops=1, limit=3)
    assert found == {shown: 0, peer: 1}
    call = conn.fetchval.call_args
    assert call is not None
    assert call.args[1:] == ([shown], [shown], 2)


@pytest.mark.asyncio
async def test_foci_past_the_limit_are_not_walked() -> None:
    conn = make_conn()
    a, b = new_uuid(), new_uuid()
    conn.fetchval.side_effect = [[a, b]]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        assert await read_neighbourhood(held, focus=[a, b], hops=2, limit=1) == {a: 0}
    assert conn.fetchval.await_count == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
