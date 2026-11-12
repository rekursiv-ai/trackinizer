"""Shared session identity boundaries preserve authorization inputs and refusals."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast, override
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.access_policy import DefaultPolicy, Principal
from trackinizer.server.api.conftest import (
    SCOUT,
    make_caller,
    make_test_identity,
)
from trackinizer.server.api.session_identity import (
    Asker,
    asker_reader,
    hidden_session,
    is_the_assistant,
    request_principal,
    require_session,
    session_reader,
)
from trackinizer.server.audience import SessionReader
from trackinizer.server.config import Assistant, Config
from trackinizer.types.inquiries import AgentSession, Inquiry, Issue
from trackinizer.wire.audience import AUDIENCES


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn
    from trackinizer.server.auth import Role
    from trackinizer.server.config import ChatOrgs


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


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("role", ["writer", "admin"])
def test_asker_reader_keeps_the_humans_personal_rights(
    role: Role,
    *,
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
        role=role,
        read_audiences=frozenset({"open", "private"}),
        hosted_domain="Lab.Example",
        chat=uuid4(),
    )

    reader = asker_reader(request, asker)

    assert reader == SessionReader(
        email="reader@lab.example",
        audiences=asker.read_audiences,
        org="lab.example",
        oversight=role == "admin",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Policy(DefaultPolicy):
    seen: list[Principal] = field(default_factory=list)

    @override
    async def groups(self, conn: Conn, principal: Principal) -> frozenset[str]:
        del conn
        self.seen.append(principal)
        return frozenset({"research"})


@pytest.mark.parametrize(
    ("orgs", "email", "hosted_domain", "org", "oversight"),
    [
        ("domain", "Ann@Lab.Example", "Lab.example", "lab.example", True),
        ("domain", "Ann@Lab.Example", None, None, True),
        ("domain", "sam@gmail.com", None, None, True),
        ("domain", "agent@service.invalid", "service.invalid", None, False),
        ("domain", "scout@example.com", "example.com", None, False),
        ("single", "Ann@Lab.Example", "lab.example", None, True),
        ("single", "agent@service.invalid", None, None, False),
        ("single", "scout@example.com", None, None, False),
        ("individual", "Ann@Lab.Example", "lab.example", None, True),
        ("individual", "scout@example.com", None, None, False),
    ],
)
def test_session_reader_follows_the_chat_orgs_setting(
    orgs: ChatOrgs,
    *,
    email: str,
    hosted_domain: str | None,
    org: str | None,
    oversight: bool,
) -> None:
    """Only a person has an organisation, which Google's hd claim names, and oversees.

    One server reads every session.
    """
    state = SimpleNamespace(config=Config(assistant=SCOUT, chat_orgs=orgs))
    request = cast("Request", SimpleNamespace(app=SimpleNamespace(state=state)))

    reader = session_reader(
        request,
        make_caller("open", "staff", email=email, hosted_domain=hosted_domain),
    )
    listing = session_reader(request, make_caller("open", email=email), hidden=False)

    assert reader == SessionReader(
        audiences=frozenset({"open", "staff"}),
        email=email.lower(),
        org=org,
        every_session=orgs == "single",
        oversight=oversight,
    )
    assert (listing.hidden, listing.oversight) == (False, False)


def test_without_auth_the_one_caller_reads_every_session_and_open_chat() -> None:
    state = SimpleNamespace(config=Config(auth_disabled=True, chat_orgs="individual"))
    request = cast("Request", SimpleNamespace(app=SimpleNamespace(state=state)))

    reader = session_reader(request, make_caller(email="no-auth@localhost"))

    assert reader == SessionReader(
        audiences=AUDIENCES,
        email="no-auth@localhost",
        every_session=True,
        oversight=True,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
