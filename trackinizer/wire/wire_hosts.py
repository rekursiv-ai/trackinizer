"""Wire contract for the hosts registry: where campaigns may run.

A host is a name an admin records, with a role, one line telling an agent
how to use it, and labels. The registry never reaches a host; a host that
runs a host reports in by heartbeat (``wire_daemon``), and its status is
derived from that. The writer role reads it and the admin role changes it.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

import uuid

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
)

from trackinizer.wire.wire_sessions import RunState


__all__ = [
    "HOSTS_PATH",
    "HOST_DISCONNECT_PATH",
    "HOST_LABELS_PATH",
    "HOST_LOGOUT_PATH",
    "HOST_PATH",
    "HOST_RUNS_PATH",
    "HOST_RUN_STOP_PATH",
    "KNOWHOST_LABEL",
    "MAX_FACTS_BYTES",
    "MAX_HOW_CHARS",
    "NAME_PATTERN",
    "PERSONAL_ROLE",
    "RESERVED_LABELS",
    "RESERVED_NAMES",
    "ROLE_PATTERN",
    "Facts",
    "Host",
    "HostDetail",
    "HostLabels",
    "HostList",
    "HostLogout",
    "HostPut",
    "HostRun",
    "HostRuns",
    "HostStatus",
]

NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,62}$"
"""A host name, as the store accepts it."""

ROLE_PATTERN: Final = r"^(?:[a-z][a-z0-9-]{0,31})?$"
"""A host role; the empty string means none."""

MAX_HOW_CHARS: Final = 2_000
"""Longest ``how`` line a host may hold, in characters."""

RESERVED_NAMES: Final = frozenset(
    {"enroll", "join", "init", "import", "check", "connect", "leave", "top", "start"},
)
"""Names no host may take: they are, or will be, route segments and CLI words."""

KNOWHOST_LABEL: Final = "knowhost"
"""The label of a KnowHost's host, which the server writes as it enrolls one."""

RESERVED_LABELS: Final = frozenset({KNOWHOST_LABEL})
"""Labels the server alone writes, which no label edit adds or removes: KnowHost's sweep
reads its own label as which hosts are KnowHosts, so an edit would end a real
host, or a KnowHost's sandbox."""

PERSONAL_ROLE: Final = "personal"
"""The role of a host a writer enrolled for themselves: only its owner (and an admin)
sees it, so no campaign is offered it."""

MAX_FACTS_BYTES: Final = 4_096
"""Largest ``facts`` object, as compact JSON; the table's limit is twice this."""

type FactValue = (
    Annotated[str, StringConstraints(max_length=256, pattern=r"^[^\x00]*$")]
    | Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
    | Annotated[bool, Field(strict=True)]
    | list[Annotated[str, StringConstraints(max_length=256, pattern=r"^[^\x00]*$")]]
)

_FACTS = TypeAdapter(dict[str, FactValue])


def _small(value: dict[str, FactValue]) -> dict[str, FactValue]:
    """Refuse facts too large for the table, which would otherwise answer 409."""
    if len(_FACTS.dump_json(value)) > MAX_FACTS_BYTES:
        raise ValueError(f"facts exceed {MAX_FACTS_BYTES} bytes")
    return value


type Facts = Annotated[
    dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")],
        FactValue,
    ],
    Field(max_length=64),
    AfterValidator(_small),
]
"""What a host reports about itself: OS, CPUs, memory, CLIs found, and so on."""

type HostStatus = Literal["revoked", "never", "online", "offline"]
"""Derived, never stored: ``revoked`` when every credential was revoked, ``never``
before a first heartbeat, ``online`` within 180 s of the last one unless the host
released the host since, else ``offline``."""

HOSTS_PATH: Final = "/api/hosts"
HOST_PATH: Final = "/api/hosts/{name}"
HOST_LABELS_PATH: Final = "/api/hosts/{name}/labels"
HOST_RUNS_PATH: Final = "/api/hosts/{name}/runs"
HOST_RUN_STOP_PATH: Final = "/api/hosts/{name}/runs/{session_id}/stop"
HOST_DISCONNECT_PATH: Final = "/api/hosts/{name}/disconnect"
HOST_LOGOUT_PATH: Final = "/api/hosts/{name}/logout"


