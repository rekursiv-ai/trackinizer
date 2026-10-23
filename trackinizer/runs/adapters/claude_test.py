"""Tests for Claude session-file discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from trackinizer.runs.adapters.claude import ClaudeAdapter


adapter = ClaudeAdapter()


class TestClaudeSessionId:
    """Claude's own session id is the ``<session-id>.jsonl`` filename stem."""

    def test_session_id_from_path_is_filename_stem(self) -> None:
        path = Path.home() / ".claude" / "projects" / "hash" / "abc-123-def.jsonl"  # noqa: TID251 -- Vendor fixed path, not ours (AGENTS.md rule 3).
        assert adapter.session_id_from_path(path) == "abc-123-def"

    def test_session_id_from_non_jsonl_path_is_none(self, tmp_path: Path) -> None:
        assert adapter.session_id_from_path(tmp_path / "notes.txt") is None


class TestClaudeProjectsDir:
    """The projects root honors ``$CLAUDE_CONFIG_DIR`` (hermetic launchers)."""

    def test_claude_config_dir_env_locates_sessions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A run under ``CLAUDE_CONFIG_DIR=<dir>`` must discover its projects.

        Study launchers spawn claude with a throwaway ``$CLAUDE_CONFIG_DIR``
        for hermeticity; an adapter hard-coded to ``~/.claude`` polls the
        wrong tree and captures nothing.
        """
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        project = tmp_path / "projects" / "-Users-x-repo"
        project.mkdir(parents=True)
        fixture = project / "abc-123-def.jsonl"
        fixture.write_text('{"type": "user"}\n')
        assert tuple(adapter.session_dirs()) == (tmp_path / "projects",)
        assert adapter.matches_session_file(fixture)

    def test_falls_back_to_home_claude_without_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        project = tmp_path / ".claude" / "projects" / "hash"
        project.mkdir(parents=True)
        assert tuple(adapter.session_dirs()) == (tmp_path / ".claude" / "projects",)

    def test_watches_the_root_so_a_new_project_is_covered(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The projects ROOT is returned, not the per-project subdirectories.

        Claude mints ``projects/<hashed-cwd>/`` the first time it runs in a
        workspace, which for the run being captured is after the watch was
        armed. Returning today's leaves leaves tomorrow's sibling unwatched
        and the run captures nothing silently.
        """
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        projects = tmp_path / "projects"
        (projects / "-one").mkdir(parents=True)
        (projects / "-two").mkdir()
        # One entry (the root), never one per project.
        assert tuple(adapter.session_dirs()) == (projects,)


_ID: Final = "0b3c2f7e-1d4a-4f5e-9a1b-2c3d4e5f6a7b"


class TestClaudeResumedSession:
    """Which session a claude command line continues, read the way claude reads it.

    Verified against claude 2.1.302: ``-r`` takes the rest of its short-flag
    cluster as its value (``-r<id>``, ``-pr <id>``), a session id is looked up
    exactly as typed, and any other value -- ``=<id>``, an uppercase or dashless
    id -- opens the picker with it as a search term.
    """

    @pytest.mark.parametrize(
        "cli_args",
        [
            ("--resume", _ID),
            (f"--resume={_ID}",),
            ("-r", _ID),
            (f"-r{_ID}",),
            ("-pr", _ID),
            ("--model", "haiku", "--resume", _ID, "hello"),
        ],
        ids=["split", "equals", "short", "attached", "cluster", "among-others"],
    )
    def test_a_resume_by_id_names_the_session(self, cli_args: tuple[str, ...]) -> None:
        assert adapter.resumed_session(cli_args) == _ID

    @pytest.mark.parametrize(
        "cli_args",
        [
            ("--continue",),
            ("-c",),
            ("-pc",),
            ("--resume",),
            ("-r",),
            ("--resume", "--model", "haiku"),
            ("--resume", "fix bug"),
            ("--resume=fix bug",),
            (f"-r={_ID}",),
            ("--resume", _ID.upper()),
            ("--resume", _ID.replace("-", "")),
        ],
        ids=[
            "continue",
            "short-continue",
            "cluster-continue",
            "picker",
            "short-picker",
            "picker-then-flag",
            "search",
            "equals-search",
            "short-equals",
            "uppercase",
            "dashless",
        ],
    )
    def test_a_resume_claude_picks_names_no_session(
        self,
        cli_args: tuple[str, ...],
    ) -> None:
        """An empty id: the run continues a session it cannot name."""
        assert adapter.resumed_session(cli_args) == ""

    @pytest.mark.parametrize(
        "cli_args",
        [
            (),
            ("--model", "haiku"),
            ("--resume", _ID, "--fork-session"),
            ("-c", "--fork-session"),
            ("-dr",),
            ("-n", "-c"),
            ("--", "-c"),
        ],
        ids=[
            "fresh",
            "options",
            "fork",
            "fork-continue",
            "debug-filter",
            "name-value",
            "after-double-dash",
        ],
    )
    def test_a_new_session_is_not_a_resume(self, cli_args: tuple[str, ...]) -> None:
        """A fork writes a NEW transcript, so it starts a session too."""
        assert adapter.resumed_session(cli_args) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
