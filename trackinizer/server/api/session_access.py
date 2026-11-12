"""Authorize who may read, write, drain and message an AgentSession.

Who reads a session's transcript is one rule,
:func:`~trackinizer.server.audience.visible_sql`, which every reader of session
content takes: its audience, and as the server's organisation mode
(``TRACKINIZER_CHAT_ORGS``) says, its owner, its organisation, the people who oversee
and, for a science chat, its members. Reading a session controls nothing, since a run
keeps its input, its records and its life there: only its account or opening key sends
it input, regardless of transcript visibility, and only they write, drain or end it. The
opening key always passes, whatever the audience, so a key whose owner lost ``staff`` can
still run the session it opened; its account must read it too. Only the transcript is
gated: a hidden session still has an open envelope, and the rows it files stay open.

The configured assistant acts for the person it answers, the ``asker`` it names, on
that person's rights and none of its own, and the server finds them in its own
tables. It relays input only for a writer whose account holds the target session,
even when its own key opened that session. It reads a session for an asker into the
chat it answers them in, the ``chat`` it names, when the asker reads both. What it reads
lands in the chat, so every destination reader must be allowed to read it; a
``staff`` session only once the chat is ``staff`` too
(:func:`~trackinizer.server.private.chat_audience.raise_chat_audience`), which
only the chat's starter makes it; and any other session only when everyone who
reads the chat reads it too
(:func:`~trackinizer.server.private.science_chats.chat_readers`). The
assistant reads a chat one of its own keys opened by its id and audience alone, as it
must to answer and fork chats an earlier key of its own opened. It reads, writes and
drains, by its id and audience alone too, a chat a CLI began under its starter's key
once the starter's canvas answers it with the assistant: it runs there as the CLI's
run did. Any other chat it reads only as the transcript test lets it.

A session opens into the audience its key is pinned to, else ``open``, and only into
one its caller may open into: :func:`granted_audience` refuses a pin the caller no
longer holds instead of lowering the session to ``open``. A caller asks for
``private`` to start a session hidden.

A viewer key mutates only the session it opened. A science chat is stricter: only the
key that opened it, or the assistant on a chat it runs, edits its row, and only that
key or an admin purges it by id. Other rows are purged by their account or an admin.
Only a session's account may reassign it, since the account controls its input;
other rows may also be reassigned by an admin. A chat's lines name their poster,
and its labels and account say who started it and
who posted, so a second writer's write would forge a poster or hide the chat from
History. Its transcript is one part per run that answered in it: a new part, or a run
attaching to it, comes from its opening key, the configured assistant's or its
starter's, and each part is written by the key that opened it alone, so no run
rewrites another's lines. A chat is known by its ``chat:`` id alone, whoever opened
it and whether or not an assistant is configured.

Every rule above stays where it is. On top of them, a field edit, a lock and a
purge by id ask the deployment's access policy (:func:`require_row_write`), whose
default allows what these rules allow. A write naming a row the caller may not read
answers 404, as a read of it does (:func:`require_readable`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import uuid

from fastapi import HTTPException
from treekle import from_plain

from trackinizer.server.access_policy import (
    Principal,
    Resource,
    inquiry_read_filter,
)
from trackinizer.server.api._deps import (
    get_assistant,
    get_staff,
    get_store,
)
from trackinizer.server.api.session_identity import (
    Asker,
    asker_reader,
    hidden_session,
    is_the_assistant,
    request_principal,
    session_reader,
)
from trackinizer.server.audience import (
    person_audiences,
    reads_session,
)
from trackinizer.server.notify import tx
from trackinizer.server.values import vetted_sql
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.audience import OPEN, PRIVATE
from trackinizer.wire.wire_service_accounts import is_service_address


if TYPE_CHECKING:
    from collections.abc import Collection

    from fastapi import Request
    from starlette.requests import HTTPConnection

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.access_policy import AccessPolicy
    from trackinizer.server.auth import AuthIdentity, Role
    from trackinizer.types.inquiries import AgentSession


__all__ = [
    "SessionAccess",
    "check_row_write",
    "granted_audience",
    "require_attributable",
    "require_readable",
    "require_row_account",
    "require_row_purge",
    "require_row_write",
    "require_session_access",
    "require_session_id_change",
    "require_session_row_write",
    "require_session_write_access",
    "resolve_asker",
]


type SessionAccess = Literal["read", "write", "drain", "input"]
"""What a caller does to a session: read its transcript, write or end it, drain its
inbound queue, or send it input."""


async def resolve_asker(
    request: HTTPConnection,
    identity: AuthIdentity,
    *,
    email: str | None,
    chat: uuid.UUID | None = None,
) -> Asker | None:
    """Find the user the assistant says it acts for, if the caller is the assistant.

    The assistant is trusted to say whom it speaks or reads for, and no further: what
    that user may do the server reads from its own tables. Anyone else's naming an
    asker is ignored, since it gives them nothing.

    Args:
      request: The request, which carries the store and the configuration.
      identity: Authenticated caller.
      email: The account the request names, or ``None``.
      chat: The conversation the assistant reads into for them, if it names one.

    Returns:
      asker: The named user, holding nothing when the account is unknown, disabled or
        blank; ``None`` when no one is named or the caller is not the assistant.

    """
    if email is None or not is_the_assistant(identity, get_assistant(request)):
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
        return Asker(
            email=email.lower(),
            role="viewer",
            read_audiences=frozenset(),
            chat=chat,
        )
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
        chat=chat,
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


async def require_session_access(
    request: Request,
    identity: AuthIdentity,
    *,
    session: AgentSession,
    mode: SessionAccess,
    asker: Asker | None = None,
) -> None:
    """Refuse a caller who may not do ``mode`` to this session.

    Input requires the session's account or opening key; transcript visibility,
    organisation membership and admin status grant no input rights. When the
    assistant names an asker, only a writer whose account holds the session passes.
    Reading asks the database's transcript rule, with the opening key exempt; writing
    and draining ask it of the session's account, for the reason input does. A science
    chat's own rules (:mod:`~trackinizer.server.api.private.chat_sessions`) narrow
    a reader's writes and drains instead.

    The configured assistant acts for an ``asker`` on the asker's rights alone: it
    reads a session for one only if they read it. On its own rights it reads a chat
    it opened, and reads, writes and drains a chat a CLI began that its starter gave it
    (:func:`~trackinizer.server.api.private.chat_sessions.assistant_runs_chat`).

    Args:
      request: The request, which carries the store and the configuration.
      identity: Authenticated caller and the audiences it reads.
      session: Target session with its audience and opening key.
      mode: What the caller is about to do.
      asker: The user the assistant reads or sends this input for, as the server
        finds them (:func:`resolve_asker`); ``None`` when none was named. It lends
        nothing to a caller that is not the assistant.

    Raises:
      HTTPException: 403 ``session_audience``: the caller may not do ``mode`` there,
        either on its own rights or for the asker it names.

    """
    assistant = get_assistant(request)
    lender = asker if is_the_assistant(identity, assistant) else None
    if mode == "input":
        _require_input(identity, session=session, asker=lender)
        return
    if mode == "read" and lender is not None:
        await _require_asker_reads(
            request,
            asker=lender,
            session=session,
        )
        return
    if _opened_by(identity, session.opened_by_api_key_id):
        return
    async with get_store(request).engine.acquire() as conn:
        if (
            mode == "read" or _holds(identity, account=session.account)
        ) and await reads_session(
            conn,
            reader=session_reader(request, identity),
            session_id=session.id,
        ):
            return
    raise hidden_session(session.audience)


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


async def require_readable(
    request: Request,
    identity: AuthIdentity,
    row_ids: Collection[uuid.UUID],
) -> None:
    """Refuse a write naming a row the caller may not read, as a read of it is: 404.

    A row the access policy hides is absent to its reader. It answers as a row that
    never existed does, before any other check can name it, so a write neither
    changes it nor says it exists.

    Args:
      request: The request, which carries the store and its policy.
      identity: Authenticated caller; an admin is asked too.
      row_ids: The rows the route is about to change or link.

    Raises:
      NotFoundError: A named row is absent or hidden from the caller.

    """
    ids = list(row_ids)
    if not ids:
        return
    principal = await request_principal(request, identity)
    store = get_store(request)
    params: list[object] = [ids]
    readable = inquiry_read_filter(
        "inquiries.id",
        policy=store.access_policy,
        principal=principal,
        params=params,
    )
    async with store.engine.acquire() as conn:
        unread = await conn.fetchval(
            vetted_sql(
                "SELECT named.id FROM unnest($1::uuid[]) AS named(id) WHERE NOT EXISTS "
                "(SELECT 1 FROM inquiries WHERE inquiries.id = named.id AND (",
                readable,
                ")) LIMIT 1",
            ),
            *params,
        )
    if unread is not None:
        raise NotFoundError(f"inquiry {unread} not found")


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


async def require_session_row_write(
    request: Request,
    identity: AuthIdentity,
    target_id: uuid.UUID,
) -> None:
    """Refuse a write by id to a session the caller may not write; other rows pass.

    One query reads the row's kind, audience, opening key and ``cli_session_id``,
    so a generic route (a field edit, a purge) learns whether the id is a session
    before it acts. A science chat is then written by its opening key, or the
    assistant on a chat it runs, and any session by a caller who opened it or reads its
    audience.

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
    if not _holds(identity, account=row.account):
        raise HTTPException(
            status_code=403,
            detail="A row is purged by its account or an admin",
        )


