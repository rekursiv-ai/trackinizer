"""Lightweight types shared across the library.

Importable from any CLI / tool / non-tensor library without dragging in heavy
tensor dependencies.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, Self, override, runtime_checkable


__all__ = [
    "ABSENT",
    "Absent",
    "JobProtocol",
    "LaunchableExperiment",
]


@runtime_checkable
class JobProtocol(Protocol):
    def run(self, *args: str) -> None: ...


@runtime_checkable
class LaunchableExperiment(Protocol):
    """A config the launcher can stamp with run identity and a docstring.

    The launcher auto-derives ``study_name`` (run-family prefix from the module
    path) and ``experiment_name`` (the factory function name) when either is left
    empty. It attaches the factory's docstring to ``doc`` when unset. A config
    opts in by declaring these fields; a standalone job lacking them is launched
    untouched.
    """

    study_name: str
    experiment_name: str
    doc: str


class Absent:
    """Sentinel for omitted keyword values. Compare with ``is ABSENT``."""

    __slots__ = ()

    _instance: ClassVar[Self | None] = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @override
    def __repr__(self) -> str:
        return "ABSENT"

    def __bool__(self) -> bool:
        return False

    @override
    def __reduce__(self) -> str:
        return "ABSENT"


ABSENT = Absent()
