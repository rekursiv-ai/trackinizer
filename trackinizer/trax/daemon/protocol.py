"""Length-prefixed JSON framing for the traxd Unix socket.

The thin client imports this before it knows whether a daemon is running, so
it must not reach the CLI's import graph: pulling in ``client.client`` here
would cost the ~190ms the daemon exists to remove, on every invocation,
daemon or not. ``protocol_test`` asserts that behaviorally. The shared
``userdirs`` helper is the one internal import allowed -- it is stdlib-only
and measured at 0.2ms.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Self, cast, override

import hashlib
import json
import os
import socket
import stat

from trackinizer.lib.userdirs import config_dir, state_dir


PROTOCOL_VERSION: Final = 1
"""Bumped when the frame payload's shape changes incompatibly."""

_LENGTH_BYTES: Final = 4
"""Width of the big-endian length prefix on every frame."""

_MAX_FRAME_BYTES: Final = 64 * 1024 * 1024
"""Cap on one frame. A listing of every issue runs to a few MB; 64MB leaves
room while keeping a corrupt or hostile length prefix from sizing a buffer
that exhausts memory before the read fails."""

_UNIX_SOCKET_PATH_MAX_BYTES: Final = 103
"""Portable pathname payload for ``sockaddr_un.sun_path``.

Darwin and the BSDs provide 104 bytes including the trailing NUL. Linux is
slightly larger, but using its limit would leave the same path broken on a
developer's Mac.
"""

# The environment a verb reads. Shipped from the caller and bound around the
# request, so the daemon resolves the INVOKING user's identity and profile
# rather than whichever shell happened to spawn it: ``USER``/``USERNAME``
# back ``resolve_actor``; ``TRACKINIZER_PROFILE`` and ``TRACKINIZER_URL``
# back the connection chain in ``cli.connect``.
#
# The XDG variables are deliberately ABSENT; these should only be accessed
# through userdirs.py.
FORWARDED_ENV: Final[tuple[str, ...]] = (
    "USER",
    "USERNAME",
    "TRACKINIZER_PROFILE",
    "TRACKINIZER_URL",
)


class Request:
    """One CLI invocation, with the ambient state the daemon cannot observe.

    Hand-written rather than a ``@dataclass`` for the same reason
    :func:`_decode_object` narrows by hand: ``dataclasses`` costs 8.4ms to
    import (it pulls ``inspect`` -> ``ast`` + ``dis``), which every delegating
    invocation would pay to generate an ``__init__`` and ``__eq__`` this
    module could spell out in ten lines. That is 12% of a 70ms ``trax``.

    Attributes:
      argv: The CLI arguments, program name already stripped.
      cwd: The caller's directory; ``field to @relative/path`` resolves
        against it.
      env: The forwarded subset of the caller's environment.
      isatty: Whether the CALLER's stdout is a terminal. The daemon's is a
        socket, so it cannot answer this for itself and would size every
        table as if piped.
      columns: Terminal width, or 0 when not a terminal.
      protocol_version: Frame-shape version; a mismatch is answered, not
        dropped.
      source_version: Fingerprint of the caller's source tree.

    """

    __slots__ = (
        "argv",
        "columns",
        "cwd",
        "env",
        "isatty",
        "protocol_version",
        "source_version",
    )

    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        cwd: str,
        env: dict[str, str],
        isatty: bool,
        columns: int,
        protocol_version: int,
        source_version: str,
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.isatty = isatty
        self.columns = columns
        self.protocol_version = protocol_version
        self.source_version = source_version

    @override
    def __eq__(self, other: object) -> bool:
        """Field-wise equality; the framing round-trip tests assert on it."""
        if not isinstance(other, Request):
            return NotImplemented
        return all(
            getattr(self, name) == getattr(other, name) for name in Request.__slots__
        )

    @override
    def __hash__(self) -> int:
        """Hash the immutable fields, skipping the unhashable ``env`` dict."""
        return hash((self.argv, self.cwd, self.source_version))

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "argv": list(self.argv),
                "cwd": self.cwd,
                "env": self.env,
                "isatty": self.isatty,
                "columns": self.columns,
                "protocol_version": self.protocol_version,
                "source_version": self.source_version,
            },
            separators=(",", ":"),
        ).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        """Parse one request frame.

        Raises:
          ProtocolVersionError: The peer speaks a different frame shape. A
            distinct type so the daemon can answer it rather than dropping
            the connection, which would look like "no daemon" to the client.
          ValueError: The frame is not a well-formed request.

        """
        payload = _decode_object(raw, "request")
        version = payload.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise ProtocolVersionError(
                f"unsupported protocol version {version!r}; expected {PROTOCOL_VERSION}"
            )
        return cls(
            argv=tuple(_str_list(_require(payload, "argv", "request"))),
            cwd=_str(_require(payload, "cwd", "request")),
            env=_str_map(_require(payload, "env", "request")),
            isatty=bool(payload.get("isatty")),
            columns=_int(_require(payload, "columns", "request")),
            protocol_version=PROTOCOL_VERSION,
            source_version=_str(_require(payload, "source_version", "request")),
        )


