from collections.abc import Iterable, Sequence
from typing import Any, Generic
from typing_extensions import TypeVar

from . import (
    connection as _connection,
    connresource,
    cursor,
    types,
)
from .protocol import (
    Record,
    protocol as _cprotocol,
)

_Record = TypeVar("_Record", bound=Record, default=Record)

class PreparedStatement(connresource.ConnectionResource, Generic[_Record]):
    __slots__ = ("_last_status", "_query", "_state")
    def __init__(
        self,
        connection: _connection.Connection[Any],
        query: str,
        state: _cprotocol.PreparedStatementState[_Record],
    ) -> None: ...
    def get_name(self) -> str: ...
    def get_query(self) -> str: ...
    def get_statusmsg(self) -> str | None: ...
    def get_parameters(self) -> tuple[types.Type, ...]: ...
    def get_attributes(self) -> tuple[types.Attribute, ...]: ...
    def cursor(
        self,
        *args: object,
        prefetch: int | None = ...,
        timeout: float | None = ...,
    ) -> cursor.CursorFactory[_Record]: ...
    async def explain(self, *args: object, analyze: bool = ...) -> Any: ...
    async def fetch(
        self,
        *args: object,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    ) -> list[_Record]: ...
    async def fetchval(
        self,
        *args: object,
        column: int = ...,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    ) -> object: ...
    async def fetchrow(
        self,
        *args: object,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    ) -> _Record | None: ...
    async def fetchmany(
        self,
        args: Iterable[Sequence[object]],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    ) -> list[_Record]: ...
    async def executemany(
        self,
        args: Iterable[Sequence[object]],
        *,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    ) -> None: ...
    def __del__(self) -> None: ...
