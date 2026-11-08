"""What a published machine host still imports of the old exec channel, for one release.

That host allows any newer version of this package, so without this module it crashes on
start after an upgrade. It holds a ``GET`` on :data:`MACHINE_EXEC_WAIT_PATH`, which
answers ``null``, so it never runs a command. Tools reach a machine over a pipe now
(``wire_tools``). This module goes once a host without it is out, but for
:data:`MAX_STREAM_BYTES` and :data:`Base64`, which ``wire_machine_job`` reads.

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
    "MACHINE_EXEC_RESULT_PATH",
    "MACHINE_EXEC_WAIT_PATH",
    "MAX_ENV_VARS",
    "MAX_STREAM_BYTES",
    "TIMEOUT_EXIT_CODE",
    "WAIT_MAX_SEC",
    "ExecRequest",
    "ExecResult",
    "ExecWait",
]

MACHINE_EXEC_WAIT_PATH: Final = "/api/machines/{machine_id}/exec/wait"
"""``GET ?instance=ID``, held up to :data:`WAIT_MAX_SEC`: always ``null`` now."""

MACHINE_EXEC_RESULT_PATH: Final = "/api/machines/{machine_id}/exec/{exec_id}/result"
"""``POST``: no route answers this now, and no host is handed a command to report."""

WAIT_MAX_SEC: Final = 25
"""The longest the server holds a host's wait."""

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