class Response:
    """What the CLI prints and exits with.

    Hand-written for the reason given on :class:`Request`.
    """

    __slots__ = ("exit_code", "stderr", "stdout")

    def __init__(self, *, stdout: str, stderr: str, exit_code: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code

    @override
    def __eq__(self, other: object) -> bool:
        """Field-wise equality; the framing round-trip tests assert on it."""
        if not isinstance(other, Response):
            return NotImplemented
        return (
            self.stdout == other.stdout
            and self.stderr == other.stderr
            and self.exit_code == other.exit_code
        )

    @override
    def __hash__(self) -> int:
        return hash((self.stdout, self.stderr, self.exit_code))

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
        """Parse one response frame.

        Every field is required. Defaulting a missing ``exit_code`` to 0 would
        turn a truncated or corrupt frame into a reported SUCCESS, which is
        the worst failure this protocol can produce: a script branching on the
        exit status proceeds as though the command worked.

        Raises:
          ValueError: The frame is not a well-formed response.

        """
        payload = _decode_object(raw, "response")
        return cls(
            stdout=_str(_require(payload, "stdout", "response")),
            stderr=_str(_require(payload, "stderr", "response")),
            exit_code=_int(_require(payload, "exit_code", "response")),
        )


class ProtocolVersionError(ValueError):
    """A peer framed its request under a different protocol version."""


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

    The logical location is under the state dir rather than config (not
    user-edited) or scratch (not bulk data). If that path exceeds AF_UNIX's
    kernel limit, :func:`socket_address` maps it into a short runtime location.

    The filename carries a digest of the CONFIG directory because the daemon
    resolves the profile store from its own environment. Keying the socket on
    that directory means such a caller connects to -- or spawns -- a daemon
    that reads the same store, since the daemon inherits the environment of
    whoever spawned it.
    """
    digest = hashlib.blake2b(str(config_dir()).encode(), digest_size=8).hexdigest()
    logical_path = state_dir() / "rekursiv-ai" / "traxd" / f"{digest}.sock"
    return socket_address(logical_path)


def socket_address(logical_path: Path) -> Path:
    """Return a bindable AF_UNIX address for a logical state path.

    Long user state roots exceed ``sockaddr_un.sun_path`` on macOS even though
    the filesystem accepts them. A stable alias in an owner-only runtime
    directory points to the logical parent, shortening the kernel address while
    keeping the socket inode in the state directory selected through
    :mod:`userdirs`.

    Args:
      logical_path: Desired socket location in per-user state.

    Returns:
      path: Direct path when it fits, otherwise a stable short alias.

    Raises:
      OSError: The temporary root cannot safely hold a short alias.

    """
    if len(os.fsencode(logical_path)) <= _UNIX_SOCKET_PATH_MAX_BYTES:
        return logical_path
    return _aliased_socket_path(logical_path)


def package_root() -> Path:
    """Root of the distribution whose behavior the daemon serves.

    The whole package, not the ``trax`` subpackage inside it: a daemon holds
    ``client`` and ``wire`` resident too, so a fingerprint scoped to the CLI
    alone would keep serving stale behavior after an edit to the HTTP client
    or a wire contract -- output that looks correct and is not.
    """
    return Path(__file__).resolve().parents[2]


def source_version(root: Path) -> str:
    """Fingerprint a source tree, for stale-daemon detection.

    A daemon outliving an edit keeps serving the old behavior, which looks
    like correct output and is the sharpest failure mode this design
    introduces. Hashing every ``.py`` path plus its size and mtime catches an
    edit without reading file contents (a few hundred ``stat`` calls,
    sub-millisecond) and without importing anything.
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


def _aliased_socket_path(logical_path: Path) -> Path:
    """Create a short, secure alias to a long socket parent directory."""
    logical_parent = logical_path.parent.absolute()
    logical_parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Imported only for the exceptional long-path case. Importing tempfile on
    # every thin-client invocation would consume part of the latency the daemon
    # exists to remove.
    import tempfile  # noqa: PLC0415

    alias_root = Path(tempfile.gettempdir()) / f"t-{os.getuid():x}"
    alias_root.mkdir(mode=0o700, exist_ok=True)
    root_info = alias_root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or root_info.st_mode & 0o077
    ):
        raise PermissionError(f"unsafe traxd socket alias root: {alias_root}")

    alias_name = hashlib.blake2b(os.fsencode(logical_parent), digest_size=8).hexdigest()
    alias_parent = alias_root / alias_name
    try:
        alias_parent.symlink_to(logical_parent, target_is_directory=True)
    except FileExistsError:
        if (
            not alias_parent.is_symlink()
            or alias_parent.resolve() != logical_parent.resolve()
        ):
            raise OSError(f"unsafe traxd socket alias: {alias_parent}") from None

    address = alias_parent / logical_path.name
    if len(os.fsencode(address)) > _UNIX_SOCKET_PATH_MAX_BYTES:
        raise OSError(f"temporary directory is too long for AF_UNIX: {address}")
    return address


