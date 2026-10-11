"""Authorize who may read, write, drain and message an AgentSession.

A session says who may read its transcript: its ``audience``. A caller whose
``read_audiences`` holds it may read, write, drain and end the session, and the
key that opened a session always may, whatever the audience, so a key whose owner
lost ``staff`` can still run the ``staff`` session it opened. Sending input needs the audience too, or the
configured assistant, which relays a user's line to a lead it does not read: only for
an asker it names, who is that session's account or holds its audience, and who is a
writer. The assistant passes on the asker's rights, not its own, and the server finds
them in its own tables. Only the transcript is gated: a hidden session still has an
open envelope, and the rows it files stay open.

A session opens into the audience its key is pinned to, else ``open``, and only into
one its caller may open into: :func:`granted_audience` refuses a pin the caller no
longer holds instead of lowering the session to ``open``.

A viewer key mutates only the session it opened. A science chat is stricter: it is
public to every signed-in user, so any writer may read it, but only the key that
opened it writes it. Its lines name their poster, and its labels and account say who
started it and who posted, so a second writer's write would forge a poster or hide the
chat from History. A chat is known by its ``chat:`` id alone, whoever opened it and
whether or not an assistant is configured: a user's own ``trax helper`` opens chats
under that user's key, and answers the last line of one with that user's local CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import uuid

from fastapi import HTTPException
from treekle import from_plain

from trackinizer.server.api._deps import get_assistant, get_staff, get_store
from trackinizer.server.audience import audiences_for
from trackinizer.server.auth import ROLE_ORDER, Role
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.audience import OPEN
from trackinizer.wire.wire_science_chat import CHAT_SESSION_PREFIX


if TYPE_CHECKING:
    from fastapi import Request

    from trackinizer.server.auth import AuthIdentity
    from trackinizer.server.config import Assistant
    from trackinizer.server.store.core import Store


__all__ = [
    "Asker",
    "SessionAccess",
    "granted_audience",
    "hidden_session",
    "is_a_science_chat",
    "require_chat_opener",
    "require_session",
    "require_session_access",
    "require_session_row_write",
    "require_session_write_access",
    "resolve_asker",
]


type SessionAccess = Literal["read", "write", "drain", "input"]
"""What a caller does to a session: read its transcript, write or end it, drain its
inbound queue, or send it input."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Asker:
    """The person the configured assistant sends a line for, as the server finds them.

    Attributes:
      email: Their account.
      role: Their role as a person, which no key narrows.
      read_audiences: The audiences they hold as a person, which no key narrows.

    """

    email: str
    role: Role
    read_audiences: frozenset[str]


async def resolve_asker(
    request: Request,
    identity: AuthIdentity,
    *,
    email: str | None,
) -> Asker | None:
    """Find the user the assistant says it sends for, if the sender is the assistant.

    The assistant is trusted to say whom it speaks for, and no further: what that user
    may do the server reads from its own tables. Anyone else's naming an asker is
    ignored, since it gives them nothing.

    Args:
      request: The request, which carries the store and the configuration.
      identity: Authenticated sender.
      email: The account the body names, or ``None``.

    Returns:
      asker: The named user, or ``None`` when no one is named, the sender is not the
        assistant, or the account is unknown or disabled.

    """
    if email is None or not _is_the_assistant(identity, get_assistant(request)):
        return None
    async with get_store(request).engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT email, role, hosted_domain FROM users "
            "WHERE email = $1 AND status = 'active'",
            email.lower(),
        )
    if row is None:
        return None
    account = from_plain(row["email"], str)
    return Asker(
        email=account,
        role=cast("Role", row["role"]),
        read_audiences=audiences_for(
            account,
            hosted_domain=from_plain(row["hosted_domain"], str, default=None),
            staff=get_staff(request),
        ),
    )


def require_session_write_access(identity: AuthIdentity, session: AgentSession) -> None:
    """Keep viewer capture scoped to the credential that opened the session.

    Args:
      identity: Authenticated caller and its API key.
      session: Target session with its opening key attribution.

    Raises:
      HTTPException: A viewer did not open this session with its own key.

    """
    if identity.role == "viewer" and (
        identity.api_key_id is None
        or session.opened_by_api_key_id != identity.api_key_id
    ):
        raise HTTPException(status_code=403, detail="Own session key required")


def is_a_science_chat(session: AgentSession) -> bool:
    """Say whether ``session`` is a science chat: one under a ``chat:`` id.

    Args:
      session: The session.

    Returns:
      chat: Whether its ``cli_session_id`` starts with ``chat:``.

    """
    return (session.cli_session_id or "").startswith(CHAT_SESSION_PREFIX)


def require_chat_opener(identity: AuthIdentity, *, session: AgentSession) -> None:
    """Refuse a write to a science chat from any key but the one that opened it.

    Args:
      identity: Authenticated caller and its API key.
      session: Target session.

    Raises:
      HTTPException: ``session`` is a science chat and the caller did not open it.

    """
    _refuse_a_stranger(
        identity,
        cli_session_id=session.cli_session_id,
        opener=session.opened_by_api_key_id,
    )


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


