from asyncio import AbstractEventLoop
from collections.abc import (
    AsyncIterable,
    Awaitable,
    Callable,
    Coroutine,
    Generator,
    Iterable,
    Sequence,
)
from os import PathLike
from typing import (
    Any,
    BinaryIO,
    Generic,
    Literal,
    NamedTuple,
    Protocol,
    overload,
)
from typing_extensions import TypeVar

import contextlib

from _typeshed import StrPath

from . import (
    connect_utils,
    cursor,
    exceptions,
    pool,
    prepared_stmt,
    protocol,
    transaction,
    types,
)
from .protocol import protocol as _cprotocol

_Connection = TypeVar("_Connection", bound=Connection[Any])
type _Writer = Callable[[bytes], Coroutine[Any, Any, None]]
_Record = TypeVar("_Record", bound=protocol.Record, default=protocol.Record)
_OtherRecord = TypeVar("_OtherRecord", bound=protocol.Record)

type _SSLStringValues = Literal[
    "disable",
    "prefer",
    "allow",
    "require",
    "verify-ca",
    "verify-full",
]
type _SSLType = connect_utils._ParsedSSLType | _SSLStringValues | bool
type _HostType = list[str] | tuple[str] | str
type _PortListType = list[int | str] | list[int] | list[str]
type _PortType = _PortListType | int | str

type _OutputType = PathLike[Any] | BinaryIO | _Writer
type _SourceType = PathLike[Any] | BinaryIO | AsyncIterable[bytes]

type _CopyFormat = Literal["text", "csv", "binary"]
type _GSSLibType = Literal["gssapi", "sspi"]

class _Listener(Protocol):
    def __call__(
        self,
        con_ref: Connection[Any] | pool.PoolConnectionProxy[Any],
        pid: int,
        channel: str,
        payload: object,
        /,
    ) -> Awaitable[None] | Generator[Any] | None: ...

class _LogListener(Protocol):
    def __call__(
        self,
        con_ref: Connection[Any] | pool.PoolConnectionProxy[Any],
        message: exceptions.PostgresLogMessage,
        /,
    ) -> Awaitable[None] | Generator[Any] | None: ...

class _TerminationListener(Protocol):
    def __call__(
        self,
        con_ref: Connection[Any] | pool.PoolConnectionProxy[Any],
        /,
    ) -> Awaitable[None] | Generator[Any] | None: ...

class _QueryLogger(Protocol):
    def __call__(
        self,
        record: LoggedQuery,
        /,
    ) -> Awaitable[None] | Generator[Any] | None: ...

class ConnectionMeta(type):
    def __instancecheck__(cls, instance: object) -> bool: ...

