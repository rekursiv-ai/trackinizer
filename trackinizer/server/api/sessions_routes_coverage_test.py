"""Unit coverage for session lifecycle, run reports and drain admission."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi import HTTPException, Request

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.api import sessions_routes
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.private import chat_sessions
from trackinizer.server.api.session_identity import request_principal
from trackinizer.server.config import Config
from trackinizer.server.inbound import Inbound, InboundQueue, PollerRun
from trackinizer.server.private.chat_hub import ChatHub
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.bodies import SubmitAgentSession
from trackinizer.wire.wire_science_chat import CHAT_TITLE
from trackinizer.wire.wire_sessions import (
    SendMessage,
    SessionEnd,
    SessionRunState,
    SessionStart,
)


@pytest.mark.asyncio
async def test_chat_start_never_publishes_supplied_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = make_store()
    identity = make_test_identity()
    session = AgentSession(
        cli="codex",
        cli_session_id=f"chat:{uuid4()}",
        account=identity.email,
        owner="runner#2",
        audience="open",
        opened_by_api_key_id=identity.api_key_id,
    )
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store, config=Config()),
            ),
            "state": {},
        },
    )
    start = AsyncMock(return_value=(session.id, session.owner, 3))
    monkeypatch.setattr(store, "start_session", start)
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    active = AsyncMock()
    monkeypatch.setattr(sessions_routes, "assert_account_active", active)
    chat_audience = AsyncMock(return_value=None)
    monkeypatch.setattr(chat_sessions, "chat_audience", chat_audience)
    nudge = MagicMock()
    monkeypatch.setattr(sessions_routes, "nudge_streams", nudge)
    principal = await request_principal(request, identity)
    response = await sessions_routes.session_start_route(
        SessionStart(
            cli="codex",
            cli_session_id=session.cli_session_id,
            title="secret conversation",
            actor="runner",
        ),
        request,
        identity,
    )
    start.assert_awaited_once_with(
        SubmitAgentSession(
            title=CHAT_TITLE,
            cli="codex",
            cli_session_id=session.cli_session_id,
            started=None,
            rooms=None,
            account=identity.email,
            idempotency_key=None,
        ),
        requested_actor="runner",
        audience="open",
        api_key_id=identity.api_key_id,
        attach=None,
        principal=principal,
    )
    active.assert_awaited_once_with(store.engine, identity.email)
    chat_audience.assert_awaited_once_with(
        request,
        cli_session_id=session.cli_session_id,
    )
    assert (response.id, response.seq, response.actor, response.cli_session_id) == (
        session.id,
        3,
        "runner#2",
        session.cli_session_id,
    )
    nudge.assert_called_once_with(request)
    assert engine.conn.fetchrow.await_count == 0


@pytest.mark.asyncio
async def test_bare_multiroom_send_refuses_without_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _engine = make_store()
    inbound, session_id = InboundQueue(), uuid4()
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    store=store,
                    inbound=inbound,
                ),
            ),
            "state": {},
        },
    )
    resolve = AsyncMock(return_value=[(session_id, ("z", "a"))])
    monkeypatch.setattr(store, "resolve_live_sessions", resolve)
    monkeypatch.setattr(sessions_routes, "resolve_asker", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as refused:
        await sessions_routes.send_message_route(
            SendMessage(actor="runner", text="hello"),
            request,
            make_test_identity(),
        )
    assert refused.value.status_code == 409
    assert refused.value.detail == (
        "@runner is in rooms ['a', 'z']; address one explicitly (e.g. @runner:a)"
    )
    assert inbound.pending(session_id) == 0
    resolve.assert_awaited_once_with("runner", room=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [None, "a" * 32, "b" * 32])
async def test_run_report_nudges_only_for_kept_state_changes(
    monkeypatch: pytest.MonkeyPatch,
    held: str | None,
) -> None:
    session_id = uuid4()
    inbound, hub = InboundQueue(), ChatHub()
    if held is not None:
        inbound.mark_poller(session_id, instance=held)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(inbound=inbound, hub=hub),
            ),
        },
    )
    authorize = AsyncMock()
    monkeypatch.setattr(sessions_routes, "authorize_drain", authorize)
    identity = make_test_identity()
    body = SessionRunState(state="running", instance="a" * 32)
    with hub.subscribe(uuid4()) as stream:
        await sessions_routes.session_run_state_route(
            session_id,
            body,
            request,
            identity,
        )
        assert stream.nudged.is_set() == (held == body.instance)
        stream.nudged.clear()
        await sessions_routes.session_run_state_route(
            session_id,
            body,
            request,
            identity,
        )
        assert not stream.nudged.is_set()
    assert inbound.poller_run(session_id) == (
        None
        if held is None
        else PollerRun(
            instance=held,
            state="running" if held == body.instance else "idle",
        )
    )
    assert authorize.await_count == 2
    authorize.assert_awaited_with(
        request,
        identity,
        session_id=session_id,
        cli=None,
        names_cli=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("other_provider", [False, True])
async def test_late_end_preserves_replacement_run_and_queued_messages(
    monkeypatch: pytest.MonkeyPatch,
    other_provider: bool,
) -> None:
    store, _engine = make_store()
    identity = make_test_identity()
    session = AgentSession(cli="codex", opened_by_api_key_id=identity.api_key_id)
    inbound = InboundQueue()
    inbound.mark_poller(session.id, cli="codex", instance="b" * 32)
    message = Inbound(text="keep")
    inbound.enqueue(session.id, message)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    store=store,
                    inbound=inbound,
                ),
            ),
        },
    )
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    monkeypatch.setattr(sessions_routes, "require_session_access", AsyncMock())
    monkeypatch.setattr(
        chat_sessions,
        "require_chat_writer",
        AsyncMock(),
    )
    monkeypatch.setattr(
        chat_sessions,
        "chat_provider",
        AsyncMock(return_value="codex"),
    )
    release, end, nudge = AsyncMock(), AsyncMock(), MagicMock()
    monkeypatch.setattr(sessions_routes, "release_open_asks", release)
    monkeypatch.setattr(store, "end_session", end)
    monkeypatch.setattr(sessions_routes, "nudge_streams", nudge)
    response = await sessions_routes.session_end_route(
        session.id,
        SessionEnd(
            cli="claude" if other_provider else "codex",
            instance="a" * 32,
        ),
        request,
        identity,
    )
    assert (response.id, response.ended) == (session.id, None)
    assert inbound.poller_run(session.id) == PollerRun(cli="codex", instance="b" * 32)
    assert inbound.drain(session.id) == [message]
    release.assert_not_awaited()
    end.assert_not_awaited()
    nudge.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_keeps_queue", [False, True])
async def test_end_guards_chat_id_releases_asks_and_records_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    chat_keeps_queue: bool,
) -> None:
    store, _engine = make_store()
    identity = make_test_identity()
    session = AgentSession(
        cli="codex",
        cli_session_id="vendor-id",
        opened_by_api_key_id=identity.api_key_id,
    )
    inbound = InboundQueue()
    inbound.mark_poller(session.id, instance="a" * 32, cli="codex")
    inbound.enqueue(session.id, Inbound(text="pending"))
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    store=store,
                    inbound=inbound,
                ),
            ),
        },
    )
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    monkeypatch.setattr(sessions_routes, "require_session_access", AsyncMock())
    monkeypatch.setattr(
        chat_sessions,
        "require_chat_writer",
        AsyncMock(),
    )
    monkeypatch.setattr(
        chat_sessions,
        "chat_provider",
        AsyncMock(return_value="codex"),
    )
    release_unread = AsyncMock(return_value=chat_keeps_queue)
    monkeypatch.setattr(chat_sessions, "release_unread", release_unread)
    ended = datetime.now(UTC)
    guard, release, end = AsyncMock(), AsyncMock(), AsyncMock(return_value=ended)
    order = MagicMock()
    order.attach_mock(guard, "guard")
    order.attach_mock(release, "release")
    order.attach_mock(end, "end")
    monkeypatch.setattr(sessions_routes, "require_session_id_change", guard)
    monkeypatch.setattr(sessions_routes, "release_open_asks", release)
    monkeypatch.setattr(store, "end_session", end)
    nudge = MagicMock()
    monkeypatch.setattr(sessions_routes, "nudge_streams", nudge)
    body = SessionEnd(
        ended=ended,
        cli="codex",
        instance="a" * 32,
        cli_session_id=f"chat:{uuid4()}",
        failed="executable missing",
    )
    response = await sessions_routes.session_end_route(
        session.id,
        body,
        request,
        identity,
    )
    assert (response.id, response.ended) == (session.id, ended)
    guard.assert_awaited_once_with(
        request,
        identity,
        session.id,
        value=body.cli_session_id,
    )
    assert [entry[0] for entry in order.mock_calls] == ["guard", "release", "end"]
    end.assert_awaited_once_with(
        session.id,
        ended=ended,
        cli_session_id=body.cli_session_id,
        api_key_id=identity.api_key_id,
        actor=identity.email,
    )
    assert inbound.poller_run(session.id) is None
    refusal = inbound.refusal(session.id)
    assert refusal is not None
    assert refusal.reason == body.failed
    assert inbound.pending(session.id) == int(chat_keeps_queue)
    release_unread.assert_awaited_once_with(request, session=session)
    nudge.assert_called_once_with(request)


@pytest.mark.asyncio
async def test_stopped_drain_refuses_before_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound, session_id = InboundQueue(), uuid4()
    inbound.mark_poller(session_id, instance="a" * 32)
    inbound.stop_run(session_id)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(inbound=inbound),
            ),
        },
    )
    authorize = AsyncMock()
    monkeypatch.setattr(sessions_routes, "authorize_drain", authorize)
    with pytest.raises(HTTPException) as refused:
        await sessions_routes._admit_drain(
            request,
            make_test_identity(),
            session_id=session_id,
            cli="codex",
            instance="a" * 32,
        )
    assert refused.value.status_code == 403
    assert refused.value.detail == {
        "code": "run_stopped",
        "message": "This run was stopped",
    }
    authorize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["revived", "closed", "held"])
async def test_drain_revives_or_refuses_closed_and_replaced_runs(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    store, _engine = make_store()
    identity = make_test_identity()
    active = AgentSession(cli="codex", status="active")
    session = active if case == "held" else replace(active, ended=datetime.now(UTC))
    inbound = InboundQueue()
    if case == "held":
        inbound.mark_poller(session.id, cli="codex", instance="b" * 32)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store, inbound=inbound),
            ),
        },
    )
    authorize = AsyncMock(return_value=session)
    revive = AsyncMock(return_value=case == "revived")
    fetch = AsyncMock(return_value=active)
    monkeypatch.setattr(sessions_routes, "authorize_drain", authorize)
    monkeypatch.setattr(sessions_routes, "revive_if_reaped", revive)
    monkeypatch.setattr(store, "get_inquiry", fetch)
    monkeypatch.setattr(
        chat_sessions,
        "chat_provider",
        AsyncMock(return_value="codex"),
    )
    if case == "revived":
        assert (
            await sessions_routes._admit_drain(
                request,
                identity,
                session_id=session.id,
                cli="codex",
                instance="a" * 32,
            )
            is True
        )
        fetch.assert_awaited_once_with(session.id)
    else:
        with pytest.raises(HTTPException) as refused:
            await sessions_routes._admit_drain(
                request,
                identity,
                session_id=session.id,
                cli="codex",
                instance="a" * 32,
            )
        assert refused.value.status_code == 409
        assert refused.value.detail == (
            {"code": "session_ended", "message": "This chat was closed; its run stops"}
            if case == "closed"
            else {
                "code": "chat_run_live",
                "message": "Another run is draining this chat; stop it first",
            }
        )
        fetch.assert_not_awaited()
    authorize.assert_awaited_once_with(
        request,
        identity,
        session_id=session.id,
        cli="codex",
        names_cli=True,
    )
    if case == "held":
        revive.assert_not_awaited()
    else:
        revive.assert_awaited_once_with(store, session_id=session.id)


@pytest.mark.asyncio
async def test_attach_requires_write_access_and_chat_run_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _engine = make_store()
    identity = make_test_identity(role="viewer")
    session = AgentSession(opened_by_api_key_id=identity.api_key_id)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store),
            ),
        },
    )
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    access, writer = AsyncMock(), AsyncMock()
    monkeypatch.setattr(sessions_routes, "require_session_access", access)
    monkeypatch.setattr(chat_sessions, "require_chat_writer", writer)
    assert (
        await sessions_routes._require_attach(
            request,
            identity,
            session_id=session.id,
        )
        is session
    )
    access.assert_awaited_once_with(request, identity, session=session, mode="write")
    writer.assert_awaited_once_with(request, identity, session=session)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
