"""Wire contract for the variables an agent launch exports.

A variable is a name a launch puts in its environment. A plain variable's
value is stored and read back. A secret's value lives in the server's secret
backend and no route returns it, so a secret on the wire always has
``value=None``. Layers order the values: org, then machine, then user, each
later one winning. Only the org layer has routes today.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field


__all__ = [
    "MAX_VALUE_BYTES",
    "NAME_PATTERN",
    "VARIABLES_PATH",
    "VARIABLE_PATH",
    "Layer",
    "Variable",
    "VariableList",
    "VariablePut",
]

Layer = Literal["org", "machine", "user"]
"""Where a value is set: org, then machine, then user, each later one winning."""

NAME_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"
"""An environment-variable name, as the store accepts it."""

MAX_VALUE_BYTES: Final = 65_536
"""Longest value a variable may hold, in UTF-8 bytes."""

VARIABLES_PATH: Final = "/api/variables"
VARIABLE_PATH: Final = "/api/variables/{name}"


class Variable(BaseModel):
    """One stored variable, as listed; a secret never carries its value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    layer: Layer
    """The layer the value is set in."""

    owner: str
    """Empty for the org layer; the machine's name or the user's email otherwise."""

    name: str = Field(pattern=NAME_PATTERN)
    """The environment-variable name."""

    secret: bool
    """Whether the value is write-only."""

    value: str | None
    """The plain value; always ``None`` for a secret."""

    updated_by: str
    """Who last set it."""

    updated: datetime
    """When it was last set."""


class VariableList(BaseModel):
    """Every variable of a layer, sorted by name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variables: list[Variable]


class VariablePut(BaseModel):
    """Set one variable. The response has no body, so a secret is never echoed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1)
    """The value; at most ``MAX_VALUE_BYTES`` in UTF-8."""

    secret: bool = False
    """Store the value write-only, in the secret backend."""