class Connection(Generic[_Record], metaclass=ConnectionMeta):
    __slots__ = (
        "__weakref__",
        "_aborted",
        "_addr",
        "_cancellations",
        "_config",
        "_intro_query",
        "_listeners",
        "_log_listeners",
        "_loop",
        "_params",
        "_pool_release_ctr",
        "_protocol",
        "_proxy",
        "_query_loggers",
        "_reset_query",
        "_server_caps",
        "_server_version",
        "_source_traceback",
        "_stmt_cache",
        "_stmt_cache_enabled",
        "_stmt_exclusive_section",
        "_stmts_to_close",
        "_termination_listeners",
        "_top_xact",
        "_transport",
    )
    def __init__(
        self,
        protocol: _cprotocol.BaseProtocol[_Record],
        transport: object,
        loop: AbstractEventLoop,
        addr: tuple[str, int] | str,
        config: connect_utils._ClientConfiguration,
        params: connect_utils._ConnectionParameters,
    ) -> None: ...
    def __del__(self) -> None: ...
    async def add_listener(self, channel: str, callback: _Listener) -> None: ...
    async def remove_listener(self, channel: str, callback: _Listener) -> None: ...
    def add_log_listener(self, callback: _LogListener) -> None: ...
    def remove_log_listener(self, callback: _LogListener) -> None: ...
    def add_termination_listener(self, callback: _TerminationListener) -> None: ...
    def remove_termination_listener(self, callback: _TerminationListener) -> None: ...
    def add_query_logger(self, callback: _QueryLogger) -> None: ...
    def remove_query_logger(self, callback: _QueryLogger) -> None: ...
    def get_server_pid(self) -> int: ...
    def get_server_version(self) -> types.ServerVersion: ...
    def get_settings(self) -> _cprotocol.ConnectionSettings: ...
    def transaction(
        self,
        *,
        isolation: transaction._IsolationLevels | None = ...,
        readonly: bool = ...,
        deferrable: bool = ...,
    ) -> transaction.Transaction: ...
    def is_in_transaction(self) -> bool: ...
    async def execute(
        self,
        query: str,
        *args: object,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    ) -> str: ...
    async def executemany(
        self,
        command: str,
        args: Iterable[Sequence[object]],
        *,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    ) -> None: ...
    @overload
    def cursor(
        self,
        query: str,
        *args: object,
        prefetch: int | None = ...,
        timeout: float | None = ...,
        record_class: None = ...,
    ) -> cursor.CursorFactory[_Record]: ...
    @overload
    def cursor(
        self,
        query: str,
        *args: object,
        prefetch: int | None = ...,
        timeout: float | None = ...,
        record_class: type[_OtherRecord],
    ) -> cursor.CursorFactory[_OtherRecord]: ...
    @overload
    def cursor(
        self,
        query: str,
        *args: object,
        prefetch: int | None = ...,
        timeout: float | None = ...,
        record_class: type[_OtherRecord] | None,
    ) -> cursor.CursorFactory[_Record] | cursor.CursorFactory[_OtherRecord]: ...
    @overload
    async def prepare(
        self,
        query: str,
        *,
        name: str | None = ...,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: None = ...,
    ) -> prepared_stmt.PreparedStatement[_Record]: ...
    @overload
    async def prepare(
        self,
        query: str,
        *,
        name: str | None = ...,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: type[_OtherRecord],
    ) -> prepared_stmt.PreparedStatement[_OtherRecord]: ...
    @overload
    async def prepare(
        self,
        query: str,
        *,
        name: str | None = ...,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: type[_OtherRecord] | None,
    ) -> (
        prepared_stmt.PreparedStatement[_Record]
        | prepared_stmt.PreparedStatement[_OtherRecord]
    ): ...
    @overload
    async def fetch(
        self,
        query: str,
        *args: object,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: None = ...,
    ) -> list[_Record]: ...
    @overload
    async def fetch(
        self,
        query: str,
        *args: object,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: type[_OtherRecord],
    ) -> list[_OtherRecord]: ...
    @overload
    async def fetch(
        self,
        query: str,
        *args: object,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: type[_OtherRecord] | None,
    ) -> list[_Record] | list[_OtherRecord]: ...
    async def fetchval(
        self,
        query: str,
        *args: object,
        column: int = ...,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    ) -> object: ...
    @overload
    async def fetchrow(
        self,
        query: str,
        *args: object,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: None = ...,
    ) -> _Record | None: ...
    @overload
    async def fetchrow(
        self,
        query: str,
        *args: object,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: type[_OtherRecord],
    ) -> _OtherRecord | None: ...
    @overload
    async def fetchrow(
        self,
        query: str,
        *args: object,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: type[_OtherRecord] | None,
    ) -> _Record | _OtherRecord | None: ...
    @overload
    async def fetchmany(
        self,
        query: str,
        args: Iterable[Any],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: None = ...,
    ) -> list[_Record]: ...
    @overload
    async def fetchmany(
        self,
        query: str,
        args: Iterable[Any],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: type[_OtherRecord],
    ) -> list[_OtherRecord]: ...
    @overload
    async def fetchmany(
        self,
        query: str,
        args: Iterable[Any],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        record_class: type[_OtherRecord] | None,
    ) -> list[_Record | _OtherRecord]: ...
    async def copy_from_table(
        self,
        table_name: str,
        *,
        output: _OutputType,
        columns: Iterable[str] | None = ...,
        schema_name: str | None = ...,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        format: _CopyFormat | None = ...,
        oids: int | None = ...,
        delimiter: str | None = ...,
        null: str | None = ...,
        header: bool | None = ...,
        quote: str | None = ...,
        escape: str | None = ...,
        force_quote: bool | Iterable[str] | None = ...,
        encoding: str | None = ...,
    ) -> str: ...
    async def copy_from_query(
        self,
        query: str,
        *args: object,
        output: _OutputType,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        format: _CopyFormat | None = ...,
        oids: int | None = ...,
        delimiter: str | None = ...,
        null: str | None = ...,
        header: bool | None = ...,
        quote: str | None = ...,
        escape: str | None = ...,
        force_quote: bool | Iterable[str] | None = ...,
        encoding: str | None = ...,
    ) -> str: ...
    async def copy_to_table(
        self,
        table_name: str,
        *,
        source: _SourceType,
        columns: Iterable[str] | None = ...,
        schema_name: str | None = ...,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        format: _CopyFormat | None = ...,
        oids: int | None = ...,
        freeze: bool | None = ...,
        delimiter: str | None = ...,
        null: str | None = ...,
        header: bool | None = ...,
        quote: str | None = ...,
        escape: str | None = ...,
        force_quote: bool | Iterable[str] | None = ...,
        force_not_null: bool | Iterable[str] | None = ...,
        force_null: bool | Iterable[str] | None = ...,
        encoding: str | None = ...,
        where: str | None = ...,
    ) -> str: ...
    async def copy_records_to_table(
        self,
        table_name: str,
        *,
        records: Iterable[Sequence[object]] | AsyncIterable[Sequence[object]],
        columns: Iterable[str] | None = ...,
        schema_name: str | None = ...,
        timeout: float | None = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
        where: str | None = ...,
    ) -> str: ...
    async def set_type_codec(
        self,
        typename: str,
        *,
        schema: str = ...,
        encoder: Callable[[Any], Any],
        decoder: Callable[[Any], Any],
        format: str = ...,
    ) -> None: ...
    async def reset_type_codec(self, typename: str, *, schema: str = ...) -> None: ...
    async def set_builtin_type_codec(
        self,
        typename: str,
        *,
        schema: str = ...,
        codec_name: str,
        format: str | None = ...,
    ) -> None: ...
    def is_closed(self) -> bool: ...
    async def close(self, *, timeout: float | None = ...) -> None: ...  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    def terminate(self) -> None: ...
    async def _reset(self) -> None: ...
    async def reset(self, *, timeout: float | None = ...) -> None: ...  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    def get_reset_query(self) -> str: ...
    async def reload_schema_state(self) -> None: ...
    @contextlib.contextmanager
    def query_logger(self, callback: _QueryLogger) -> Generator[None]: ...