async def require_row_account(
    request: Request,
    identity: AuthIdentity,
    target_id: uuid.UUID,
) -> None:
    """Require the current account to reassign a session; other rows also allow admins.

    A session's account controls its input and visibility, so even an admin may not
    take it by changing its account. Other rows keep the account-or-admin rule.

    Args:
      request: The request, which carries the store.
      identity: Authenticated caller and its role.
      target_id: The row whose ``account`` a route is about to change.

    Raises:
      HTTPException: 403: the caller does not hold the session's account, or is
        neither the account nor an admin for another kind of row.

    """
    row = await _read_row(request, target_id=target_id)
    if row is None or _holds(identity, account=row.account):
        return
    if row.kind == "AgentSession":
        raise HTTPException(
            status_code=403,
            detail="A session's account is changed by that account alone",
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


def _not_held(audience: str) -> HTTPException:
    """Build the refusal of a session opened into an audience the caller may not."""
    return HTTPException(
        status_code=403,
        detail={"code": "audience_not_held", "audience": audience},
    )


async def _require_asker_reads(
    request: Request,
    *,
    asker: Asker,
    session: AgentSession,
) -> None:
    """Refuse the assistant's read for ``asker`` of a session the asker does not read."""
    async with get_store(request).engine.acquire() as conn:
        if not await reads_session(
            conn,
            reader=asker_reader(request, asker),
            session_id=session.id,
        ):
            raise hidden_session(session.audience)


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


def _holds(identity: AuthIdentity, *, account: str) -> bool:
    """Say whether ``identity`` is the account a row is held under."""
    return account.lower() == identity.email.lower()


def _require_input(
    identity: AuthIdentity,
    *,
    session: AgentSession,
    asker: Asker | None,
) -> None:
    """Allow input only from the account, its delegated writer, or the opening key."""
    # Reading a transcript grants no control of the host that consumes its input.
    if asker is not None:
        if asker.role in {"writer", "admin"} and (
            asker.email.lower() == session.account.lower()
        ):
            return
    elif _opened_by(identity, session.opened_by_api_key_id) or _holds(
        identity,
        account=session.account,
    ):
        return
    raise hidden_session(session.audience)


def _opened_by(identity: AuthIdentity, opener: uuid.UUID | None) -> bool:
    """Say whether ``identity``'s key is the one that opened a session."""
    return identity.api_key_id is not None and identity.api_key_id == opener
