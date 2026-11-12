"""Typed structural views of unittest.mock recording and asynchronous boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, overload
from unittest.mock import AsyncMock


if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        Iterable,
        Iterator,
        Mapping,
        Sequence,
    )
    from contextlib import AbstractAsyncContextManager


__all__ = [
    "AsyncMockProtocol",
    "MockCall",
    "MockConnection",
    "MockControl",
    "MockProtocol",
    "MockTransaction",
]


class MockCall(Protocol):
    """Arguments recorded for one call or await."""

    @property
    def args(self) -> tuple[object, ...]:
        """Return the recorded positional arguments."""
        ...

    @property
    def kwargs(self) -> Mapping[str, object]:
        """Return the recorded keyword arguments."""
        ...

    @overload
    def __getitem__(self, index: Literal[0], /) -> tuple[object, ...]: ...

    @overload
    def __getitem__(self, index: Literal[1], /) -> Mapping[str, object]: ...

    def __getitem__(
        self,
        index: Literal[0, 1],
        /,
    ) -> tuple[object, ...] | Mapping[str, object]: ...

    def __iter__(self) -> Iterator[tuple[object, ...] | Mapping[str, object]]: ...


class MockControl(Protocol):
    """Configuration and call recording shared by synchronous and async mocks."""

    side_effect: (
        Callable[..., object]
        | Iterable[object]
        | BaseException
        | type[BaseException]
        | None
    )

    @property
    def call_args(self) -> MockCall | None:
        """Return the last call, or None before the first call."""
        ...

    @property
    def call_args_list(self) -> Sequence[MockCall]:
        """Return every call in order."""
        ...

    @property
    def call_count(self) -> int:
        """Return the number of calls."""
        ...

    @property
    def called(self) -> bool:
        """Say whether the mock has been called."""
        ...

    def assert_called(self) -> None:
        """Assert that at least one call was made."""
        ...

    def assert_called_once(self) -> None:
        """Assert that exactly one call was made."""
        ...

    def assert_not_called(self) -> None:
        """Assert that no call was made."""
        ...

    def assert_called_with(self, *args: object, **kwargs: object) -> None:
        """Assert that the last call used these arguments."""
        ...

    def assert_called_once_with(self, *args: object, **kwargs: object) -> None:
        """Assert that the sole call used these arguments."""
        ...

    def assert_any_call(self, *args: object, **kwargs: object) -> None:
        """Assert that a call used these arguments."""
        ...

    def assert_has_calls(
        self,
        calls: Iterable[object],
        any_order: bool = False,
    ) -> None:
        """Assert that the recorded calls contain these calls."""
        ...

    def reset_mock(
        self,
        *,
        return_value: bool = False,
        side_effect: bool = False,
    ) -> None:
        """Clear recording and optionally clear configured results and effects."""
        ...


class MockProtocol[T](MockControl, Protocol):
    """A synchronous mock whose return schema is fixed by its consumer."""

    @property
    def return_value(self) -> T:
        """Return the configured result."""
        ...

    @return_value.setter
    def return_value(self, value: T) -> None:
        """Set the configured result."""
        ...

    def __call__(self, *args: object, **kwargs: object) -> T:
        """Call the mock and return its configured result."""
        ...


class AsyncMockProtocol[T](MockControl, Protocol):
    """An asynchronous mock whose awaited result has a declared schema."""

    @property
    def return_value(self) -> T:
        """Return the configured result."""
        ...

    @return_value.setter
    def return_value(self, value: T) -> None:
        """Set the configured result."""
        ...

    @property
    def await_args(self) -> MockCall | None:
        """Return the last await, or None before the first await."""
        ...

    @property
    def await_args_list(self) -> Sequence[MockCall]:
        """Return every await in order."""
        ...

    @property
    def await_count(self) -> int:
        """Return the number of awaits."""
        ...

    def __call__(self, *args: object, **kwargs: object) -> Awaitable[T]:
        """Call the mock and await its configured result."""
        ...

    def assert_awaited(self) -> None:
        """Assert that at least one await occurred."""
        ...

    def assert_awaited_once(self) -> None:
        """Assert that exactly one await occurred."""
        ...

    def assert_not_awaited(self) -> None:
        """Assert that no await occurred."""
        ...

    def assert_awaited_with(self, *args: object, **kwargs: object) -> None:
        """Assert that the last await used these arguments."""
        ...

    def assert_awaited_once_with(self, *args: object, **kwargs: object) -> None:
        """Assert that the sole await used these arguments."""
        ...

    def assert_any_await(self, *args: object, **kwargs: object) -> None:
        """Assert that an await used these arguments."""
        ...

    def assert_has_awaits(
        self,
        calls: Iterable[object],
        any_order: bool = False,
    ) -> None:
        """Assert that the recorded awaits contain these calls."""
        ...


class MockConnection(AsyncMock):
    """An AsyncMock connection with typed query methods and field queues."""

    execute: AsyncMockProtocol[object]
    fetch: AsyncMockProtocol[object]
    fetchrow: AsyncMockProtocol[object]
    fetchval: AsyncMockProtocol[object]
    set_type_codec: AsyncMockProtocol[object]
    close: AsyncMockProtocol[object]
    add_listener: AsyncMockProtocol[object]
    is_closed: MockProtocol[bool]
    transaction: MockProtocol[AbstractAsyncContextManager[object]]
    field_default: list[object]
    field_queue: list[object]


class MockTransaction(AsyncMock):
    """An AsyncMock transaction supporting manual and context-managed lifecycles."""

    start: AsyncMockProtocol[object]
    commit: AsyncMockProtocol[object]
    rollback: AsyncMockProtocol[object]
    __aenter__: AsyncMockProtocol[object]
    __aexit__: AsyncMockProtocol[bool | None]
