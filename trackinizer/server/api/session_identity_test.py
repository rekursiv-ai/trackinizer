"""Shared session identity boundaries preserve authorization inputs and refusals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.access_policy import DefaultPolicy, Principal
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.session_identity import (
    Asker,
    asker_reader,
    hidden_session,
    is_the_assistant,
    request_principal,
    require_session,
)
from trackinizer.server.audience import SessionReader
from trackinizer.server.config import Assistant, Config
from trackinizer.types.inquiries import AgentSession, Inquiry, Issue


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, Issue(), AgentSession()])
async def test_require_session_returns_only_the_requested_session(
    row: Inquiry | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _engine = make_store()
    session_id = uuid4()
    seen: list[UUID] = []

    async def get_inquiry(target_id: UUID) -> Inquiry | None:
        seen.append(target_id)
        return row

    monkeypatch.setattr(store, "get_inquiry", get_inquiry)
    if isinstance(row, AgentSession):
        assert await require_session(store, session_id) is row
    else:
        with pytest.raises(HTTPException) as error:
            await require_session(store, session_id)
        assert (error.value.status_code, error.value.detail) == (
            404,
            f"unknown session {session_id}",
        )
    assert seen == [session_id]


@pytest.mark.asyncio
async def test_request_principal_resolves_groups_once_and_keeps_identity() -> None:
    policy = _Policy()
    store, _engine = make_store()
    store.access_policy = policy
    app = FastAPI()
    app.state.store = store
    request = Request({"type": "http", "app": app})
    identity = make_test_identity(email="Reader@Example.com", role="writer")

    first = await request_principal(request, identity)
    second = await request_principal(request, identity)

    expected = Principal(
        email="reader@example.com",
        role="writer",
        api_key_id=identity.api_key_id,
    )
    assert policy.seen == [expected]
    assert first == Principal(
        email=expected.email,
        role=expected.role,
        groups=frozenset({"research"}),
        api_key_id=expected.api_key_id,
    )
    assert second is first


@pytest.mark.parametrize("audience", [None, "open", "staff", "private"])
def test_hidden_session_preserves_its_audience(audience: str | None) -> None:
    refused = hidden_session(audience)
    assert (refused.status_code, refused.detail) == (
        403,
        {"code": "session_audience", "audience": audience},
    )


@pytest.mark.parametrize(
    "assistant",
    [None, Assistant(actor="scout", email="bot@example.com")],
)
@pytest.mark.parametrize(
    "email",
    ["bot@example.com", "Bot@example.com", "other@example.com"],
)
def test_only_the_configured_assistant_matches(
    assistant: Assistant | None,
    email: str,
) -> None:
    identity = make_test_identity(email=email)
    assert is_the_assistant(identity, assistant) == (
        assistant is not None and email == "bot@example.com"
    )


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("configured", [False, True])
def test_asker_reader_keeps_personal_rights_and_removes_delegated_oversight(
    *,
    delegated: bool,
    configured: bool,
) -> None:
    app = FastAPI()
    assistant = (
        Assistant(actor="scout", email="bot@example.com") if configured else None
    )
    app.state.config = Config(chat_orgs="domain", assistant=assistant)
    request = Request({"type": "http", "app": app})
    asker = Asker(
        email="Reader@Lab.Example",
        role="admin",
        read_audiences=frozenset({"open", "private", "staff"}),
        hosted_domain="Lab.Example",
        chat=uuid4(),
    )

    reader = asker_reader(request, asker, delegated=delegated)

    assert reader == SessionReader(
        email="reader@lab.example",
        audiences=asker.read_audiences,
        org="lab.example",
        oversight=not delegated,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Policy(DefaultPolicy):
    seen: list[Principal] = field(default_factory=list)

    @override
    async def groups(self, conn: Conn, principal: Principal) -> frozenset[str]:
        del conn
        self.seen.append(principal)
        return frozenset({"research"})


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
