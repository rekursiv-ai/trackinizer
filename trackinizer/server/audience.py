"""Who holds which audience, and who reads which session's transcript.

Staff is a deploy-time setting: a list of patterns, each ``user@host`` or
``*@domain``. A literal pattern matches the verified email alone. A domain pattern
also needs Google's ``hd`` claim (stored as ``users.hosted_domain``) to name the same
domain, because Google verifies a consumer account's address on a staff domain without
making it a member of the organisation. Nothing set means nobody is staff. Everyone
holds ``open`` and ``private``: who reads a private session is the owner rule below,
not the audience.

An API key narrows its owner and never widens it: ``open_into`` pins the one audience
it opens sessions into. It reads what its owner reads.

The server's organisation mode (``TRACKINIZER_CHAT_ORGS``) says who reads what.
``single``: the server is one organisation, so every caller reads every ``open``
session. ``domain``: a person's organisation is their email's domain when Google's
``hd`` claim names it, as a staff domain pattern asks, and they read the open sessions
of their organisation. ``individual``: each person is an organisation of one.
Whatever the mode, a session's owner (its ``account``) reads it, and a person who
holds ``staff`` or is an admin reads every session. A ``private`` session is its
owner's and those people's. A ``staff`` session also needs the reader to hold
``staff``. :class:`SessionReader` carries what the test asks of the caller, and
:func:`caller_reader` derives it.

A query that returns session content narrows itself to what its caller reads with the
SQL fragments here, so the rule is spelled once: :func:`visible_sql` for a query that
already joins ``inquiries``, :func:`hidden_exists_sql` for one that does not, and
:func:`reads_session` for one session by id. A store read that takes the caller's
reader defaults to :data:`NOBODY`, so a caller that leaves it out reads nothing. A
live stream asks :class:`TranscriptPings` which writes to a transcript its viewer
hears of: those of the sessions it reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final
from uuid import UUID

import time

from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.values import vetted_sql
from trackinizer.wire.audience import OPEN, PRIVATE, STAFF
from trackinizer.wire.wire_service_accounts import is_service_address


if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.config import ChatOrgs


__all__ = [
    "CONSUMER_DOMAINS",
    "NOBODY",
    "Audiences",
    "SessionReader",
    "TranscriptPings",
    "caller_reader",
    "email_org",
    "hidden_exists_sql",
    "key_audiences",
    "may_hide",
    "person_audiences",
    "reads_session",
    "visible_sql",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class Audiences:
    """The audiences one credential reads and opens into.

    Attributes:
      read: Audiences whose transcripts it may read.
      opening: Audiences it may open a session into.
      pinned: The one audience a key is pinned to open into, or ``None``.

    """

    read: frozenset[str]
    opening: frozenset[str]
    pinned: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionReader:
    """What the transcript test asks of the caller reading a session.

    Attributes:
      audiences: The audiences the caller holds; a session outside them is hidden.
        Empty reads nothing.
      email: The caller's lowercase email: the sessions it owns. Empty matches none.
      org: The caller's organisation, whose open sessions it reads; ``None`` for
        none.
      every_session: Read every ``open`` session, as anyone on a server that is one
        organisation does.
      every_chat: Read every chat that is not private, as a person or the assistant
        on a server that is one organisation does.
      oversight: Read every session, a private one included, as a person who holds
        ``staff`` or is an admin does.
      hidden: Keep private sessions. A listing passes False unless asked to show
        them; a read by id keeps them for whoever may read them.
      assistant: The configured assistant's email, empty when none: a chat's first
        session that its key opened names the chat's starter.

    """

    audiences: frozenset[str] = frozenset()
    email: str = ""
    org: str | None = None
    every_session: bool = False
    oversight: bool = False
    hidden: bool = True


NOBODY: Final = SessionReader()
"""A reader who reads no session: the default of every store read."""

CONSUMER_DOMAINS: Final = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "proton.me",
        "protonmail.com",
    },
)
"""Mail domains anyone signs up on, whose users Google names members of no
organisation, so an organisation of one has no members."""


def email_org(email: str, *, hosted_domain: str | None) -> str | None:
    """Return the organisation a person belongs to: their email's domain, if Google says.

    Google verifies a consumer account's address on any domain, and names the domain
    in its ``hd`` claim only for a member of that domain's organisation, so a mail
    domain anyone signs up on, or a company's that a consumer account uses, makes no
    one an organisation.

    Args:
      email: A verified email address.
      hosted_domain: Google's ``hd`` claim from the person's last login, if any.

    Returns:
      org: The lowercase domain when ``hosted_domain`` names it; ``None`` otherwise.

    """
    domain = email.lower().rpartition("@")[2] if "@" in email else ""
    return domain if domain and domain == (hosted_domain or "").lower() else None


def person_audiences(
    email: str,
    *,
    hosted_domain: str | None,
    staff: Sequence[str],
) -> frozenset[str]:
    """Return the audiences a person holds.

    Args:
      email: The verified email, lowercase or not.
      hosted_domain: Google's ``hd`` claim from the person's last login, if any.
      staff: The configured staff patterns, lowercase.

    Returns:
      audiences: ``open`` and ``private``, and ``staff`` too when a pattern matches.

    """
    email = email.lower()
    domain = email.rpartition("@")[2] if "@" in email else ""
    in_domain = hosted_domain is not None and hosted_domain.lower() == domain
    for pattern in staff:
        # A domain pattern is never a literal address: an email that spells it
        # (``*`` is legal in a local part) must still pass the ``hd`` check.
        is_domain = pattern.startswith("*@")
        if is_domain and in_domain and pattern == f"*@{domain}":
            return frozenset({OPEN, PRIVATE, STAFF})
        if not is_domain and pattern == email:
            return frozenset({OPEN, PRIVATE, STAFF})
    return frozenset({OPEN, PRIVATE})


def key_audiences(
    owner: frozenset[str],
    *,
    open_into: str | None,
) -> Audiences:
    """Apply a key's pin to what its owner holds.

    Args:
      owner: The audiences the key's owner holds now.
      open_into: The audience the key is pinned to open into, or ``None``.

    Returns:
      audiences: What the key reads and opens. A pin the owner no longer holds opens
        nothing, so a demoted owner's key does not fall back to another audience.

    """
    opening = owner if open_into is None else owner & {open_into}
    return Audiences(read=owner, opening=opening, pinned=open_into)


def caller_reader(
    email: str,
    *,
    audiences: frozenset[str],
    admin: bool,
    orgs: ChatOrgs,
    assistant: str,
    hosted_domain: str | None,
    delegated: bool = False,
    hidden: bool = True,
) -> SessionReader:
    """Describe a caller as the transcript test reads it on a server in ``orgs`` mode.

    A person is any caller but the assistant and a service account. Only a person has
    an organisation, and only on a ``domain`` server, and only a person oversees. A
    read the assistant makes for a person (``delegated``) is that person's, less their
    oversight.

    Args:
      email: The caller's email.
      audiences: The audiences the caller holds.
      admin: Whether the caller is an admin.
      orgs: The server's organisation mode.
      assistant: The configured assistant's email; empty when none.
      hosted_domain: Google's ``hd`` claim from the caller's last login, which
        makes them a member of their email domain's organisation (:func:`email_org`).
      delegated: The assistant reads for this caller.
      hidden: Keep private sessions.

    Returns:
      reader: The caller, as :func:`visible_sql` tests it.

    """
    email = email.lower()
    the_assistant = bool(assistant) and email == assistant
    person = not the_assistant and not is_service_address(email)
    return SessionReader(
        audiences=audiences,
        email=email,
        org=(
            email_org(email, hosted_domain=hosted_domain)
            if person and orgs == "domain"
            else None
        ),
        every_session=orgs == "single",
        oversight=person and not delegated and (STAFF in audiences or admin),
        hidden=hidden,
    )


def visible_sql(
    row: str,
    *,
    params: list[object],
    reader: SessionReader,
) -> str:
    """Return the test that keeps rows of a session whose transcript ``reader`` reads.

    The reader must hold the session's audience, and a private one only when
    ``reader.hidden``. A session is then read when it is ``staff``, by its account,
    with ``reader.oversight``, and, when it is ``open``, with
    ``reader.every_session`` or by its account's organisation. A session with no
    audience is dropped.

    Args:
      row: The alias, or the table name, the query gives the session's ``inquiries``
        row, such as ``i``.
      params: The query's bound values so far; the reader's are appended.
      reader: Who reads.
      single: The query tests one row, so a chat's starter is looked up for it alone.

    Returns:
      clause: A boolean expression binding the parameters it appended.

    """
    held = _bound(params, sorted(reader.audiences))
    email = _bound(params, reader.email)
    org = _bound(params, reader.org)
    every_session = _bound(params, reader.every_session)
    oversight = _bound(params, reader.oversight)
    hidden = _bound(params, reader.hidden)
    audience = vetted_sql(row, ".agentsession_audience")
    shown = vetted_sql(
        audience,
        " = 'staff' OR lower(",
        row,
        ".account) = ",
        email,
        "::text OR ",
        oversight,
        "::boolean OR (",
        audience,
        " = 'open' AND (",
        every_session,
        "::boolean OR split_part(lower(",
        row,
        ".account), '@', -1) = ",
        org,
        "::text))",
    )
    return vetted_sql(
        "COALESCE(",
        audience,
        " = ANY(",
        held,
        "::text[]) AND (",
        audience,
        " <> 'private' OR ",
        hidden,
        "::boolean) AND (",
        shown,
        "), FALSE)",
    )


def hidden_exists_sql(
    session_column: str,
    *,
    params: list[object],
    reader: SessionReader,
) -> str:
    """Return the test that drops rows of a session whose transcript is not read.

    For a query over a table that does not join ``inquiries``. A row naming no
    session passes, because there is no transcript to hide it from. A session with
    no audience is hidden, as ``require_session_access`` hides it.

    Args:
      session_column: The column holding the session id, such as ``e.session_id``.
      params: The query's bound values so far; the reader's are appended.
      reader: Who reads.

    Returns:
      clause: A boolean expression binding the parameters it appended.

    """
    return vetted_sql(
        "NOT EXISTS (SELECT 1 FROM inquiries AS hidden WHERE hidden.id = ",
        session_column,
        " AND NOT ",
        visible_sql("hidden", params=params, reader=reader),
        ")",
    )


async def reads_session(
    conn: Conn,
    *,
    reader: SessionReader,
    session_id: UUID,
) -> bool:
    """Say whether ``reader`` reads one session's transcript, by :func:`visible_sql`.

    Args:
      conn: A database connection.
      reader: Who reads.
      session_id: The session.

    Returns:
      reads: True when the session exists and the reader reads it.

    """
    params: list[object] = [session_id]
    clause = visible_sql(
        "target",
        params=params,
        reader=reader,
    )
    return bool(
        await conn.fetchval(
            vetted_sql(
                "SELECT ",
                clause,
                " FROM inquiries AS target WHERE target.id = $1",
            ),
            *params,
        ),
    )


def may_hide(caller: str, *, owners: Collection[str], audience: str | None) -> bool:
    """Say whether ``caller`` may hide a session, or show it again.

    Args:
      caller: The email of the person asking.
      owners: The session's owners: its account, and the user of the key that opened
        it; for a chat, its starter.
      audience: The session's audience. A ``staff`` one is never lowered.

    Returns:
      allowed: True for an owner of an ``open`` or ``private`` session. An empty
        owner, a session opened with no key, names no one.

    """
    return audience in {OPEN, PRIVATE} and caller.lower() in {
        owner.lower() for owner in owners if owner
    }


@dataclass(slots=True, kw_only=True)
class TranscriptPings:
    """Which writes to a session's transcript a live stream tells its viewer of.

    A write's ping names the session and its audience, and the viewer hears of it when
    they read that session, by :func:`reads_session`: when a session receives a record
    is part of its transcript. Each answer is asked of the database once and kept for
    ``keep_sec`` under the session and its audience, so a hide or an unhide, which
    changes the audience a ping names, is asked again at once, and a share, an unshare
    or an account move is heard within ``keep_sec``.

    Attributes:
      engine: The database the answers are asked of.
      reader: The stream's viewer.
      keep_sec: How long an answer is kept.
      most_kept: How many answers are kept before every one is asked afresh.

    """

    engine: DatabaseEngine
    reader: SessionReader
    keep_sec: float = 30.0
    most_kept: int = 4_096
    _kept: dict[tuple[UUID, str], tuple[float, bool]] = field(
        default_factory=dict,
        init=False,
    )

    async def hears(self, session_id: UUID, audience: str) -> bool:
        """Say whether the viewer hears of a write to a session's transcript.

        Args:
          session_id: The session written to.
          audience: The audience its ping names.

        Returns:
          heard: True when the viewer reads the session.

        """
        if audience not in self.reader.audiences:
            return False
        now = time.monotonic()
        kept = self._kept.get((session_id, audience))
        if kept is not None and now - kept[0] < self.keep_sec:
            return kept[1]
        async with self.engine.acquire() as conn:
            heard = await reads_session(conn, reader=self.reader, session_id=session_id)
        if len(self._kept) >= self.most_kept:
            self._kept.clear()
        self._kept[(session_id, audience)] = (now, heard)
        return heard


def _bound(params: list[object], value: object) -> str:
    """Append ``value`` to a query's bound values; return its placeholder."""
    params.append(value)
    return f"${len(params)}"
