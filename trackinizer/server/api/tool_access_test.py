"""The rule's checks, in order, over a stand-in connection.

That each part of its query reads what it should is ``tool_routes_pglite_test``'s: there,
each change that breaks the rule is applied to a real database, at the open and at the
sweep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast
from uuid import UUID, uuid4

import pytest

from trackinizer.conftest import make_conn
from trackinizer.server.api.tool_access import serves_tools, tool_access
from trackinizer.server.config import Assistant


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn
    from trackinizer.mock_test_support import MockConnection
    from trackinizer.server.api.tool_access import Check, ToolAccess


_KB: Final = Assistant(actor="kb", email="kb@x.y")
_HOST: Final = UUID(int=1)
_DAEMON: Final = UUID(int=2)
_KEY: Final = UUID(int=3)

_HOLDS: Final[dict[str, object]] = {
    "key": True,
    "asker": True,
    "id": _HOST,
    "facts": {"tools": True},
    "confirmed": True,
    "daemon_instance": _DAEMON,
    "credentialed": True,
}
"""The row of a host whose every check holds."""


def _answering(row: dict[str, object]) -> MockConnection:
    conn = make_conn()
    conn.fetch.return_value = [row]
    return conn


async def _ask(
    conn: MockConnection,
    *,
    assistant: Assistant | None = _KB,
    api_key_id: UUID | None = _KEY,
    asker: str | None = "ada@x.y",
    host: UUID | str = _HOST,
    instance: UUID | None = None,
) -> ToolAccess:
    return await tool_access(
        cast("Conn", conn),
        assistant=assistant,
        api_key_id=api_key_id,
        asker=asker,
        host=host,
        instance=instance,
    )


@pytest.mark.parametrize(
    ("changed", "check"),
    [
        ({}, None),
        ({"key": False}, "key"),
        ({"key": None, "asker": False}, "key"),
        ({"asker": False}, "asker"),
        ({"asker": False, "id": None}, "asker"),
        ({"id": None, "credentialed": None, "confirmed": None}, "host"),
        ({"credentialed": False, "confirmed": False}, "credential"),
        ({"confirmed": False, "facts": {}}, "confirmed"),
        ({"facts": {"tools": "true"}, "daemon_instance": None}, "tools"),
        ({"daemon_instance": None}, "instance"),
        ({"daemon_instance": uuid4()}, "instance"),
    ],
)
@pytest.mark.asyncio
async def test_the_first_check_that_fails_is_named(
    changed: dict[str, object],
    check: Check | None,
) -> None:
    access = await _ask(_answering(_HOLDS | changed), instance=_DAEMON)
    assert access.failed == check
    assert access.host_id == (None if "id" in changed else _HOST)


@pytest.mark.asyncio
async def test_a_pipe_no_host_has_attached_is_not_asked_of_its_host() -> None:
    access = await _ask(_answering(_HOLDS | {"daemon_instance": None}))
    assert access.failed is None


@pytest.mark.parametrize(
    ("host", "by_id", "by_name"),
    [(_HOST, _HOST, None), ("gx10", None, "gx10")],
)
@pytest.mark.asyncio
async def test_one_query_names_the_key_assistant_asker_and_host(
    host: UUID | str,
    by_id: UUID | None,
    by_name: str | None,
) -> None:
    conn = _answering(_HOLDS)
    _ = await _ask(conn, asker="Ada@X.Y", host=host)
    (asked,) = conn.fetch.await_args_list
    _, *arguments = asked.args
    assert arguments == [_KEY, "kb@x.y", "ada@x.y", by_id, by_name]


@pytest.mark.asyncio
async def test_no_key_or_asker_is_bound_as_none() -> None:
    conn = _answering(_HOLDS | {"key": False, "asker": False, "id": None})
    access = await _ask(conn, api_key_id=None, asker=None)
    (asked,) = conn.fetch.await_args_list
    assert asked.args[1:4] == (None, "kb@x.y", None)
    assert access.failed == "key"


@pytest.mark.asyncio
async def test_a_server_with_no_assistant_fails_at_once_and_asks_nothing() -> None:
    conn = _answering(_HOLDS)
    access = await _ask(conn, assistant=None)
    assert (access.host_id, access.failed) == (None, "assistant")
    conn.fetch.assert_not_awaited()


@pytest.mark.parametrize(
    ("facts", "serves"),
    [
        ({"tools": True}, True),
        ({"tools": "true"}, False),
        ({"tools": 1}, False),
        ({}, False),
        (["tools"], False),
        (None, False),
    ],
)
def test_only_a_true_tools_fact_serves_tools(facts: object, serves: bool) -> None:
    assert serves_tools(facts) is serves


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
