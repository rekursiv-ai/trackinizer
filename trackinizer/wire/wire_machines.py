"""Wire contract for the machines registry: where campaigns may run.

A machine is a name an admin records, with a role, one line telling an agent
how to use it, and labels. The registry never reaches a machine; a machine that
runs a host reports in by heartbeat (``wire_machine_host``), and its status is
derived from that. The writer role reads it and the admin role changes it.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
)


__all__ = [
    "MACHINES_PATH",
    "MACHINE_LABELS_PATH",
    "MACHINE_PATH",
    "MAX_FACTS_BYTES",
    "MAX_HOW_CHARS",
    "NAME_PATTERN",
    "RESERVED_NAMES",
    "ROLE_PATTERN",
    "Facts",
    "Machine",
    "MachineDetail",
    "MachineLabels",
    "MachineList",
    "MachinePut",
    "MachineStatus",
]

NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,62}$"
"""A machine name, as the store accepts it."""

ROLE_PATTERN: Final = r"^(?:[a-z][a-z0-9-]{0,31})?$"
"""A machine role; the empty string means none."""

MAX_HOW_CHARS: Final = 2_000
"""Longest ``how`` line a machine may hold, in characters."""

RESERVED_NAMES: Final = frozenset(
    {"enroll", "join", "init", "import", "check", "connect", "leave", "top"},
)
"""Names no machine may take: they are, or will be, route segments and CLI words."""

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

type MachineStatus = Literal["revoked", "never", "online", "offline"]
"""Derived, never stored: ``revoked`` when every credential was revoked, ``never``
before a first heartbeat, ``online`` within 180 s of the last one, else ``offline``."""

MACHINES_PATH: Final = "/api/machines"
MACHINE_PATH: Final = "/api/machines/{name}"
MACHINE_LABELS_PATH: Final = "/api/machines/{name}/labels"


class Machine(BaseModel):
    """One registered machine, as listed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    """The machine's name."""

    role: str = Field(pattern=ROLE_PATTERN)
    """What the machine is for; empty when none is recorded."""

    how: str = Field(max_length=MAX_HOW_CHARS)
    """One line telling an agent how to use the machine; empty when none."""

    labels: list[str]
    """Free-form tags, in the order they were added."""

    updated_by: str
    """Who last changed it."""

    updated: datetime
    """When it was last changed."""

    status: MachineStatus = "never"
    """Whether a host is connected."""

    last_heartbeat: datetime | None = None
    """When the host last heartbeat, or ``None`` before the first."""


class MachineDetail(Machine):
    """One machine with what its host reported about itself."""

    host_version: str = ""
    """The host software's version; empty before the first heartbeat."""

    facts: Facts = Field(default_factory=dict)
    """What the host reported: OS, CPUs, memory, CLIs found, and so on."""


class MachineList(BaseModel):
    """Every registered machine, sorted by name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    machines: list[Machine]


class MachinePut(BaseModel):
    """Create a machine or change its fields; a field left out keeps its value."""

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


class MachineLabels(BaseModel):
    """Add labels, then remove labels; each is a no-op when already so."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    add: list[str] = Field(default_factory=list)
    """Labels to add, as the Issue label rules canonicalise them."""

    remove: list[str] = Field(default_factory=list)
    """Labels to remove."""
