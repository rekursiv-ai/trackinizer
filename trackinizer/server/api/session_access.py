"""Authorize who may read, write, drain and message an AgentSession.

Who reads a session's transcript is one rule,
:func:`~trackinizer.server.audience.visible_sql`, which every reader of session
content takes: its audience, and as the server's organisation mode
(``TRACKINIZER_CHAT_ORGS``) says, its owner, its organisation, the people who oversee
and, for a science chat, its members. Whoever reads a session may also write, drain
and end it, and the key that opened a session always may, whatever the audience, so a
key whose owner lost ``staff`` can still run the ``staff`` session it opened. Only
the transcript is gated: a hidden session still has an open envelope, and the rows it
files stay open.

The configured assistant acts for the person it answers, the ``asker`` it names, on
that person's rights and none of its own, and the server finds them in its own
tables. It relays a line to a session it cannot read for an asker who is a writer and
is the session's account or reads it. It reads a session for an asker into the chat
it answers them in, the ``chat`` it names, when the asker reads both. What it reads
lands in the chat, so it reads no chat and no ``private`` session for anyone; a
``staff`` session only once the chat is ``staff`` too
(:func:`~trackinizer.server.visuals.chat_audience.raise_chat_audience`), which
only the chat's starter makes it; and any other session only when everyone who
reads the chat reads it too
(:func:`~trackinizer.server.visuals.science_chats.chat_readers`). The
assistant reads a chat one of its own keys opened by its id and audience alone, as
it must to answer and fork chats an earlier key of its own opened; any other chat
only as the transcript test lets it.

A session opens into the audience its key is pinned to, else ``open``, and only into
one its caller may open into: :func:`granted_audience` refuses a pin the caller no
longer holds instead of lowering the session to ``open``. A caller asks for
``private`` to start a session hidden.

A viewer key mutates only the session it opened. A science chat is stricter: only the
key that opened it edits its row, and only that key or an admin purges it by id,
where any other row is purged, and moved to another account, by its account or an
admin. Its lines name their poster, and its labels and account say who started it and
who posted, so a second writer's write would forge a poster or hide the chat from
History. Its transcript is one part per run that answered in it: a new part, or a run
attaching to it, comes from its opening key, the configured assistant's or its
starter's, and each part is written by the key that opened it alone, so no run
rewrites another's lines. A chat is known by its ``chat:`` id alone, whoever opened
it and whether or not an assistant is configured.

Every rule above stays where it is. On top of them, a field edit, a lock and a
purge by id ask the deployment's access policy (:func:`require_row_write`), whose
default allows what these rules allow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import uuid

from fastapi import HTTPException
from treekle import from_plain

from trackinizer.server.access_policy import Principal, Resource
from trackinizer.server.api._deps import (
    get_assistant,
    get_chat_orgs,
    get_staff,
    get_store,
)
from trackinizer.server.audience import (
    SessionReader,
    caller_reader,
    person_audiences,
    reads_session,
)
from trackinizer.server.auth import ROLE_ORDER, Role
from trackinizer.server.config import Config
from trackinizer.server.notify import tx
from trackinizer.server.visuals.chat_audience import raise_chat_audience
from trackinizer.server.visuals.partners import opener_email
from trackinizer.server.visuals.science_chats import (
    chat_readers,
    read_first_session,
    read_starter,
)
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.audience import AUDIENCES, OPEN, PRIVATE, STAFF
from trackinizer.wire.wire_science_chat import (
    CHAT_SESSION_PREFIX,
    chat_conversation_id,
)
from trackinizer.wire.wire_service_accounts import is_service_address


if TYPE_CHECKING:
    from fastapi import FastAPI, Request

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.access_policy import AccessPolicy
    from trackinizer.server.auth import AuthIdentity
    from trackinizer.server.config import Assistant
    from trackinizer.server.store.core import Store


__all__ = [
    "Asker",
    "SessionAccess",
    "asker_reader",
    "check_row_write",
    "granted_audience",
    "hidden_session",
    "is_a_science_chat",
    "request_principal",
    "require_attributable",
    "require_chat_writer",
    "require_part_writer",
    "require_row_account",
    "require_row_purge",
    "require_row_write",
    "require_session",
    "require_session_access",
    "require_session_id_change",
    "require_session_row_write",
    "require_session_write_access",
    "resolve_asker",
    "session_reader",
]


type SessionAccess = Literal["read", "write", "drain", "input"]
"""What a caller does to a session: read its transcript, write or end it, drain its
inbound queue, or send it input."""


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

    """

    email: str
    role: Role
    read_audiences: frozenset[str]
    hosted_domain: str | None = None


