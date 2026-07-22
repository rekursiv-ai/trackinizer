"""Tests for the task-scoped client change-id slot."""

from __future__ import annotations

import asyncio

import pytest

from trackinizer.conftest import make_conn, make_store, new_uuid
from trackinizer.server.store.change_id_slot import (
    _peek_client_change_id,
    set_client_change_id,
)
from trackinizer.types.cost import Cost


class TestClientChangeIdGather:
    """``set_client_change_id`` is task-scoped under ``asyncio.gather``.

    Each child task copied from the parent context sees the same
    client-supplied UUID. The first to consume wins; the second
    must mint a fresh server-side change_id rather than reuse the
    same UUID (which collides on ``change_log.id`` and is treated
    as a replay).
    """

    @pytest.mark.asyncio
    async def test_gather_siblings_do_not_share_client_change_id(self) -> None:
        conn = make_conn()
        conn.fetchval.return_value = "Issue"
        store, _engine = make_store(conn)
        client_id = new_uuid()
        set_client_change_id(client_id)
        # A non-zero delta is required: ``add_cost`` short-circuits a zero
        # delta before emitting (see ``test_zero_delta_returns_none_...``),
        # so each sibling must carry real cost to reach ``emit_change``.
        await asyncio.gather(
            store.add_cost(new_uuid(), Cost(agent_usd=1.0), actor="alice"),
            store.add_cost(new_uuid(), Cost(agent_usd=1.0), actor="bob"),
        )
        change_ids = [
            call.args[1]  # ``id`` is the first column in ``emit_change``.
            for call in conn.execute.call_args_list
            if "INSERT INTO change_log" in call.args[0]
        ]
        assert len(change_ids) == 2
        # Without coordination both siblings see client_id and use it
        # twice, collapsing to a single distinct id. The contract is
        # that at most one INSERT carries the client-supplied UUID.
        assert client_id in change_ids
        assert len(set(change_ids)) == 2, (
            f"expected one fresh server-minted id, both used {client_id}"
        )

    @pytest.mark.asyncio
    async def test_clearing_the_slot_drains_it_for_gather_siblings(self) -> None:
        """``set_client_change_id(None)`` clears the slot for sibling tasks too.

        The replay/no-op drain paths clear the slot so a leftover key cannot leak
        into the next mutation. Because gather siblings share the holder object by
        reference, a clear must MUTATE the shared cell, not just rebind the
        current task's ContextVar -- otherwise a sibling still sees the stale key.
        """
        set_client_change_id(new_uuid())
        ready = asyncio.Event()
        cleared = asyncio.Event()

        async def clearer() -> None:
            await ready.wait()
            set_client_change_id(None)
            cleared.set()

        async def peeker() -> object:
            ready.set()
            await cleared.wait()
            return _peek_client_change_id()

        _, seen = await asyncio.gather(clearer(), peeker())
        assert seen is None


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
