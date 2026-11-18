"""Where a secret variable's value lives, behind one small interface.

The ``variables`` table holds a secret's name and who set it, never its value;
a :class:`SecretBackend` holds the value. The backend is synchronous, and
routes call it through ``asyncio.to_thread``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, get_args
from urllib.parse import quote

import os
import re
import stat
import uuid

from trackinizer.lib.userdirs import data_dir
from trackinizer.server.config import ConfigError
from trackinizer.wire.wire_variables import NAME_PATTERN, Layer


__all__ = [
    "FileSecrets",
    "SecretBackend",
    "SecretNotFoundError",
    "SecretRef",
    "SecretSchemes",
    "parse_secrets",
    "validate_ref",
]


_MAX_OWNER_PART_LENGTH: Final = 200
"""Longest encoded owner directory name; file systems stop at 255 bytes."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SecretRef:
    """The address of one secret value."""

    layer: Layer
    owner: str
    name: str


class SecretNotFoundError(Exception):
    """The backend holds no value for the requested secret."""


class SecretBackend(Protocol):
    """Stores secret values by :class:`SecretRef`."""

    def get(self, ref: SecretRef) -> str:
        """Return the value; raise :class:`SecretNotFoundError` when absent."""
        ...

    def put(self, ref: SecretRef, value: str) -> None:
        """Store the value, replacing any earlier one."""
        ...

    def delete(self, ref: SecretRef) -> None:
        """Remove the value; an absent one is not an error."""
        ...


SecretSchemes = Mapping[str, Callable[[str], SecretBackend]]
"""Backends beyond the built-in forms: spec scheme to a factory of the spec's rest."""


def validate_ref(ref: SecretRef) -> None:
    """Refuse a layer or name that no backend may turn into an address.

    Args:
      ref: The secret to check.

    Raises:
      ValueError: The layer is not a known layer or the name is not a valid name.

    """
    if ref.layer not in get_args(Layer):
        raise ValueError(f"unknown secret layer {ref.layer!r}")
    if re.fullmatch(NAME_PATTERN, ref.name) is None:
        raise ValueError(f"invalid secret name {ref.name!r}")


class FileSecrets:
    """Secret values as files under one root: ``<root>/<layer>/<owner>/<name>``.

    Files are mode 0o640 and directories 0o750, so nothing is readable or
    writable by others. Not 0o600: the server writes, and the campaign
    launcher, running under another account on the same box, reads. The root
    directory's group is that read grant -- an operator sets it, with the
    setgid bit so every directory below inherits it -- the way a cloud secret
    store grants read to one role and write to another.

    The owner part is ``_`` for the empty owner and the percent-encoded owner
    otherwise, at most 200 characters; names and layers are validated before
    any path is built, so no input names a file outside the root. A symlink
    under the root is never followed out of it: a directory that resolves
    elsewhere is refused and a leaf symlink is not read. A write closes an
    existing root to others.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def get(self, ref: SecretRef) -> str:
        """Return the stored value.

        Args:
          ref: The secret to read.

        Returns:
          value: The text last stored for ``ref``.

        Raises:
          SecretNotFoundError: Nothing is stored for ``ref``.

        """
        path = self._path(ref)
        try:
            # A symlink in the leaf's place is refused (ELOOP), not followed.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise SecretNotFoundError(ref.name) from None
        with os.fdopen(fd, "rb") as handle:
            return handle.read().decode()

    def put(self, ref: SecretRef, value: str) -> None:
        """Store the value atomically: a reader sees the old file or the new one.

        Args:
          ref: The secret to write.
          value: The text to store.

        """
        path = self._path(ref)
        _close_to_others(self._root)
        _make_private_dirs(path.parent)
        temp = path.with_name(f".tmp-{uuid.uuid4().hex}")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            with os.fdopen(fd, "wb") as handle:
                # ``mode`` above is masked by the umask; fchmod is not.
                os.fchmod(fd, 0o640)
                _ = handle.write(value.encode())
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def delete(self, ref: SecretRef) -> None:
        """Remove the stored value, if any."""
        self._path(ref).unlink(missing_ok=True)

    def _path(self, ref: SecretRef) -> Path:
        validate_ref(ref)
        owner_part = _owner_part(ref.owner)
        if len(owner_part) > _MAX_OWNER_PART_LENGTH:
            raise ValueError(f"secret owner {ref.owner!r} is too long")
        path = self._root / ref.layer / owner_part / ref.name
        # A symlinked directory under the root would send the file elsewhere.
        if not path.parent.resolve().is_relative_to(self._root.resolve()):
            raise ValueError(f"secret path {path} is outside the secrets root")
        return path


def parse_secrets(
    spec: str,
    *,
    schemes: SecretSchemes = MappingProxyType({}),
) -> SecretBackend | None:
    """Build the backend a ``TRACKINIZER_SECRETS`` value names.

    Args:
      spec: ``file`` (under the user data directory), ``file:/abs/path``,
        ``none`` (secret variables are refused), or ``<scheme>:<rest>`` for a
        scheme in ``schemes``.
      schemes: Further backends: a scheme name maps to a factory that takes the
        text after the first colon of ``spec``. A scheme never shadows the
        built-in forms.

    Returns:
      backend: The backend, or ``None`` for ``none``.

    Raises:
      ConfigError: ``spec`` is none of the accepted forms.

    """
    if spec == "file":
        return FileSecrets(data_dir() / "rekursiv-ai" / "trackinizer" / "secrets")
    if spec == "none":
        return None
    if (
        spec.startswith("file:")
        and (root := Path(spec.removeprefix("file:"))).is_absolute()
    ):
        return FileSecrets(root)
    scheme, colon, rest = spec.partition(":")
    if colon and scheme != "file" and scheme in schemes:
        return schemes[scheme](rest)
    forms = ["'file'", "'file:/abs/path'", *(f"'{name}:<value>'" for name in schemes)]
    raise ConfigError(
        f"unknown secrets backend {spec!r}; use {', '.join(forms)} or 'none'",
    )


def _owner_part(owner: str) -> str:
    """Encode an owner as one directory name that is never ``.`` or ``..``."""
    if not owner:
        return "_"
    # ``_`` is unreserved to ``quote`` and would collide with the empty owner;
    # a dot-only name would name the directory itself or its parent.
    if owner == "_" or set(owner) == {"."}:
        return "".join(f"%{ord(char):02X}" for char in owner)
    return quote(owner, safe="")


def _close_to_others(root: Path) -> None:
    """Drop every permission an existing root grants to others."""
    try:
        mode = stat.S_IMODE(root.stat().st_mode)
    except FileNotFoundError:
        return
    if mode & 0o007:
        root.chmod(mode & ~0o007)


def _make_private_dirs(path: Path) -> None:
    """Create ``path`` and any missing parents as 0o750, whatever the umask."""
    missing: list[Path] = []
    directory = path
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o750)
        # A numeric chmod clears setgid, which a group-owned root passes down so
        # the launcher's group can read what is created below it.
        setgid = directory.parent.stat().st_mode & stat.S_ISGID
        directory.chmod(0o750 | setgid)