def granted_audience(identity: AuthIdentity) -> str:
    """Return the audience a session this caller opens is placed in.

    The audience the caller's key is pinned to, else ``open``. A caller that does not
    hold that audience is refused rather than given ``open``: a staff key whose owner
    lost ``staff`` must not start a public transcript.

    Args:
      identity: Authenticated caller and the audiences it may open into.

    Returns:
      audience: The pin, or ``open`` when the caller is not pinned.

    Raises:
      HTTPException: 403 ``audience_not_held``: the caller may not open into it.

    """
    audience = OPEN if identity.pinned_audience is None else identity.pinned_audience
    if audience not in identity.open_audiences:
        raise HTTPException(
            status_code=403,
            detail={"code": "audience_not_held", "audience": audience},
        )
    return audience


def hidden_session(audience: str | None) -> HTTPException:
    """Build the refusal for a session whose audience the caller does not read.

    Args:
      audience: The session's audience, named in the body so a caller knows why.

    Returns:
      refusal: A 403 whose detail carries ``code: session_audience``.

    """
    return HTTPException(
        status_code=403,
        detail={"code": "session_audience", "audience": audience},
    )


def require_session_access(
    identity: AuthIdentity,
    *,
    session: AgentSession,
    mode: SessionAccess,
    assistant: Assistant | None = None,
    asker: Asker | None = None,
) -> None:
    """Refuse a caller who may not do ``mode`` to a session of this audience.

    A session with no audience reads as hidden from everyone but its opening key,
    so a row that lost its audience fails closed rather than open.

    The configured assistant relays a user's line to a session it cannot read, and
    passes on that user's rights, not its own: it needs an ``asker`` who is the
    session's account, or who holds its audience, and who is a writer. A send that
    names no asker, or one who does not qualify, is refused.

    Args:
      identity: Authenticated caller and the audiences it reads.
      session: Target session with its audience and opening key.
      mode: What the caller is about to do.
      assistant: The configured assistant, if any.
      asker: The user the assistant sends this input for, as the server finds them
        (:func:`resolve_asker`); ``None`` when none was named or found. It lends
        nothing to a sender that is not the assistant.

    Raises:
      HTTPException: 403 ``session_audience``: the caller neither opened the session
        nor reads its audience, and for ``input`` is not the assistant sending for an
        asker who may write there either.

    """
    if _opened_by(identity, session.opened_by_api_key_id):
        return
    if session.audience in identity.read_audiences:
        return
    if (
        mode == "input"
        and _is_the_assistant(identity, assistant)
        and asker is not None
        and ROLE_ORDER.index(asker.role) >= ROLE_ORDER.index("writer")
        and (session.account == asker.email or session.audience in asker.read_audiences)
    ):
        return
    raise hidden_session(session.audience)


async def require_session_row_write(
    request: Request,
    identity: AuthIdentity,
    target_id: uuid.UUID,
) -> None:
    """Refuse a write by id to a session the caller may not write; other rows pass.

    One query reads the row's kind, audience, opening key and ``cli_session_id``,
    so a generic route (a field edit, a purge) learns whether the id is a session
    before it acts. A science chat is then written by its opening key alone, and
    any session by a caller who opened it or reads its audience.

    Args:
      request: The request, which carries the store.
      identity: Authenticated caller and its API key.
      target_id: The row a route is about to change.

    Raises:
      HTTPException: ``target_id`` is a session the caller may not write.

    """
    async with get_store(request).engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT kind, agentsession_cli_session_id AS cli_session_id, "
            "agentsession_opened_by_api_key_id AS opener, "
            "agentsession_audience AS audience FROM inquiries WHERE id = $1",
            target_id,
        )
    if row is None or row.get("kind") != "AgentSession":
        return
    opener = from_plain(row.get("opener"), uuid.UUID, default=None)
    audience = from_plain(row.get("audience"), str, default=None)
    if not _opened_by(identity, opener) and audience not in identity.read_audiences:
        raise hidden_session(audience)
    _refuse_a_stranger(
        identity,
        cli_session_id=from_plain(row.get("cli_session_id"), str, default=""),
        opener=opener,
    )


def _opened_by(identity: AuthIdentity, opener: uuid.UUID | None) -> bool:
    """Say whether ``identity``'s key is the one that opened a session."""
    return identity.api_key_id is not None and identity.api_key_id == opener


def _is_the_assistant(identity: AuthIdentity, assistant: Assistant | None) -> bool:
    """Say whether ``identity`` is the configured assistant's account."""
    return assistant is not None and identity.email == assistant.email


def _refuse_a_stranger(
    identity: AuthIdentity,
    *,
    cli_session_id: str | None,
    opener: uuid.UUID | None,
) -> None:
    """Refuse the write to a science chat that ``identity`` did not open."""
    if identity.api_key_id != opener and (cli_session_id or "").startswith(
        CHAT_SESSION_PREFIX,
    ):
        raise HTTPException(
            status_code=403,
            detail="A science chat is written by the key that opened it",
        )
