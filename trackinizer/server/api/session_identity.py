"""Shared session identities and readers, independent of chat authorization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import uuid

from fastapi import HTTPException

from trackinizer.server.access_policy import Principal
from trackinizer.server.api._deps import get_assistant, get_chat_orgs, get_store
from trackinizer.server.audience import SessionReader, caller_reader
from trackinizer.server.auth import Role
from trackinizer.server.config import Config
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.audience import AUDIENCES


if TYPE_CHECKING:
    from fastapi import FastAPI, Request

    from trackinizer.server.auth import AuthIdentity
    from trackinizer.server.config import Assistant
    from trackinizer.server.store.core import Store


@dataclass(frozen=True, slots=True, kw_only=True)
class Asker:
    """The person the configured assistant acts for, as the server finds them.

    An account no active user holds is found holding nothing: no audience, and the
    least role, which writes nowhere.

    Attributes:
      email: Their account.
      role: Their role as a person, which no key narrows.
      read_audiences: The audiences they hold as a person, which no key narrows.
      hosted_domain: Google's ``hd`` claim from their last login, if any.
      chat: The conversation the assistant reads into for them, if it names one.

    """

    email: str
    role: Role
    read_audiences: frozenset[str]
    hosted_domain: str | None = None
    chat: uuid.UUID | None = None


async def require_session(store: Store, session_id: uuid.UUID) -> AgentSession:
    """Fetch an AgentSession row or raise 404; reject non-session ids.

    Args:
      store: The store to read from.
      session_id: The id a route was given.

    Returns:
      session: The session row.

    Raises:
      HTTPException: 404: ``session_id`` is no session.

    """
    row = await store.get_inquiry(session_id)
    if not isinstance(row, AgentSession):
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    return row


async def request_principal(request: Request, identity: AuthIdentity) -> Principal:
    """Describe ``identity`` as the access policy sees it, once per request.

    The policy's :meth:`~trackinizer.server.access_policy.AccessPolicy.groups`
    runs on the first call and its answer is kept on ``request.state``, so every
    later check in the same request reads the same groups without a query.

    Args:
      request: The request, which carries the store and its policy.
      identity: Authenticated caller.

    Returns:
      principal: Its lowercase email, its role, the policy's groups for it, the
        API key it used and how the transcript test reads it.

    """
    held = cast(object, getattr(request.state, "access_principal", None))
    if isinstance(held, Principal):
        return held
    store = get_store(request)
    asking = Principal(
        email=identity.email.lower(),
        role=identity.role,
        api_key_id=identity.api_key_id,
    )
    async with store.engine.acquire() as conn:
        groups = await store.access_policy.groups(conn, asking)
    principal = Principal(
        email=asking.email,
        role=asking.role,
        groups=groups,
        api_key_id=asking.api_key_id,
    )
    request.state.access_principal = principal
    return principal


def session_reader(
    request: Request,
    identity: AuthIdentity,
    *,
    hidden: bool = True,
) -> SessionReader:
    """Describe ``identity`` as the transcript test reads it on this server.

    Under ``--no-auth`` the one caller reads every audience, every session and every
    chat but a private one.

    Args:
      request: The request, which carries the configuration.
      identity: Authenticated caller.
      hidden: Keep private sessions; a listing passes False unless asked for them.

    Returns:
      reader: As :func:`~trackinizer.server.audience.caller_reader` derives it for
        the server's organisation mode.

    """
    assistant = get_assistant(request)
    config: object = getattr(cast("FastAPI", request.app).state, "config", None)
    if isinstance(config, Config) and config.auth_disabled:
        return SessionReader(
            audiences=AUDIENCES,
            email=identity.email.lower(),
            every_session=True,
            oversight=True,
            hidden=hidden,
        )
    return caller_reader(
        identity.email,
        audiences=identity.read_audiences,
        admin=identity.role == "admin",
        orgs=get_chat_orgs(request),
        assistant="" if assistant is None else assistant.email,
        hosted_domain=identity.hosted_domain,
        hidden=hidden,
    )


def hidden_session(audience: str | None) -> HTTPException:
    """Build the refusal for a session whose transcript the caller does not read.

    Args:
      audience: The session's audience, named in the body so a caller knows why.

    Returns:
      refusal: A 403 whose detail carries ``code: session_audience``.

    """
    return HTTPException(
        status_code=403,
        detail={"code": "session_audience", "audience": audience},
    )


def is_the_assistant(identity: AuthIdentity, assistant: Assistant | None) -> bool:
    """Say whether ``identity`` is the configured assistant's account."""
    return assistant is not None and identity.email == assistant.email


def asker_reader(request: Request, asker: Asker) -> SessionReader:
    """Describe the person the assistant acts for as the transcript test reads them.

    Args:
      request: The request, which carries the configuration.
      asker: The person, as ``session_access.resolve_asker`` finds them.

    Returns:
      reader: Their reader on this server.

    """
    assistant = get_assistant(request)
    return caller_reader(
        asker.email,
        audiences=asker.read_audiences,
        admin=asker.role == "admin",
        orgs=get_chat_orgs(request),
        assistant="" if assistant is None else assistant.email,
        hosted_domain=asker.hosted_domain,
    )
