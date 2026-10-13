"""Sharing and deleting a chat: what each asks the database, and who it refuses."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast

import asyncio
import uuid

import pytest

from trackinizer.conftest import FakeEngine
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.config import Assistant
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.visuals import chat_shares
from trackinizer.server.visuals.chat_forks import ChatRefusedError
from trackinizer.server.visuals.chat_shares import (
    UnboundStation,
    delete_chat,
    set_share,
)
from trackinizer.server.visuals.science_chats import Conversation
from trackinizer.wire.wire_science_chat import chat_session_id
from trackinizer.wire.wire_sessions import WorkspaceMessageContext


if TYPE_CHECKING:
    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


_CHAT: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SESSIONS: Final = (
    uuid.UUID("22222222-2222-2222-2222-222222222222"),
    uuid.UUID("33333333-3333-3333-3333-333333333333"),
)
_STATION: Final = uuid.UUID("44444444-4444-4444-4444-444444444444")
_OWNER: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")
_KEY: Final = uuid.UUID("66666666-6666-6666-6666-666666666666")
_ADA: Final = "ada@example.com"
_ASSISTANT: Final = Assistant(actor="kb", email="kb@example.com")
_UNBIND: Final = (
    "UPDATE visual_workspaces SET station = station - 'conversation_id', "
    "revision = revision + 1 "
    "WHERE station->>'conversation_id' = $1 RETURNING id, user_id"
)


def test_the_starter_shares_a_chat_with_a_lowercase_email() -> None:
    engine, shared = _share(_caller(_ADA), email="Bob@Example.com", shared=True)

    assert shared == ["bob@example.com"]
    assert _executed(engine)[-2:] == [
        (
            (
                "INSERT INTO science_chat_shares (conversation_id, email, shared_by) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING"
            ),
            (_CHAT, "bob@example.com", _ADA),
        ),
        ("COMMIT", ()),
    ]


def test_an_admin_unshares_a_chat_of_someone_else() -> None:
    admin = _caller("root@example.com", role="admin")

    engine, shared = _share(admin, email="Bob@Example.com", shared=False)

    assert shared == ["bob@example.com"]
    assert (
        "DELETE FROM science_chat_shares WHERE conversation_id = $1 AND email = $2",
        (_CHAT, "bob@example.com"),
    ) in _executed(engine)


def test_anyone_else_is_refused_and_a_chat_that_is_not_found_is_none() -> None:
    bob = _caller("bob@example.com")

    with pytest.raises(ChatRefusedError) as refused:
        _ = _share(bob, email="b@example.com", shared=True)

    assert refused.value.code == "chat_not_starter"
    assert _share(bob, email="b@example.com", shared=True, found=False)[1] is None


def test_a_delete_purges_each_session_drops_its_shares_and_moves_its_stations() -> None:
    engine = FakeEngine()
    engine.conn.fetch.return_value = [{"id": str(_STATION), "user_id": str(_OWNER)}]

    stations, purged = _delete(engine, _caller(_ADA, key=_KEY))
    executed = _executed(engine)

    assert stations == [UnboundStation(workspace_id=_STATION, user_id=_OWNER)]
    assert purged == list(_SESSIONS)
    assert executed[1] == (
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        (f"trackinizer.science_chat.{_CHAT}",),
    )
    assert (
        (
            "SELECT id FROM inquiries WHERE kind = 'AgentSession' "
            "AND agentsession_cli_session_id = $1 ORDER BY id FOR UPDATE"
        ),
        (chat_session_id(_CHAT),),
    ) in executed
    assert (
        (
            "UPDATE change_log SET old_title = CASE WHEN kind = 'title' THEN '' END, "
            "new_title = CASE WHEN kind = 'title' THEN '' END, "
            "old_description = NULL, new_description = NULL "
            "WHERE subject_id = ANY($1::uuid[]) AND kind IN ('title', 'description')"
        ),
        (list(_SESSIONS),),
    ) in executed
    assert (
        "DELETE FROM science_chat_shares WHERE conversation_id = $1",
        (_CHAT,),
    ) in executed
    assert (
        (
            "INSERT INTO science_chat_tombstones (conversation_id, deleted_by) "
            "VALUES ($1, $2) ON CONFLICT DO NOTHING"
        ),
        (_CHAT, _ADA),
    ) in executed
    assert engine.conn.fetch.await_args is not None
    assert engine.conn.fetch.await_args.args == (_UNBIND, str(_CHAT))


@pytest.mark.parametrize(
    ("email", "role", "opened"),
    [("root@example.com", "admin", False), ("kb@example.com", "writer", True)],
    ids=["admin", "opening-key"],
)
def test_an_admin_and_the_opening_key_delete_a_chat_others_posted_in(
    email: str,
    role: Role,
    opened: bool,
) -> None:
    caller = _caller(email, role=role, key=_KEY)

    _, purged = _delete(FakeEngine(), caller, others_posted=True, opened=opened)

    assert purged == list(_SESSIONS)


def test_a_line_another_person_has_queued_counts_as_posted_and_ones_own_goes() -> None:
    bobs = InboundQueue()
    bobs.enqueue(_SESSIONS[0], _line("bob@example.com"))
    adas = InboundQueue()
    adas.enqueue(_SESSIONS[0], _line(_ADA.upper()))
    adas.enqueue(_SESSIONS[1], _line(_ADA, conversation=uuid.uuid4()))

    for session in _SESSIONS:
        adas.mark_poller(session)

    with pytest.raises(ChatRefusedError) as refused:
        _ = _delete(FakeEngine(), _caller(_ADA), inbound=bobs)
    _, purged = _delete(FakeEngine(), _caller(_ADA), inbound=adas)

    assert refused.value.code == "chat_has_other_posters"
    assert bobs.pending(_SESSIONS[0]) == 1
    assert purged == list(_SESSIONS)
    assert (adas.pending(_SESSIONS[0]), adas.pending(_SESSIONS[1])) == (0, 1)
    assert [adas.has_poller(session) for session in _SESSIONS] == [False, False]


def test_a_delete_refused_or_of_nothing_changes_nothing() -> None:
    engine = FakeEngine()

    with pytest.raises(ChatRefusedError) as refused:
        _ = _delete(engine, _caller(_ADA), others_posted=True)
    nothing, purged = _delete(engine, _caller(_ADA), found=False)

    assert refused.value.code == "chat_has_other_posters"
    assert (nothing, purged) == (None, [])
    assert all(call.args[0] != _UNBIND for call in engine.conn.fetch.await_args_list)


def _caller(
    email: str,
    *,
    role: Role = "writer",
    key: uuid.UUID | None = None,
) -> AuthIdentity:
    return AuthIdentity(user_id=uuid.uuid4(), api_key_id=key, email=email, role=role)


def _share(
    caller: AuthIdentity,
    *,
    email: str,
    shared: bool,
    found: bool = True,
) -> tuple[FakeEngine, list[str] | None]:
    """Share Ada's chat as ``caller``; return the engine and the answer."""
    engine = FakeEngine()
    engine.conn.fetch.return_value = [{"email": email.lower()}]
    read = partial(_found, expected=caller, others_posted=False, opened=False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(chat_shares, "read_conversation", read if found else _missing)
        answer = asyncio.run(
            set_share(
                cast("DatabaseEngine", engine),
                assistant=_ASSISTANT,
                caller=caller,
                conversation_id=_CHAT,
                email=email,
                shared=shared,
            ),
        )
    return engine, answer


def _delete(
    engine: FakeEngine,
    caller: AuthIdentity,
    *,
    others_posted: bool = False,
    opened: bool = False,
    found: bool = True,
    inbound: InboundQueue | None = None,
) -> tuple[list[UnboundStation] | None, list[uuid.UUID]]:
    """Delete Ada's chat as ``caller``; return the stations and each session purged."""
    purged: list[uuid.UUID] = []
    store = SimpleNamespace(
        engine=engine,
        purge_held=partial(_purge_into, purged, caller=caller),
    )
    read = partial(
        _found,
        expected=caller,
        others_posted=others_posted,
        opened=opened,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(chat_shares, "read_conversation", read if found else _missing)
        stations = asyncio.run(
            delete_chat(
                cast("Store", store),
                assistant=_ASSISTANT,
                caller=caller,
                conversation_id=_CHAT,
                inbound=inbound or InboundQueue(),
            ),
        )
    return stations, purged


async def _purge_into(
    purged: list[uuid.UUID],
    conn: object,
    session_id: uuid.UUID,
    *,
    caller: AuthIdentity,
    **audit: object,
) -> None:
    """Note a session ``purge_held`` was given, with the audit it was asked to write."""
    assert conn is not None
    assert audit == {
        "kind": "AgentSession",
        "api_key_id": caller.api_key_id,
        "actor": caller.email,
        "reason": "Deleted with its science chat",
    }
    purged.append(session_id)


async def _found(
    conn: object,
    *,
    expected: AuthIdentity,
    others_posted: bool,
    opened: bool,
    **asked: object,
) -> Conversation:
    """Find Ada's chat of two sessions for ``expected``, as the server reads it."""
    assert conn is not None
    assert asked == {
        "assistant": _ASSISTANT,
        "caller": expected,
        "conversation_id": _CHAT,
    }
    return Conversation(
        session_id=_SESSIONS[0],
        cli_session_id=chat_session_id(_CHAT),
        title="tails",
        account=_ADA,
        modified=datetime(2026, 1, 1, tzinfo=UTC),
        live=True,
        sessions=list(_SESSIONS),
        opened=opened,
        others_posted=others_posted,
    )


def _line(sender: str, *, conversation: uuid.UUID = _CHAT) -> Inbound:
    """Build a line ``sender`` posted into a conversation, not yet read."""
    return Inbound(
        text="q",
        source=sender,
        context=WorkspaceMessageContext(
            workspace_id=_STATION,
            visible_visuals=[],
            conversation_id=conversation,
        ),
    )


async def _missing(conn: object, **asked: object) -> None:
    del conn, asked


def _executed(engine: FakeEngine) -> list[tuple[object, tuple[object, ...]]]:
    """List each statement ``execute`` ran, with its arguments."""
    return [
        (call.args[0], tuple(call.args[1:]))
        for call in engine.conn.execute.await_args_list
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
