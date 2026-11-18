"""Tests for the session worktree-diff capture.

Git is injected as a :class:`~trackinizer.trax.run.worktree_diff.Runner`,
so these are hermetic and fast -- no real repository, no subprocess. One
real-git smoke lives in ``worktree_diff_cli_test`` behind the ``cli_git`` marker.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    FileEditResult,
    FileWriteResult,
)
from trackinizer.trax.run import worktree_diff
from trackinizer.trax.run.worktree_diff import (
    MIN_REFRESH_SEC,
    REFRESH_INTERVAL_SEC,
    Output,
    WorktreeDiff,
    held,
    open_worktree_diff,
    run_git,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import pytest


_TOP = "/work/tree"


def _out(text: str = "", *, returncode: int = 0, cut: bool = False) -> Output:
    return Output(returncode=returncode, stdout=text.encode(), cut=cut)


# A diff git would print for one changed file: header lines, then one hunk.
def _diff(path: str, *, before: str, after: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -1 +1 @@\n-{before}\n+{after}\n"
    )


class _FakeGit:
    """Canned git: the first response whose predicate matches ``argv`` wins."""

    def __init__(
        self,
        responses: Sequence[tuple[Callable[[tuple[str, ...]], bool], Output]],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []
        self.caps: list[int] = []

    def __call__(self, argv: Sequence[str], max_bytes: int) -> Output:
        got = tuple(argv)
        self.calls.append(got)
        self.caps.append(max_bytes)
        for matches, out in self.responses:
            if matches(got):
                return out
        return _out(returncode=1)


def _has(*needles: str) -> Callable[[tuple[str, ...]], bool]:
    return lambda argv: all(needle in argv for needle in needles)


def _toplevel(top: str = _TOP) -> tuple[Callable[[tuple[str, ...]], bool], Output]:
    return (_has("rev-parse", "--show-toplevel"), _out(f"{top}\n"))


def _head(sha: str = "basesha") -> tuple[Callable[[tuple[str, ...]], bool], Output]:
    return (lambda a: "rev-parse" in a and a[-1] == "HEAD", _out(f"{sha}\n"))


def _opened(
    responses: Sequence[tuple[Callable[[tuple[str, ...]], bool], Output]],
) -> tuple[WorktreeDiff, _FakeGit]:
    git = _FakeGit([_toplevel(), _head(), *responses])
    diff = open_worktree_diff(Path("/work/tree/sub"), run=git)
    assert diff is not None
    return diff, git


class _Emits:
    """Collects each batch of records a refresh uploads."""

    def __init__(self) -> None:
        self.batches: list[list[FileEditResult]] = []

    def __call__(self, records: Sequence[FileEditResult]) -> None:
        self.batches.append(list(records))


class TestOpen:
    def test_not_a_repo_is_none(self) -> None:
        """A cwd in no work tree yields no capture, not an error."""
        git = _FakeGit([(_has("rev-parse", "--show-toplevel"), _out(returncode=128))])
        assert open_worktree_diff(Path("/nonexistent/x"), run=git) is None

    def test_base_is_the_head_sha_at_open(self) -> None:
        """The base is pinned to HEAD when the run starts, not re-read later."""
        diff, _ = _opened([])
        assert diff.base == "basesha"

    def test_runs_rev_parse_in_each_directory_under_the_byte_cap(self) -> None:
        """Toplevel resolves in the cwd, HEAD in the resolved tree, both bounded."""
        git = _FakeGit([_toplevel(), _head("basesha")])
        diff = open_worktree_diff(Path("/work/tree/sub"), run=git)
        assert diff is not None
        assert diff.top == Path("/work/tree").resolve()
        assert diff.base == "basesha"
        assert git.calls == [
            tuple(
                worktree_diff._argv(
                    Path("/work/tree/sub"),
                    ("rev-parse", "--show-toplevel"),
                ),
            ),
            tuple(worktree_diff._argv(diff.top, ("rev-parse", "HEAD"))),
        ]
        assert git.caps == [worktree_diff._LISTING_BYTES, worktree_diff._LISTING_BYTES]

    def test_base_falls_back_to_head_when_no_sha(self) -> None:
        """A tree with no resolvable HEAD (an empty repo) pins the base to ``HEAD``."""
        git = _FakeGit([_toplevel()])  # HEAD rev-parse falls through to returncode 1.
        diff = open_worktree_diff(Path("/work/tree"), run=git)
        assert diff is not None
        assert diff.base == "HEAD"


class TestNote:
    def test_tracks_only_edit_and_write_paths(self) -> None:
        """An edit or a write inside the tree is tracked; other records are not."""
        diff, _ = _opened([])
        diff.note(AssistantMessage(content="not a file"))
        diff.note(FileEditResult(call_id="", path="/work/tree/a.py"))
        diff.note(FileWriteResult(call_id="", path="/work/tree/sub/b.py", content="x"))
        assert diff.touched == {"a.py", "sub/b.py"}

    def test_path_outside_the_tree_is_dropped(self) -> None:
        """An edit above the tree (another agent's file) is never attributed here."""
        diff, _ = _opened([])
        diff.note(FileEditResult(call_id="", path="/etc/passwd"))
        diff.note(FileEditResult(call_id="", path="/work/other/c.py"))
        assert diff.touched == set()

    def test_held_credential_path_is_dropped(self) -> None:
        """A credential file the session edited is not tracked, listed or diffed."""
        diff, _ = _opened([])
        diff.note(FileEditResult(call_id="", path="/work/tree/.env"))
        diff.note(FileEditResult(call_id="", path="/work/tree/deploy/id_rsa"))
        assert diff.touched == set()


class TestRefresh:
    def test_one_edit_record_per_changed_file(self) -> None:
        """Each changed file becomes a FileEditResult whose edits are its diff."""
        diff, _ = _opened(
            [
                (
                    _has("diff", "--", "a.py"),
                    _out(_diff("a.py", before="old", after="new")),
                ),
            ],
        )
        diff.note(FileEditResult(call_id="", path="/work/tree/a.py"))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        [batch] = emit.batches
        [record] = batch
        assert record.path == "a.py"
        assert record.edits[0].before == "old\n"
        assert record.edits[0].after == "new\n"

    def test_read_only_flags_on_every_git_call(self) -> None:
        """Every git invocation is read-only: no lock, no hook, no ext-diff."""
        diff, git = _opened(
            [(_has("diff", "--", "a.py"), _out(_diff("a.py", before="x", after="y")))],
        )
        diff.note(FileEditResult(call_id="", path="/work/tree/a.py"))
        diff.refresh(_Emits(), now=0.0)
        assert git.calls  # At least the diff ran.
        for argv in git.calls:
            assert "--no-optional-locks" in argv
            assert "core.hooksPath=/dev/null" in argv

    def test_over_cap_diff_is_a_note_not_the_text(self) -> None:
        """A diff past the byte cap is reported omitted, never silently cut.

        The diff holds real hunks, so dropping the cut check would parse them
        into edits rather than a note -- the omitted note is the ``cut`` guard.
        """
        diff, _ = _opened(
            [
                (
                    _has("diff", "--", "big.py"),
                    _out(_diff("big.py", before="o", after="n"), cut=True),
                ),
            ],
        )
        diff.note(FileEditResult(call_id="", path="/work/tree/big.py"))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        [record] = emit.batches[0]
        assert record.path == "big.py"
        assert "omitted" in (record.edits[0].lead or "")
        assert record.edits[0].before is None

    def test_base_falls_back_to_head_when_sha_is_gone(self) -> None:
        """A rebase that drops the pinned sha falls back to diffing against HEAD."""
        diff, git = _opened(
            [
                # The pinned sha no longer resolves.
                (lambda a: "--verify" in a, _out(returncode=1)),
                (
                    _has("diff", "--", "a.py"),
                    _out(_diff("a.py", before="o", after="n")),
                ),
            ],
        )
        diff.note(FileEditResult(call_id="", path="/work/tree/a.py"))
        diff.refresh(_Emits(), now=0.0)
        diff_calls = [a for a in git.calls if "diff" in a and "a.py" in a]
        assert diff_calls
        assert "HEAD" in diff_calls[0]
        assert all("basesha" not in a for a in diff_calls)


class TestTrigger:
    def test_dirty_refreshes_then_clean_does_not(self) -> None:
        """A refresh runs on a fresh edit and not again until something changes."""
        diff, _ = _opened(
            [(_has("diff", "--", "a.py"), _out(_diff("a.py", before="o", after="n")))],
        )
        diff.note(FileEditResult(call_id="", path="/work/tree/a.py"))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        diff.refresh(emit, now=1.0)  # Nothing changed, well within the interval.
        assert len(emit.batches) == 1

    def test_interval_refreshes_without_a_new_edit(self) -> None:
        """A shell fixer changes a touched file with no edit record; time triggers it."""
        diff, git = _opened(
            [(_has("diff", "--", "a.py"), _out(_diff("a.py", before="o", after="n")))],
        )
        diff.note(FileEditResult(call_id="", path="/work/tree/a.py"))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        # The file's content differs by the next interval, with no new edit record.
        git.responses = [
            _toplevel(),
            _head(),
            (_has("diff", "--", "a.py"), _out(_diff("a.py", before="o", after="z"))),
        ]
        diff.refresh(emit, now=REFRESH_INTERVAL_SEC + 1.0)
        assert len(emit.batches) == 2

    def test_a_dirty_burst_is_throttled(self) -> None:
        """Rapid edits do not re-run git every drain tick; a floor spaces them."""
        diff, git = _opened(
            [(_has("diff", "--", "a.py"), _out(_diff("a.py", before="o", after="n")))],
        )
        diff.note(FileEditResult(call_id="", path="/work/tree/a.py"))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        # A second edit changes the diff, but barely any time has passed.
        git.responses = [
            _toplevel(),
            _head(),
            (_has("diff", "--", "a.py"), _out(_diff("a.py", before="o", after="z"))),
        ]
        diff.note(FileEditResult(call_id="", path="/work/tree/a.py"))
        diff.refresh(emit, now=1.0)
        assert len(emit.batches) == 1
        diff.refresh(emit, now=MIN_REFRESH_SEC + 0.5)
        assert len(emit.batches) == 2

    def test_force_refreshes_the_final_diff_even_when_not_due(self) -> None:
        """A forced refresh at exit captures the last edit, dedup still applying."""
        diff, git = _opened(
            [(_has("diff", "--", "a.py"), _out(_diff("a.py", before="o", after="n")))],
        )
        diff.note(FileEditResult(call_id="", path="/work/tree/a.py"))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        git.responses = [
            _toplevel(),
            _head(),
            (
                _has("diff", "--", "a.py"),
                _out(_diff("a.py", before="o", after="final")),
            ),
        ]
        # Not due (not dirty, inside the interval), but forced at teardown.
        diff.refresh(emit, now=0.1, force=True)
        assert len(emit.batches) == 2
        assert emit.batches[1][0].edits[0].after == "final\n"

    def test_unchanged_diff_is_not_re_uploaded(self) -> None:
        """Dedup: an identical rendered diff after the interval uploads nothing."""
        diff, _ = _opened(
            [(_has("diff", "--", "a.py"), _out(_diff("a.py", before="o", after="n")))],
        )
        diff.note(FileEditResult(call_id="", path="/work/tree/a.py"))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        diff.refresh(emit, now=REFRESH_INTERVAL_SEC + 1.0)  # Same diff.
        assert len(emit.batches) == 1


class TestHeld:
    def test_held_predicate(self) -> None:
        """``held`` names env, key, credential and cluster-config files."""
        assert held(".env")
        assert held("sub/.env.local")
        assert held("deploy/id_rsa")
        assert held("certs/server.pem")
        assert held(".ssh/config")
        assert not held("src/main.py")
        assert not held("README.md")


class TestFileCases:
    """Per-file outcomes: untracked, unchanged, too large, binary, and the caps."""

    def _diffing(
        self,
        tmp_path: Path,
        responses: list[tuple[Callable[[tuple[str, ...]], bool], Output]],
    ) -> WorktreeDiff:
        return WorktreeDiff(top=tmp_path, run=_FakeGit(responses), base="HEAD")

    def test_untracked_file_is_diffed_against_nothing(self, tmp_path: Path) -> None:
        """A new file git does not track shows as an all-added diff."""
        (tmp_path / "new.py").write_text("x\n")
        diff = self._diffing(
            tmp_path,
            [
                (_has("ls-files"), _out(returncode=1)),  # `git` does not track it.
                (_has("--no-index"), _out("@@ -0,0 +1 @@\n+x\n")),
                # The tracked diff (no match) returns empty, so the untracked path runs.
            ],
        )
        diff.note(FileEditResult(call_id="", path=str(tmp_path / "new.py")))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        [record] = emit.batches[0]
        assert record.path == "new.py"
        assert record.edits[0].after == "x\n"

    def test_an_unchanged_tracked_file_is_skipped(self, tmp_path: Path) -> None:
        """A touched file with no net change is neither listed nor uploaded."""
        diff = self._diffing(
            tmp_path,
            [(_has("ls-files"), _out())],
        )  # Tracked, empty diff.
        diff.note(FileEditResult(call_id="", path=str(tmp_path / "same.py")))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        # An empty net diff dedups against the empty baseline: nothing uploads.
        assert emit.batches == []

    def test_a_large_file_is_a_note(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A file bigger than the source cap is reported omitted, not diffed."""
        monkeypatch.setattr(worktree_diff, "SOURCE_BYTES", 4)
        (tmp_path / "big.py").write_text("x" * 20)
        diff = self._diffing(tmp_path, [])
        diff.note(FileEditResult(call_id="", path=str(tmp_path / "big.py")))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        [record] = emit.batches[0]
        assert "large file" in (record.edits[0].lead or "")

    def test_a_binary_diff_is_a_note(self, tmp_path: Path) -> None:
        """A diff with no textual hunk (a binary file) is reported, not parsed."""
        diff = self._diffing(
            tmp_path,
            [(_has("diff"), _out("Binary files a/x.bin and b/x.bin differ\n"))],
        )
        diff.note(FileEditResult(call_id="", path=str(tmp_path / "x.bin")))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        [record] = emit.batches[0]
        assert "binary" in (record.edits[0].lead or "")

    def test_the_total_cap_stops_and_says_so(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once the diffs reach the total cap, the rest are reported omitted."""
        text = _diff("a.py", before="o", after="n")
        monkeypatch.setattr(worktree_diff, "TOTAL_BYTES", len(text.encode()))
        diff = self._diffing(tmp_path, [(_has("diff"), _out(text))])
        diff.note(FileEditResult(call_id="", path=str(tmp_path / "a.py")))
        diff.note(FileEditResult(call_id="", path=str(tmp_path / "b.py")))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        batch = emit.batches[0]
        assert len(batch) == 2
        assert "total size cap" in (batch[-1].edits[0].lead or "")

    def test_the_file_cap_counts_the_rest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Past the file cap, the remaining changed files are counted, not drawn."""
        monkeypatch.setattr(worktree_diff, "MAX_FILES", 1)
        diff = self._diffing(
            tmp_path,
            [(_has("diff"), _out(_diff("f", before="o", after="n")))],
        )
        diff.note(FileEditResult(call_id="", path=str(tmp_path / "a.py")))
        diff.note(FileEditResult(call_id="", path=str(tmp_path / "b.py")))
        emit = _Emits()
        diff.refresh(emit, now=0.0)
        batch = emit.batches[0]
        assert "more changed files" in (batch[-1].edits[0].lead or "")


class TestRunGit:
    """The real subprocess runner: bounded output, and a missing binary."""

    def test_reads_output_up_to_the_byte_cap(self) -> None:
        """Output is kept to ``max_bytes`` and the cut is reported."""
        done = run_git(["printf", "hello"], 100)
        assert done.returncode == 0
        assert done.stdout == b"hello"
        assert not done.cut
        cut = run_git(["printf", "hello"], 2)
        assert cut.stdout == b"he"
        assert cut.cut

    def test_a_missing_binary_is_not_an_exception(self) -> None:
        """A git that cannot start ends as ``-1``, not a crash."""
        done = run_git(["trax-run-no-such-binary-xyz"], 100)
        assert done.returncode == -1
        assert done.stdout == b""


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
