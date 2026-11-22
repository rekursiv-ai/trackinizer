"""Wire contract for a daemon: enrollment, joining, heartbeat and revoke.

An admin enrolls a host by name and receives a one-use token. The daemon
presents that token once to join, receives a host credential, and then
heartbeats with the credential. An admin revokes the host, after which every
daemon route answers the credential 410.

Both secrets are bearer strings of the form ``<prefix><32 hex id>_<43 char
secret>``. The server stores only a hash of the secret and finds the row by the
id, so a lookup costs one indexed read.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

import re
import uuid

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from trackinizer.wire.wire_hosts import NAME_PATTERN, Facts


__all__ = [
    "CREDENTIAL_PREFIX",
    "ENROLLMENT_PREFIX",
    "ENROLL_PATH",
    "ENROLL_TTL_SEC",
    "HEARTBEAT_PATH",
    "HEARTBEAT_SEC",
    "JOIN_PATH",
    "MIN_DAEMON_VERSION",
    "OFFLINE_AFTER_SEC",
    "REVOKE_PATH",
    "TOLERANT_DAEMON_VERSION",
    "Claim",
    "DaemonVersion",
    "EnrollRequest",
    "EnrollResponse",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "JoinRequest",
    "JoinResponse",
    "older_than",
]

CREDENTIAL_PREFIX: Final = "trax_machine_"
"""Starts every host credential; no API key may start with it."""

ENROLLMENT_PREFIX: Final = "enr_"
"""Starts every enrollment token."""

ENROLL_TTL_SEC: Final = 900
"""How long an enrollment token stays valid, in seconds."""

HEARTBEAT_SEC: Final = 15
"""How often a daemon heartbeats, in seconds."""

OFFLINE_AFTER_SEC: Final = 180
"""Silence after which a host reads ``offline``."""

MIN_DAEMON_VERSION: Final = "0.1.10"
"""The oldest trackinizer a daemon may run, as its heartbeat's ``daemon_version`` names it.
0.1.10 first carried today's terminal wait, with ``canvas`` and ``workspace``: 0.1.8's
names the fields they were renamed from, so it refuses every wait from a canvas and
never opens a terminal. 0.1.9 was never released."""

TOLERANT_DAEMON_VERSION: Final = "0.1.11"
"""The first trackinizer whose daemon ignores a field it does not know in an answer. A
heartbeat tells only such a daemon that it is outdated: an older one would refuse the
whole answer over the field, and stop serving."""

ENROLL_PATH: Final = "/api/hosts/enroll"
JOIN_PATH: Final = "/api/hosts/join"
HEARTBEAT_PATH: Final = "/api/hosts/{host_id}/heartbeat"
REVOKE_PATH: Final = "/api/hosts/{name}/revoke"

type DaemonVersion = Annotated[
    str,
    StringConstraints(max_length=64, pattern=r"^[^\x00]*$"),
]
"""The daemon software's version, as the table holds it: no NUL, at most 64 characters."""

type Claim = Literal["keep", "take", "release"]
"""What a heartbeat does with the daemon slot. ``keep`` holds it unless another
instance beat within 180 s; ``take`` holds it whatever the other did, and is a daemon's
first heartbeat, so a restarted daemon never waits out its old self; ``release`` lets
go, so the host reads ``offline`` at once, and is a stopping daemon's last call."""


class EnrollRequest(BaseModel):
    """Enroll a host: the name the daemon will join as."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    """The host's name; it is registered when new."""

    asker: str | None = Field(default=None, max_length=254)
    """From the configured assistant alone: the person the host is enrolled for.
    It becomes theirs, personal and already confirmed. The assistant attests to it as
    it does for a chat's settings; anyone else's ``asker`` is refused."""


class EnrollResponse(BaseModel):
    """A one-use enrollment token, shown once."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(repr=False)
    """The token the daemon joins with; the server keeps only a hash of it."""

    expires_at: datetime
    """When the token stops working."""

    owner: str
    """Who owns the host: the enroller, or the person who enrolled it first."""


class JoinRequest(BaseModel):
    """Join as a host with an enrollment token."""

    # Without this, the error for an over-long token quotes the token.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    name: str = Field(pattern=NAME_PATTERN)
    """The host's name; the token is bound to it."""

    token: str = Field(max_length=128, repr=False)
    """The enrollment token; never echoed by a rejection."""

    instance: uuid.UUID
    """The daemon's id, new each time ``connect`` starts."""

    daemon_version: DaemonVersion
    """The daemon software's version."""

    facts: Facts = Field(default_factory=dict)
    """What the daemon reports about itself."""


class JoinResponse(BaseModel):
    """The host credential, shown once."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host_id: uuid.UUID
    """The host's id, which every daemon route carries in its path."""

    credential: str = Field(repr=False)
    """The host credential; the server keeps only a hash of it."""


class HeartbeatRequest(BaseModel):
    """One heartbeat: who is calling, and optionally what changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance: uuid.UUID
    """The daemon's id; ``keep`` refuses a second id until the first goes silent."""

    daemon_version: DaemonVersion
    """The daemon software's version."""

    facts: Facts | None = None
    """Replaces the stored facts when present; absent keeps them."""

    claim: Claim = "keep"
    """Keep the host, take it from another instance, or release it."""


class HeartbeatResponse(BaseModel):
    """The server's clock at the heartbeat, and whether the daemon must be upgraded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server_time: datetime

    daemon_outdated: bool = False
    """The daemon runs a trackinizer older than :data:`MIN_DAEMON_VERSION`. The server
    sends it only when true, and only to a daemon at :data:`TOLERANT_DAEMON_VERSION` or
    later."""


def older_than(version: str, *, oldest: str) -> bool:
    """Return whether a daemon's ``version`` is a release before ``oldest``.

    Only the leading dotted numbers count, so ``0.1.11rc1`` and ``0.1.11.dev3`` read as
    ``0.1.11``. A version with none, such as a source checkout's empty one, is never
    older: nothing says the daemon is.

    Args:
      version: The ``daemon_version`` a heartbeat reported.
      oldest: The oldest release that is not older.

    Returns:
      older: Whether ``version`` names a release before ``oldest``.

    """
    release = _release(version)
    return bool(release) and release < _release(oldest)


def _release(version: str) -> tuple[int, ...]:
    """Return the leading dotted numbers of ``version``; none when it starts with none."""
    found = re.match(r"\d+(?:\.\d+)*", version)
    return () if found is None else tuple(int(part) for part in found[0].split("."))
