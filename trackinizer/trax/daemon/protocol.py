"""Length-prefixed JSON framing for the traxd Unix socket.

STDLIB ONLY. The thin client imports this before it knows whether a daemon
is running, so a package-internal import here would cost the ~190ms the
daemon exists to remove -- on every invocation, daemon or not.
``protocol_test`` asserts that behaviorally.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self, cast

import hashlib
import json
import os
import socket


PROTOCOL_VERSION: Final = 1
"""Bumped when the frame payload's shape changes incompatibly."""

_LENGTH_BYTES: Final = 4
"""Width of the big-endian length prefix on every frame."""

_MAX_FRAME_BYTES: Final = 64 * 1024 * 1024
"""Cap on one frame. A listing of every issue runs to a few MB; 64MB leaves
room while keeping a corrupt or hostile length prefix from sizing a buffer
that exhausts memory before the read fails."""

# The environment a verb reads. Shipped from the caller and applied around
# the request, so the daemon resolves the INVOKING user's identity and
# profile rather than whichever shell happened to spawn it:
# ``USER``/``USERNAME`` back ``resolve_actor``; ``TRACKINIZER_PROFILE`` and
# ``TRACKINIZER_URL`` back the connection chain in ``cli.connect``.
FORWARDED_ENV: Final[tuple[str, ...]] = (
    "USER",
    "USERNAME",
    "TRACKINIZER_PROFILE",
    "TRACKINIZER_URL",
)


@dataclass(frozen=True, kw_only=True, slots=True)
class Request:
    """One CLI invocation, with the ambient state the daemon cannot observe."""

    argv: tuple[str, ...]
    cwd: str
    """The caller's directory; ``field to @relative/path`` resolves against it."""
    env: dict[str, str]
    isatty: bool
    """Whether the CALLER's stdout is a terminal. The daemon's is a socket, so
    it cannot answer this for itself and would size every table as if piped."""
    columns: int
    stdin: str
    protocol_version: int
    source_version: str

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "argv": list(self.argv),
                "cwd": self.cwd,
                "env": self.env,
                "isatty": self.isatty,
                "columns": self.columns,
                "stdin": self.stdin,
                "protocol_version": self.protocol_version,
                "source_version": self.source_version,
            },
            separators=(",", ":"),
        ).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        payload = _decode_object(raw, "request")
        version = payload.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise ValueError(
                f"unsupported protocol version {version!r}; expected {PROTOCOL_VERSION}"
            )
        return cls(
            argv=tuple(_str_list(payload.get("argv"))),
            cwd=_str(payload.get("cwd")),
            env=_str_map(payload.get("env")),
            isatty=bool(payload.get("isatty")),
            columns=_int(payload.get("columns")),
            stdin=_str(payload.get("stdin")),
            protocol_version=PROTOCOL_VERSION,
            source_version=_str(payload.get("source_version")),
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class Response:
    """What the CLI prints and exits with."""

    stdout: str
    stderr: str
    exit_code: int

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "stdout": self.stdout,
                "stderr": self.stderr,
                "exit_code": self.exit_code,
            },
            separators=(",", ":"),
        ).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        payload = _decode_object(raw, "response")
        return cls(
            stdout=_str(payload.get("stdout")),
            stderr=_str(payload.get("stderr")),
            exit_code=_int(payload.get("exit_code")),
        )


def write_frame(conn: socket.socket, payload: bytes) -> None:
    """Send one length-prefixed frame."""
    conn.sendall(len(payload).to_bytes(_LENGTH_BYTES, "big") + payload)


def read_frame(conn: socket.socket) -> bytes:
    """Read exactly one length-prefixed frame.

    Raises:
      ConnectionError: The peer closed before the frame completed.
      ValueError: The length prefix exceeds :data:`_MAX_FRAME_BYTES`.

    """
    size = int.from_bytes(_read_exactly(conn, _LENGTH_BYTES), "big")
    if size > _MAX_FRAME_BYTES:
        raise ValueError(f"frame too large: {size} bytes")
    return _read_exactly(conn, size)


def socket_path() -> Path:
    """Path of this user's daemon socket.

    Session state, so it lives under the state dir rather than config (not
    user-edited) or scratch (not bulk data). The XDG layout is spelled out
    here rather than taken from the shared ``userdirs`` helper because this
    module must stay import-free; it resolves to the same directory.
    """
    if env := os.environ.get("XDG_STATE_HOME"):
        base = Path(env)
    else:
        base = Path.home() / ".local" / "state"
    return base / "rekursiv-ai" / "traxd" / "traxd.sock"


def source_version(root: Path) -> str:
    """Fingerprint the CLI's source tree, for stale-daemon detection.

    A daemon outliving an edit to ``verbs.py`` would keep serving the old
    behavior, which looks like correct output and is the sharpest failure
    mode this design introduces. Hashing every ``.py`` path plus its size and
    mtime catches an edit without reading file contents (a few hundred
    ``stat`` calls, sub-millisecond) and without importing anything.
    """
    digest = hashlib.blake2b(digest_size=16)
    for path in sorted(root.rglob("*.py")):
        try:
            info = path.stat()
        except OSError:
            continue
        digest.update(str(path).encode())
        digest.update(str(info.st_size).encode())
        digest.update(str(info.st_mtime_ns).encode())
    return digest.hexdigest()


def _decode_object(raw: bytes, where: str) -> dict[str, object]:
    """Decode one frame into a JSON object, or raise ``ValueError``.

    The shared typed-JSON extractor is the house tool for this, but it is a
    package-internal import and this module must stay import-free, so the
    narrowing is spelled out here instead. A non-object frame raises
    ``ValueError`` rather than ``TypeError``: every malformed-frame failure
    is one thing to the caller -- a peer that sent garbage.
    """
    try:
        payload: object = json.loads(raw)
    except ValueError as err:
        raise ValueError(f"malformed {where} frame: {err}") from err
    if not isinstance(payload, dict):
        raise ValueError(f"malformed {where} frame: {payload!r}")  # noqa: TRY004 -- a non-object frame is malformed input, not a caller type error.
    return cast("dict[str, object]", payload)


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast("list[object]", value) if isinstance(item, str)]


def _str_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in cast("dict[object, object]", value).items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _read_exactly(conn: socket.socket, size: int) -> bytes:
    """Read ``size`` bytes, or raise if the peer closes first.

    ``recv`` returns what is available, not what was asked for: a 167KB
    listing arrives in many segments, so a single ``recv`` would silently
    truncate the frame and the CLI would print a partial table.
    """
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = conn.recv(min(remaining, 1 << 20))
        if not chunk:
            raise ConnectionError(
                f"connection closed with {remaining} of {size} bytes unread"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
