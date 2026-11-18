"""Snapshot a worktree without touching it, and bundle the snapshot for a node.

The snapshot is a git commit of the worktree's exact bytes, untracked files
included and ignored files left out. It is built in a private object store with a
private index, so the user's index, ``HEAD``, refs and objects are never written.
The bundle holds only what the node lacks: it is cut on top of commits the node
says it already has.

An untracked file is in the snapshot because the user did not ignore it, which is not
the same as meaning to send it. A key, a ``.env`` or a credentials file that nobody
ignored would sit on the node for two weeks and in every later job of its owner, so
the snapshot refuses untracked files with such names unless the caller says to send
them, and reports which untracked files it took.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess

from trackinizer.client.errors import ClientError
from trackinizer.trax.job_ssh import require_tool


if TYPE_CHECKING:
    from collections.abc import Sequence


__all__ = [
    "BundleInfo",
    "Snapshot",
    "bundle",
    "plain_name",
    "snapshot",
    "worktree_top",
]

# These name the repository git works on, so a caller's values must not redirect a
# command meant for the user's repository or the private one.
_REPOSITORY_VARIABLES: Final = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_WORK_TREE",
    },
)

# The base names of files that hold a credential, matched without regard to case. An
# untracked one is not sent unless the caller says so.
_SECRET_NAMES: Final = (
    ".env*",
    "*.pem",
    "id_*",
    "*credentials*",
    ".netrc",
    "*.key",
)
_SECRETS_SHOWN: Final = 10

# A snapshot commit needs an identity, and a fresh dev box may have none set.
_IDENTITY: Final = {
    "GIT_AUTHOR_NAME": "trax job",
    "GIT_AUTHOR_EMAIL": "trax-job@localhost",
    "GIT_COMMITTER_NAME": "trax job",
    "GIT_COMMITTER_EMAIL": "trax-job@localhost",
}


@dataclass(frozen=True, kw_only=True, slots=True)
class Snapshot:
    """A commit of the worktree, and the private repository that holds it."""

    commit: str
    """The snapshot commit; ``HEAD`` itself when the worktree is clean."""

    private_git_dir: Path
    """A bare repository whose alternates point at the user's objects."""

    files: int
    """How many files the commit holds."""

    untracked: tuple[str, ...] = ()
    """The untracked files that are not ignored, which the snapshot added to the
    commit, by path from the worktree's top."""


@dataclass(frozen=True, kw_only=True, slots=True)
class BundleInfo:
    """A bundle file ready to upload."""

    path: Path
    bytes: int
    sha256: str

    base: str | None
    """The newest commit the bundle was cut on top of; ``None`` for a full bundle."""


def snapshot(repo: Path, tmp: Path, *, allow_secret_names: bool = False) -> Snapshot:
    """Commit the worktree's current bytes into a private object store.

    Args:
      repo: Any directory inside the worktree.
      tmp: An empty directory that becomes the private bare repository.
      allow_secret_names: Send untracked files whose names look like credentials
        (``.env*``, ``*.pem``, ``id_*``, ``*credentials*``, ``.netrc``, ``*.key``)
        instead of refusing them.

    Returns:
      snapshot: The commit, the private repository, the file count, and the untracked
        files taken.

    Raises:
      ClientError: ``repo`` is not a git worktree, a git command failed, or an
        untracked file has a secret's name and ``allow_secret_names`` is not set.

    """
    top, common, index = _Git(cwd=repo, env=_clean_env())(
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-common-dir",
        "--git-path",
        "index",
    ).splitlines()
    _ = _Git(cwd=repo, env=_clean_env())("init", "--bare", str(tmp))
    (tmp / "objects" / "info" / "alternates").write_text(
        f"{Path(common) / 'objects'}\n",
    )
    # Seeded with the user's stat cache, so ``add -A`` hashes only what changed. A
    # repository before its first ``git add`` has no index file yet.
    private_index = tmp / "index"
    with suppress(FileNotFoundError):
        _ = shutil.copyfile(index, private_index)
    git = _Git(
        cwd=Path(top),
        env={
            **_clean_env(),
            **_IDENTITY,
            "GIT_INDEX_FILE": str(private_index),
            # New objects land in the private store, which reads the user's through
            # the alternates file above.
            "GIT_OBJECT_DIRECTORY": str(tmp / "objects"),
        },
    )
    # A split index keeps its shared half in the user's git dir; written unsplit, the
    # private index makes git write nothing there.
    unsplit = ("-c", "core.splitIndex=false")
    untracked = _untracked(git)
    if not allow_secret_names:
        _refuse_secrets(untracked)
    _ = git(*unsplit, "add", "-A")
    tree = git(*unsplit, "write-tree").strip()
    head = _head(git)
    if head is not None and head[1] == tree:
        commit = head[0]
    else:
        parent = [] if head is None else ["-p", head[0]]
        base = "an empty repository" if head is None else head[0]
        commit = git("commit-tree", tree, *parent, "-m", f"trax job snapshot of {base}")
        commit = commit.strip()
    files = git("ls-tree", "-r", "-z", tree).count("\0")
    return Snapshot(
        commit=commit,
        private_git_dir=tmp,
        files=files,
        untracked=untracked,
    )


