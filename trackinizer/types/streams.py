"""The three standard streams, as records a scrape can carry.

``trax run sh -- CMD`` wraps a command that writes no session file, so the
process's own IO IS the session. A line is not merely text: WHICH stream it
crossed is the only structure a scrape has, and it is what tells an answer from
a question. Recording every line as one kind lost that -- a captured session
showed replies with nothing prompting them.

These live in trackinizer rather than in ``trackinizer.lib.agent.types.sessions``
because they describe a TRANSPORT this package owns, not an act a model
performed. Every other IR record answers "what did the agent do"; these answer
"which fd did these bytes cross", which no other adapter can ever emit.

Which streams are distinguishable is decided by the capture mode, not by these
types -- see :mod:`~trackinizer.trax.run.adapters.iostream`. A pty merges
the child's stdout and stderr onto one tty before trax sees a byte, so a pty
run emits no :class:`Stderr` at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from trackinizer.lib.agent.types.sessions import SessionRecord


__all__ = ["Stderr", "Stdin", "Stdout", "TraxRecord"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Stdin:
    """One line delivered TO the wrapped command.

    Either the human's own typing or a message the server spliced in; both are
    bytes the child read, and the child cannot tell them apart either.

    Attributes:
      text: The line as written, its terminator included when it had one.

    """

    context_id: int | None = None
    timestamp: str | None = None
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Stdout:
    """One line the wrapped command wrote to its standard output."""

    context_id: int | None = None
    timestamp: str | None = None
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Stderr:
    """One line the wrapped command wrote to its standard error.

    Emitted only under pipe capture. A pty gives the child ONE terminal for
    both output streams, so under it these bytes arrive as :class:`Stdout` and
    no separation is recoverable afterwards.
    """

    context_id: int | None = None
    timestamp: str | None = None
    text: str


type TraxRecord = SessionRecord | Stdin | Stdout | Stderr
"""Everything ``trax run`` can capture: the shared IR, plus the scrape streams.

A WIDENING of :data:`~trackinizer.lib.agent.types.sessions.SessionRecord`, not a
replacement. Every CLI adapter still yields the shared IR unchanged; only the
scrape adds members, and only because a raw stream has no act to name.
"""
