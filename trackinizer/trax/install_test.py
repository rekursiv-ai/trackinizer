"""Skill lifecycle preserves source links and detects local modifications."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import json
import shutil

from treekle import json_loads, to_plain

import pytest

from trackinizer.trax.install import TARGETS, InstallError, install, uninstall

import trackinizer.trax.install as install_module


if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep all destination writes inside this test's temporary directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("trackinizer.trax.install.Path.home", lambda: home)
    return home


@pytest.fixture
def bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Supply a small complete package whose version can change independently."""
    package = tmp_path / "package"
    source = package / "docs/skills/trax"
    (source / "belief").mkdir(parents=True)
    (source / "SKILL.md").write_text("router: belief/SKILL.md")
    (source / "belief/SKILL.md").write_text("belief guidance")
    for name in ("grammar.lark", "GRAMMAR.md"):
        (package / "docs" / name).write_text(f"original {name}")
    monkeypatch.setattr(install_module, "_CWD", package)
    return source


@pytest.mark.parametrize(
    ("target", "directory"),
    [("claude", ".claude"), ("cursor", ".cursor"), ("codex", ".agents")],
)
@pytest.mark.parametrize("project", [False, True])
def test_supported_destinations(
    target: str,
    directory: str,
    *,
    project: bool,
    fake_home: Path,
    tmp_path: Path,
    bundle: Path,
) -> None:
    del bundle
    destination, count = install(TARGETS[target], project=project, root=tmp_path)
    assert (
        destination == (tmp_path if project else fake_home) / directory / "skills/trax"
    )
    assert (destination / "belief/SKILL.md").is_file()
    assert count == 4


def test_installed_bundle_is_self_contained() -> None:
    destination, count = install(TARGETS["claude"])
    docs = Path(install_module.__file__).parent / "docs"
    source = docs / "skills/trax"
    expected = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    expected.update(
        {
            Path(name): (docs / name).read_bytes()
            for name in ("grammar.lark", "GRAMMAR.md")
        },
    )
    actual = {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file() and path.name != ".trax-install.json"
    }
    assert actual == expected
    assert count == len(expected)
    assert (destination / ".trax-install.json").is_file()
    skill = (destination / "SKILL.md").read_text()
    assert "`trackinizer/trax/docs/{grammar.lark,GRAMMAR.md}`" not in skill
    assert "`trackinizer/trax/docs/skills/trax/<kind>/SKILL.md`" not in skill
    assert "<kind>/SKILL.md" in skill


def test_dry_run_reports_without_writing(fake_home: Path, bundle: Path) -> None:
    del bundle
    destination, count = install(TARGETS["claude"], dry_run=True)
    assert count == 4
    assert destination == fake_home / ".claude/skills/trax"
    assert not destination.parent.exists()


def test_reinstall_is_idempotent(bundle: Path) -> None:
    del bundle
    destination, _ = install(TARGETS["claude"])
    before = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (destination, *destination.rglob("*"))
    }
    assert install(TARGETS["claude"])[0] == destination
    assert before == {
        path: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (destination, *destination.rglob("*"))
    }


def test_uninstall_restores_empty_project(tmp_path: Path, bundle: Path) -> None:
    del bundle
    project = tmp_path / "project"
    project.mkdir()
    destination, _ = install(TARGETS["codex"], project=True, root=project)
    assert uninstall(TARGETS["codex"], project=True, root=project) == destination
    assert list(project.iterdir()) == []
    assert uninstall(TARGETS["codex"], project=True, root=project) is None


@pytest.mark.parametrize("preexisting", [True, False])
def test_uninstall_preserves_unowned_parent_contents(
    fake_home: Path,
    bundle: Path,
    *,
    preexisting: bool,
) -> None:
    del bundle
    parent = fake_home / ".claude/skills"
    if preexisting:
        parent.mkdir(parents=True)
    destination, _ = install(TARGETS["claude"])
    if not preexisting:
        (parent / "other-skill").mkdir()
    assert uninstall(TARGETS["claude"]) == destination
    assert parent.is_dir()
    assert parent.exists()


