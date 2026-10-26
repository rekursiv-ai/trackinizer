"""Manage copied skills while preserving source links and local changes.

The package bundles the trax skill: a router, per-kind guidance, and both
grammar manuals. A ``Skill`` places any other skill, as the endpoint commands do.
Managed copies carry a receipt so upgrades and removal can distinguish package
snapshots from user edits. Existing source discovery links remain authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypedDict, cast

import ctypes
import errno
import hashlib
import os
import platform
import shutil
import stat
import tempfile

from treekle import json_dumps, json_loads


_CWD: Final = Path(__file__).resolve().parent

__all__ = [
    "TARGETS",
    "InstallError",
    "Skill",
    "SkillCheck",
    "Target",
    "bundled_skills",
    "check_skills",
    "install",
    "trax_skill",
    "uninstall",
]


class InstallError(Exception):
    """A skill tree could not be inspected, installed, or removed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Target:
    """Describe an agent's user-wide and project-local skill directories."""

    name: str
    label: str
    user: str | None
    project: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Skill:
    """Describe a skill directory to place under an agent's skills folder.

    Attributes:
      name: Directory name under the agent's skills folder; one path segment.
      source: Directory holding the skill's ``SKILL.md`` and its subdirectories.
      extras: Files copied beside ``SKILL.md``, each under its own file name.

    """

    name: str
    source: Path
    extras: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        """Reject a name that would leave the skills folder.

        Raises:
          InstallError: The name is empty, nested, or a parent reference.

        """
        if not self.name or Path(self.name).name != self.name or self.name == "..":
            raise InstallError(f"skill name {self.name!r} must be one path segment")


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillCheck:
    """Report the ownership and package freshness of one skill destination."""

    destination: Path
    status: Literal["missing", "current", "stale", "source", "unmanaged", "modified"]
    detail: str

    @property
    def healthy(self) -> bool:
        """Return whether copied or source guidance is ready to use."""
        return self.status in {"current", "source"}


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
        user=".cursor/skills",
        project=".cursor/skills",
    ),
    "codex": Target(
        name="codex",
        label="Codex",
        user=".agents/skills",
        project=".agents/skills",
    ),
}


def bundled_skills() -> Path:
    """Return the packaged skills root, requiring its router and manuals.

    Returns:
      root: Directory containing the router and per-kind skill guidance.

    Raises:
      InstallError: Required package resources are missing or unreadable.

    """
    docs = _CWD / "docs"
    root = docs / "skills"
    try:
        for resource in (
            root / "trax/SKILL.md",
            docs / "grammar.lark",
            docs / "GRAMMAR.md",
        ):
            if not stat.S_ISREG(resource.stat().st_mode):
                raise InstallError(f"bundled skills missing at {resource}")
    except OSError as error:
        raise InstallError(f"bundled skills unavailable: {error}") from error
    return root


def trax_skill() -> Skill:
    """Return the trax skill bundled with this package.

    Returns:
      skill: The router and per-kind guidance, with both grammar manuals.

    Raises:
      InstallError: Required package resources are missing or unreadable.

    """
    root = bundled_skills()
    return Skill(
        name="trax",
        source=root / "trax",
        extras=(root.parent / "grammar.lark", root.parent / "GRAMMAR.md"),
    )


def check_skills(
    target: Target,
    *,
    project: bool = False,
    root: Path | None = None,
    skill: Skill | None = None,
) -> SkillCheck:
    """Inspect guidance without changing an agent's skill directories.

    Args:
      target: Agent directory convention.
      project: Inspect the project rather than the user's home.
      root: Project root; defaults to the current directory.
      skill: Skill to inspect; defaults to the bundled trax skill.

    Returns:
      check: Destination, ownership status, and a human-readable explanation.

    Raises:
      InstallError: The destination or required package resources cannot be read.

    """
    destination, boundary = _location(
        target,
        project=project,
        root=root or Path.cwd(),
        name=skill.name if skill else "trax",
    )
    try:
        check, receipt = _inspect(destination, boundary=boundary)
        if receipt is not None and check.status == "current":
            _, expected = _bundle(skill)
            if expected != (receipt["files"], receipt["directories"]):
                return SkillCheck(
                    destination=destination,
                    status="stale",
                    detail="Managed skills differ from the installed package; run --fix.",
                )
        return check
    except OSError as error:
        raise InstallError(f"could not inspect {destination}: {error}") from error


