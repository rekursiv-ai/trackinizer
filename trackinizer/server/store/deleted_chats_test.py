"""A deleted chat's id: what each guard asks the database, and what it refuses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import asyncio
import uuid

import pytest

from trackinizer.conftest import make_conn
from trackinizer.server.store.deleted_chats import (
    chat_was_deleted,
    lock_chat_id,
    refuse_a_deleted_chat,
    session_was_purged,
)
from trackinizer.types.errors import ChatDeletedError


if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from trackinizer.lib.postgres import Conn


_CHAT: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_LOCK: Final = (
    "SELECT pg_advisory_xact_lock(hashtext($1))",
    f"trackinizer.science_chat.{_CHAT}",
)
_TOMBSTONE: Final = (
    "SELECT EXISTS (SELECT 1 FROM science_chat_tombstones WHERE conversation_id = $1)",
    _CHAT,
)


def test_a_chat_id_is_locked_by_a_key_of_its_own() -> None:
    conn: AsyncMock = make_conn()

    asyncio.run(lock_chat_id(cast("Conn", conn), conversation_id=_CHAT))

    conn.execute.assert_awaited_once_with(*_LOCK)


@pytest.mark.parametrize("stands", [True, False])
def test_a_chat_was_deleted_when_its_tombstone_stands(stands: bool) -> None:
    conn: AsyncMock = make_conn()
    conn.fetchval.return_value = stands

    deleted = asyncio.run(chat_was_deleted(cast("Conn", conn), conversation_id=_CHAT))

    assert deleted is stands
    conn.fetchval.assert_awaited_once_with(*_TOMBSTONE)


def test_a_deleted_chat_is_refused_under_its_lock_and_another_passes() -> None:
    deleted: AsyncMock = make_conn()
    deleted.fetchval.return_value = True
    standing: AsyncMock = make_conn()
    standing.fetchval.return_value = False

    with pytest.raises(ChatDeletedError):
        asyncio.run(refuse_a_deleted_chat(cast("Conn", deleted), conversation_id=_CHAT))
    asyncio.run(refuse_a_deleted_chat(cast("Conn", standing), conversation_id=_CHAT))

    for conn in (deleted, standing):
        conn.execute.assert_awaited_once_with(*_LOCK)
        conn.fetchval.assert_awaited_once_with(*_TOMBSTONE)


def test_a_fork_holds_its_original_too_the_smaller_id_first() -> None:
    conn: AsyncMock = make_conn()
    conn.fetchval.return_value = False
    origin = uuid.UUID("00000000-0000-0000-0000-000000000001")

    asyncio.run(
        refuse_a_deleted_chat(
            cast("Conn", conn),
            conversation_id=_CHAT,
            forked_from=origin,
        ),
    )

    assert [call.args for call in conn.execute.await_args_list] == [
        (_LOCK[0], f"trackinizer.science_chat.{origin}"),
        _LOCK,
    ]
    conn.fetchval.assert_awaited_once_with(*_TOMBSTONE)


@pytest.mark.parametrize("purged", [True, False])
def test_a_session_was_purged_when_the_change_log_says_so(purged: bool) -> None:
    conn: AsyncMock = make_conn()
    conn.fetchval.return_value = purged

    found = asyncio.run(session_was_purged(cast("Conn", conn), session_id=_CHAT))

    assert found is purged
    conn.fetchval.assert_awaited_once_with(
        "SELECT EXISTS (SELECT 1 FROM change_log "
        "WHERE subject_id = $1 AND kind = 'purged')",
        _CHAT,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
