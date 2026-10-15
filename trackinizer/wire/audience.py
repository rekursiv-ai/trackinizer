"""Session audiences: the values a session may be opened into and what one looks like.

An audience is a short text name. A viewer holds a set of them and opens sessions into
a set of them, and a session's transcript is visible to a viewer who holds its audience
and whom the server's organisation rule lets read it: a science chat's members, a
session's owner, its organisation. ``open`` is the default everyone holds. ``private``
is held by everyone too, and hides a session from all but its owner and the people
who oversee: its owner sets and clears it. ``staff`` is held only by the people the
server names as staff. The column that stores a value checks only its
:data:`AUDIENCE_SHAPE`, so a later value is a change to :data:`AUDIENCES` and not a
migration.

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Annotated, Final

import re

from pydantic import AfterValidator


__all__ = [
    "AUDIENCES",
    "AUDIENCE_SHAPE",
    "OPEN",
    "PRIVATE",
    "SESSION_CONTENT_ROUTES",
    "STAFF",
    "AudienceName",
    "validate_audience",
]


OPEN: Final[str] = "open"
PRIVATE: Final[str] = "private"
STAFF: Final[str] = "staff"

AUDIENCES: Final[frozenset[str]] = frozenset({OPEN, PRIVATE, STAFF})
"""The values a server accepts today."""

AUDIENCE_SHAPE: Final[str] = "^[a-z][a-z0-9_-]{0,31}$"
"""What any stored audience looks like; the SQL CHECK and this pattern agree."""


SESSION_CONTENT_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("POST", "/api/sessions/start"),
        ("POST", "/api/sessions/{session_id}/audience"),
        ("POST", "/api/sessions/{session_id}/end"),
        ("POST", "/api/sessions/{session_id}/inbound"),
        ("GET", "/api/sessions/{session_id}/inbound"),
        ("GET", "/api/sessions/{session_id}/parts"),
        ("GET", "/api/sessions/{session_id}/records"),
        ("POST", "/api/sessions/{session_id}/records"),
        ("GET", "/api/sessions/{session_id}/turns"),
        ("POST", "/api/messages"),
        ("GET", "/api/web/feed"),
        ("GET", "/api/web/feed/facets"),
        ("GET", "/api/web/feed/histogram"),
        ("GET", "/api/web/search_sessions"),
        ("GET", "/api/inquiries"),
        ("GET", "/api/export"),
    },
)
"""Every route that reads, writes or sends into a session's transcript, as
``(method, path)``.

A route belongs here when its path is under ``/api/sessions/``, ``/api/web/feed``,
``/api/messages`` or is ``/api/web/search_sessions``, ``/api/inquiries`` (whose record
filters match transcript text) or ``/api/export``. The audit test lists the routes
the app registers under those and fails on any this set lacks, so a new route that
reads a session cannot ship without being placed here, where a review asks which
check it carries. Each one that reads takes the server's one transcript test, which
holds a session to its audience and to who the server's organisation rule lets read
it. Generic by-id writes (a field edit, a purge) have no path of their own; they look
up the row's kind before they act."""


def validate_audience(value: str) -> str:
    """Return ``value`` when it is an accepted audience.

    Args:
      value: A name from a request body.

    Returns:
      audience: ``value`` unchanged.

    Raises:
      ValueError: ``value`` has the wrong shape or is not an accepted audience.

    """
    if re.fullmatch(AUDIENCE_SHAPE, value) is None or value not in AUDIENCES:
        raise ValueError(f"audience must be one of {sorted(AUDIENCES)}, got {value!r}")
    return value


type AudienceName = Annotated[str, AfterValidator(validate_audience)]