class Host(BaseModel):
    """One registered host, as listed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    """The host's name."""

    role: str = Field(pattern=ROLE_PATTERN)
    """What the host is for; empty when none is recorded."""

    how: str = Field(max_length=MAX_HOW_CHARS)
    """One line telling an agent how to use the host; empty when none."""

    labels: list[str]
    """Free-form tags, in the order they were added."""

    updated_by: str
    """Who last changed it."""

    updated: datetime
    """When it was last changed."""

    status: HostStatus = "never"
    """Whether a host is connected."""

    last_heartbeat: datetime | None = None
    """When the host last heartbeat, or ``None`` before the first."""

    owner: str | None = None
    """The person responsible for it, shown to that person and to admins only."""


class HostDetail(Host):
    """One host with what its host reported about itself."""

    daemon_version: str = ""
    """The host software's version; empty before the first heartbeat."""

    facts: Facts = Field(default_factory=dict)
    """What the host reported: OS, CPUs, memory, CLIs found, and so on."""


class HostList(BaseModel):
    """Every registered host, sorted by name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hosts: list[Host]


class HostPut(BaseModel):
    """Create a host or change its fields; a field left out keeps its value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str | None = Field(default=None, pattern=ROLE_PATTERN)
    """The new role; ``""`` clears it."""

    how: str | None = Field(default=None, max_length=MAX_HOW_CHARS)
    """The new how line; ``""`` clears it."""

    @field_validator("how")
    @classmethod
    def _no_nul(cls, value: str | None) -> str | None:
        """Refuse a NUL, which the store's text type cannot hold."""
        if value is not None and "\x00" in value:
            raise ValueError("how contains a NUL character")
        return value


class HostLabels(BaseModel):
    """Add labels, then remove labels; each is a no-op when already so."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    add: list[str] = Field(default_factory=list)
    """Labels to add, as the Issue label rules canonicalise them."""

    remove: list[str] = Field(default_factory=list)
    """Labels to remove."""


class HostRun(BaseModel):
    """A run of a CLI that names the host, under its owner's account: live, or past.

    The host is the run's own word on each poll, shown to the owner and stopped by
    them, never a grant.
    """

    # ``wire_terminals.TerminalHosts`` holds it, which the configured assistant reads
    # without the ``Client``: it ignores a field a newer server added, as the listing
    # does.
    model_config = ConfigDict(extra="ignore", frozen=True)

    session_id: uuid.UUID
    """The session the run drains, which its chat names."""

    cli: str
    """The CLI the run is: ``claude`` or ``codex``."""

    run: str | None = None
    """The detached host's name, which an attach takes; ``None`` for a run in a terminal
    of its own."""

    cwd: str | None = None
    """Its working directory, ``~/...`` under its home."""

    state: RunState = "idle"
    """What the run last reported of itself."""

    last_seen: datetime | None = None
    """A past run's: when its session last polled; ``None`` for a live session."""

    ended: datetime | None = None
    """A past run's: when its session ended; ``None`` while it is open."""

    seq: int | None = None
    """Its AgentSession's number; ``None`` for a session that is not the owner's, and
    where the route stops runs rather than lists them."""

    conversation_id: uuid.UUID | None = None
    """The chat its session is a session of; ``None`` for a session that is no
    chat's."""


class HostRuns(BaseModel):
    """The live sessions of one of the caller's hosts, and the ones it had before."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runs: list[HostRun]

    past: list[HostRun] = Field(default_factory=list)
    """The runs that last said they ran there and are not live, most recently heard
    from first; empty where the route stops runs rather than lists them."""


class HostLogout(BaseModel):
    """What logging a host out stopped and revoked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runs: list[HostRun]
    """The live sessions it stopped."""

    keys: int
    """How many of the caller's keys it revoked: those a device login made for it."""