def install(
    target: Target,
    *,
    project: bool = False,
    root: Path | None = None,
    dry_run: bool = False,
    skill: Skill | None = None,
) -> tuple[Path, int]:
    """Install missing guidance or refresh an unchanged managed copy.

    Args:
      target: Agent directory convention.
      project: Install into the project rather than the user's home.
      root: Project root; defaults to the current directory.
      dry_run: Inspect and report the destination without writing.
      skill: Skill to install; defaults to the bundled trax skill.

    Returns:
      destination: Installed, preserved, or proposed skill directory.
      count: Number of guidance files, including the skill's extras.

    Raises:
      InstallError: Resources are unavailable, ownership is unsafe, or a write fails.

    """
    destination, boundary = _location(
        target,
        project=project,
        root=root or Path.cwd(),
        name=skill.name if skill else "trax",
    )
    created: list[Path] = []
    try:
        check, receipt = _inspect(destination, boundary=boundary)
        _require_owned(check)
        bundled, expected = _bundle(skill)
        count = len(expected[0])
        if dry_run or check.status == "source":
            return destination, count
        if receipt is not None and expected == (
            receipt["files"],
            receipt["directories"],
        ):
            return destination, count
        _make_parents(destination.parent, boundary=boundary, created=created)
        parents = (
            receipt["created_parents"]
            if receipt is not None
            else [str(p) for p in created]
        )
        _install_tree(
            bundled,
            destination=destination,
            expected=expected,
            parents=parents,
            receipt=receipt,
            boundary=boundary,
        )
    except (OSError, InstallError) as error:
        try:
            _cleanup_parents(created)
        except OSError as cleanup:
            raise InstallError(
                f"{error}; parent cleanup failed: {cleanup}",
            ) from cleanup
        if isinstance(error, InstallError):
            raise
        raise InstallError(f"could not install {destination}: {error}") from error
    return destination, count


def uninstall(
    target: Target,
    *,
    project: bool = False,
    root: Path | None = None,
    dry_run: bool = False,
    skill: Skill | None = None,
) -> Path | None:
    """Remove unchanged managed guidance and its empty installer-created parents.

    Args:
      target: Agent directory convention.
      project: Operate on the project rather than the user's home.
      root: Project root; defaults to the current directory.
      dry_run: Inspect and report an owned copy without removing it.
      skill: Skill to remove; defaults to trax. Only its name is read.

    Returns:
      removed: Removed or proposed path, or None for absent guidance or a source link.

    Raises:
      InstallError: Ownership is unsafe or inspection, removal, or cleanup fails.

    """
    destination, boundary = _location(
        target,
        project=project,
        root=root or Path.cwd(),
        name=skill.name if skill else "trax",
    )
    try:
        check, receipt = _inspect(destination, boundary=boundary)
        _require_owned(check)
        if check.status in {"missing", "source"}:
            return None
        assert isinstance(receipt, dict)
        if dry_run:
            return destination
        _remove_tree(destination, receipt=receipt, boundary=boundary)
    except OSError as error:
        raise InstallError(f"could not remove {destination}: {error}") from error
    try:
        _cleanup_parents([Path(parent) for parent in receipt["created_parents"]])
    except OSError as error:
        raise InstallError(
            f"removed {destination}; could not clean empty parent: {error}",
        ) from error
    return destination


def _destination(target: Target, *, project: bool, root: Path) -> Path:
    """Resolve where ``target``'s skills live for this scope."""
    if project:
        return (root / target.project).absolute()
    if target.user is None:
        raise InstallError(
            f"{target.label} reads project-local skills only; re-run with --project",
        )
    return Path.home() / target.user  # noqa: TID251 -- vendor fixed path, not ours (AGENTS.md rule 3)  # house-ignore[xdg-literal] -- Agent CLI's fixed skill dir, not ours (AGENTS.md rule 3).


def _location(
    target: Target,
    *,
    project: bool,
    root: Path,
    name: str,
) -> tuple[Path, Path]:
    destination = _destination(target, project=project, root=root) / name
    convention = target.project if project else target.user
    assert isinstance(convention, str)
    return destination, destination.parents[len(Path(convention).parts)]


type _Snapshot = tuple[dict[str, str], list[str]]


class _Receipt(TypedDict):
    owner: Literal["trackinizer.trax.install"]
    version: Literal[1]
    destination: str
    files: dict[str, str]
    directories: list[str]
    created_parents: list[str]