def plain_name(name: str) -> str:
    r"""Spell a file name so that showing it cannot command a terminal.

    Args:
      name: A path from the worktree.

    Returns:
      shown: The name with every control character, backslash and non-ASCII
        character written as an escape (``\x1b``).

    """
    return name.encode("unicode_escape").decode("ascii")


def bundle(snap: Snapshot, bases: Sequence[str], out: Path) -> BundleInfo:
    """Write a git bundle of the snapshot, thin on top of the bases it can use.

    Args:
      snap: The snapshot to send.
      bases: Commits the node holds, newest first; unknown ones are ignored.
      out: Where the bundle is written.

    Returns:
      info: The file, its size and digest, and the base it was cut on.

    Raises:
      ClientError: A git command failed.

    """
    git = _Git(cwd=snap.private_git_dir, env=_clean_env())
    ref = f"refs/trax/gpu/{snap.commit}"
    _ = git("update-ref", ref, snap.commit)
    held = _held(git, bases)
    kept = [each for each in held if each != snap.commit]
    if snap.commit in held:
        # The node already has this commit, so its parents are on the node too.
        kept = git("rev-parse", f"{snap.commit}^@").split()
    try:
        _ = git("bundle", "create", str(out), ref, *(f"^{each}" for each in kept))
    except ClientError:
        if not kept:
            raise
        # A base that descends from the snapshot leaves nothing to send.
        kept.clear()
        _ = git("bundle", "create", str(out), ref)
    with out.open("rb") as file:
        digest = hashlib.file_digest(file, hashlib.sha256).hexdigest()
    return BundleInfo(
        path=out,
        bytes=out.stat().st_size,
        sha256=digest,
        base=kept[0] if kept else None,
    )


def worktree_top(directory: Path) -> Path | None:
    """Return the top of the git worktree ``directory`` is in, or ``None``.

    Args:
      directory: Any directory.

    Returns:
      top: The resolved worktree root; ``None`` outside one or without git.

    """
    try:
        top = _Git(cwd=directory, env=_clean_env())("rev-parse", "--show-toplevel")
    except ClientError:
        return None
    return Path(top.strip()).resolve()


@dataclass(frozen=True, kw_only=True, slots=True)
class _Git:
    """Runs git in one directory with one environment."""

    cwd: Path
    env: Mapping[str, str]

    def run(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[bytes]:
        """Run git and return the finished process, whatever its exit code.

        Args:
          *args: The git arguments after ``git``.
          stdin: Text fed to git; empty by default, so git never reads a terminal.

        Returns:
          done: The finished process, with its output as bytes.

        """
        return subprocess.run(  # noqa: S603 -- fixed git argv.
            [require_tool("git"), *args],
            cwd=self.cwd,
            env=dict(self.env),
            input=os.fsencode(stdin),
            capture_output=True,
            check=False,
        )

    def __call__(self, *args: str, stdin: str = "") -> str:
        """Run git and return its stdout, or raise one plain error with its reason."""
        done = self.run(*args, stdin=stdin)
        if done.returncode:
            raise ClientError(
                f"git {args[0]} failed: {os.fsdecode(done.stderr).strip()}",
            )
        return os.fsdecode(done.stdout)


def _untracked(git: _Git) -> tuple[str, ...]:
    """List the untracked files that are not ignored, by path from the top."""
    listed = git("ls-files", "--others", "--exclude-standard", "-z")
    return tuple(sorted(name for name in listed.split("\0") if name))


def _refuse_secrets(untracked: Sequence[str]) -> None:
    """Raise a ClientError naming the untracked files whose names look like secrets."""
    found = [name for name in untracked if _looks_secret(name)]
    if not found:
        return
    shown = ", ".join(plain_name(name) for name in found[:_SECRETS_SHOWN])
    more = len(found) - _SECRETS_SHOWN
    names = f"{shown} and {more} more" if more > 0 else shown
    raise ClientError(
        f"refusing to upload untracked files that look like secrets: {names}. "
        "Add them to .gitignore, or pass --include-untracked to send them.",
    )


def _looks_secret(name: str) -> bool:
    """Say whether a path's base name is one a credential is usually kept in."""
    base = name.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(base, pattern) for pattern in _SECRET_NAMES)


def _head(git: _Git) -> tuple[str, str] | None:
    """Return ``HEAD``'s commit and tree, or ``None`` before the first commit."""
    done = git.run("rev-parse", "HEAD", "HEAD^{tree}")
    if done.returncode:
        return None
    commit, tree = os.fsdecode(done.stdout).split()
    return commit, tree


def _held(git: _Git, bases: Sequence[str]) -> list[str]:
    """Return the bases that are commits in this repository, in order."""
    wanted = [each for each in bases if re.fullmatch(r"[0-9a-f]{40}", each)]
    if not wanted:
        return []
    answer = git(
        "cat-file",
        "--batch-check",
        stdin="".join(f"{each}^{{commit}}\n" for each in wanted),
    ).splitlines()
    return [
        each
        for index, each in enumerate(wanted)
        if answer[index].split()[1] == "commit"
    ]


def _clean_env() -> dict[str, str]:
    """Return the environment without the variables that pick a repository."""
    return {k: v for k, v in os.environ.items() if k not in _REPOSITORY_VARIABLES}
