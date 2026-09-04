"""Tests for Claude session-file discovery."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    import pytest

from trackinizer.trax.run.adapters.claude import ClaudeAdapter


adapter = ClaudeAdapter()


class TestClaudeSessionId:
    """Claude's own session id is the ``<session-id>.jsonl`` filename stem."""

    def test_session_id_from_path_is_filename_stem(self) -> None:
        path = Path.home() / ".claude" / "projects" / "hash" / "abc-123-def.jsonl"
        assert adapter.session_id_from_path(path) == "abc-123-def"

    def test_session_id_from_non_jsonl_path_is_none(self, tmp_path: Path) -> None:
        assert adapter.session_id_from_path(tmp_path / "notes.txt") is None


class TestClaudeProjectsDir:
    """The projects root honors ``$CLAUDE_CONFIG_DIR`` (hermetic launchers)."""

    def test_claude_config_dir_env_locates_sessions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        project = tmp_path / ".claude" / "projects" / "hash"
        project.mkdir(parents=True)
        assert tuple(adapter.session_dirs()) == (tmp_path / ".claude" / "projects",)

    def test_watches_the_root_so_a_new_project_is_covered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
