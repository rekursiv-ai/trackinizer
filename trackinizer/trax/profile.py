"""The saved-profile store, independent of profile commands and device login."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import os
import re
import shlex
import tempfile

from trackinizer.client.client import server_url
from trackinizer.client.errors import ClientError
from trackinizer.lib.userdirs import config_dir
from trackinizer.trax.client_cache import close_clients
from trackinizer.trax.context import env, trax_env


if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True, kw_only=True, slots=True)
class Profile:
    """A saved server identity: URL, audit actor, and bearer token."""

    url: str
    author: str = ""
    api_key: str = field(default="", repr=False)
    """Sent as ``Authorization: Bearer <api_key>``."""


LOCALHOST_FALLBACK_URL: Final = "http://127.0.0.1:8765"


def load_profile() -> Profile:
    """Return the profile for the active name, or the localhost fallback if none is.

    pinned.

    Returns:
      profile: Current trax Profile with URL and API key, falling back to
        localhost when no profile is explicitly set.

    """
    name = current_profile()
    if (config_dir() / "rekursiv-ai" / "trax" / "profiles" / name).exists():
        return read_profile(name)
    if _explicit_profile() is not None:
        raise ClientError(f"profile {name!r} not found")
    server = terminal_server()
    if server is not None and (named := profile_for_server(server)) is not None:
        return read_profile(named)
    return Profile(url=LOCALHOST_FALLBACK_URL)


def terminal_server() -> str | None:
    """Return the server a shell from a trax terminal names, if it names a sound one.

    The variable travels through ``ssh`` and so can be set by whoever controls the
    far end. Only an ``http`` or ``https`` URL of printable characters, with no
    whitespace and no credentials, is believed.

    Returns:
      server: The URL, or ``None`` when unset or not sound.

    """
    server = trax_env("SERVER") or ""
    if re.fullmatch(r"[!-~]+", server) is None:
        return None
    try:
        parts = urlsplit(server)
    except ValueError:
        return None
    if (
        parts.scheme not in ("http", "https")
        or parts.netloc == ""
        or "@" in parts.netloc
    ):
        return None
    return server


def profile_for_server(server: str) -> str | None:
    """Return the name of the saved profile whose URL is ``server``, or ``None``.

    Args:
      server: A server URL, compared without a trailing slash.

    Returns:
      name: The first such profile by name.

    """
    wanted = server.rstrip("/")
    return next(
        (
            name
            for name, profile in list_profiles()
            if profile.url.rstrip("/") == wanted
        ),
        None,
    )


def unmatched_server_hint() -> str | None:
    """Return what to run when a terminal's server has no saved profile, else ``None``.

    A shell that came from a trax terminal names its server (``LC_TRAX_SERVER``). When
    nothing selects a profile and no saved profile has that URL, trax would report to
    localhost, so it says how to log in instead.

    Returns:
      hint: The message, or ``None`` when there is nothing to say.

    """
    server = terminal_server()
    if (
        server is None
        or _explicit_profile() is not None
        or (env("TRACKINIZER_URL") or "") != ""
        or profile_for_server(server) is not None
    ):
        return None
    quoted = shlex.quote(server)
    return (
        f"trax: this shell came from {quoted}, which has no saved profile; to use it: "
        f"trax profile remote url to {quoted}, then add a token from Settings.\n"
    )


def current_profile() -> str:
    """Name of the active profile, defaulting to ``default``.

    Returns:
      result: The str.

    """
    return _explicit_profile() or "default"


def has_usable_profile() -> bool:
    """Return whether trax already knows which profile to use.

    Returns:
      usable: True when a profile is pinned (``$TRACKINIZER_PROFILE`` or the
        ``current`` file) or a ``default`` profile is saved.

    """
    return (
        _explicit_profile() is not None
        or (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "default").exists()
    )


def save_profile(name: str, profile: Profile) -> None:
    """Persist ``profile`` under ``name`` atomically.

    The URL is validated up front so bad input fails on write instead of
    on the next read. The file is created ``0o600`` before any bytes land,
    so the token never has a world-readable window.

    Args:
      name: Profile name to save under.
      profile: Profile object with URL, author, and optional API key.

    """
    validate_profile_name(name)
    url = server_url(profile.url, f"profile {name!r}")
    lines = [f"url={url}"]
    if profile.author:
        lines.append(f"author={profile.author}")
    if profile.api_key:
        lines.append(f"api_key={profile.api_key}")
    _write_atomic(
        config_dir() / "rekursiv-ai" / "trax" / "profiles" / name,
        "\n".join(lines) + "\n",
        mode=0o600,
    )
    # A long-lived process (the daemon) caches clients keyed on the resolved
    # url/author/token. This profile just changed one of them, so a cached
    # client would keep talking to the old server under the old credential.
    close_clients()


def switch_profile(name: str) -> None:
    """Pin ``name`` as the active profile for future invocations.

    Args:
      name: Name.

    """
    validate_profile_name(name)
    _write_atomic(
        config_dir() / "rekursiv-ai" / "trax" / "current",
        name + "\n",
        mode=0o600,
    )


def list_profiles() -> list[tuple[str, Profile]]:
    """Every saved ``(name, profile)`` pair, sorted by name.

    Returns:
      result: The list[tuple[str, Profile]].

    """
    if not (config_dir() / "rekursiv-ai" / "trax" / "profiles").exists():
        return []
    return sorted(_iter_profiles())


def del_profile(name: str) -> bool:
    """Delete profile ``name``; return whether it existed.

    Args:
      name: Profile name to delete.

    Returns:
      deleted: True if the profile existed and was removed; False if not found.

    """
    validate_profile_name(name)
    if name == _explicit_profile():
        raise ClientError(f"cannot delete active profile {name!r}; switch first")
    path = config_dir() / "rekursiv-ai" / "trax" / "profiles" / name
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def read_profile(name: str) -> Profile:
    """Read the saved profile for ``name``.

    A missing or unparseable ``url`` line is a hard error; substituting
    localhost would mask typos and torn writes.

    Args:
      name: Profile name to load.

    Returns:
      profile: Parsed Profile with validated URL and optional API key.

    Raises:
      ClientError: If the file is absent or has no ``url=`` line.

    """
    validate_profile_name(name)
    path = config_dir() / "rekursiv-ai" / "trax" / "profiles" / name
    try:
        text = path.read_text()
    except FileNotFoundError as err:
        raise ClientError(f"profile {name!r} not found") from err
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    if "url" not in fields:
        raise ClientError(f"profile {name!r} has no url= line")
    url = server_url(fields["url"], f"profile {name!r}")
    return Profile(
        url=url,
        author=fields.get("author", ""),
        api_key=fields.get("api_key", ""),
    )


# A name is a single path segment, so anything outside ``[A-Za-z0-9._-]`` (path
# separators, ``..`` traversal) is refused before it reaches ``config_dir() / "rekursiv-
# ai" / "trax" / "profiles" / name``. ``.`` and ``..`` match the character class but are
# still directory references, so they are rejected explicitly.
def validate_profile_name(name: str) -> None:
    """Reject a profile name that could escape the profiles directory.

    Args:
      name: A profile name, as typed or derived.

    Raises:
      ClientError: The name is empty, a directory reference, or holds a character
        outside ``[A-Za-z0-9._-]``.

    """
    if name in {".", ".."} or not re.match(r"\A[A-Za-z0-9._-]+\Z", name):
        raise ClientError(f"invalid profile name {name!r}")


# A single malformed profile must not block bare ``trax profile`` (the listing), so
# callers see the survivors and find out about the bad one on its next read.
def _iter_profiles() -> Iterator[tuple[str, Profile]]:
    """Yield ``(name, profile)`` for each saved profile, skipping unreadable ones."""
    for path in (config_dir() / "rekursiv-ai" / "trax" / "profiles").iterdir():
        if not path.is_file():
            continue
        try:
            yield path.name, read_profile(path.name)
        except ClientError:
            continue


# Writes a temp file in the same directory (chmod'd to ``mode`` before any content
# lands), fsyncs it, then renames it over the destination. The rename is atomic on POSIX
# within one filesystem, so an observer sees either the old file or the new one, never a
# half-written one.
def _write_atomic(path: Path, content: str, *, mode: int) -> None:
    """Atomically write ``content`` to ``path`` with ``mode`` permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp file per write (not a fixed ``.{name}.tmp``): two concurrent
    # writers of the same profile must not share -- and clobber -- one temp file
    # before either renames (F27). ``NamedTemporaryFile`` mints a distinct file
    # in the destination directory, so the rename stays atomic within the
    # filesystem. ``delete=False`` keeps it for the rename; ``chmod`` sets the
    # final mode before any content lands, so the token never has a window wider
    # than ``mode``.
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as fh:
        tmp = Path(fh.name)
        try:
            tmp.chmod(mode)
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _explicit_profile() -> str | None:
    """Profile name pinned by ``$TRACKINIZER_PROFILE`` or the ``current`` file, if any."""
    if pinned := env("TRACKINIZER_PROFILE") or "":
        return pinned
    try:
        text = (config_dir() / "rekursiv-ai" / "trax" / "current").read_text().strip()
    except FileNotFoundError:
        return None
    return text or None