@pytest.mark.parametrize("installed", [False, True])
def test_uninstall_dry_run_preserves_destination(
    *,
    installed: bool,
    fake_home: Path,
    bundle: Path,
) -> None:
    del bundle
    destination = fake_home / ".claude/skills/trax"
    if installed:
        install(TARGETS["claude"])
    assert uninstall(TARGETS["claude"], dry_run=True) == (
        destination if installed else None
    )
    assert destination.exists() == installed
    assert destination.parent.exists() == installed


@pytest.mark.parametrize(
    "operation",
    ["install", "uninstall", "dry-install", "dry-uninstall"],
)
def test_source_symlink_is_preserved(
    tmp_path: Path,
    fake_home: Path,
    bundle: Path,
    operation: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("live source")
    destination = fake_home / ".claude/skills/trax"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(source, target_is_directory=True)
    if operation.endswith("uninstall"):
        assert uninstall(TARGETS["claude"], dry_run=operation.startswith("dry")) is None
    else:
        assert (
            install(TARGETS["claude"], dry_run=operation.startswith("dry"))[0]
            == destination
        )
    assert destination.is_symlink()
    assert destination.readlink() == source
    assert (source / "SKILL.md").read_text() == "live source"
    assert (bundle / "SKILL.md").read_text() == "router: belief/SKILL.md"


def test_unreadable_source_router_is_a_controlled_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("source guidance")
    destination = tmp_path / ".agents/skills/trax"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(source, target_is_directory=True)
    read = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path == destination / "SKILL.md":
            raise PermissionError("source router unreadable")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(InstallError, match="source router unreadable"):
        install_module.check_skills(TARGETS["codex"], project=True, root=tmp_path)
    assert destination.is_symlink()
    assert destination.resolve() == source
    assert (source / "SKILL.md").read_bytes() == b"source guidance"


@pytest.mark.parametrize("kind", ["directory", "file", "broken-link", "other-link"])
@pytest.mark.parametrize(
    "operation",
    ["install", "uninstall", "dry-install", "dry-uninstall"],
)
def test_unowned_destination_is_refused(
    tmp_path: Path,
    fake_home: Path,
    bundle: Path,
    kind: str,
    operation: str,
) -> None:
    del bundle
    destination = fake_home / ".claude/skills/trax"
    destination.parent.mkdir(parents=True)
    target = tmp_path / "source"
    if kind == "directory":
        destination.mkdir()
        (destination / "SKILL.md").write_text("unowned skill")
    elif kind == "file":
        destination.write_text("unowned file")
    else:
        if kind == "other-link":
            target.mkdir()
            (target / "sentinel").write_text("keep")
        destination.symlink_to(target, target_is_directory=True)
    action = uninstall if operation.endswith("uninstall") else install
    with pytest.raises(InstallError, match="unmanaged"):
        action(TARGETS["claude"], dry_run=operation.startswith("dry"))
    assert destination.is_symlink() or destination.exists()
    if kind == "other-link":
        assert (target / "sentinel").read_text() == "keep"


@pytest.mark.parametrize("change", ["edit", "delete", "add", "directory", "symlink"])
@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_modified_installation_is_refused(
    tmp_path: Path,
    bundle: Path,
    change: str,
    operation: str,
) -> None:
    del bundle
    destination, _ = install(TARGETS["claude"])
    skill = destination / "belief/SKILL.md"
    if change == "edit":
        skill.write_text("user edit")
    elif change == "delete":
        skill.unlink()
    elif change == "add":
        (destination / "sentinel").write_text("untracked")
    elif change == "directory":
        (destination / "local-empty").mkdir()
    else:
        skill.unlink()
        outside = tmp_path / "outside"
        outside.write_text("external")
        skill.symlink_to(outside)
    action = install if operation == "install" else uninstall
    with pytest.raises(InstallError, match="modified"):
        action(TARGETS["claude"])
    assert destination.is_dir()
    if change == "symlink":
        assert (tmp_path / "outside").read_text() == "external"


def test_copy_failure_cleans_new_parents(
    monkeypatch: pytest.MonkeyPatch,
    fake_home: Path,
    bundle: Path,
) -> None:
    del bundle
    monkeypatch.setattr(shutil, "copytree", _copy_failed)
    with pytest.raises(InstallError, match="copy failed"):
        install(TARGETS["claude"])
    assert list(fake_home.iterdir()) == []


def test_copy_failure_preserves_working_install(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
) -> None:
    destination, _ = install(TARGETS["claude"])
    previous = (destination / "SKILL.md").read_bytes()
    (bundle / "SKILL.md").write_text("new version")
    monkeypatch.setattr(shutil, "copytree", _copy_failed)
    with pytest.raises(InstallError, match="copy failed"):
        install(TARGETS["claude"])
    assert (destination / "SKILL.md").read_bytes() == previous
    assert sorted(path.name for path in destination.parent.iterdir()) == ["trax"]


def test_publish_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
) -> None:
    destination, _ = install(TARGETS["claude"])
    previous = (destination / "SKILL.md").read_bytes()
    (bundle / "SKILL.md").write_text("new version")
    monkeypatch.setattr(
        install_module,
        "_rename_noreplace",
        _failing_rename(install_module._rename_noreplace, rollback=False),
    )
    with pytest.raises(InstallError, match="publish failed"):
        install(TARGETS["claude"])
    assert (destination / "SKILL.md").read_bytes() == previous
    assert sorted(path.name for path in destination.parent.iterdir()) == ["trax"]