def _inspect(
    destination: Path,
    *,
    boundary: Path,
) -> tuple[SkillCheck, _Receipt | None]:
    try:
        mode = destination.lstat().st_mode
    except FileNotFoundError:
        return SkillCheck(
            destination=destination,
            status="missing",
            detail="Skills are missing; run --fix.",
        ), None
    if stat.S_ISLNK(mode):
        try:
            source = stat.S_ISDIR(destination.stat().st_mode) and stat.S_ISREG(
                (destination / "SKILL.md").stat().st_mode,
            )
        except (FileNotFoundError, NotADirectoryError):
            source = False
        if source:
            (destination / "SKILL.md").read_bytes()
            return SkillCheck(
                destination=destination,
                status="source",
                detail="Source skill link preserved.",
            ), None
    receipt = (
        _read_receipt(destination, boundary=boundary) if stat.S_ISDIR(mode) else None
    )
    if receipt is None:
        return SkillCheck(
            destination=destination,
            status="unmanaged",
            detail="Unmanaged destination preserved; move it explicitly before installing.",
        ), None
    actual = _snapshot(destination, installed=True)
    if actual != (receipt["files"], receipt["directories"]):
        return SkillCheck(
            destination=destination,
            status="modified",
            detail="Managed guidance contains local changes; preserve them before reinstalling.",
        ), receipt
    return SkillCheck(
        destination=destination,
        status="current",
        detail="Managed skills match their installation receipt.",
    ), receipt


def _read_receipt(
    destination: Path,
    *,
    boundary: Path,
    root: Path | None = None,
) -> _Receipt | None:
    path = (root or destination) / ".trax-install.json"
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        receipt = json_loads(path.read_text(), _Receipt)
    except (FileNotFoundError, ValueError, TypeError):
        return None
    if receipt["destination"] != str(destination):
        return None
    if any(
        not _relative(path) for path in (*receipt["files"], *receipt["directories"])
    ):
        return None
    if any(
        len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
        for digest in receipt["files"].values()
    ):
        return None
    if ".trax-install.json" in receipt["files"]:
        return None
    parents = receipt["created_parents"]
    allowed = [
        str(parent)
        for parent in destination.parents
        if parent != boundary and parent.is_relative_to(boundary)
    ]
    if parents != allowed[: len(parents)]:
        return None
    return receipt


def _relative(value: str) -> bool:
    path = Path(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
        and value != "."
    )


def _require_owned(check: SkillCheck) -> None:
    if check.status in {"unmanaged", "modified"}:
        raise InstallError(
            f"{check.status} skills at {check.destination}: {check.detail}",
        )


def _snapshot(root: Path, *, installed: bool = False) -> _Snapshot | None:
    files: dict[str, str] = {}
    directories: list[str] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if installed and relative == ".trax-install.json":
                continue
            if stat.S_ISDIR(mode):
                directories.append(relative)
                pending.append(path)
            elif stat.S_ISREG(mode):
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                return None
    return files, sorted(directories)


def _bundle(skill: Skill | None) -> tuple[Skill, _Snapshot]:
    bundled = skill or trax_skill()
    expected = _snapshot(bundled.source)
    if expected is None or ".trax-install.json" in expected[0]:
        raise InstallError(
            f"bundled skills contain unsupported entries at {bundled.source}",
        )
    for extra in bundled.extras:
        expected[0][extra.name] = hashlib.sha256(extra.read_bytes()).hexdigest()
    return bundled, expected


def _make_parents(parent: Path, *, boundary: Path, created: list[Path]) -> None:
    pending: list[Path] = []
    while parent != boundary and not _exists(parent):
        pending.append(parent)
        parent = parent.parent
    for path in reversed(pending):
        path.mkdir()
        created.insert(0, path)


def _cleanup_parents(parents: list[Path]) -> None:
    for parent in parents:
        if parent.is_symlink():
            continue
        try:
            parent.rmdir()
        except FileNotFoundError:
            continue
        except OSError as error:
            if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                continue
            raise


