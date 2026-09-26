"""Install the bundled trax skills into an agent's skill directory.

The skills under ``trax/docs/skills`` teach an agent how to author each
inquiry kind. They ship in the wheel but land inside the installed package,
where no agent looks -- so a ``uv tool install trackinizer`` gives you the
grammar and none of the guidance. This module copies that tree into the
directories the agents actually read.

Skills are COPIED, never symlinked: the codex loader skips a skill whose
``SKILL.md`` is a symlink (see ``docs/skills/README.md``), and a copy also
survives the package being upgraded or removed underneath it.

Layout is the Agent Skills convention -- one directory per skill, each
holding a ``SKILL.md``. The nesting under ``trax/`` is preserved because the
per-kind child skills (``trax-belief``, ``trax-paper``, ...) are reachable
only through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import shutil


__all__ = [
    "TARGETS",
    "InstallError",
    "Target",
    "bundled_skills",
    "install",
    "uninstall",
]


class InstallError(Exception):
    """A skill tree could not be installed or removed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Target:
    """One agent's skill directory convention.

    Attributes:
      name: The ``--target`` token.
      label: Human name for receipts.
      user: Path under ``$HOME`` for a user-wide install, or ``None`` when the
        agent only reads project-local skills.
      project: Path relative to the project root for a project-local install.

    """

    name: str

    label: str

    user: str | None

    project: str


# Claude Code reads both ``~/.claude/skills`` and ``.claude/skills``; Cursor
# reads ``.cursor/skills``. Codex is given ``.sagent/skills`` because that is
# the path ``docs/skills/README.md`` already names for the sibling sagent CLI.
TARGETS: Final[dict[str, Target]] = {
    "claude": Target(
        name="claude",
        label="Claude Code",
        user=".claude/skills",
        project=".claude/skills",
    ),
    "cursor": Target(
        name="cursor",
        label="Cursor",
        user=None,
        project=".cursor/skills",
    ),
    "codex": Target(
        name="codex",
        label="Codex",
        user=None,
        project=".sagent/skills",
    ),
}


def bundled_skills() -> Path:
    """Return the packaged skills root (the directory holding ``trax/``).

    Returns:
      root: Directory containing the ``trax`` skill tree.

    Raises:
      InstallError: The package shipped without its skills.

    """
    root = Path(__file__).resolve().parent / "docs" / "skills"
    if not (root / "trax" / "SKILL.md").is_file():
        raise InstallError(
            f"bundled skills missing at {root} -- reinstall trackinizer",
        )
    return root


def _destination(target: Target, *, project: bool, root: Path) -> Path:
    """Resolve where ``target``'s skills live for this scope."""
    if project:
        return root / target.project
    if target.user is None:
        raise InstallError(
            f"{target.label} reads project-local skills only; re-run with --project",
        )
    return Path.home() / target.user  # noqa: TID251 -- vendor fixed path, not ours (AGENTS.md rule 3)  # house-ignore[xdg-literal] -- Agent CLI's fixed skill dir, not ours (AGENTS.md rule 3).


def install(
    target: Target,
    *,
    project: bool = False,
    root: Path | None = None,
    dry_run: bool = False,
) -> tuple[Path, int]:
    """Copy the bundled skill tree into ``target``'s skill directory.

    Idempotent: an existing ``trax`` skill directory is replaced wholesale, so
    upgrading is the same command as installing and a stale child skill from a
    previous version cannot survive.

    Args:
      target: Which agent's convention to write.
      project: Install into the project rather than the user's home.
      root: Project root for a project-scoped install; defaults to the cwd.
      dry_run: Report the destination without writing.

    Returns:
      destination: The ``trax`` directory that was (or would be) written.
      count: How many files the tree holds.

    Raises:
      InstallError: The destination is unusable.

    """
    source = bundled_skills() / "trax"
    dest_root = _destination(target, project=project, root=root or Path.cwd())
    dest = dest_root / "trax"
    count = sum(1 for p in source.rglob("*") if p.is_file())
    if dry_run:
        return dest, count
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
    except OSError as err:
        raise InstallError(f"could not write {dest}: {err}") from err
    return dest, count


def uninstall(
    target: Target,
    *,
    project: bool = False,
    root: Path | None = None,
) -> Path | None:
    """Remove the installed ``trax`` skill tree.

    Args:
      target: Which agent's convention to clear.
      project: Operate on the project rather than the user's home.
      root: Project root for a project-scoped install; defaults to the cwd.

    Returns:
      removed: The directory removed, or ``None`` when nothing was installed.

    Raises:
      InstallError: The directory exists but could not be removed.

    """
    dest = _destination(target, project=project, root=root or Path.cwd()) / "trax"
    if not dest.exists():
        return None
    try:
        shutil.rmtree(dest)
    except OSError as err:
        raise InstallError(f"could not remove {dest}: {err}") from err
    return dest
