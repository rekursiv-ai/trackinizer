"""Refuse a command that moved out of trax, in one line.

``trax run``, ``trax machine`` and ``trax job`` act on computers, agent runs and GPU
nodes rather than on the research records, so they left trax. Each exits 2 saying so.
"""

from __future__ import annotations

from typing import Final, NoReturn

from trackinizer.trax.render import echo


__all__ = ["MOVED_VERBS", "RUN_CLIS", "moved"]

MOVED_VERBS: Final = frozenset({"run", "machine", "job"})
"""The verbs that moved out of trax; each still says so."""

RUN_CLIS: Final = frozenset({"claude", "codex", "sagent"})
"""The CLIs a stored session can resume as: each names a session by a stable id."""


def moved(form: str) -> NoReturn:
    """Say that ``trax FORM`` moved out of trax, on one line, and exit 2.

    Args:
      form: The command's words after ``trax``, such as ``run`` or ``job``.

    Raises:
      SystemExit: Always, with status 2.

    """
    line = f"trax {form} moved out of trax"
    echo(line, err=True)
    raise SystemExit(2)