def test_failed_rollback_retains_recoverable_backup(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
) -> None:
    destination, _ = install(TARGETS["claude"])
    previous = (destination / "SKILL.md").read_bytes()
    (bundle / "SKILL.md").write_text("new version")
    monkeypatch.setattr(
        install_module,
        "_rename_noreplace",
        _failing_rename(install_module._rename_noreplace, rollback=True),
    )
    with pytest.raises(InstallError, match="previous") as error:
        install(TARGETS["claude"])
    backups = list(destination.parent.glob(".trax-*/previous/SKILL.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == previous
    assert str(backups[0].parent) in str(error.value)


def test_published_install_reports_retained_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
) -> None:
    destination, _ = install(TARGETS["claude"])
    (bundle / "SKILL.md").write_text("new version")
    monkeypatch.setattr(shutil, "rmtree", _copy_failed)
    with pytest.raises(InstallError, match="installed") as error:
        install(TARGETS["claude"])
    assert (destination / "SKILL.md").read_text() == "new version"
    cleanup = list(destination.parent.glob(".trax-*/previous"))
    assert len(cleanup) == 1
    assert str(cleanup[0].parent) in str(error.value)


@pytest.mark.parametrize("missing", ["SKILL.md", "grammar.lark", "GRAMMAR.md"])
def test_missing_bundle_preserves_working_install(bundle: Path, missing: str) -> None:
    destination, _ = install(TARGETS["claude"])
    previous = (destination / "SKILL.md").read_bytes()
    ((bundle if missing == "SKILL.md" else bundle.parent.parent) / missing).unlink()
    with pytest.raises(InstallError, match="bundled"):
        install(TARGETS["claude"])
    assert (destination / "SKILL.md").read_bytes() == previous


@pytest.mark.parametrize(
    "operation",
    ["install", "uninstall", "dry-install", "dry-uninstall"],
)
def test_inspection_errors_are_translated(
    monkeypatch: pytest.MonkeyPatch,
    fake_home: Path,
    bundle: Path,
    operation: str,
) -> None:
    del bundle
    destination = fake_home / ".claude/skills/trax"
    monkeypatch.setattr(
        Path,
        "lstat",
        _inspection_failed(Path.lstat, destination=destination),
    )
    action = uninstall if operation.endswith("uninstall") else install
    with pytest.raises(InstallError, match="inspection denied"):
        action(TARGETS["claude"], dry_run=operation.startswith("dry"))


def test_check_inspection_errors_are_translated(
    monkeypatch: pytest.MonkeyPatch,
    fake_home: Path,
    bundle: Path,
) -> None:
    del bundle
    destination = fake_home / ".claude/skills/trax"
    monkeypatch.setattr(
        Path,
        "lstat",
        _inspection_failed(Path.lstat, destination=destination),
    )
    with pytest.raises(InstallError, match="inspection denied"):
        install_module.check_skills(TARGETS["claude"])


def test_check_reports_missing_current_and_stale(bundle: Path) -> None:
    missing = install_module.check_skills(TARGETS["claude"])
    assert missing.status == "missing"
    assert not missing.healthy
    assert missing.detail
    destination, _ = install(TARGETS["claude"])
    current = install_module.check_skills(TARGETS["claude"])
    assert current.destination == destination
    assert current.status == "current"
    assert current.healthy
    (bundle / "belief/SKILL.md").write_text("updated guidance")
    (bundle.parent.parent / "grammar.lark").write_text("updated grammar")
    (bundle.parent.parent / "GRAMMAR.md").write_text("updated manual")
    (bundle / "new-kind").mkdir()
    (bundle / "new-kind/SKILL.md").write_text("new kind")
    stale = install_module.check_skills(TARGETS["claude"])
    assert stale.status == "stale"
    assert not stale.healthy
    assert install(TARGETS["claude"])[0] == destination
    assert (destination / "new-kind/SKILL.md").read_text() == "new kind"
    assert (destination / "belief/SKILL.md").read_text() == "updated guidance"
    assert (destination / "grammar.lark").read_text() == "updated grammar"
    assert (destination / "GRAMMAR.md").read_text() == "updated manual"
    assert install_module.check_skills(TARGETS["claude"]).status == "current"
    uninstall(TARGETS["claude"])
    assert not destination.parent.parent.exists()


@pytest.mark.parametrize("kind", ["source", "unmanaged", "modified"])
def test_check_distinguishes_existing_ownership(
    fake_home: Path,
    bundle: Path,
    kind: str,
) -> None:
    destination = fake_home / ".claude/skills/trax"
    if kind == "source":
        destination.parent.mkdir(parents=True)
        destination.symlink_to(bundle, target_is_directory=True)
    elif kind == "unmanaged":
        shutil.copytree(bundle, destination)
    else:
        install(TARGETS["claude"])
        (destination / "SKILL.md").write_text("local edit")
    check = install_module.check_skills(TARGETS["claude"])
    assert check.status == kind
    assert check.healthy == (kind == "source")


@pytest.mark.parametrize(
    "damage",
    [
        "owner",
        "version",
        "destination",
        "parents",
        "ancestor",
        "file-path",
        "hash",
        "json",
        "symlink",
    ],
)
def test_invalid_receipt_cannot_authorize_removal(
    tmp_path: Path,
    bundle: Path,
    damage: str,
) -> None:
    del bundle
    destination, _ = install(TARGETS["claude"])
    receipt = destination / ".trax-install.json"
    fields = json_loads(receipt.read_text(), dict[str, object])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("keep")
    if damage == "owner":
        fields["owner"] = "someone-else"
    elif damage == "version":
        fields["version"] = 99
    elif damage == "destination":
        fields["destination"] = str(outside)
    elif damage == "parents":
        fields["created_parents"] = [str(outside)]
    elif damage == "ancestor":
        fields["created_parents"] = [str(parent) for parent in destination.parents[:3]]
    elif damage == "file-path":
        fields["files"] = {"../outside/sentinel": "0" * 64}
    elif damage == "hash":
        fields["files"] = {"SKILL.md": "invalid"}
    if damage == "json":
        receipt.write_text("{")
    elif damage == "symlink":
        external = outside / "receipt.json"
        receipt.rename(external)
        receipt.symlink_to(external)
    else:
        receipt.write_text(json.dumps(to_plain(fields)))
    assert install_module.check_skills(TARGETS["claude"]).status == "unmanaged"
    with pytest.raises(InstallError, match="unmanaged"):
        uninstall(TARGETS["claude"])
    assert (outside / "sentinel").read_text() == "keep"
    assert destination.is_dir()


def test_uninstall_does_not_require_available_bundle(bundle: Path) -> None:
    destination, _ = install(TARGETS["claude"])
    (bundle / "SKILL.md").unlink()
    assert uninstall(TARGETS["claude"]) == destination


def test_uninstall_preserves_symlink_parent(
    tmp_path: Path,
    fake_home: Path,
    bundle: Path,
) -> None:
    del bundle
    shared = tmp_path / "shared"
    shared.mkdir()
    parent = fake_home / ".claude"
    parent.symlink_to(shared, target_is_directory=True)
    destination, _ = install(TARGETS["claude"])
    assert uninstall(TARGETS["claude"]) == destination
    assert parent.is_symlink()
    assert shared.is_dir()
    assert list(shared.iterdir()) == []


def test_project_symlink_round_trip_preserves_preexisting_directories(
    tmp_path: Path,
    bundle: Path,
) -> None:
    del bundle
    shared = tmp_path / "shared-project"
    (shared / ".agents").mkdir(parents=True)
    project = tmp_path / "project"
    project.symlink_to(shared, target_is_directory=True)
    destination, _ = install(TARGETS["codex"], project=True, root=project)
    assert uninstall(TARGETS["codex"], project=True, root=project) == destination
    assert project.is_symlink()
    assert shared.is_dir()
    assert list(shared.iterdir()) == [shared / ".agents"]
    assert list((shared / ".agents").iterdir()) == []


def test_parent_cleanup_error_reports_removed_install(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
) -> None:
    del bundle
    destination, _ = install(TARGETS["claude"])
    original = Path.rmdir
    monkeypatch.setattr(
        Path,
        "rmdir",
        _rmdir_failed(original, parent=destination.parent),
    )
    with pytest.raises(InstallError, match="removed") as error:
        uninstall(TARGETS["claude"])
    assert not destination.exists()
    assert str(destination.parent) in str(error.value)


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("changed", ["SKILL.md", ".trax-install.json"])
def test_edit_after_inspection_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
    operation: str,
    changed: str,
) -> None:
    destination, _ = install(TARGETS["claude"])
    (bundle / "SKILL.md").write_text("new package version")
    inspect = install_module._inspect

    def edit_after_inspection(path: Path, *, boundary: Path) -> object:
        result = inspect(path, boundary=boundary)
        (destination / changed).write_text("late local edit")
        return result

    monkeypatch.setattr(install_module, "_inspect", edit_after_inspection)
    action = install if operation == "install" else uninstall
    with pytest.raises(InstallError, match=r"changed|modified"):
        action(TARGETS["claude"])
    assert (destination / changed).read_text() == "late local edit"
    assert sorted(path.name for path in destination.parent.iterdir()) == ["trax"]


