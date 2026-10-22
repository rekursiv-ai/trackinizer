"""Snapshot a worktree privately and bundle it, against small real git repos."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import hashlib
import os
import shutil
import subprocess

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax import job_source


if TYPE_CHECKING:
    from collections.abc import Iterator

    from trackinizer.trax.job_source import Snapshot


_NO_SUCH_COMMIT = "f" * 40


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 -- fixed git argv on a tmp_path repo.
        ["git", "-C", str(repo), *args],  # noqa: S607 -- git is the tool under test.
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


# Git touches the time of an object it would write again, so an object file keeps its
# name and size only.
def _tree(directory: Path) -> dict[str, tuple[int, int]]:
    """Every file under ``directory`` with its size and modification time."""
    return {
        str(path.relative_to(directory)): (
            path.stat().st_size,
            0
            if path.relative_to(directory).parts[0] == "objects"
            else path.stat().st_mtime_ns,
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _contents(directory: Path) -> dict[str, bytes]:
    """Every file under ``directory`` with its bytes."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module", autouse=True)
def _git_identity(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    config = tmp_path_factory.mktemp("gitconfig") / "config"
    config.write_text("[user]\n\tname = t\n\temail = t@example.com\n")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GIT_CONFIG_GLOBAL", str(config))
        patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        for name in (
            "GIT_AUTHOR_NAME",
            "GIT_COMMITTER_NAME",
            "GIT_DIR",
            "GIT_INDEX_FILE",
        ):
            patch.delenv(name, raising=False)
        yield


@pytest.fixture(scope="module")
def committed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a repo with one commit: a tracked file, a nested one, an ignore rule."""
    repo = tmp_path_factory.mktemp("committed")
    (repo / "sub").mkdir()
    (repo / "a.txt").write_text("one\n")
    (repo / "sub" / "b.txt").write_text("two\n")
    (repo / ".gitignore").write_text("*.log\n")
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "one")
    return repo


@pytest.fixture
def repo(committed: Path, tmp_path: Path) -> Path:
    shutil.copytree(committed, tmp_path / "repo", symlinks=True)
    return tmp_path / "repo"


@pytest.fixture
def dirty(repo: Path) -> Path:
    (repo / "a.txt").write_text("one changed\n")
    (repo / "new.py").write_text("print(1)\n")
    (repo / "sub" / "c.txt").write_text("three\n")
    (repo / "run.log").write_text("ignored\n")
    return repo


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    (tmp_path / "private").mkdir()
    return tmp_path / "private"


def test_a_snapshot_holds_the_worktree_untracked_files_included_ignored_excluded(
    dirty: Path,
    scratch: Path,
) -> None:
    snap = job_source.snapshot(dirty, scratch)

    names = _git(scratch, "ls-tree", "-r", "--name-only", snap.commit).split()
    assert names == [".gitignore", "a.txt", "new.py", "sub/b.txt", "sub/c.txt"]
    assert _git(scratch, "show", f"{snap.commit}:a.txt") == "one changed"
    assert _git(scratch, "rev-parse", f"{snap.commit}^") == _git(
        dirty,
        "rev-parse",
        "HEAD",
    )
    assert snap.files == 5
    assert snap.private_git_dir == scratch


def test_a_snapshot_leaves_the_users_repository_untouched(
    dirty: Path,
    scratch: Path,
) -> None:
    status = _git(dirty, "status", "--porcelain")
    refs = _git(dirty, "for-each-ref")
    before = _tree(dirty / ".git")

    snap = job_source.snapshot(dirty, scratch)

    assert _tree(dirty / ".git") == before
    assert _git(dirty, "for-each-ref") == refs
    assert _git(dirty, "status", "--porcelain") == status
    assert _git(scratch, "cat-file", "-t", snap.commit) == "commit"
    absent = subprocess.run(  # noqa: S603 -- fixed git argv on a tmp_path repo.
        ["git", "-C", str(dirty), "cat-file", "-e", snap.commit],  # noqa: S607 -- git is the tool under test.
        check=False,
        capture_output=True,
    )
    assert absent.returncode != 0


def test_a_split_index_gets_no_new_shared_index_in_the_users_repository(
    dirty: Path,
    scratch: Path,
) -> None:
    # A split index keeps its shared half beside the user's index, and git writes a
    # new one there when it rewrites the private copy unless told not to split.
    # Reading the shared half touches its time, as any git command does.
    _git(dirty, "update-index", "--split-index")
    before = _contents(dirty / ".git")

    snap = job_source.snapshot(dirty, scratch)

    assert _contents(dirty / ".git") == before
    assert _git(scratch, "show", f"{snap.commit}:a.txt") == "one changed"
    assert snap.files == 5


def test_a_clean_tree_reuses_head_and_writes_no_commit(
    repo: Path,
    scratch: Path,
) -> None:
    head = _git(repo, "rev-parse", "HEAD")

    snap = job_source.snapshot(repo, scratch)

    assert snap.commit == head
    assert snap.files == 3


def test_a_snapshot_works_from_a_subdirectory_and_a_linked_worktree(
    dirty: Path,
    scratch: Path,
    tmp_path: Path,
) -> None:
    linked = tmp_path / "linked"
    _git(dirty, "worktree", "add", "--quiet", "--detach", str(linked))
    (linked / "linked.txt").write_text("x\n")

    snap = job_source.snapshot(linked / "sub", scratch)

    assert "linked.txt" in _git(scratch, "ls-tree", "-r", "--name-only", snap.commit)
    assert (linked / "sub" / "b.txt").read_text() == "two\n"


def test_a_repository_with_no_commit_snapshots_as_a_root_commit(
    tmp_path: Path,
    scratch: Path,
) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "a.txt").write_text("x\n")
    _git(fresh, "init", "--quiet")

    snap = job_source.snapshot(fresh, scratch)

    assert _git(scratch, "rev-list", "--parents", "-n1", snap.commit) == snap.commit
    assert snap.files == 1


def test_a_directory_outside_any_repository_is_refused(
    tmp_path: Path,
    scratch: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(
        ClientError,
        match=r"^git rev-parse failed: \S",
    ):
        job_source.snapshot(outside, scratch)


def test_the_worktree_top_is_found_from_a_subdirectory_and_absent_outside(
    repo: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    assert job_source.worktree_top(repo / "sub") == repo.resolve()
    assert job_source.worktree_top(outside) is None


def test_the_snapshot_commit_is_named_for_its_parent_and_authored_by_the_tool(
    dirty: Path,
    scratch: Path,
) -> None:
    head = _git(dirty, "rev-parse", "HEAD")

    snap = job_source.snapshot(dirty, scratch)

    assert _git(
        scratch,
        "log",
        "-1",
        "--format=%an <%ae>|%cn <%ce>|%s",
        snap.commit,
    ) == (
        f"trax job <trax-job@localhost>|trax job <trax-job@localhost>|"
        f"trax job snapshot of {head}"
    )


def test_the_first_snapshot_of_an_empty_repository_says_so(
    tmp_path: Path,
    scratch: Path,
) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "a.txt").write_text("x\n")
    _git(fresh, "init", "--quiet")

    snap = job_source.snapshot(fresh, scratch)

    assert _git(scratch, "log", "-1", "--format=%s", snap.commit) == (
        "trax job snapshot of an empty repository"
    )


def test_a_checkout_whose_path_holds_quotes_colons_and_backslashes_snapshots(
    dirty: Path,
    scratch: Path,
    tmp_path: Path,
) -> None:
    odd = tmp_path / 'we"ird:dir\\x'
    shutil.move(dirty, odd)

    snap = job_source.snapshot(odd, scratch)

    assert _git(scratch, "rev-parse", f"{snap.commit}^") == _git(
        odd,
        "rev-parse",
        "HEAD",
    )


def test_the_private_index_starts_as_a_copy_of_the_users_when_there_is_one(
    dirty: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied: list[tuple[str, str]] = []
    real = shutil.copyfile

    def spy(source: str, target: Path) -> Path:
        copied.append((source, str(target)))
        return Path(real(source, target))

    monkeypatch.setattr(shutil, "copyfile", spy)

    _ = job_source.snapshot(dirty, scratch)

    assert copied == [(str(dirty / ".git" / "index"), str(scratch / "index"))]


def test_a_filename_that_is_not_text_is_counted_and_kept(
    dirty: Path,
    scratch: Path,
) -> None:
    (dirty / os.fsdecode(b"bad\xff.txt")).write_text("x\n")

    snap = job_source.snapshot(dirty, scratch)

    assert snap.files == 6


def test_a_snapshot_lists_the_untracked_files_it_added_and_not_the_ignored_ones(
    dirty: Path,
    scratch: Path,
) -> None:
    (dirty / "sub" / "deep").mkdir()
    (dirty / "sub" / "deep" / "d.txt").write_text("x\n")

    snap = job_source.snapshot(dirty, scratch)

    assert snap.untracked == ("new.py", "sub/c.txt", "sub/deep/d.txt")


def test_a_snapshot_of_a_clean_tree_adds_no_untracked_file(
    repo: Path,
    scratch: Path,
) -> None:
    assert job_source.snapshot(repo, scratch).untracked == ()


def test_the_untracked_files_are_listed_from_a_subdirectory_by_their_path_from_the_top(
    dirty: Path,
    scratch: Path,
) -> None:
    snap = job_source.snapshot(dirty / "sub", scratch)

    assert snap.untracked == ("new.py", "sub/c.txt")


def test_an_untracked_file_whose_name_is_not_text_or_holds_a_newline_is_listed(
    dirty: Path,
    scratch: Path,
) -> None:
    (dirty / os.fsdecode(b"bad\xff.txt")).write_text("x\n")
    (dirty / "two\nlines.txt").write_text("x\n")

    snap = job_source.snapshot(dirty, scratch)

    assert os.fsdecode(b"bad\xff.txt") in snap.untracked
    assert "two\nlines.txt" in snap.untracked


_SECRET_NAMES = [
    ".env",
    ".env.local",
    ".envrc",
    "sub/.ENV",
    "id_rsa",
    "keys/id_ed25519",
    "ID_RSA",
    "server.pem",
    "a/b/tls.key",
    "TLS.KEY",
    "aws_credentials.json",
    "credentials",
    "gcp-Credentials.json",
    ".netrc",
]


@pytest.mark.parametrize("name", _SECRET_NAMES)
def test_an_untracked_file_that_looks_like_a_secret_is_refused_by_name(
    dirty: Path,
    scratch: Path,
    name: str,
) -> None:
    secret = dirty / name
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("shh\n")

    with pytest.raises(ClientError) as refused:
        _ = job_source.snapshot(dirty, scratch)

    assert str(refused.value) == (
        f"refusing to upload untracked files that look like secrets: {name}. "
        "Add them to .gitignore, or pass --include-untracked to send them."
    )


@pytest.mark.parametrize("name", _SECRET_NAMES)
def test_a_secret_named_file_goes_when_the_caller_says_to_include_it(
    dirty: Path,
    scratch: Path,
    name: str,
) -> None:
    secret = dirty / name
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("shh\n")

    snap = job_source.snapshot(dirty, scratch, allow_secret_names=True)

    assert name in snap.untracked
    assert name in _git(scratch, "ls-tree", "-r", "--name-only", snap.commit).split()


@pytest.mark.parametrize("name", _SECRET_NAMES)
def test_a_secret_named_file_that_gitignore_covers_is_neither_sent_nor_refused(
    dirty: Path,
    scratch: Path,
    name: str,
) -> None:
    secret = dirty / name
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("shh\n")
    with (dirty / ".gitignore").open("a") as ignore:
        _ = ignore.write(f"/{name}\n")

    snap = job_source.snapshot(dirty, scratch)

    assert name not in snap.untracked
    assert name not in _git(scratch, "ls-tree", "-r", "--name-only", snap.commit)


def test_a_tracked_file_with_a_secret_name_is_the_users_choice_and_goes(
    repo: Path,
    scratch: Path,
) -> None:
    (repo / ".env.example").write_text("A=\n")
    _git(repo, "add", ".env.example")
    _git(repo, "commit", "--quiet", "-m", "env")
    (repo / ".env.example").write_text("A=1\n")

    snap = job_source.snapshot(repo, scratch)

    assert snap.untracked == ()
    assert _git(scratch, "show", f"{snap.commit}:.env.example") == "A=1"


@pytest.mark.parametrize(
    "name",
    [
        "envelope.py",
        "keys.py",
        "identity.py",
        "monkey.txt",
        "id.txt",
        "a.key.md",
        "pem",
    ],
)
def test_an_untracked_file_that_only_resembles_a_secret_name_goes(
    dirty: Path,
    scratch: Path,
    name: str,
) -> None:
    (dirty / name).write_text("x\n")

    snap = job_source.snapshot(dirty, scratch)

    assert name in snap.untracked


def test_the_refusal_names_every_secret_file_up_to_ten_and_counts_the_rest(
    dirty: Path,
    scratch: Path,
) -> None:
    for number in range(13):
        (dirty / f"k{number:02d}.pem").write_text("x\n")

    with pytest.raises(ClientError) as refused:
        _ = job_source.snapshot(dirty, scratch)

    names = ", ".join(f"k{number:02d}.pem" for number in range(10))
    assert str(refused.value) == (
        "refusing to upload untracked files that look like secrets: "
        f"{names} and 3 more. "
        "Add them to .gitignore, or pass --include-untracked to send them."
    )


def test_a_refusal_shows_a_name_with_a_control_character_as_an_escape(
    dirty: Path,
    scratch: Path,
) -> None:
    (dirty / "x\x1b[2J.pem").write_text("x\n")

    with pytest.raises(ClientError) as refused:
        _ = job_source.snapshot(dirty, scratch)

    assert "\x1b" not in str(refused.value)
    assert "x\\x1b[2J.pem" in str(refused.value)


def test_a_refused_snapshot_writes_nothing_to_the_users_repository(
    dirty: Path,
    scratch: Path,
) -> None:
    (dirty / ".env").write_text("shh\n")
    before = _tree(dirty / ".git")

    with pytest.raises(ClientError):
        _ = job_source.snapshot(dirty, scratch)

    assert _tree(dirty / ".git") == before


def test_plain_name_escapes_what_a_terminal_would_act_on() -> None:
    assert job_source.plain_name("a/b.txt") == "a/b.txt"
    assert job_source.plain_name("a\x1bb\nc") == "a\\x1bb\\nc"


def test_variables_that_pick_a_repository_do_not_redirect_git(
    dirty: Path,
    scratch: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    ):
        monkeypatch.setenv(name, str(tmp_path / "nowhere"))

    snap = job_source.snapshot(dirty, scratch)
    info = job_source.bundle(snap, [], tmp_path / "out.bundle")

    assert info.bytes > 0
    assert job_source.worktree_top(dirty) == dirty.resolve()


def _receiver(tmp_path: Path, source: Path) -> Path:
    """Build an empty bare repo holding the source's branch as a ``refs/have`` ref."""
    receiver = tmp_path / "receiver"
    _git(tmp_path, "init", "--quiet", "--bare", str(receiver))
    _git(receiver, "fetch", "--quiet", str(source), "main:refs/have/base")
    return receiver


def test_a_bundle_on_a_known_base_is_thin_and_verifies_where_the_base_is_a_ref(
    dirty: Path,
    scratch: Path,
    tmp_path: Path,
) -> None:
    base = _git(dirty, "rev-parse", "HEAD")
    snap = job_source.snapshot(dirty, scratch)
    receiver = _receiver(tmp_path, dirty)

    thin = job_source.bundle(snap, [base], tmp_path / "thin.bundle")
    full = job_source.bundle(snap, [], tmp_path / "full.bundle")

    assert thin.base == base
    assert full.base is None
    assert thin.bytes < full.bytes
    assert thin.path == tmp_path / "thin.bundle"
    assert "requires this ref" in _git(receiver, "bundle", "verify", str(thin.path))
    _git(
        receiver,
        "fetch",
        "--quiet",
        str(thin.path),
        f"refs/trax/gpu/{snap.commit}:refs/job/head",
    )
    assert _git(receiver, "rev-parse", "refs/job/head") == snap.commit


def test_a_full_bundle_verifies_in_an_empty_repository(
    dirty: Path,
    scratch: Path,
    tmp_path: Path,
) -> None:
    snap = job_source.snapshot(dirty, scratch)
    empty = tmp_path / "empty"
    _git(tmp_path, "init", "--quiet", "--bare", str(empty))

    info = job_source.bundle(snap, [], tmp_path / "full.bundle")

    assert info.base is None
    assert "complete history" in _git(empty, "bundle", "verify", str(info.path))


def test_a_bundle_reports_the_size_and_digest_of_its_file(
    dirty: Path,
    scratch: Path,
    tmp_path: Path,
) -> None:
    snap = job_source.snapshot(dirty, scratch)

    info = job_source.bundle(snap, [], tmp_path / "out.bundle")

    data = info.path.read_bytes()
    assert info.bytes == len(data)
    assert info.sha256 == hashlib.sha256(data).hexdigest()


def test_a_base_the_repository_lacks_or_that_is_not_a_sha_is_dropped(
    dirty: Path,
    scratch: Path,
    tmp_path: Path,
) -> None:
    snap = job_source.snapshot(dirty, scratch)

    info = job_source.bundle(
        snap,
        [_NO_SUCH_COMMIT, "not-a-sha", "--all"],
        tmp_path / "out.bundle",
    )

    assert info.base is None


def test_the_newest_known_base_is_the_one_a_bundle_is_cut_on(
    repo: Path,
    scratch: Path,
    tmp_path: Path,
) -> None:
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "--quiet", "-am", "two")
    second = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("three\n")
    snap = job_source.snapshot(repo, scratch)

    newest = job_source.bundle(snap, [second, first], tmp_path / "a.bundle")
    oldest = job_source.bundle(snap, [first, second], tmp_path / "b.bundle")
    skipped = job_source.bundle(snap, [_NO_SUCH_COMMIT, first], tmp_path / "c.bundle")

    assert (newest.base, oldest.base, skipped.base) == (second, first, first)


