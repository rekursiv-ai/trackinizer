"""Wire contract for the exec channel: run one command on a connected computer.

The configured assistant runs a command on a computer its asker connected, and gets
the command's result back in the same request:

- The assistant ``POST``s :data:`EXEC_PATH` with an :class:`ExecRequest` and holds the
  request until the host reports.
- The machine's host holds a ``GET`` on :data:`MACHINE_EXEC_WAIT_PATH`; a queued command
  returns at once as an :class:`ExecWait`, and the host runs it as the machine's user.
- The host ``POST``s an :class:`ExecResult` to :data:`MACHINE_EXEC_RESULT_PATH`.

A command that passes its ``timeout_sec`` is killed by the host, which reports exit code
:data:`TIMEOUT_EXIT_CODE`. Output is bytes, so both streams travel as base64.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Annotated, Final

import uuid

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from trackinizer.wire.wire_terminals import RunFolder


__all__ = [
    "EXEC_PATH",
    "MACHINE_EXEC_RESULT_PATH",
    "MACHINE_EXEC_WAIT_PATH",
    "MAX_ENV_VARS",
    "MAX_EXECS_PER_MIN_PER_USER",
    "MAX_RUNNING_PER_MACHINE",
    "MAX_STREAM_BYTES",
    "OFFER_TTL_SEC",
    "RESULT_GRACE_SEC",
    "TIMEOUT_EXIT_CODE",
    "WAIT_MAX_SEC",
    "ExecRequest",
    "ExecResult",
    "ExecWait",
]

EXEC_PATH: Final = "/api/machines/{name}/exec"
"""``POST ?asker=EMAIL``: run a command on the asker's machine; the configured
assistant's alone. The request is held until the host reports."""

MACHINE_EXEC_WAIT_PATH: Final = "/api/machines/{machine_id}/exec/wait"
"""``GET ?instance=ID``, held up to :data:`WAIT_MAX_SEC`: the host's next command, or
``null`` when none came."""

MACHINE_EXEC_RESULT_PATH: Final = "/api/machines/{machine_id}/exec/{exec_id}/result"
"""``POST``: the host's :class:`ExecResult` for a command it was handed."""

MAX_RUNNING_PER_MACHINE: Final = 4
"""Commands one machine holds at once, queued or running (409 ``exec_busy``)."""

MAX_EXECS_PER_MIN_PER_USER: Final = 60
"""Commands one asker may start in a minute (429 ``exec_limit``)."""

WAIT_MAX_SEC: Final = 25
"""The longest the server holds a host's wait."""

OFFER_TTL_SEC: Final = 30
"""How long a queued command waits for its host to take it (504 ``exec_lost``)."""

RESULT_GRACE_SEC: Final = 15
"""How long past ``timeout_sec`` the server waits for the host's result."""

TIMEOUT_EXIT_CODE: Final = 124
"""The exit code of a command killed at its timeout, as ``timeout(1)`` reports it."""

MAX_STREAM_BYTES: Final = 1_048_576
"""The most a host reports for one stream; it cuts a longer one from the middle."""

MAX_ENV_VARS: Final = 64

_MAX_STREAM_B64: Final = (MAX_STREAM_BYTES + 2) // 3 * 4
"""The base64 length of :data:`MAX_STREAM_BYTES` bytes."""


def _within_stream(value: str) -> str:
    """Refuse base64 that decodes to more than :data:`MAX_STREAM_BYTES` bytes."""
    if len(value) // 4 * 3 - value.count("=") > MAX_STREAM_BYTES:
        raise ValueError(f"decodes to more than {MAX_STREAM_BYTES} bytes")
    return value


type Base64 = Annotated[
    str,
    Field(
        max_length=_MAX_STREAM_B64,
        pattern=r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$",
    ),
    AfterValidator(_within_stream),
]
"""Standard padded base64 of at most :data:`MAX_STREAM_BYTES` bytes."""


class ExecRequest(BaseModel):
    """One command to run on a machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: Annotated[
        str,
        Field(min_length=1, max_length=65_536, pattern=r"^[^\x00]+$"),
    ]
    """The command line, which the host gives to ``bash -lc``."""

    cwd: RunFolder
    """The folder it starts in, absolute or ``~``-relative."""

    timeout_sec: Annotated[int, Field(ge=1, le=600)]
    """Seconds the host lets it run before it kills the process group."""

    stdin_b64: Base64 | None = None
    """What the command reads on standard input."""

    env: (
        Annotated[
            dict[
                Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")],
                Annotated[str, Field(max_length=8_192, pattern=r"^[^\x00]*$")],
            ],
            Field(max_length=MAX_ENV_VARS),
        ]
        | None
    ) = None
    """Variables added to the host's environment for this command."""


class ExecWait(ExecRequest):
    """A queued command, as the host receives it."""

    exec_id: uuid.UUID
    """The id the host reports the result under."""

    asker: str
    """The person the command runs for, as the server claims it; the host logs it."""


class ExecResult(BaseModel):
    """What running a command produced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exit_code: Annotated[int, Field(ge=-255, le=255)]
    """The command's exit status, or :data:`TIMEOUT_EXIT_CODE` for a timeout."""

    stdout_b64: Base64
    stderr_b64: Base64

    cwd: RunFolder
    """The folder the command ended in, so the next one can start there."""

    truncated: bool
    """Whether the host cut either stream to fit :data:`MAX_STREAM_BYTES`."""

    duration_ms: Annotated[int, Field(ge=0)]
