"""Who holds which audience.

Staff is a deploy-time setting: a list of patterns, each ``user@host`` or
``*@domain``. A literal pattern matches the verified email alone. A domain pattern
also needs Google's ``hd`` claim (stored as ``users.hosted_domain``) to name the same
domain, because Google verifies a consumer account's address on a staff domain without
making it a member of the organisation. Nothing set means nobody is staff.

An API key narrows its owner and never widens it: ``open_into`` pins the one audience
it opens sessions into. It reads what its owner reads.

A query that returns session content narrows itself to the audiences its caller reads
with the SQL fragments here, so the rule is spelled once: :func:`visible_sql` for a
query that already joins ``inquiries``, :func:`hidden_exists_sql` for one that does not.
A store read that takes the caller's audiences defaults to :data:`OPEN_ONLY`, so a
caller that leaves them out hides a staff session from a reader who may see it and
never shows one to a reader who may not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from trackinizer.server.values import vetted_sql
from trackinizer.wire.audience import OPEN, STAFF


if TYPE_CHECKING:
    from collections.abc import Sequence


__all__ = [
    "OPEN_ONLY",
    "Audiences",
    "audiences_for",
    "hidden_exists_sql",
    "key_audiences",
    "visible_sql",
]


OPEN_ONLY: Final = frozenset({OPEN})
"""What any signed-in caller reads: the sessions whose audience is ``open``."""


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
      audiences: ``open``, and ``staff`` too when a pattern matches.

    """
    email = email.lower()
    domain = email.rpartition("@")[2] if "@" in email else ""
    in_domain = hosted_domain is not None and hosted_domain.lower() == domain
    for pattern in staff:
        # A domain pattern is never a literal address: an email that spells it
        # (``*`` is legal in a local part) must still pass the ``hd`` check.
        is_domain = pattern.startswith("*@")
        if is_domain and in_domain and pattern == f"*@{domain}":
            return frozenset({OPEN, STAFF})
        if not is_domain and pattern == email:
            return frozenset({OPEN, STAFF})
    return frozenset({OPEN})


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


def visible_sql(
    column: str,
    *,
    params: list[object],
    audiences: frozenset[str],
) -> str:
    """Return the test that keeps rows of a session whose audience is read.

    Args:
      column: The session's audience column as the query names it, such as
        ``i.agentsession_audience`` when the query joins ``inquiries`` as ``i``.
      params: The query's bound values so far; the audiences are appended.
      audiences: What the caller reads.

    Returns:
      clause: A boolean expression binding the next parameter.

    """
    params.append(sorted(audiences))
    return vetted_sql(column, " = ANY($", str(len(params)), "::text[])")


def hidden_exists_sql(
    session_column: str,
    *,
    params: list[object],
    audiences: frozenset[str],
) -> str:
    """Return the test that drops rows of a session whose audience is not read.

    For a query over a table that does not join ``inquiries``. A row naming no
    session passes, because there is no audience to hide it from. A session with no
    audience is hidden, as ``require_session_access`` hides it.

    Args:
      session_column: The column holding the session id, such as ``e.session_id``.
      params: The query's bound values so far; the audiences are appended.
      audiences: What the caller reads.

    Returns:
      clause: A boolean expression binding the next parameter.

    """
    params.append(sorted(audiences))
    return vetted_sql(
        "NOT EXISTS (SELECT 1 FROM inquiries AS hidden WHERE hidden.id = ",
        session_column,
        " AND NOT coalesce(hidden.agentsession_audience, '') = ANY($",
        str(len(params)),
        "::text[]))",
    )
