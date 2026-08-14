"""The ``trax profile`` command and the saved-profile store it reads and writes."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final, cast, override

import argparse
import os
import re
import tempfile

from trackinizer.client.client import Client, server_url
from trackinizer.client.errors import ClientError
from trackinizer.lib.userdirs import config_dir
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.render import echo


class Profiles(Command):
    """Show and mutate saved server profiles, subject-first like the rest of trax."""

    names = ("profile",)
    fields: ClassVar[tuple[str, ...]] = ("url", "actor", "token", "current")
    help = """\
Usage: trax profile [NAME] [ACTION]

Examples:
  trax profile                                     list all profiles (active *)
  trax profile foo                                 show profile foo
  trax profile url                                 show active profile URL
  trax profile url to https://trackinizer.example  set active profile URL
  trax profile token to trax__...                  set active token
  trax profile foo token to trax__...              set profile foo token
  trax profile current foo                         select profile foo
  trax profile foo del                             delete profile foo

Fields: url actor token
"""
    field_help: ClassVar[HelpPage] = HelpPage(
        usage="trax profile [NAME] FIELD [to VALUE]",
        summary="No VALUE projects the field; 'to VALUE' mutates it.",
        examples=("trax profile url", "trax profile foo token to trax__..."),
    )
    field_set_help: ClassVar[HelpPage] = HelpPage(
        usage="trax profile [NAME] FIELD to VALUE",
        summary="Mutates the selected profile field (one field per command).",
        examples=("trax profile token to trax__...",),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="trax profile",
            description="Show or mutate saved server profiles.",
        )
        parser.add_argument("rest", nargs="*", metavar="POS")
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb, client_factory
        tokens = cast(Sequence[str], args.rest)
        if not tokens:
            # Bare verb lists all profiles, like ``trax issue`` lists rows; the
            # active one is ``*``-marked. ``trax profile <name>`` shows detail.
            cls.run_list()
            return
        if tokens[-1] == "del":
            cls.run_del(tokens[:-1])
            return
        if tokens[0] == "current":
            cls.run_current(tokens[1:])
            return
        if tokens[0] in cls.fields:
            cls.run_field_or_set(current_profile(), tokens)
            return
        if len(tokens) == 1:
            cls.run_show(tokens[0])
            return
        cls.run_field_or_set(tokens[0], tokens[1:])

    @classmethod
    @override
    def help_with_context(cls, verb: str, prefix: list[str]) -> str:
        del verb
        return cls.help_for(prefix)

    @classmethod
    def help_for(cls, tokens: Sequence[str]) -> str:
        if not tokens:
            return cls.help_text()
        if tokens[0] in cls.fields:
            return cls._field_help_for(tokens[0], value=len(tokens) > 1)
        if len(tokens) == 1:
            return cls.help_text()
        if len(tokens) >= 2 and tokens[1] in cls.fields:
            prefix = f"{tokens[0]} {tokens[1]}"
            return cls._field_help_for(prefix, value=len(tokens) > 2)
        return cls.help_text()

    @classmethod
    def _field_help_for(cls, prefix: str, *, value: bool) -> str:
        page = cls.field_set_help if value else cls.field_help
        suffix = "to VALUE" if value else "[to VALUE]"
        return page.with_usage(f"trax profile {prefix} {suffix}").render()

    @classmethod
    def run_list(cls) -> None:
        rows = list_profiles()
        if not rows:
            echo("(no profiles)")
            return
        active = current_profile()
        width = max(len(name) for name, _ in rows)
        for name, profile in rows:
            marker = "*" if name == active else " "
            as_who = f"  (as {profile.author})" if profile.author else ""
            echo(f"{marker} {name.ljust(width)}  {profile.url}{as_who}")

    @classmethod
    def run_show(cls, name: str) -> None:
        profile = load_profile() if name == current_profile() else read_profile(name)
        echo(f"profile: {name}")
        echo(f"url:     {profile.url}")
        echo(f"actor:   {profile.author or '(none)'}")
        if profile.api_key:
            echo(f"token:   set (prefix {profile.api_key[:12]})")
        else:
            echo("token:   unset")

    @classmethod
    def run_field_or_set(cls, name: str, tokens: Sequence[str]) -> None:
        if not tokens:
            cls.run_show(name)
            return
        if len(tokens) == 1:
            cls.run_field(name, tokens[0])
            return
        # Set mirrors the row grammar exactly: ``field to value`` (GRAMMAR.md
        # §4/§8). One field per command, like a row scalar edit -- no bare
        # adjacency, no multi-pair.
        if len(tokens) != 3 or tokens[1] != "to":
            raise ClientError(
                f"expected '{tokens[0]} to VALUE'; profile set is 'field to value'"
            )
        cls.run_set(name, field=tokens[0], value=tokens[2])

    @classmethod
    def run_field(cls, name: str, field: str) -> None:
        if field not in cls.fields or field == "current":
            raise ClientError(f"unknown profile field {field!r}")
        profile = load_profile() if name == current_profile() else read_profile(name)
        if field == "url":
            echo(profile.url)
            return
        if field == "actor":
            echo(profile.author)
            return
        if field == "token":
            if profile.api_key:
                echo(f"set (prefix {profile.api_key[:12]})")
            else:
                echo("unset")
            return
        raise ClientError(f"unknown profile field {field!r}")

    @classmethod
    def run_set(cls, name: str, *, field: str, value: str) -> None:
        if field not in cls.fields or field == "current":
            raise ClientError(f"unknown profile field {field!r}")
        profile = cls._profile_for_write(name)
        save_profile(
            name,
            Profile(
                url=value if field == "url" else profile.url,
                author=value if field == "actor" else profile.author,
                api_key=value if field == "token" else profile.api_key,
            ),
        )
        echo(f"set: profile {name} {field}")

    @classmethod
    def run_current(cls, tokens: Sequence[str]) -> None:
        if len(tokens) != 1:
            raise ClientError("expected profile name after current")
        read_profile(tokens[0])
        switch_profile(tokens[0])
        echo(f"set: profile current {tokens[0]}")

    @classmethod
    def run_del(cls, tokens: Sequence[str]) -> None:
        if len(tokens) != 1:
            raise ClientError("expected profile name before del")
        if not del_profile(tokens[0]):
            raise ClientError(f"profile {tokens[0]!r} not found")
        echo(f"deleted: profile {tokens[0]}")

    @classmethod
    def _profile_for_write(cls, name: str) -> Profile:
        """The existing profile, or a localhost-URL template on first write.

        Before a profile exists, the first field-set bootstraps it; the URL
        defaults to the localhost fallback until the user sets one.
        """
        try:
            return read_profile(name)
        except ClientError:
            return Profile(url=LOCALHOST_FALLBACK_URL)


LOCALHOST_FALLBACK_URL: Final = "http://127.0.0.1:8765"


@dataclass(frozen=True, kw_only=True, slots=True)
class Profile:
    """A saved server identity: URL, audit actor, and bearer token."""

    url: str
    author: str = ""
    api_key: str = ""
    """Sent as ``Authorization: Bearer <api_key>``."""


def load_profile() -> Profile:
    """The profile for the active name, or the localhost fallback if none is pinned."""
    name = current_profile()
    if (config_dir() / "rekursiv-ai" / "trax" / "profiles" / name).exists():
        return read_profile(name)
    if _explicit_profile() is not None:
        raise ClientError(f"profile {name!r} not found")
    return Profile(url=LOCALHOST_FALLBACK_URL)


def current_profile() -> str:
    """Name of the active profile, defaulting to ``default``."""
    return _explicit_profile() or "default"


def save_profile(name: str, profile: Profile) -> None:
    """Persist ``profile`` under ``name`` atomically.

    The URL is validated up front so bad input fails on write instead of
    on the next read. The file is created ``0o600`` before any bytes land,
    so the token never has a world-readable window.
    """
    _validate_profile_name(name)
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


def switch_profile(name: str) -> None:
    """Pin ``name`` as the active profile for future invocations."""
    _validate_profile_name(name)
    _write_atomic(
        config_dir() / "rekursiv-ai" / "trax" / "current", name + "\n", mode=0o600
    )


def list_profiles() -> list[tuple[str, Profile]]:
    """Every saved ``(name, profile)`` pair, sorted by name."""
    if not (config_dir() / "rekursiv-ai" / "trax" / "profiles").exists():
        return []
    return sorted(_iter_profiles())


def del_profile(name: str) -> bool:
    """Delete profile ``name``; return whether it existed."""
    _validate_profile_name(name)
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

    Raises:
      ClientError: If the file is absent or has no ``url=`` line.

    """
    _validate_profile_name(name)
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


