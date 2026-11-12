"""Tests for the task-scoped client change-id slot."""

from __future__ import annotations

from contextvars import Context
from typing import TYPE_CHECKING

import asyncio

import pytest

from trackinizer.conftest import make_conn, make_store, new_uuid
from trackinizer.server.store.change_id_slot import (
    _CLIENT_CHANGE_ID,
    _consume_client_change_id,
    _peek_client_change_id,
    set_client_change_id,
)
from trackinizer.types.cost import Cost


if TYPE_CHECKING:
    from uuid import UUID


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
            call.args[1]
            for call in conn.execute.call_args_list
            if isinstance(call.args[0], str)
            and "INSERT INTO change_log" in call.args[0]
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


class TestSlotWithoutHolder:
    """A context that never set a key has nothing to clear, peek, or consume."""

    def test_clear_without_holder_leaves_slot_empty(self) -> None:
        assert Context().run(_clear_then_peek) == (None, None)

    def test_consume_without_holder_returns_none(self) -> None:
        assert Context().run(_consume_client_change_id) is None

    def test_consume_returns_key_once_then_none(self) -> None:
        key = new_uuid()
        assert Context().run(_set_then_consume_twice, key) == (key, None)


def _clear_then_peek() -> tuple[object, object]:
    set_client_change_id(None)
    return _CLIENT_CHANGE_ID.get(), _peek_client_change_id()


def _set_then_consume_twice(key: UUID) -> tuple[object, object]:
    set_client_change_id(key)
    return _consume_client_change_id(), _consume_client_change_id()


def test_an_uninstalled_slot_clears_and_consumes_without_installing_a_holder() -> None:
    token = _CLIENT_CHANGE_ID.set(None)
    try:
        set_client_change_id(None)
        assert _CLIENT_CHANGE_ID.get() is None
        assert _peek_client_change_id() is None
        assert _consume_client_change_id() is None
    finally:
        _CLIENT_CHANGE_ID.reset(token)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
