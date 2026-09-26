"""``install``/``uninstall``: skill-tree delivery to agent skill directories.

Exercised with the real bundled skill tree against a fake home / temp project
root: the copy must be complete (SKILL.md + per-kind child skills), idempotent
(wholesale replace so stale child skills cannot survive an upgrade), and
reversible. Dry-run writes nothing. Cursor/codex read project-local skills
only, so a user-scope install for them is refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.trax.install import (
    TARGETS,
    InstallError,
    install,
    uninstall,
)

import trackinizer.trax.install as install_module


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``Path.home`` at a temp dir so user-scope installs are isolated."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(
        "trackinizer.trax.install.Path.home",
        lambda: home,
    )
    return home


def test_installs_user_scope_tree_with_child_skills(fake_home: Path) -> None:
    dest, count = install(TARGETS["claude"])
    assert dest == fake_home / ".claude/skills/trax"
    assert (dest / "SKILL.md").is_file()
    # per-kind child skills stay nested under trax/ (reachable only through it)
    assert (dest / "belief" / "SKILL.md").is_file()
    assert (dest / "paper" / "SKILL.md").is_file()
    assert count >= 3


def test_dry_run_reports_without_writing(fake_home: Path) -> None:
    _dest, count = install(TARGETS["claude"], dry_run=True)
    assert count > 0
    # the destination tree specifically is untouched
    assert not (fake_home / ".claude" / "skills").exists()


@pytest.mark.usefixtures("fake_home")
def test_reinstall_replaces_wholesale() -> None:
    """A stale child skill from a previous version cannot survive an upgrade."""
    dest, _ = install(TARGETS["claude"])
    stale = dest / "stale-kind" / "SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("obsolete")

    dest2, count2 = install(TARGETS["claude"])
    assert dest2 == dest
    assert count2 >= 3
    assert not stale.exists()


@pytest.mark.usefixtures("fake_home")
def test_uninstall_removes_then_reports_absent() -> None:
    install(TARGETS["claude"])
    removed = uninstall(TARGETS["claude"])
    assert removed is not None
    assert not removed.exists()
    assert uninstall(TARGETS["claude"]) is None


def test_project_scope_installs_under_root(tmp_path: Path) -> None:
    dest, count = install(TARGETS["cursor"], project=True, root=tmp_path)
    assert dest == tmp_path / ".cursor/skills/trax"
    assert (dest / "SKILL.md").is_file()
    assert count >= 2
    # removing via the same scope clears it
    assert uninstall(TARGETS["cursor"], project=True, root=tmp_path) is not None


@pytest.mark.usefixtures("fake_home")
def test_user_scope_refused_for_project_only_agents() -> None:
    """Cursor/codex read project-local skills only; a user install is a mistake."""
    with pytest.raises(InstallError, match="project-local"):
        install(TARGETS["cursor"])


def test_every_target_installs_project_scope(tmp_path: Path) -> None:
    """The multi-target table stays honest: each target's layout lands."""
    for target in TARGETS.values():
        dest, count = install(target, project=True, root=tmp_path / target.name)
        assert (dest / "SKILL.md").is_file()
        assert count >= 2


def test_install_error_is_raised_when_bundled_skills_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A package shipped without its skills fails loudly, not silently."""
    empty_bundle = tmp_path / "bundle-without-skills"
    empty_bundle.mkdir()
    monkeypatch.setattr(
        install_module,
        "bundled_skills",
        lambda: empty_bundle,
    )
    # the bundle exists but has no trax/SKILL.md: the copy fails loudly as an
    # InstallError rather than silently writing an empty tree
    with pytest.raises(InstallError):
        install(TARGETS["claude"])
