"""The rule's checks, in order, over a stand-in connection.

That each part of its query reads what it should is the hand-run
``private/tool_routes_parity.py``'s: there, each change that breaks the rule is applied
to a real database, at the open and at the sweep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import UUID, uuid4

import pytest

from trackinizer.conftest import make_conn
from trackinizer.server.api.tool_access import _RULE, serves_tools, tool_access
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
    api_key_id: UUID | Literal["browser"] | None = _KEY,
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
    query, *arguments = asked.args
    assert query is _RULE
    assert arguments == [_KEY, "kb@x.y", "ada@x.y", by_id, by_name]


@pytest.mark.asyncio
async def test_a_host_id_a_driver_hands_as_text_is_read_as_a_uuid() -> None:
    access = await _ask(_answering(_HOLDS | {"id": str(_HOST)}))
    assert access.host_id == _HOST


@pytest.mark.asyncio
async def test_no_key_or_asker_is_bound_as_none() -> None:
    conn = _answering(_HOLDS | {"key": False, "asker": False, "id": None})
    access = await _ask(conn, api_key_id=None, asker=None)
    (asked,) = conn.fetch.await_args_list
    assert asked.args[1:4] == (None, "kb@x.y", None)
    assert access.failed == "key"


@pytest.mark.parametrize("instance", [None, _DAEMON])
@pytest.mark.asyncio
async def test_no_key_is_refused_unless_the_caller_says_it_is_the_askers_browser(
    instance: UUID | None,
) -> None:
    """At the open and at the sweep alike: no key is no browser."""
    keyless = _HOLDS | {"key": False}
    refused = await _ask(_answering(keyless), api_key_id=None, instance=instance)
    assert refused.failed == "key"

    conn = _answering(keyless)
    access = await _ask(conn, api_key_id="browser", instance=instance)

    (asked,) = conn.fetch.await_args_list
    assert asked.args[1:4] == (None, "kb@x.y", "ada@x.y")
    assert (access.host_id, access.failed) == (_HOST, None)


@pytest.mark.parametrize(
    ("changed", "check"),
    [
        ({"asker": False}, "asker"),
        ({"id": None}, "host"),
        ({"facts": {"exec": True}}, "tools"),
    ],
)
@pytest.mark.asyncio
async def test_the_askers_browser_still_needs_an_active_writer_and_a_host_of_theirs(
    changed: dict[str, object],
    check: Check,
) -> None:
    keyless = _HOLDS | {"key": False}
    access = await _ask(_answering(keyless | changed), api_key_id="browser")
    assert access.failed == check


@pytest.mark.asyncio
async def test_with_no_assistant_a_key_reaches_its_own_askers_host_alone() -> None:
    conn = _answering(_HOLDS)
    access = await _ask(conn, assistant=None)
    (asked,) = conn.fetch.await_args_list
    assert asked.args[1:4] == (_KEY, None, "ada@x.y")
    assert (access.host_id, access.failed) == (_HOST, None)


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