def _iter_profiles() -> Iterator[tuple[str, Profile]]:
    """Yield ``(name, profile)`` for each saved profile, skipping unreadable ones.

    A single malformed profile must not block bare ``trax profile`` (the
    listing), so callers see the survivors and find out about the bad one on
    its next read.
    """
    for path in (config_dir() / "rekursiv-ai" / "trax" / "profiles").iterdir():
        if not path.is_file():
            continue
        try:
            yield path.name, read_profile(path.name)
        except ClientError:
            continue


def _write_atomic(path: Path, content: str, *, mode: int) -> None:
    """Atomically write ``content`` to ``path`` with ``mode`` permissions.

    Writes a temp file in the same directory (chmod'd to ``mode`` before any
    content lands), fsyncs it, then renames it over the destination. The
    rename is atomic on POSIX within one filesystem, so an observer sees
    either the old file or the new one, never a half-written one.
    """
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


_PROFILE_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")


def _validate_profile_name(name: str) -> None:
    """Reject a profile name that could escape the profiles directory.

    A name is a single path segment, so anything outside ``[A-Za-z0-9._-]``
    (path separators, ``..`` traversal) is refused before it reaches
    ``config_dir() / "rekursiv-ai" / "trax" / "profiles" / name``. ``.`` and ``..`` match the character class but
    are still directory references, so they are rejected explicitly.
    """
    if name in {".", ".."} or not _PROFILE_NAME_RE.match(name):
        raise ClientError(f"invalid profile name {name!r}")


def _explicit_profile() -> str | None:
    """Profile name pinned by ``$TRACKINIZER_PROFILE`` or the ``current`` file, if any."""
    if env := os.environ.get("TRACKINIZER_PROFILE"):
        return env
    try:
        text = (config_dir() / "rekursiv-ai" / "trax" / "current").read_text().strip()
    except FileNotFoundError:
        return None
    return text or None
