"""Wire contract for a machine host: enrollment, joining, heartbeat and revoke.

An admin enrolls a machine by name and receives a one-use token. The host
presents that token once to join, receives a machine credential, and then
heartbeats with the credential. An admin revokes the machine, after which every
host route answers the credential 410.

Both secrets are bearer strings of the form ``<prefix><32 hex id>_<43 char
secret>``. The server stores only a hash of the secret and finds the row by the
id, so a lookup costs one indexed read.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final

import uuid

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from trackinizer.wire.wire_machines import NAME_PATTERN, Facts


__all__ = [
    "CREDENTIAL_PREFIX",
    "ENROLLMENT_PREFIX",
    "ENROLL_PATH",
    "ENROLL_TTL_SEC",
    "HEARTBEAT_PATH",
    "HEARTBEAT_SEC",
    "JOIN_PATH",
    "OFFLINE_AFTER_SEC",
    "REVOKE_PATH",
    "EnrollRequest",
    "EnrollResponse",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "HostVersion",
    "JoinRequest",
    "JoinResponse",
]

CREDENTIAL_PREFIX: Final = "trax_machine_"
"""Starts every machine credential; no API key may start with it."""

ENROLLMENT_PREFIX: Final = "enr_"
"""Starts every enrollment token."""

ENROLL_TTL_SEC: Final = 900
"""How long an enrollment token stays valid, in seconds."""

HEARTBEAT_SEC: Final = 15
"""How often a host heartbeats, in seconds."""

OFFLINE_AFTER_SEC: Final = 180
"""Silence after which a machine reads ``offline``, and a second host may take over."""

ENROLL_PATH: Final = "/api/machines/enroll"
JOIN_PATH: Final = "/api/machines/join"
HEARTBEAT_PATH: Final = "/api/machines/{machine_id}/heartbeat"
REVOKE_PATH: Final = "/api/machines/{name}/revoke"

type HostVersion = Annotated[
    str,
    StringConstraints(max_length=64, pattern=r"^[^\x00]*$"),
]
"""The host software's version, as the table holds it: no NUL, at most 64 characters."""


class EnrollRequest(BaseModel):
    """Enroll a machine: the name the host will join as."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    """The machine's name; it is registered when new."""


class EnrollResponse(BaseModel):
    """A one-use enrollment token, shown once."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(repr=False)
    """The token the host joins with; the server keeps only a hash of it."""

    expires_at: datetime
    """When the token stops working."""


class JoinRequest(BaseModel):
    """Join as a machine with an enrollment token."""

    # Without this, the error for an over-long token quotes the token.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    name: str = Field(pattern=NAME_PATTERN)
    """The machine's name; the token is bound to it."""

    token: str = Field(max_length=128, repr=False)
    """The enrollment token; never echoed by a rejection."""

    instance: uuid.UUID
    """The host's own id, kept across its restarts."""

    host_version: HostVersion
    """The host software's version."""

    facts: Facts = Field(default_factory=dict)
    """What the host reports about itself."""


class JoinResponse(BaseModel):
    """The machine credential, shown once."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    machine_id: uuid.UUID
    """The machine's id, which every host route carries in its path."""

    credential: str = Field(repr=False)
    """The machine credential; the server keeps only a hash of it."""


class HeartbeatRequest(BaseModel):
    """One heartbeat: who is calling, and optionally what changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance: uuid.UUID
    """The host's id; a second id is refused until the first goes silent."""

    host_version: HostVersion
    """The host software's version."""

    facts: Facts | None = None
    """Replaces the stored facts when present; absent keeps them."""


class HeartbeatResponse(BaseModel):
    """The server's clock at the heartbeat."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server_time: datetime
