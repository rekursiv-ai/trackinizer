"""The typed mock view preserves the external mock's return and recording behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, call

import pytest

from trackinizer.mock_test_support import (
    AsyncMockProtocol,
    MockCall,
    MockConnection,
    MockTransaction,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn
    from trackinizer.mock_test_support import MockProtocol


def test_a_real_call_satisfies_the_recording_contract() -> None:
    mock = Mock(return_value=None)
    mock("SELECT", 2, timeout=3)
    assert mock.call_args is not None
    _assert_recording(mock.call_args)


@pytest.mark.asyncio
async def test_a_typed_async_mock_keeps_arguments_and_return_values() -> None:
    mock = cast(AsyncMockProtocol[int], AsyncMock(return_value=7))

    assert await mock("SELECT", 2, timeout=3) == 7
    assert mock.await_args is not None
    assert mock.call_args is not None
    assert mock.await_args.args == ("SELECT", 2)
    assert mock.call_args[0] == ("SELECT", 2)
    assert mock.call_args[1] == {"timeout": 3}
    assert mock.await_args.kwargs == {"timeout": 3}
    assert mock.await_args_list == [call("SELECT", 2, timeout=3)]
    mock.assert_awaited_once_with("SELECT", 2, timeout=3)
    assert (mock.call_count, mock.await_count, mock.called) == (1, 1, True)
    mock.reset_mock()
    mock.assert_not_called()
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_typed_async_mock_preserves_side_effects() -> None:
    mock = cast(AsyncMockProtocol[int], AsyncMock())
    mock.side_effect = [3, RuntimeError("unavailable")]

    assert await mock() == 3
    with pytest.raises(RuntimeError, match="unavailable"):
        _ = await mock()
    assert mock.await_count == 2


@pytest.mark.asyncio
async def test_connection_keeps_typed_query_recording_and_nominal_compatibility() -> (
    None
):
    conn = MockConnection()
    conn.fetchval = AsyncMock(side_effect=[7, None])
    connections: list[Conn] = [conn]

    assert connections == [conn]
    assert await conn.fetchval("SELECT", 2, timeout=3) == 7
    assert await conn.fetchval("SELECT next") is None
    _assert_recording(conn.fetchval.call_args_list[0])
    assert conn.mock_calls == [
        ("fetchval", ("SELECT", 2), {"timeout": 3}),
        ("fetchval", ("SELECT next",), {}),
    ]


@pytest.mark.asyncio
async def test_transaction_keeps_manual_and_context_managed_recording() -> None:
    conn = MockConnection()
    transaction = MockTransaction()
    conn.transaction = Mock(return_value=transaction)

    assert conn.transaction() is transaction
    await transaction.start()
    await transaction.rollback()
    transaction.start.assert_awaited_once_with()
    transaction.rollback.assert_awaited_once_with()
    transaction.commit.assert_not_awaited()
    async with transaction:
        await conn.execute("SELECT")
    transaction.__aenter__.assert_awaited_once_with()
    transaction.__aexit__.assert_awaited_once_with(None, None, None)


@pytest.mark.asyncio
async def test_generic_mock_payloads_keep_typed_members_after_assignment() -> None:
    await _assert_connection_payload(Mock(return_value=MockConnection()))
    await _assert_connection_payload(AsyncMock(return_value=MockConnection()))


async def _assert_connection_payload(
    mock: MockProtocol[MockConnection] | AsyncMockProtocol[MockConnection],
) -> None:
    mock.return_value.fetchval = AsyncMock(return_value=7)
    assert await mock.return_value.fetchval("SELECT", 2, timeout=3) == 7
    _assert_recording(mock.return_value.fetchval.call_args_list[0])

    replacement = MockConnection()
    replacement.fetchval = AsyncMock(return_value=9)
    mock.return_value = replacement
    assert mock.return_value is replacement
    assert await mock.return_value.fetchval("SELECT next") == 9
    mock.return_value.fetchval.assert_awaited_once_with("SELECT next")


def _assert_recording(recorded: MockCall) -> None:
    assert recorded.args == ("SELECT", 2)
    assert recorded.kwargs == {"timeout": 3}
    assert recorded[0] == ("SELECT", 2)
    assert recorded[1] == {"timeout": 3}


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
