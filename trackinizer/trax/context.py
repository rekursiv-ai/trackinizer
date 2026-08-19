"""Per-invocation ambient state: output streams, environment, directory.

A one-shot CLI reads these straight off the process -- ``sys.stdout``,
``os.environ``, the working directory. The daemon serves many invocations
concurrently in one process, so reading them there would hand every caller
whichever request happened to set them last.

Each is therefore a :class:`~contextvars.ContextVar` with a default that
falls back to the real process state. A direct CLI run leaves them unset and
behaves exactly as before; the daemon binds them per request inside its own
:class:`~contextvars.Context`, which is thread-local by construction.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import TextIO, cast

import os
import sys


OUT_STREAM: ContextVar[TextIO | None] = ContextVar("trax_out_stream", default=None)
"""Where ``echo`` writes, or ``None`` for the process ``sys.stdout``."""

ERR_STREAM: ContextVar[TextIO | None] = ContextVar("trax_err_stream", default=None)
"""Where ``echo(err=True)`` writes, or ``None`` for the process ``sys.stderr``."""

ENV: ContextVar[dict[str, str] | None] = ContextVar("trax_env", default=None)
"""Caller-supplied environment overlay, or ``None`` for ``os.environ``.

When bound, the overlay is AUTHORITATIVE for the names the caller claims --
see :func:`env`."""

OVERLAID_NAMES: ContextVar[frozenset[str] | None] = ContextVar(
    "trax_overlaid_names", default=None
)
"""Names the overlay speaks for, whether or not the caller had them set.

An unset variable must read as unset, not fall through to the daemon's own
environment: the daemon inherits the shell that happened to spawn it, so a
caller with no ``TRACKINIZER_PROFILE`` would otherwise silently adopt that
shell's profile -- writing to another server under another token."""

CWD: ContextVar[str] = ContextVar("trax_cwd", default="")
"""Caller's working directory, or ``""`` for this process's own."""


def out_stream() -> TextIO:
    """The stream ``echo`` should write to."""
    if (stream := OUT_STREAM.get()) is not None:
        return stream
    # typeshed declares ``sys.stdout`` as ``TextIO | Any``; narrow it so the
    # fallback does not widen this function's return type.
    return cast("TextIO", sys.stdout)


def err_stream() -> TextIO:
    """The stream ``echo(err=True)`` should write to."""
    if (stream := ERR_STREAM.get()) is not None:
        return stream
    return cast("TextIO", sys.stderr)


def env(name: str) -> str | None:
    """Read one environment variable for this invocation.

    A bound overlay is authoritative for the names in :data:`OVERLAID_NAMES`:
    absent there means absent, never "ask the daemon's environment". Any
    other name -- ``HOME``, ``PATH``, whatever a library reads -- still falls
    through to ``os.environ``, which is process-wide by nature.
    """
    if (overlay := ENV.get()) is not None:
        if name in overlay:
            return overlay[name]
        if (claimed := OVERLAID_NAMES.get()) is not None and name in claimed:
            return None
    return os.environ.get(name)


def cwd() -> Path:
    """The directory relative paths in this invocation resolve against."""
    return Path(bound) if (bound := CWD.get()) else Path.cwd()