def test_edit_at_refresh_rename_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
) -> None:
    destination, _ = install(TARGETS["claude"])
    (bundle / "SKILL.md").write_text("new package version")
    rename = Path.rename

    def edit_before_rename(path: Path, target: Path) -> Path:
        if path == destination:
            (path / "SKILL.md").write_text("late local edit")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", edit_before_rename)
    with pytest.raises(InstallError, match=r"changed|modified"):
        install(TARGETS["claude"])
    assert (destination / "SKILL.md").read_text() == "late local edit"
    assert sorted(path.name for path in destination.parent.iterdir()) == ["trax"]


def test_edit_during_publication_retains_recoverable_backup(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
) -> None:
    destination, _ = install(TARGETS["claude"])
    (bundle / "SKILL.md").write_text("new package version")
    rename = install_module._rename_noreplace

    def edit_previous(path: Path, target: Path) -> Path:
        if path.name == "replacement":
            (path.parent / "previous/SKILL.md").write_text("late local edit")
        return rename(path, target)

    monkeypatch.setattr(install_module, "_rename_noreplace", edit_previous)
    with pytest.raises(InstallError, match="retained") as error:
        install(TARGETS["claude"])
    assert (destination / "SKILL.md").read_text() == "new package version"
    backups = list(destination.parent.glob(".trax-*/previous/SKILL.md"))
    assert len(backups) == 1
    assert backups[0].read_text() == "late local edit"
    assert str(backups[0].parent.parent) in str(error.value)


