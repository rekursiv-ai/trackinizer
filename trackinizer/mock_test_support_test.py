"""The typed mock view preserves the external mock's return and recording behavior."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, Mock, call

import pytest

from trackinizer.mock_test_support import AsyncMockProtocol, MockCall


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


def _assert_recording(recorded: MockCall) -> None:
    assert recorded.args == ("SELECT", 2)
    assert recorded.kwargs == {"timeout": 3}
    assert recorded[0] == ("SELECT", 2)
    assert recorded[1] == {"timeout": 3}


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