async def resolve_asker(
    request: Request,
    identity: AuthIdentity,
    *,
    email: str | None,
) -> Asker | None:
    """Find the user the assistant says it acts for, if the caller is the assistant.

    The assistant is trusted to say whom it speaks or reads for, and no further: what
    that user may do the server reads from its own tables. Anyone else's naming an
    asker is ignored, since it gives them nothing.

    Args:
      request: The request, which carries the store and the configuration.
      identity: Authenticated caller.
      email: The account the request names, or ``None``.

    Returns:
      asker: The named user, holding nothing when the account is unknown, disabled or
        blank; ``None`` when no one is named or the caller is not the assistant.

    """
    if email is None or not _is_the_assistant(identity, get_assistant(request)):
        return None
    async with get_store(request).engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT email, role, hosted_domain FROM users "
            "WHERE email = $1 AND status = 'active'",
            email.lower(),
        )
    # Not ``None``: a read for an asker is the asker's alone, so one the server does
    # not hold must not fall back to what the assistant reads as itself.
    if row is None:
        return Asker(email=email.lower(), role="viewer", read_audiences=frozenset())
    account = from_plain(row["email"], str)
    hosted_domain = from_plain(row["hosted_domain"], str, default=None)
    return Asker(
        email=account,
        role=cast("Role", row["role"]),
        read_audiences=person_audiences(
            account,
            hosted_domain=hosted_domain,
            staff=get_staff(request),
        ),
        hosted_domain=hosted_domain,
    )


