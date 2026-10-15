"""Real-git smoke for the worktree-diff capture.

The unit tests inject canned git output; this runs the actual read-only git
commands against a throwaway repository, so the preamble flags and the base
resolution are exercised against git itself. Marked ``cli_git`` -- skipped
unless git and a real repository are available.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import os

import pytest

from trackinizer.lib.agent.types.sessions import FileEditResult
from trackinizer.trax.run.worktree_diff import open_worktree_diff, run_git


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


pytestmark = pytest.mark.cli_git

# Output bound for the setup commands; their output is tiny and unread.
_SETUP_BYTES = 1 << 20


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give git an identity and ignore machine config, so the test writes none."""
    identity = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for name, value in identity.items():
        monkeypatch.setenv(name, value)


def _git(tmp_path: Path, *args: str) -> None:
    """Run one git command in ``tmp_path`` through the module's own runner."""
    done = run_git(["git", "-C", str(tmp_path), *args], _SETUP_BYTES)
    assert done.returncode == 0, done.stdout


def _init_repo(tmp_path: Path) -> None:
    """Initialise a committed repository at ``tmp_path``, holding ``a.py``."""
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "a.py").write_text("one\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-m", "init")


def _refresh(tmp_path: Path) -> list[FileEditResult]:
    """Open a capture at ``tmp_path``, note ``a.py``, and return one refresh."""
    diff = open_worktree_diff(tmp_path)
    assert diff is not None
    diff.note(FileEditResult(call_id="", path=str(tmp_path / "a.py")))
    batches: list[Sequence[FileEditResult]] = []
    diff.refresh(batches.append, now=0.0)
    [batch] = batches
    return list(batch)


def test_real_git_diff_of_an_edited_file(tmp_path: Path) -> None:
    """A committed file edited in the tree shows as its net diff, via real git."""
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("two\n")  # Uncommitted edit this "session" made.
    [record] = _refresh(tmp_path)
    assert record.path == "a.py"
    assert record.edits[0].before == "one\n"
    assert record.edits[0].after == "two\n"


def test_a_commit_in_the_first_turn_still_shows(tmp_path: Path) -> None:
    """The base is pinned at run start, so an edit COMMITTED in the first turn
    still shows: it landed after the pinned base, not before it.

    Captured at the first refresh instead, the base would be the just-made
    commit and the diff against it would be empty -- the change would vanish.
    """
    _init_repo(tmp_path)
    diff = open_worktree_diff(tmp_path)  # Base pinned to the initial commit HERE.
    assert diff is not None
    (tmp_path / "a.py").write_text("two\n")
    _git(tmp_path, "commit", "-am", "edit in the first turn")  # HEAD moves past base.
    diff.note(FileEditResult(call_id="", path=str(tmp_path / "a.py")))
    batches: list[Sequence[FileEditResult]] = []
    diff.refresh(batches.append, now=0.0)
    [record] = batches[0]
    assert record.edits[0].before == "one\n"
    assert record.edits[0].after == "two\n"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
