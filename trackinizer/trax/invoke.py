"""Run one trax command inside another program, with its own streams and state.

The daemon serves many callers in one process, and an agent's tool runs trax inside
the agent's process. Neither may let one command's output, environment or working
directory touch another's, or the host program's own: each run binds the ambient
state of ``context`` in a fresh :class:`~contextvars.Context`, and writes nothing
process-wide.

Typical usage example:

  ran = invoke(["issue", "7"], env={}, width=0, local_input=None, run=cli.parse_and_run)
  print(ran.stdout)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import contextvars
import io
import traceback

from trackinizer.client.errors import ClientError
from trackinizer.trax.context import (
    CWD,
    ENV,
    ERR_STREAM,
    LOCAL_INPUT,
    OUT_STREAM,
    OVERLAID_NAMES,
)
from trackinizer.trax.daemon.protocol import FORWARDED_ENV, KEY_ENV, KEY_FILE_ENV
from trackinizer.trax.render import TERMINAL_WIDTH


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


__all__ = ["INTERNAL_ERROR_EXIT_CODE", "Invocation", "invoke"]


INTERNAL_ERROR_EXIT_CODE: Final = 70
"""EX_SOFTWARE: an unexpected exception escaped a command. Distinct from 2 so a bug
is not mistaken for a user-facing CLI error."""

_CLIENT_ERROR_EXIT_CODE: Final = 2
"""``cli.main`` exits 2 on ``ClientError``; scripts branch on it, so a run inside
another program must reproduce it rather than invent its own code."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Invocation:
    """What one command wrote, and how it ended."""

    stdout: str
    """Everything it wrote through ``echo``."""

    stderr: str
    """Everything it wrote through ``echo(err=True)``, then the error it ended with."""

    exit_code: int
    """0 on success; 2 on a ``ClientError``, as ``cli.main``; a ``SystemExit``'s
    status; 70 when another exception escaped, its traceback in ``stderr``."""


def invoke(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    width: int,
    local_input: str | None,
    run: Callable[[Sequence[str]], None],
) -> Invocation:
    """Run one trax command and capture what it wrote.

    Args:
      argv: The command's tokens, without the leading ``trax``.
      env: The caller's environment for trax's routing names. Every routing name
        (``TRACKINIZER_URL``, its key and profile, ``TRAX_*`` the daemon forwards)
        is claimed, so a name absent here reads as unset, never as this process's.
      width: The table width to render at; 0 renders every column, as piped.
      local_input: The caller's folder: a ``-`` value reads stdin and an ``@path``
        value reads a file against it, as at the caller's shell. ``None`` reads no
        local input: either value is refused before anything is read.
      run: Parses and runs the command: ``cli.parse_and_run``, or, for a caller
        with its own client or a narrower verb list,
        ``functools.partial(parse_and_run, client_factory=..., allowed=...)``.

    Returns:
      invocation: The command's stdout, stderr and exit code.

    """
    out, err = io.StringIO(), io.StringIO()
    # A fresh Context per run: every binding below is a ContextVar, so concurrent
    # runs, and the caller itself, would otherwise see each other's.
    context = contextvars.copy_context()
    context.run(OUT_STREAM.set, out)
    context.run(ERR_STREAM.set, err)
    context.run(ENV.set, dict(env))
    # The key and its file are claimed though no caller forwards them: without the
    # claim, a name the caller did not send falls through to this process's own
    # environment, and its key would authenticate the caller.
    context.run(OVERLAID_NAMES.set, frozenset((*FORWARDED_ENV, KEY_ENV, KEY_FILE_ENV)))
    context.run(TERMINAL_WIDTH.set, width)
    context.run(LOCAL_INPUT.set, local_input is not None)
    if local_input is not None:
        context.run(CWD.set, local_input)
    try:
        exit_code = context.run(_exit_code, run, argv, err)
    except BaseException:  # noqa: BLE001 -- one bad command must not kill its host.
        return Invocation(
            stdout=out.getvalue(),
            stderr=err.getvalue() + traceback.format_exc(),
            exit_code=INTERNAL_ERROR_EXIT_CODE,
        )
    return Invocation(stdout=out.getvalue(), stderr=err.getvalue(), exit_code=exit_code)


def _exit_code(
    run: Callable[[Sequence[str]], None],
    argv: Sequence[str],
    err: io.StringIO,
) -> int:
    """Run the command; map how it ended to an exit code, as ``cli.main`` does."""
    try:
        run(argv)
    except ClientError as error:
        err.write(f"trax: {error}\n")
        return _CLIENT_ERROR_EXIT_CODE
    except SystemExit as exit_request:
        return _exit_status(exit_request, err)
    return 0


# ``sys.exit`` accepts a string, which the interpreter prints to stderr and reports as
# status 1. Coercing it with ``int()`` would raise instead, and the real message would
# be replaced by an internal-error traceback.
def _exit_status(exit_request: SystemExit, err: io.StringIO) -> int:
    """Map a ``SystemExit`` payload to a process exit status."""
    code = exit_request.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    err.write(f"{code}\n")
    return 1