def test_a_snapshot_that_is_itself_a_base_is_bundled_on_its_parent(
    repo: Path,
    scratch: Path,
    tmp_path: Path,
) -> None:
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "--quiet", "-am", "two")
    second = _git(repo, "rev-parse", "HEAD")
    snap = job_source.snapshot(repo, scratch)

    info = job_source.bundle(snap, [second], tmp_path / "out.bundle")

    assert snap.commit == second
    assert info.base == first


def test_a_base_that_descends_from_the_snapshot_falls_back_to_a_full_bundle(
    repo: Path,
    scratch: Path,
    tmp_path: Path,
) -> None:
    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "--quiet", "-am", "two")
    snap = job_source.snapshot(repo, scratch)
    (repo / "a.txt").write_text("three\n")
    _git(repo, "commit", "--quiet", "-am", "three")
    newer = _git(repo, "rev-parse", "HEAD")

    info = job_source.bundle(snap, [newer], tmp_path / "out.bundle")

    assert info.base is None
    assert info.bytes > 0


def test_a_bundle_that_cannot_be_written_is_one_plain_error(
    dirty: Path,
    scratch: Path,
    tmp_path: Path,
) -> None:
    snap: Snapshot = job_source.snapshot(dirty, scratch)

    with pytest.raises(ClientError, match=r"^git bundle failed: \S"):
        job_source.bundle(snap, [], tmp_path / "no-such-dir" / "out.bundle")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