@overload
async def connect(
    dsn: str | None = ...,
    *,
    host: _HostType | None = ...,
    port: _PortType | None = ...,
    user: str | None = ...,
    password: connect_utils._PasswordType | None = ...,
    passfile: str | None = ...,
    service: str | None = ...,
    servicefile: StrPath | None = ...,
    database: str | None = ...,
    loop: AbstractEventLoop | None = ...,
    timeout: float = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    statement_cache_size: int = ...,
    max_cached_statement_lifetime: int = ...,
    max_cacheable_statement_size: int = ...,
    command_timeout: float | None = ...,
    ssl: _SSLType | None = ...,
    direct_tls: bool | None = ...,
    record_class: type[_Record],
    server_settings: dict[str, str] | None = ...,
    target_session_attrs: connect_utils.SessionAttribute | None = ...,
    krbsrvname: str | None = ...,
    gsslib: _GSSLibType | None = ...,
) -> Connection[_Record]: ...
@overload
async def connect(
    dsn: str | None = ...,
    *,
    host: _HostType | None = ...,
    port: _PortType | None = ...,
    user: str | None = ...,
    password: connect_utils._PasswordType | None = ...,
    passfile: str | None = ...,
    service: str | None = ...,
    servicefile: StrPath | None = ...,
    database: str | None = ...,
    loop: AbstractEventLoop | None = ...,
    timeout: float = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    statement_cache_size: int = ...,
    max_cached_statement_lifetime: int = ...,
    max_cacheable_statement_size: int = ...,
    command_timeout: float | None = ...,
    ssl: _SSLType | None = ...,
    direct_tls: bool | None = ...,
    connection_class: type[_Connection],
    record_class: type[_Record] = ...,
    server_settings: dict[str, str] | None = ...,
    target_session_attrs: connect_utils.SessionAttribute | None = ...,
    krbsrvname: str | None = ...,
    gsslib: _GSSLibType | None = ...,
) -> _Connection: ...
@overload
async def connect(
    dsn: str | None = ...,
    *,
    host: _HostType | None = ...,
    port: _PortType | None = ...,
    user: str | None = ...,
    password: connect_utils._PasswordType | None = ...,
    passfile: str | None = ...,
    service: str | None = ...,
    servicefile: StrPath | None = ...,
    database: str | None = ...,
    loop: AbstractEventLoop | None = ...,
    timeout: float = ...,  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
    statement_cache_size: int = ...,
    max_cached_statement_lifetime: int = ...,
    max_cacheable_statement_size: int = ...,
    command_timeout: float | None = ...,
    ssl: _SSLType | None = ...,
    direct_tls: bool | None = ...,
    server_settings: dict[str, str] | None = ...,
    target_session_attrs: connect_utils.SessionAttribute | None = ...,
    krbsrvname: str | None = ...,
    gsslib: _GSSLibType | None = ...,
) -> Connection[protocol.Record]: ...

class _ConnectionProxy(Generic[_Record]):
    __slots__ = ()

class LoggedQuery(NamedTuple):
    query: str
    args: Any
    timeout: float | None
    elapsed: float
    exception: BaseException | None
    conn_addr: tuple[str, int] | str
    conn_params: connect_utils._ConnectionParameters

class ServerCapabilities(NamedTuple):
    advisory_locks: bool
    notifications: bool
    plpgsql: bool
    sql_reset: bool
    sql_close_all: bool
    sql_copy_from_where: bool
    jit: bool