def test_restore_preserves_a_destination_that_appeared(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
) -> None:
    destination, _ = install(TARGETS["claude"])
    previous = (destination / "SKILL.md").read_bytes()
    (bundle / "SKILL.md").write_text("new package version")
    rename = install_module._rename_noreplace

    def create_conflict(path: Path, target: Path) -> Path:
        return _create_conflict(path, target=target, rename=rename)

    monkeypatch.setattr(install_module, "_rename_noreplace", create_conflict)
    with pytest.raises(InstallError, match="retained") as error:
        install(TARGETS["claude"])
    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    backups = list(destination.parent.glob(".trax-*/previous/SKILL.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == previous
    assert str(backups[0].parent) in str(error.value)


def test_publication_preserves_a_concurrent_empty_directory(
    monkeypatch: pytest.MonkeyPatch,
    fake_home: Path,
    bundle: Path,
) -> None:
    del bundle
    destination = fake_home / ".claude/skills/trax"
    appeared: list[int] = []
    monkeypatch.setattr(
        install_module,
        "_rename_noreplace",
        partial(
            _appear_before_rename,
            rename=install_module._rename_noreplace,
            appeared=appeared,
        ),
    )
    with pytest.raises(InstallError):
        install(TARGETS["claude"])
    assert destination.stat().st_ino == appeared[0]
    assert list(destination.iterdir()) == []


def _appear_before_rename(
    path: Path,
    *,
    target: Path,
    rename: Callable[[Path, Path], Path],
    appeared: list[int],
) -> Path:
    if path.name == "replacement":
        target.mkdir()
        appeared.append(target.stat().st_ino)
    return rename(path, target)


def _copy_failed(*_args: object, **_kwargs: object) -> None:
    raise OSError("copy failed")


def _create_conflict(
    path: Path,
    *,
    target: Path,
    rename: Callable[[Path, Path], Path],
) -> Path:
    if path.name == "replacement":
        target.mkdir()
        raise OSError("publish failed")
    return rename(path, target)


def _failing_rename(
    original: Callable[[Path, Path], Path],
    *,
    rollback: bool,
) -> Callable[[Path, Path], Path]:
    def rename(path: Path, target: Path) -> Path:
        if path.name in ({"replacement", "previous"} if rollback else {"replacement"}):
            raise OSError("publish failed")
        return original(path, target)

    return rename


def _inspection_failed(
    original: Callable[[Path], object],
    *,
    destination: Path,
) -> Callable[[Path], object]:
    def inspect(path: Path) -> object:
        if path == destination:
            raise PermissionError("inspection denied")
        return original(path)

    return inspect


def _rmdir_failed(
    original: Callable[[Path], None],
    *,
    parent: Path,
) -> Callable[[Path], None]:
    def remove(path: Path) -> None:
        if path == parent:
            raise PermissionError("cleanup denied")
        original(path)

    return remove


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
