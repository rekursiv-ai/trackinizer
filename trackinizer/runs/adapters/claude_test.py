"""Tests for Claude session-file discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import json

import pytest

from trackinizer.runs.adapters.claude import ClaudeAdapter
from trackinizer.runs.adapters.custom_types import Subagent
from trackinizer.sagent.session.types import UserMessage


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
        canvas, which for the run being captured is after the watch was
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


class TestClaudeSubagents:
    """A sub-agent's transcript is a session of its own, linked to its parent.

    Measured on claude 2.1.312: it lands at
    ``<project>/<session>/subagents/agent-<id>.jsonl``, beside a ``.meta.json``
    naming the call that spawned it and, below the first level, the agent that did.
    """

    @classmethod
    def _agent(
        cls,
        root: Path,
        agent: str,
        meta: dict[str, str | int] | None,
    ) -> tuple[ClaudeAdapter, Path]:
        folder = root / "projects" / "-w" / "s1" / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        if meta is not None:
            _ = (folder / f"agent-{agent}.meta.json").write_text(json.dumps(meta))
        return ClaudeAdapter(), folder / f"agent-{agent}.jsonl"

    def test_a_subagent_is_matched_and_names_its_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        meta = {
            "agentType": "general-purpose",
            "description": "Run echo",
            "toolUseId": "toolu_1",
            "spawnDepth": 1,
        }
        claude, path = self._agent(tmp_path, "a1", meta)

        assert claude.matches_session_file(path)
        assert claude.subagent(path) == Subagent(
            child="agent-a1",
            parent="s1",
            note="general-purpose: Run echo (toolu_1)",
        )

    def test_a_nested_subagent_names_the_agent_that_spawned_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        meta = {"description": "Dig", "parentAgentId": "a0", "spawnDepth": 2}
        claude, path = self._agent(tmp_path, "a2", meta)

        assert claude.subagent(path) == Subagent(
            child="agent-a2",
            parent="agent-a0",
            note="Dig",
        )

    def test_a_subagent_without_its_meta_still_names_its_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        claude, path = self._agent(tmp_path, "a1", None)
        _ = path.with_name("agent-a1.meta.json").write_text("{not json")

        assert claude.subagent(path) == Subagent(child="agent-a1", parent="s1")

    def test_a_session_file_and_a_side_file_are_no_subagents(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        claude = ClaudeAdapter()
        session = tmp_path / "projects" / "-w" / "s1.jsonl"
        side = tmp_path / "projects" / "-w" / "s1" / "tool-results" / "agent-x.jsonl"

        assert claude.matches_session_file(session)
        assert claude.subagent(session) is None
        assert not claude.matches_session_file(side)
        assert claude.subagent(side) is None

    def test_a_copy_outside_claudes_home_names_its_subagents_but_is_not_captured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
        folder = tmp_path / "Downloads" / "-w" / "s1" / "subagents"
        folder.mkdir(parents=True)
        _ = (folder / "agent-a2.meta.json").write_text('{"parentAgentId": "a1"}')
        claude = ClaudeAdapter()

        assert claude.subagent(folder / "agent-a2.jsonl") == Subagent(
            child="agent-a2",
            parent="agent-a1",
        )
        assert claude.subagent(folder / "agent-a1.jsonl") == Subagent(
            child="agent-a1",
            parent="s1",
        )
        assert not claude.matches_session_file(folder / "agent-a2.jsonl")
        assert not claude.matches_session_file(
            tmp_path / "Downloads" / "-w" / "s1.jsonl",
        )

    def test_a_subagent_deeper_in_claudes_home_is_not_captured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        deep = tmp_path / "projects" / "-w" / "x" / "s1" / "subagents" / "agent-a.jsonl"

        assert ClaudeAdapter().subagent(deep) is not None
        assert not ClaudeAdapter().matches_session_file(deep)

    # Measured on claude 2.1.312: 264 such transcripts in one home, none with a
    # ``parentAgentId``, each beside its workflow's ``journal.jsonl``.
    def test_a_workflows_agent_is_matched_and_names_its_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        subagents = tmp_path / "projects" / "-w" / "s1" / "subagents"
        folder = subagents / "workflows" / "wf_1"
        folder.mkdir(parents=True)
        meta = {"agentType": "workflow-subagent", "description": "audit:NODE"}
        _ = (folder / "agent-a1.meta.json").write_text(json.dumps(meta))
        claude = ClaudeAdapter()

        assert claude.matches_session_file(folder / "agent-a1.jsonl")
        assert claude.subagent(folder / "agent-a1.jsonl") == Subagent(
            child="agent-a1",
            parent="s1",
            note="workflow-subagent: audit:NODE",
        )
        assert claude.session_folder(folder / "journal.jsonl") == subagents.parent
        # A session's folder may be named as a workflow's parent is.
        named = tmp_path / "workflows" / "subagents" / "agent-a1.jsonl"
        assert claude.session_folder(named) == tmp_path / "workflows"
        assert claude.subagent(folder / "journal.jsonl") is None
        assert not claude.matches_session_file(folder / "journal.jsonl")
        assert claude.subagent(subagents / "other" / "wf_1" / "agent-a1.jsonl") is None
        assert (
            claude.subagent(
                subagents.parent / "side" / "workflows" / "wf_1" / "agent-a1.jsonl",
            )
            is None
        )

    def test_a_workflows_agent_deeper_in_claudes_home_is_not_captured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        deep = tmp_path / "projects" / "-w" / "x" / "s1" / "subagents" / "workflows"

        assert ClaudeAdapter().subagent(deep / "wf_1" / "agent-a.jsonl") is not None
        assert not ClaudeAdapter().matches_session_file(deep / "wf_1" / "agent-a.jsonl")

    def test_a_file_that_is_not_jsonl_is_no_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

        assert not ClaudeAdapter().matches_session_file(
            tmp_path / "projects" / "-w" / "s1.json",
        )


class TestClaudeScope:
    def test_the_scope_is_the_resolved_cwd_with_each_odd_character_dashed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Claude names the project after the cwd it RESOLVED at startup."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        real = tmp_path / "my repo.v2"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        monkeypatch.chdir(link)

        scope = ClaudeAdapter().session_scope()

        assert scope is not None
        assert scope.parent == tmp_path / "config" / "projects"
        resolved = str(real.resolve())
        assert scope.name.endswith("-my-repo-v2")
        assert len(scope.name) == len(resolved)
        assert all(c.isascii() and (c.isalnum() or c == "-") for c in scope.name)
        assert [i for i, c in enumerate(resolved) if c.isalnum()] == [
            i for i, c in enumerate(scope.name) if c.isalnum()
        ]


class TestClaudeReader:
    def test_each_file_gets_a_fresh_reader_of_claudes_lines(self) -> None:
        line = json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-11-06T12:00:00Z",
                "message": {"role": "user", "content": "hello"},
            },
        )
        reader = adapter.reader()

        read = [*reader.feed(line + "\n"), *reader.close()]

        assert [r.content for r in read if isinstance(r, UserMessage)] == ["hello"]
        assert adapter.reader() is not reader


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
