"""Transport retries replay authorized queue deliveries and terminal creation."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncio

from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

import pytest

from trackinizer.conftest import make_store, set_field_row
from trackinizer.server.api import sessions_routes, terminal_routes
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.idempotency import ChangeIdMiddleware
from trackinizer.server.auth import current_user
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.server.terminals import Opening, Terminal, TerminalHub
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_sessions import DrainInboundResponse
from trackinizer.wire.wire_terminals import TerminalOpened, TerminalWait


_MACHINE = UUID(int=1)
_INSTANCE = UUID(int=2)
_SESSION = UUID(int=3)
_IDENTITY = make_test_identity(api_key_id=None)


async def _alive(term: Terminal) -> bool:
    del term
    return True


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@dataclass(frozen=True, slots=True, kw_only=True)
class _Server:
    app: FastAPI
    store: Store
    inbound: InboundQueue
    hub: TerminalHub
    clock: _Clock


def _server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    store, engine = make_store()
    monkeypatch.setattr(
        store,
        "get_inquiry",
        AsyncMock(
            return_value=AgentSession(
                id=_SESSION,
                owner="scientist",
                cli="claude",
                opened_by_api_key_id=_IDENTITY.api_key_id,
            ),
        ),
    )
    monkeypatch.setattr(store, "record_session_seen", AsyncMock())
    set_field_row(engine.conn, {"id": _MACHINE, "facts": {}, "confirmed": True})
    engine.conn.fetchval.return_value = True
    monkeypatch.setattr(terminal_routes, "current_machine", AsyncMock())
    app = FastAPI()
    app.add_middleware(ChangeIdMiddleware)
    app.include_router(sessions_routes.router)
    app.include_router(terminal_routes.router)
    app.dependency_overrides[current_user] = lambda: _IDENTITY
    app.state.engine = engine
    app.state.store = store
    app.state.inbound = inbound = InboundQueue()
    clock = _Clock()
    app.state.terminals = hub = TerminalHub(alive=_alive, clock=clock)
    asyncio.run(hub.wait(_MACHINE, wait_sec=0))
    return _Server(app=app, store=store, inbound=inbound, hub=hub, clock=clock)


def _headers(key: UUID) -> dict[str, str]:
    return {"Idempotency-Key": str(key), "Origin": "http://testserver"}


def test_inbound_retry_returns_the_original_batch_and_leaves_new_messages_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    server.inbound.enqueue(_SESSION, Inbound(text="original"))
    headers = _headers(uuid4())
    with TestClient(server.app) as browser:
        first = browser.get(f"/api/sessions/{_SESSION}/inbound", headers=headers)
        assert first.status_code == 200, first.text
        server.inbound.enqueue(_SESSION, Inbound(text="next"))
        retried = browser.get(f"/api/sessions/{_SESSION}/inbound", headers=headers)
        assert retried.status_code == 200, retried.text
        assert [
            item.text
            for item in DrainInboundResponse.model_validate(retried.json()).messages
        ] == ["original"]
        assert server.inbound.pending(_SESSION) == 1
        monkeypatch.setattr(
            sessions_routes,
            "require_session_access",
            AsyncMock(side_effect=HTTPException(403, "Access revoked")),
        )
        denied = browser.get(f"/api/sessions/{_SESSION}/inbound", headers=headers)
        assert denied.status_code == 403
        assert server.inbound.pending(_SESSION) == 1


def test_terminal_wait_retry_returns_the_original_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    term = server.hub.open(
        Opening(
            user=_IDENTITY.email,
            machine_id=_MACHINE,
            machine="gx10",
            rows=24,
            cols=80,
        ),
    )
    headers = _headers(uuid4())
    with TestClient(server.app) as browser:
        first = browser.get(
            f"/api/machines/{_MACHINE}/terminals/wait",
            params={"instance": str(_INSTANCE), "wait_sec": 0},
            headers=headers,
        )
        assert first.status_code == 200, first.text
        retried = browser.get(
            f"/api/machines/{_MACHINE}/terminals/wait",
            params={"instance": str(_INSTANCE), "wait_sec": 0},
            headers=headers,
        )
        assert retried.status_code == 200, retried.text
        assert TerminalWait.model_validate(retried.json()).terminal_id == term.id


def test_terminal_open_retry_creates_one_terminal_with_a_fresh_attach_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    headers = _headers(uuid4())
    with TestClient(server.app) as browser:
        first = browser.post(
            "/api/terminals",
            json={"machine": "gx10", "rows": 24, "cols": 80},
            headers=headers,
        )
        assert first.status_code == 201, first.text
        opened = TerminalOpened.model_validate(first.json())
        changed = browser.post(
            "/api/terminals",
            json={"machine": "gx10", "rows": 24, "cols": 81},
            headers=headers,
        )
        assert changed.status_code == 409
        server.clock.now += wire_terminals.TICKET_TTL_SEC + 1
        retried = browser.post(
            "/api/terminals",
            json={"machine": "gx10", "rows": 24, "cols": 80},
            headers=headers,
        )
        assert retried.status_code == 201, retried.text
        again = TerminalOpened.model_validate(retried.json())
        assert again.terminal_id == opened.terminal_id
        assert again.ticket != opened.ticket
        assert not server.hub.redeem(
            opened.ticket,
            user=_IDENTITY.email,
            terminal_id=opened.terminal_id,
        )
        assert server.hub.redeem(
            again.ticket,
            user=_IDENTITY.email,
            terminal_id=again.terminal_id,
        )
        offered = asyncio.run(server.hub.wait(_MACHINE, wait_sec=0))
        assert offered is not None
        assert offered.id == opened.terminal_id
        assert asyncio.run(server.hub.wait(_MACHINE, wait_sec=0)) is None
        asyncio.run(server.hub.end(offered, code=wire_terminals.CLOSE_ENDED))
        ended = browser.post(
            "/api/terminals",
            json={"machine": "gx10", "rows": 24, "cols": 80},
            headers=headers,
        )
        assert ended.status_code == 410
        assert asyncio.run(server.hub.wait(_MACHINE, wait_sec=0)) is None


def test_a_retried_run_open_replays_its_terminal_and_a_new_open_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    set_field_row(
        server.app.state.engine.conn,
        {"id": _MACHINE, "facts": {"clis": ["claude"]}, "confirmed": True},
    )
    run = {
        "machine": "gx10",
        "rows": 24,
        "cols": 80,
        "command": "claude",
        "attach": str(_SESSION),
    }
    headers = _headers(uuid4())
    with TestClient(server.app) as browser:
        first = browser.post("/api/terminals", json=run, headers=headers)
        assert first.status_code == 201, first.text
        # The run the first open queued is now attached to the session, yet the
        # retry of that same open is its receipt, not a second run.
        retried = browser.post("/api/terminals", json=run, headers=headers)
        assert retried.status_code == 201, retried.text
        assert (
            TerminalOpened.model_validate(retried.json()).terminal_id
            == TerminalOpened.model_validate(first.json()).terminal_id
        )
        second = browser.post("/api/terminals", json=run, headers=_headers(uuid4()))
        assert second.status_code == 409
        assert second.json() == {
            "detail": {
                "code": "chat_run_live",
                "message": "another run is attached to this session; stop it first",
            },
        }
    assert len(server.hub.attached_to(_SESSION)) == 1


@pytest.mark.asyncio
async def test_a_session_end_before_receipt_work_starts_does_not_park_a_new_hold() -> (
    None
):
    inbound = InboundQueue()
    inbound.mark_poller(_SESSION)
    consuming = asyncio.create_task(
        sessions_routes._drain_inbound(
            inbound,
            session_id=_SESSION,
            wait_sec=3_600,
            taker=None,
        ),
    )
    inbound.forget_poller(_SESSION)
    assert await asyncio.wait_for(consuming, 0.05) == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