def _install_tree(
    skill: Skill,
    *,
    destination: Path,
    expected: _Snapshot,
    parents: list[str],
    receipt: _Receipt | None,
    boundary: Path,
) -> None:
    staging = Path(tempfile.mkdtemp(prefix=".trax-", dir=destination.parent))
    replacement = staging / "replacement"
    previous = staging / "previous"
    published = False
    try:
        shutil.copytree(skill.source, replacement)
        for extra in skill.extras:
            shutil.copy2(extra, replacement / extra.name)
        _verify_copy(replacement, expected=expected)
        replacement_receipt = _Receipt(
            owner="trackinizer.trax.install",
            version=1,
            destination=str(destination),
            files=expected[0],
            directories=expected[1],
            created_parents=parents,
        )
        (replacement / ".trax-install.json").write_text(
            json_dumps(replacement_receipt, target=_Receipt) + "\n",
        )
        _quarantine(destination, previous=previous, receipt=receipt, boundary=boundary)
        _publish(replacement, destination=destination, previous=previous)
        published = True
        if receipt is not None:
            _verify_previous(
                previous,
                destination=destination,
                receipt=receipt,
                boundary=boundary,
            )
        shutil.rmtree(staging)
    except (OSError, InstallError) as error:
        if published:
            raise InstallError(
                f"installed {destination}; cleanup failed: {error}; retained cleanup at {staging}",
            ) from error
        if not _exists(previous):
            try:
                shutil.rmtree(staging)
            except OSError as cleanup:
                raise InstallError(
                    f"{error}; cleanup failed: {cleanup}; retained cleanup at {staging}",
                ) from cleanup
        raise


def _verify_copy(replacement: Path, *, expected: _Snapshot) -> None:
    if _snapshot(replacement) != expected:
        raise InstallError(
            "bundled skills changed while copying; previous guidance preserved",
        )


def _publish(replacement: Path, *, destination: Path, previous: Path) -> None:
    if _exists(destination):
        error = InstallError(f"destination changed while installing at {destination}")
        _restore(previous, destination=destination, failure=error)
        raise error
    try:
        _rename_noreplace(replacement, target=destination)
    except (OSError, InstallError) as error:
        _restore(previous, destination=destination, failure=error)
        raise


def _restore(
    previous: Path,
    *,
    destination: Path,
    failure: OSError | InstallError,
) -> None:
    if not _exists(previous):
        return
    if _exists(destination):
        raise InstallError(
            f"{failure}; destination appeared at {destination}; "
            f"previous installation retained at {previous}",
        ) from failure
    try:
        _rename_noreplace(previous, target=destination)
    except (OSError, InstallError) as error:
        raise InstallError(
            f"could not publish {destination}: {failure}; restore failed: {error}; "
            f"previous installation retained at {previous}",
        ) from error


def _rename_noreplace(path: Path, target: Path) -> Path:
    """Rename without replacing any entry created concurrently at the target."""
    system = platform.system()
    if system == "Windows":
        return path.rename(target)
    try:
        library = ctypes.CDLL(None, use_errno=True)
        rename = library.renameatx_np if system == "Darwin" else library.renameat2
    except AttributeError as error:
        raise InstallError("exclusive directory rename is unavailable") from error
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    # AT_FDCWD differs by OS; RENAME_EXCL and RENAME_NOREPLACE prevent replacement.
    directory_fd, flags = (-2, 4) if system == "Darwin" else (-100, 1)
    result = cast(
        int,
        rename(
            directory_fd,
            os.fsencode(path),
            directory_fd,
            os.fsencode(target),
            flags,
        ),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), target)
    return target


def _quarantine(
    destination: Path,
    *,
    previous: Path,
    receipt: _Receipt | None,
    boundary: Path,
) -> None:
    if receipt is None:
        if _exists(destination):
            raise InstallError(f"destination changed while installing at {destination}")
        return
    destination.rename(previous)
    try:
        _verify_previous(
            previous,
            destination=destination,
            receipt=receipt,
            boundary=boundary,
        )
    except (OSError, InstallError) as error:
        _restore(previous, destination=destination, failure=error)
        raise


def _verify_previous(
    previous: Path,
    *,
    destination: Path,
    receipt: _Receipt,
    boundary: Path,
) -> None:
    if (
        not stat.S_ISDIR(previous.lstat().st_mode)
        or _read_receipt(destination, boundary=boundary, root=previous) != receipt
        or _snapshot(previous, installed=True)
        != (receipt["files"], receipt["directories"])
    ):
        raise InstallError(f"managed skills changed during operation at {destination}")


def _remove_tree(destination: Path, *, receipt: _Receipt, boundary: Path) -> None:
    staging = Path(tempfile.mkdtemp(prefix=".trax-", dir=destination.parent))
    previous = staging / "previous"
    try:
        _quarantine(destination, previous=previous, receipt=receipt, boundary=boundary)
        shutil.rmtree(previous)
    except (OSError, InstallError) as error:
        _restore(previous, destination=destination, failure=error)
        raise
    finally:
        if not _exists(previous):
            staging.rmdir()


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True
