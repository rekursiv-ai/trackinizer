"""Unit tests of breadth-first traversal and its query bounds."""

from __future__ import annotations

import pytest

from trackinizer.conftest import make_conn, make_store, new_uuid
from trackinizer.server.store.graph_focus import read_neighbourhood


@pytest.mark.asyncio
async def test_missing_focus_stops_before_any_hop() -> None:
    conn = make_conn()
    conn.fetchval.return_value = False
    store, _ = make_store(conn)
    focus = new_uuid()
    async with store.engine.acquire() as held:
        assert await read_neighbourhood(held, focus, hops=3, limit=4) == {}
    assert conn.fetchval.await_count == 1
    call = conn.fetchval.call_args
    assert call is not None
    assert call.args[1:] == (focus,)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 3, 4])
async def test_hops_carry_frontier_seen_and_remaining_room(limit: int) -> None:
    conn = make_conn()
    focus, first, second = new_uuid(), new_uuid(), new_uuid()
    conn.fetchval.side_effect = [True, [first, second], []]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        found = await read_neighbourhood(held, focus, hops=4, limit=limit)
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
    conn.fetchval.side_effect = [True, [peer]]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        found = await read_neighbourhood(held, focus, hops=1, limit=3)
    assert found == {focus: 0, peer: 1}
    assert conn.fetchval.await_count == 2


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
