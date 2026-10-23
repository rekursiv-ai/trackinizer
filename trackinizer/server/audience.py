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

The server's organisation mode (``TRACKINIZER_CHAT_ORGS``) says who reads what, as a
chat canvas does. ``single``: the server is one organisation, so every caller reads
every ``open`` session and every person every open chat. ``domain``: a person's
organisation is their email's domain when Google's ``hd`` claim names it, as a staff
domain pattern asks, and they read the open sessions and chats of their
organisation. ``individual``: each person is an
organisation of one. Whatever the mode, a session's owner (its ``account``) reads it,
and a person who holds ``staff`` or is an admin reads every session that is not a
chat; no role reads another person's chat. A ``private`` session is its owner's and
those people's; a private chat its starter's and the people it is shared with. A
``staff`` session or chat also needs the reader to hold ``staff``.
:class:`SessionReader` carries what the test asks of the caller, and
:func:`reader_for` derives it.

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
    "audiences_for",
    "chat_beginnings_sql",
    "hidden_exists_sql",
    "key_audiences",
    "may_hide",
    "members_reader",
    "org_of",
    "reader_for",
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
      email: The caller's lowercase email: the sessions it owns, and the chats it
        began or that are shared with it. Empty matches none.
      org: The caller's organisation, whose open sessions and chats it reads;
        ``None`` for none.
      every_session: Read every ``open`` session that is not a chat, as anyone on a
        server that is one organisation does.
      every_chat: Read every chat that is not private, as a person or the assistant
        on a server that is one organisation does.
      oversight: Read every session that is not a chat, a private one included, as a
        person who holds ``staff`` or is an admin does.
      hidden: Keep private sessions. A listing passes False unless asked to show
        them; a read by id keeps them for whoever may read them.
      assistant: The configured assistant's email, empty when none: a chat's first
        session that its key opened names the chat's starter.

    """

    audiences: frozenset[str] = frozenset()
    email: str = ""
    org: str | None = None
    every_session: bool = False
    every_chat: bool = False
    oversight: bool = False
    hidden: bool = True
    assistant: str = ""


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


def org_of(email: str, *, hosted_domain: str | None) -> str | None:
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


def audiences_for(
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


def reader_for(
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
    read the assistant makes for a person (``delegated``) is that person's, less what
    no chat may copy out: their oversight and the every-chat grant.

    Args:
      email: The caller's email.
      audiences: The audiences the caller holds.
      admin: Whether the caller is an admin.
      orgs: The server's organisation mode.
      assistant: The configured assistant's email; empty when none.
      hosted_domain: Google's ``hd`` claim from the caller's last login, which
        makes them a member of their email domain's organisation (:func:`org_of`).
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
            org_of(email, hosted_domain=hosted_domain)
            if person and orgs == "domain"
            else None
        ),
        every_session=orgs == "single",
        every_chat=orgs == "single" and (person or the_assistant) and not delegated,
        oversight=person and not delegated and (STAFF in audiences or admin),
        hidden=hidden,
        assistant=assistant,
    )


def members_reader(
    starter: str,
    *,
    orgs: ChatOrgs,
    assistant: str,
) -> SessionReader | None:
    """Describe, as one reader, everyone an open chat ``starter`` begins is open to.

    Besides its starter and the people it is shared with: every person on a
    ``single`` server; on a ``domain`` one, each member of the organisation of the
    starter's email domain, whether or not Google names the starter a member of it,
    and none for a consumer one, of which Google names no one a member. A copy into
    the chat is read by all of them, so it may hold only what this reader reads. It
    is no person, so it owns nothing and oversees nothing.

    Args:
      starter: The email of the chat's starter.
      orgs: The server's organisation mode.
      assistant: The configured assistant's email; empty when none.

    Returns:
      reader: Any of them; ``None`` where an open chat is open to no one else: on an
        ``individual`` server, and on a ``domain`` one for a consumer domain.

    """
    domain = starter.lower().rpartition("@")[2]
    if orgs == "individual" or (orgs == "domain" and domain in CONSUMER_DOMAINS):
        return None
    return SessionReader(
        audiences=frozenset({OPEN, PRIVATE}),
        org=domain if orgs == "domain" else None,
        every_session=orgs == "single",
        every_chat=orgs == "single",
        assistant=assistant,
    )


def visible_sql(
    row: str,
    *,
    params: list[object],
    reader: SessionReader,
    single: bool = False,
) -> str:
    """Return the test that keeps rows of a session whose transcript ``reader`` reads.

    The reader must hold the session's audience, and a private one only when
    ``reader.hidden``. A science chat (a ``chat:`` id) is then read by the person
    who began it, as :func:`chat_beginnings_sql` finds the first session under its
    id, by the people it is shared with, and, unless it is private, by everyone
    with ``reader.every_chat`` and by the starter's organisation. Any other session
    is read when it is ``staff``, by its account, with ``reader.oversight``, and,
    when it is ``open``, with ``reader.every_session`` or by its account's
    organisation. A session with no audience is dropped.

    Args:
      row: The alias, or the table name, the query gives the session's ``inquiries``
        row, such as ``i``.
      params: The query's bound values so far; the reader's eight are appended.
      reader: Who reads.
      single: The query tests one row, so the starter is looked up for that row's
        chat alone. Otherwise every chat's starter is found once for all the rows,
        which a query over many rows needs: looked up row by row, each would scan
        the sessions again.

    Returns:
      clause: A boolean expression binding the next eight parameters.

    """
    # The subqueries name their own rows by these aliases, so under one of them a
    # test of ``row`` moved inside a subquery would read the inner row instead.
    if row in {"began", "began_key", "began_by", "shared", "began_chat"}:
        raise ValueError(f"{row!r} is an alias the science chat test uses itself")
    params.extend(
        (
            sorted(reader.audiences),
            reader.email,
            reader.org,
            reader.every_session,
            reader.every_chat,
            reader.oversight,
            reader.hidden,
            reader.assistant,
        ),
    )
    held, email, org, every_session, every_chat, oversight, hidden, assistant = (
        f"${len(params) - back}" for back in range(7, -1, -1)
    )
    cli = vetted_sql(row, ".agentsession_cli_session_id")
    audience = vetted_sql(row, ".agentsession_audience")
    began = vetted_sql(
        "SELECT began_chat.cli FROM (SELECT DISTINCT ON "
        "(began.agentsession_cli_session_id) began.agentsession_cli_session_id AS cli, "
        "lower(began.account) AS starter ",
        chat_beginnings_sql(assistant=vetted_sql(assistant, "::text")),
        vetted_sql(" AND began.agentsession_cli_session_id = ", cli)
        if single
        else " AND began.agentsession_cli_session_id LIKE 'chat:%'",
        " ORDER BY began.agentsession_cli_session_id, began.created, began.id) "
        "AS began_chat WHERE ",
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
        "::boolean) AND CASE WHEN ",
        cli,
        " LIKE 'chat:%' THEN ",
        cli,
        " IN (SELECT 'chat:' || shared.conversation_id::text "
        "FROM science_chat_shares AS shared WHERE shared.email = ",
        email,
        "::text) OR (",
        audience,
        " <> 'private' AND ",
        every_chat,
        "::boolean) OR ",
        cli,
        " IN (",
        began,
        "began_chat.starter = ",
        email,
        "::text) OR (",
        audience,
        " <> 'private' AND ",
        cli,
        " IN (",
        began,
        "split_part(began_chat.starter, '@', -1) = ",
        org,
        "::text)) ELSE ",
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
        "::text)) END, FALSE)",
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
      params: The query's bound values so far; the reader's eight are appended.
      reader: Who reads.

    Returns:
      clause: A boolean expression binding the next eight parameters.

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
    clause = visible_sql("target", params=params, reader=reader, single=True)
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


def chat_beginnings_sql(*, assistant: str) -> str:
    """Return the ``FROM`` and ``WHERE`` of the sessions that may begin a science chat.

    A chat is begun by the oldest session under its id that the assistant opened, or
    that a key of the session's own account opened. Any writer's key may open a
    session under a ``chat:`` id, but only as its own account and after the chat's
    first session, so neither that nor a session a rotated assistant key opens later
    for a poster begins it. The caller adds the id test and orders by
    ``began.created, began.id``: oldest is by ``created``, which no route writes, never
    by ``started``, which one does.

    Args:
      assistant: SQL naming the assistant's email, such as ``$2``; a NULL or empty
        one lets only a session's own account's key begin it.

    Returns:
      sql: ``FROM inquiries AS began`` joined to the opening key (``began_key``)
        and its user (``began_by``), and a ``WHERE`` the caller extends with
        ``AND``.

    """
    return vetted_sql(
        "FROM inquiries AS began JOIN api_keys AS began_key "
        "ON began_key.id = began.agentsession_opened_by_api_key_id "
        "JOIN users AS began_by ON began_by.id = began_key.user_id "
        "WHERE began.kind = 'AgentSession' AND (began_by.email = ",
        assistant,
        " OR lower(began_by.email) = lower(began.account))",
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
