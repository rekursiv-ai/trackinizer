"""Check trax setup and explicitly repair installer-owned authoring skills."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast, override

import argparse

from treekle import ReadError, from_plain

from trackinizer.client.errors import ClientError
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.context import cwd
from trackinizer.trax.install import (
    TARGETS,
    InstallError,
    check_skills,
    install,
    uninstall,
)
from trackinizer.trax.render import echo


if TYPE_CHECKING:
    from collections.abc import Callable

    from trackinizer.client.client import Client


class Doctor(Command):
    """Check skill discovery, profile configuration, and server access."""

    names = ("doctor",)
    help = HelpPage(
        usage=(
            "trax doctor [--target claude|codex|cursor] [--project] "
            "[--fix | --uninstall] [--offline]"
        ),
        summary="Check setup without changing it; repairs require --fix.",
        options=(
            ("--target AGENT", "Agent skill directory to inspect (default: claude)."),
            ("--project", "Use the caller's project instead of the user's home."),
            ("--fix", "Install missing skills or refresh unchanged managed copies."),
            ("--uninstall", "Remove owned skills and installer-created empty parents."),
            ("--offline", "Skip profile and server checks."),
        ),
        examples=(
            "trax doctor                                  check setup after an upgrade",
            "trax doctor --fix --target codex              install or refresh skills",
            "trax doctor --project --offline               check project skills locally",
            "trax doctor --uninstall --target cursor       remove managed Cursor skills",
        ),
        notes=(
            "Valid source symlinks are preserved; unmanaged or edited copies are refused.",
            "Skills and grammar manuals come from the installed package.",
            "Checks report discovery paths; an agent may need a new session to load skills.",
            "Local repairs finish even if profile or server checks fail; --offline skips both.",
            "Uninstall never contacts the server or removes preexisting agent directories.",
        ),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax doctor", description=cls.__doc__)
        _add_arguments(parser)
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb
        flags = cast(_Flags, args)
        if flags.uninstall:
            _uninstall(flags)
            return
        healthy = _skills(flags)
        if not flags.offline:
            healthy = _connection(client_factory) and healthy
        if not healthy:
            raise ClientError("doctor found setup problems; see the checks above")


class _Flags(Protocol):
    target: str
    project: bool
    fix: bool
    uninstall: bool
    offline: bool


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", choices=sorted(TARGETS), default="claude")
    parser.add_argument("--project", action="store_true")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--fix", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    parser.add_argument("--offline", action="store_true")


def _skills(flags: _Flags) -> bool:
    target = TARGETS[flags.target]
    try:
        if flags.fix:
            install(target, project=flags.project, root=cwd())
        checked = check_skills(target, project=flags.project, root=cwd())
    except InstallError as error:
        echo(f"skills: error: {error}")
        return False
    echo(f"skills: {checked.status}: {checked.destination}")
    echo(f"  {checked.detail}")
    if checked.status in {"missing", "stale"}:
        scope = " --project" if flags.project else ""
        echo(f"  repair: trax doctor --fix --target {flags.target}{scope}")
    return checked.healthy


def _uninstall(flags: _Flags) -> None:
    try:
        removed = uninstall(TARGETS[flags.target], project=flags.project, root=cwd())
    except InstallError as error:
        raise ClientError(str(error)) from error
    echo(
        f"skills: removed: {removed}"
        if removed is not None
        else "skills: unchanged: no installer-owned skills removed",
    )


def _connection(client_factory: Callable[[], Client]) -> bool:
    try:
        client = client_factory()
    except (ClientError, OSError, UnicodeError) as error:
        echo(f"profile: error: {error}")
        return False
    echo(f"profile: ok: {client.base_url}")
    healthy = True
    for label, path, field in (
        ("server", "/api/version", "sha"),
        ("auth", "/api/me/profile", "role"),
    ):
        try:
            payload = from_plain(client.get(path, timeout=3.0), dict[str, object])
            value = from_plain(payload.get(field), str)
        except (ClientError, OSError, ReadError) as error:
            echo(f"{label}: error: {error}")
            healthy = False
        else:
            echo(f"{label}: ok: {value}")
    return healthy