def require_attributable(identity: AuthIdentity, account: str) -> None:
    """Refuse attributing a row to a service account the caller is not.

    A row's ``account`` is who filed it, which a page or a service may rely on, so
    only that account, or an admin, may put its address on a row: at creation or by
    moving a row to it.

    Args:
      identity: Authenticated caller.
      account: The account the row would be held under.

    Raises:
      HTTPException: 403: ``account`` is a service address that is neither the
        caller's nor the admin's to hand out.

    """
    if (
        is_service_address(account)
        and account.lower() != identity.email.lower()
        and identity.role != "admin"
    ):
        raise HTTPException(
            status_code=403,
            detail="A row is attributed to a service account by that account or an admin",
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


async def require_chat_writer(
    request: Request,
    identity: AuthIdentity,
    *,
    session: AgentSession,
    closing: bool = False,
) -> None:
    """Refuse a run on a science chat from a key that may not add a part to it.

    A chat takes a new part, a run attaching to it and that run's close from the key
    that opened it, any key of the configured assistant's account, or any key of the
    account that started it (:func:`~trackinizer.server.visuals.science_chats.read_starter`).
    A browser has no key to own a part by, so it adds none; it may only close the
    chat (``closing``), and only as the chat's starter. Any other session passes.

    Args:
      request: The request, which carries the store and the configuration.
      identity: Authenticated caller and its API key.
      session: Target session.
      closing: The caller closes the chat, which its starter may do without a key.

    Raises:
      HTTPException: 403: ``session`` is a science chat the caller may not add to.

    """
    if not is_a_science_chat(session) or identity.api_key_id == (
        session.opened_by_api_key_id
    ):
        return
    assistant = get_assistant(request)
    conversation_id = chat_conversation_id(session.cli_session_id)
    if conversation_id is not None and (identity.api_key_id is not None or closing):
        if identity.api_key_id is not None and _is_the_assistant(identity, assistant):
            return
        async with get_store(request).engine.acquire() as conn:
            starter = await read_starter(
                conn,
                assistant=assistant,
                conversation_id=conversation_id,
            )
        if (starter or "").lower() == identity.email.lower():
            return
    raise HTTPException(
        status_code=403,
        detail="A science chat takes a run from its opener, the assistant or its "
        "starter",
    )


async def require_part_writer(
    request: Request,
    identity: AuthIdentity,
    *,
    session: AgentSession,
    name: str,
) -> None:
    """Refuse a write into a science chat's part by any key but the one that opened it.

    A file the chat holds no part for yet opens one, which :func:`require_chat_writer`
    decides, as it does a write that names no file. A part opened before parts named
    their key keeps the chat's old rule: its opening key alone writes it. Any other
    session passes.

    Args:
      request: The request, which carries the store.
      identity: Authenticated caller and its API key.
      session: Target session.
      name: The file the write names; empty for one that carries only slash commands.

    Raises:
      HTTPException: 403: the caller may not write that part, or open a new one.

    """
    if not is_a_science_chat(session):
        return
    manifest = (
        await get_store(request).read_session_manifest(session.id, name=name)
        if name
        else None
    )
    if manifest is None:
        await require_chat_writer(request, identity, session=session)
    elif manifest.opened_by_api_key_id is None:
        _refuse_a_stranger(
            identity,
            cli_session_id=session.cli_session_id,
            opener=session.opened_by_api_key_id,
        )
    elif identity.api_key_id != manifest.opened_by_api_key_id:
        raise HTTPException(
            status_code=403,
            detail="A part of a science chat is written by the key that opened it",
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


def granted_audience(identity: AuthIdentity, *, private: bool = False) -> str:
    """Return the audience a session this caller opens is placed in.

    The audience the caller's key is pinned to, else ``open``; ``private`` narrows
    ``open``, or a ``private`` pin, to ``private``. A caller that does not hold that
    audience is refused rather than given ``open``: a staff key whose owner lost
    ``staff`` must not start a public transcript.

    Args:
      identity: Authenticated caller and the audiences it may open into.
      private: Open the session hidden.

    Returns:
      audience: The pin, or ``open`` when the caller is not pinned; ``private`` when
        asked.

    Raises:
      HTTPException: 403 ``audience_not_held``: the caller may not open into it, or
        asks for ``private`` under a pin to another audience.

    """
    audience = OPEN if identity.pinned_audience is None else identity.pinned_audience
    if audience not in identity.open_audiences:
        raise _not_held(audience)
    if not private:
        return audience
    # Only narrows: whoever may open ``open`` may hide what they open.
    if audience in {OPEN, PRIVATE}:
        return PRIVATE
    raise _not_held(PRIVATE)


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


async def require_session_access(
    request: Request,
    identity: AuthIdentity,
    *,
    session: AgentSession,
    mode: SessionAccess,
    asker: Asker | None = None,
    chat: uuid.UUID | None = None,
) -> None:
    """Refuse a caller who may not do ``mode`` to this session.

    A session with no audience reads as hidden from everyone but its opening key,
    so a row that lost its audience fails closed rather than open. Any other caller
    is asked of the database, by :func:`session_reader`; only the configured
    assistant reads a chat by its audience alone, and only one it opened.

    The configured assistant acts for an ``asker`` on the asker's rights alone. It
    reads for one into the ``chat`` it answers them in, under the rules this module
    names, raising a chat to ``staff`` before it reads a ``staff`` session there. It
    relays a line to a session it cannot read for an asker who is a writer, and is
    the session's account or reads it.

    Args:
      request: The request, which carries the store and the configuration.
      identity: Authenticated caller and the audiences it reads.
      session: Target session with its audience and opening key.
      mode: What the caller is about to do.
      asker: The user the assistant reads or sends this input for, as the server
        finds them (:func:`resolve_asker`); ``None`` when none was named. It lends
        nothing to a caller that is not the assistant.
      chat: The conversation the assistant reads for ``asker`` into.

    Raises:
      HTTPException: 403 ``session_audience``: the caller neither opened the session
        nor reads it, and is not the assistant acting for an asker who may do
        ``mode`` there either. For a read for an asker: 422 ``chat_required``
        without a chat; 403 ``chat_not_member`` when the asker does not read the
        chat; 403 ``chat_not_starter`` for a ``staff`` session read into a chat that
        is not ``staff`` yet for anyone but its starter; 409 ``chat_private`` for one
        read into a private chat; 409 ``chat_readers`` for any other session that
        someone who reads the chat does not read.

    """
    assistant = get_assistant(request)
    lender = asker if _is_the_assistant(identity, assistant) else None
    if mode == "read" and lender is not None:
        await _require_read(
            request,
            identity,
            asker=lender,
            session=session,
            chat=chat,
        )
        return
    if _opened_by(identity, session.opened_by_api_key_id):
        return
    async with get_store(request).engine.acquire() as conn:
        if mode == "read" and await _assistants_own_chat(
            conn,
            identity,
            assistant=assistant,
            session=session,
        ):
            return
        if await reads_session(
            conn,
            reader=session_reader(request, identity),
            session_id=session.id,
        ):
            return
        if (
            mode == "input"
            and lender is not None
            and ROLE_ORDER.index(lender.role) >= ROLE_ORDER.index("writer")
            and (
                session.account == lender.email
                or await reads_session(
                    conn,
                    reader=asker_reader(request, lender, delegated=False),
                    session_id=session.id,
                )
            )
        ):
            return
    raise hidden_session(session.audience)


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
            every_chat=True,
            oversight=True,
            hidden=hidden,
            assistant="" if assistant is None else assistant.email,
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


async def request_principal(request: Request, identity: AuthIdentity) -> Principal:
    """Describe ``identity`` as the access policy sees it, once per request.

    The policy's :meth:`~trackinizer.server.access_policy.AccessPolicy.groups`
    runs on the first call and its answer is kept on ``request.state``, so every
    later check in the same request reads the same groups without a query.

    Args:
      request: The request, which carries the store and its policy.
      identity: Authenticated caller.

    Returns:
      principal: Its lowercase email, its role, the policy's groups for it and the
        API key it used.

    """
    held: object = getattr(request.state, "access_principal", None)
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


async def require_row_write(
    request: Request,
    identity: AuthIdentity,
    target_id: uuid.UUID,
) -> None:
    """Ask the access policy whether the caller may write the row ``target_id``.

    Runs before the route's write, on a transaction of its own; a route that
    already holds a connection calls :func:`check_row_write` on it instead.

    Args:
      request: The request, which carries the store and its policy.
      identity: Authenticated caller.
      target_id: The row a route is about to change or delete.

    Raises:
      AccessDeniedError: The policy refuses; the app answers 403.

    """
    principal = await request_principal(request, identity)
    store = get_store(request)
    async with store.engine.acquire() as conn, tx(conn):
        await check_row_write(
            conn,
            store.access_policy,
            principal=principal,
            target_id=target_id,
        )


async def check_row_write(
    conn: Conn,
    policy: AccessPolicy,
    *,
    principal: Principal,
    target_id: uuid.UUID,
) -> None:
    """Ask ``policy`` on ``conn`` whether ``principal`` may write ``target_id``.

    A row that does not exist is not asked about: the write it guards answers 404.

    Args:
      conn: The caller's connection, inside its transaction.
      policy: The deployment's access policy.
      principal: The caller, as :func:`request_principal` built it.
      target_id: The row about to change.

    Raises:
      AccessDeniedError: The policy refuses.

    """
    account = await conn.fetchval(
        "SELECT account FROM inquiries WHERE id = $1",
        target_id,
    )
    if account is None:
        return
    await policy.check(
        conn,
        principal=principal,
        resource=Resource(
            kind="inquiry",
            id=str(target_id),
            account=from_plain(account, str),
        ),
        action="write",
    )


def asker_reader(request: Request, asker: Asker, *, delegated: bool) -> SessionReader:
    """Describe the person the assistant acts for as the transcript test reads them.

    Args:
      request: The request, which carries the configuration.
      asker: The person, as :func:`resolve_asker` finds them.
      delegated: The assistant reads for them into a chat, which takes away their
        oversight and the every-chat grant.

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
        delegated=delegated,
    )


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
    row = await _read_row(request, target_id=target_id)
    if row is None or row.kind != "AgentSession":
        return
    _require_audience(identity, row=row)
    _refuse_a_stranger(identity, cli_session_id=row.cli_session_id, opener=row.opener)


async def require_row_purge(
    request: Request,
    identity: AuthIdentity,
    target_id: uuid.UUID,
) -> None:
    """Refuse a purge by id from a caller who may not delete the row.

    One rule for every row: its account deletes it, and so does an admin. A session
    also needs its audience, as any write by id does. A science chat's session is the
    exception: its account is its starter and its lines are its posters', so the key
    that opened it, or an admin, purges it; ``DELETE /api/chats/<id>`` deletes a whole
    chat by its starter's rule instead.

    Args:
      request: The request, which carries the store.
      identity: Authenticated caller, its role and its API key.
      target_id: The row a route is about to purge.

    Raises:
      HTTPException: 403: the caller may not purge ``target_id``.

    """
    row = await _read_row(request, target_id=target_id)
    if row is None:
        return
    if row.kind == "AgentSession":
        _require_audience(identity, row=row)
    if identity.role == "admin":
        return
    if row.cli_session_id.startswith(CHAT_SESSION_PREFIX):
        _refuse_a_stranger(
            identity,
            cli_session_id=row.cli_session_id,
            opener=row.opener,
        )
    elif not _holds(identity, row=row):
        raise HTTPException(
            status_code=403,
            detail="A row is purged by its account or an admin",
        )


async def require_row_account(
    request: Request,
    identity: AuthIdentity,
    target_id: uuid.UUID,
) -> None:
    """Refuse moving a row to another account by anyone but its account or an admin.

    A writer who could name themselves a row's account could then purge it, so the
    purge rule holds only while this one does. A ``private`` session's account is
    moved by that account alone: its account is an owner, who reads it and may show
    it again, so an admin who moved it to themselves could undo its owner's hide.

    Args:
      request: The request, which carries the store.
      identity: Authenticated caller and its role.
      target_id: The row whose ``account`` a route is about to change.

    Raises:
      HTTPException: 403: the caller is neither the row's account nor an admin, or
        the row is a ``private`` session and the caller is not its account.

    """
    row = await _read_row(request, target_id=target_id)
    if row is None or _holds(identity, row=row):
        return
    if row.kind == "AgentSession" and row.audience == PRIVATE:
        raise HTTPException(
            status_code=403,
            detail="A hidden session's account is changed by that account alone",
        )
    if identity.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="A row's account is changed by that account or an admin",
        )


async def require_session_id_change(
    request: Request,
    identity: AuthIdentity,
    target_id: uuid.UUID,
    *,
    value: str | None,
) -> None:
    """Refuse a change of a session's ``cli_session_id`` that could move a chat.

    Only the session's opening key or an admin changes it. And no one moves a session
    into or out of a science chat's ``chat:<id>``: a chat is named when its session
    starts, and a session moved under one would be taken by that chat's delete, or
    could name its starter.

    Args:
      request: The request, which carries the store.
      identity: Authenticated caller, its role and its API key.
      target_id: The row whose ``cli_session_id`` a route is about to set or clear.
      value: The id it would take; None clears it.

    Raises:
      HTTPException: 403: either rule refuses the change.

    """
    row = await _read_row(request, target_id=target_id)
    if row is None or row.kind != "AgentSession":
        return
    if any(
        (named or "").startswith(CHAT_SESSION_PREFIX)
        for named in (value, row.cli_session_id)
    ):
        raise HTTPException(
            status_code=403,
            detail="A science chat's id is named only when its session starts",
        )
    if identity.role != "admin" and (
        identity.api_key_id is None or identity.api_key_id != row.opener
    ):
        raise HTTPException(
            status_code=403,
            detail="A session's cli_session_id is changed by the key that opened it",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Row:
    """What a write by id needs to know of the row: who holds it, a session's access."""

    kind: str
    account: str
    cli_session_id: str
    opener: uuid.UUID | None
    audience: str | None


async def _read_row(request: Request, target_id: uuid.UUID) -> _Row | None:
    """Read the row a write by id names; None when there is none."""
    async with get_store(request).engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT kind, account, agentsession_cli_session_id AS cli_session_id, "
            "agentsession_opened_by_api_key_id AS opener, "
            "agentsession_audience AS audience FROM inquiries WHERE id = $1",
            target_id,
        )
    if row is None:
        return None
    return _Row(
        kind=from_plain(row.get("kind"), str),
        account=from_plain(row.get("account"), str, default=""),
        cli_session_id=from_plain(row.get("cli_session_id"), str, default=""),
        opener=from_plain(row.get("opener"), uuid.UUID, default=None),
        audience=from_plain(row.get("audience"), str, default=None),
    )


def _require_audience(identity: AuthIdentity, *, row: _Row) -> None:
    """Refuse a write to a session the caller neither opened nor reads."""
    if not _opened_by(identity, row.opener) and row.audience not in (
        identity.read_audiences
    ):
        raise hidden_session(row.audience)


def _holds(identity: AuthIdentity, *, row: _Row) -> bool:
    """Say whether ``identity`` is the account a row is held under."""
    return row.account.lower() == identity.email.lower()


def _opened_by(identity: AuthIdentity, opener: uuid.UUID | None) -> bool:
    """Say whether ``identity``'s key is the one that opened a session."""
    return identity.api_key_id is not None and identity.api_key_id == opener


def _is_the_assistant(identity: AuthIdentity, assistant: Assistant | None) -> bool:
    """Say whether ``identity`` is the configured assistant's account."""
    return assistant is not None and identity.email == assistant.email


# By its opener, not its audience: everyone holds ``private``, so the audience alone
# would let the assistant read, for anyone it answers, a hidden chat a person's own
# key opened. A key the assistant rotated away still names the assistant's account.
async def _assistants_own_chat(
    conn: Conn,
    identity: AuthIdentity,
    *,
    assistant: Assistant | None,
    session: AgentSession,
) -> bool:
    """Say whether ``identity`` is the assistant and ``session`` a chat it opened."""
    if (
        is_a_science_chat(session)
        and _is_the_assistant(identity, assistant)
        and session.audience in identity.read_audiences
    ):
        opener = await opener_email(conn, api_key_id=session.opened_by_api_key_id)
        return (opener or "").lower() == identity.email.lower()
    return False


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


def _not_held(audience: str) -> HTTPException:
    """Build the refusal of a session opened into an audience the caller may not."""
    return HTTPException(
        status_code=403,
        detail={"code": "audience_not_held", "audience": audience},
    )


# Each refusal comes before anything is read, and the chat becomes ``staff`` before the
# read it guards: the route reads the transcript only once this returns.
async def _require_read(
    request: Request,
    identity: AuthIdentity,
    *,
    asker: Asker,
    session: AgentSession,
    chat: uuid.UUID | None,
) -> None:
    """Refuse the assistant's read for ``asker`` into ``chat``; raise a chat to staff."""
    if not asker.read_audiences or is_a_science_chat(session):
        raise hidden_session(session.audience)
    if session.audience == PRIVATE:
        raise hidden_session(session.audience)
    if chat is None:
        raise HTTPException(status_code=422, detail={"code": "chat_required"})
    store = get_store(request)
    async with store.engine.acquire() as conn:
        if not await reads_session(
            conn,
            reader=asker_reader(request, asker, delegated=True),
            session_id=session.id,
        ):
            raise hidden_session(session.audience)
        first = await read_first_session(
            conn,
            assistant=get_assistant(request),
            conversation_id=chat,
        )
        if first is None or not await reads_session(
            conn,
            reader=asker_reader(request, asker, delegated=False),
            session_id=first,
        ):
            raise HTTPException(status_code=403, detail={"code": "chat_not_member"})
        await _require_chat_takes(
            conn,
            request,
            asker=asker,
            session=session,
            chat=chat,
            first=first,
        )
    if session.audience != STAFF:
        return
    try:
        _ = await raise_chat_audience(
            store,
            conversation_id=chat,
            api_key_id=identity.api_key_id,
            actor=identity.email,
            reason=f"read {session.id} for {asker.email}",
        )
    except ConflictError as error:
        raise HTTPException(status_code=409, detail={"code": "chat_private"}) from error


# A ``staff`` session's read makes the chat ``staff``, which its members who hold no
# ``staff`` then lose, so only its starter makes it so: anyone else would take the
# chat from its starter. Any other session lands in a chat whose every reader keeps
# it, so it must be one each of them reads.
async def _require_chat_takes(
    conn: Conn,
    request: Request,
    *,
    asker: Asker,
    session: AgentSession,
    chat: uuid.UUID,
    first: uuid.UUID,
) -> None:
    """Refuse a read into ``chat`` that would reach someone the session does not."""
    assistant = get_assistant(request)
    if session.audience == STAFF:
        held = await conn.fetchval(
            "SELECT agentsession_audience FROM inquiries WHERE id = $1",
            first,
        )
        starter = await read_starter(conn, assistant=assistant, conversation_id=chat)
        if held == STAFF or (starter or "").lower() == asker.email.lower():
            return
        raise HTTPException(status_code=403, detail={"code": "chat_not_starter"})
    for reader in await chat_readers(
        conn,
        assistant=assistant,
        conversation_id=chat,
        orgs=get_chat_orgs(request),
    ):
        if not await reads_session(conn, reader=reader, session_id=session.id):
            raise HTTPException(status_code=409, detail={"code": "chat_readers"})