def _decode_object(raw: bytes, where: str) -> dict[str, object]:
    """Decode one frame into a JSON object, or raise ``ValueError``.

    The shared typed-JSON extractor is the house tool for this, but it pulls
    in the import graph this module exists to avoid, so the narrowing is
    spelled out here instead. A non-object frame raises ``ValueError`` rather
    than ``TypeError``: every malformed-frame failure is one thing to the
    caller -- a peer that sent garbage.
    """
    try:
        payload: object = json.loads(raw)
    except ValueError as err:
        raise ValueError(f"malformed {where} frame: {err}") from err
    if not isinstance(payload, dict):
        raise ValueError(f"malformed {where} frame: {payload!r}")  # noqa: TRY004 -- a non-object frame is malformed input, not a caller type error.
    return cast(dict[str, object], payload)


def _require(payload: dict[str, object], key: str, where: str) -> object:
    """Return ``payload[key]``, or raise if the peer omitted it."""
    if key not in payload:
        raise ValueError(f"malformed {where} frame: missing {key!r}")
    return payload[key]


# Every helper below narrows one JSON value decoded from a REMOTE peer, so a
# wrong type is malformed input rather than a caller passing the wrong
# argument. ``ValueError`` keeps every bad-frame failure one type the socket
# layer can catch; a ``TypeError`` here would escape that handler and kill
# the connection with a traceback instead of a diagnostic.
def _str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {value!r}")  # noqa: TRY004 -- malformed wire input, not a caller type error.
    return value


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"expected an integer, got {value!r}")  # noqa: TRY004 -- malformed wire input, not a caller type error.
    return value


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"expected a list, got {value!r}")  # noqa: TRY004 -- malformed wire input, not a caller type error.
    return [_str(item) for item in cast(list[object], value)]


def _str_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"expected an object, got {value!r}")  # noqa: TRY004 -- malformed wire input, not a caller type error.
    return {
        _str(key): _str(item) for key, item in cast(dict[object, object], value).items()
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
