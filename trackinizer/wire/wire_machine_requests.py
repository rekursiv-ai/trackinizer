"""Wire contract for machine requests: what a machine asks its provider to do.

A machine may not do everything itself (a push needs a credential it does not
hold), so it writes a request with its own machine credential: a kind and a JSON
body. The provider, holding an admin key, reads the machine's pending requests,
does the work, and records the result once, as ``done`` or ``failed``. An admin or
the machine itself reads a request back; no machine reads another's.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

import uuid

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from trackinizer.wire.json_types import JSON, storable_object


__all__ = [
    "KIND_PATTERN",
    "MACHINE_REQUESTS_PATH",
    "MACHINE_REQUEST_PATH",
    "MACHINE_REQUEST_RESULT_PATH",
    "MAX_PAYLOAD_BYTES",
    "MachineRequest",
    "MachineRequestCreate",
    "MachineRequestList",
    "MachineRequestResult",
    "Payload",
    "RequestOutcome",
    "RequestStatus",
]

KIND_PATTERN: Final = r"^[a-z][a-z0-9-]{0,31}$"
"""What a request asks for, such as ``push``; the provider decides what it means."""

MAX_PAYLOAD_BYTES: Final = 65_536
"""Largest request body or result, as compact JSON; the table's limit is twice this."""

MACHINE_REQUESTS_PATH: Final = "/api/machines/{name}/requests"
MACHINE_REQUEST_PATH: Final = "/api/machines/{name}/requests/{request_id}"
MACHINE_REQUEST_RESULT_PATH: Final = "/api/machines/{name}/requests/{request_id}/result"


def _storable_payload(value: JSON) -> JSON:
    """Refuse a body or result the table cannot hold."""
    return storable_object(value, max_bytes=MAX_PAYLOAD_BYTES)


type Payload = Annotated[JSON, AfterValidator(_storable_payload)]
"""A request's body or result: a JSON object of at most 64 KiB with no NUL and no
non-finite number."""

type RequestStatus = Literal["pending", "done", "failed"]
"""``pending`` until the provider records a result, then ``done`` or ``failed``."""

type RequestOutcome = Literal["done", "failed"]
"""The status a provider records with a result."""


class MachineRequestCreate(BaseModel):
    """A request a machine writes for its provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(pattern=KIND_PATTERN)
    """What it asks for."""

    body: Payload = Field(default_factory=dict)
    """What the provider needs to do it."""


class MachineRequestResult(BaseModel):
    """The provider's one answer to a request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RequestOutcome
    """Whether the provider did what was asked."""

    result: Payload = Field(default_factory=dict)
    """What the provider reports back."""


class MachineRequest(BaseModel):
    """One request, as stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    """The request's id."""

    kind: str = Field(pattern=KIND_PATTERN)
    """What it asks for."""

    body: Payload
    """What the machine sent."""

    status: RequestStatus
    """Whether the provider answered, and how."""

    result: Payload
    """What the provider reported; empty while pending."""

    created_by: str
    """The machine that wrote it."""

    created: datetime
    """When it was written."""

    updated: datetime
    """When it last changed: its creation, or its result."""


class MachineRequestList(BaseModel):
    """A machine's requests, oldest first."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: list[MachineRequest]
