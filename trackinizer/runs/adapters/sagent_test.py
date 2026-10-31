"""Tests for sagent journal discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.lib.userdirs import data_dir
from trackinizer.runs.adapters.custom_types import Subagent
from trackinizer.runs.adapters.sagent import SagentAdapter
from trackinizer.sagent.session.jsonl import record_line
from trackinizer.sagent.session.types import AssistantMessage, UserMessage


if TYPE_CHECKING:
    from pathlib import Path


def _journal(*names: str) -> Path:
    """Return ``<projects>/<project>/<names...>/session.jsonl``, made with its folders."""
    path = (data_dir() / "rekursiv-ai" / "sagent" / "projects" / "-work-repo").joinpath(
        *names,
        "session.jsonl",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


class TestWhere:
    """Journals live under sagent's projects root, one folder per project."""

    def test_the_root_is_sagents_projects_folder(self) -> None:
        assert tuple(SagentAdapter().session_dirs()) == (
            data_dir() / "rekursiv-ai" / "sagent" / "projects",
        )

    def test_a_journal_matches_and_its_log_does_not(self) -> None:
        journal = _journal("abc123def456")
        log = journal.with_name("session.log")
        log.touch()

        adapter = SagentAdapter()

        assert adapter.matches_session_file(journal)
        assert not adapter.matches_session_file(log)

    def test_a_journal_outside_the_root_does_not_match(self, tmp_path: Path) -> None:
        assert not SagentAdapter().matches_session_file(tmp_path / "session.jsonl")


class TestIds:
    """A session's id is its folder, below its project's."""

    def test_a_root_session_is_named_by_its_folder(self) -> None:
        assert SagentAdapter().session_id_from_path(_journal("abc123def456")) == (
            "abc123def456"
        )

    def test_a_spawned_agent_is_named_below_its_spawner(self) -> None:
        """Labels repeat across sessions, so the id carries the whole path."""
        child = _journal("abc123def456", "reviewer", "checker")

        assert SagentAdapter().session_id_from_path(child) == (
            "abc123def456/reviewer/checker"
        )

    def test_a_file_outside_the_root_has_no_id(self, tmp_path: Path) -> None:
        assert SagentAdapter().session_id_from_path(tmp_path / "session.jsonl") is None


class TestSubagents:
    def test_a_spawned_agent_names_its_spawner(self) -> None:
        child = _journal("abc123def456", "reviewer", "checker")

        assert SagentAdapter().subagent(child) == Subagent(
            child="abc123def456/reviewer/checker",
            parent="abc123def456/reviewer",
            note="checker",
        )

    def test_a_root_session_is_no_subagent(self) -> None:
        assert SagentAdapter().subagent(_journal("abc123def456")) is None


class TestResumed:
    """What ``sagent ARGS`` goes on with, as ``Adapter.resumed_session`` says it."""

    @pytest.mark.parametrize(
        "args",
        [(), ("--model", "openai/gpt-5.6-sol"), ("--tools", "Read", "Bash")],
    )
    def test_no_session_flag_starts_a_new_session(self, args: tuple[str, ...]) -> None:
        assert SagentAdapter().resumed_session(args) is None

    @pytest.mark.parametrize(
        "args",
        [
            ("--continue",),
            ("--resume",),
            ("--resume", "--model", "x"),
            ("--resume-all",),
            ("--continue-all",),
        ],
    )
    def test_a_session_sagent_picks_is_unnamed(self, args: tuple[str, ...]) -> None:
        assert SagentAdapter().resumed_session(args) == ""

    @pytest.mark.parametrize("spelling", ["split", "joined"])
    def test_resume_by_prefix_names_the_one_session_it_matches(
        self,
        spelling: str,
    ) -> None:
        journal = _journal("abc123def456")
        journal.write_bytes(record_line(UserMessage(content="hi")))
        args = ("--resume", "abc1") if spelling == "split" else ("--resume=abc1",)

        assert SagentAdapter().resumed_session(args) == "abc123def456"

    def test_resume_by_a_prefix_matching_nothing_is_unnamed(self) -> None:
        assert SagentAdapter().resumed_session(("--resume", "fff")) == ""

    def test_a_named_journal_holding_records_is_resumed(self) -> None:
        journal = _journal("abc123def456")
        journal.write_bytes(record_line(UserMessage(content="hi")))

        assert SagentAdapter().resumed_session(("--session", str(journal))) == (
            "abc123def456"
        )

    def test_a_named_journal_not_yet_written_is_a_new_session(self) -> None:
        journal = _journal("abc123def456")

        assert SagentAdapter().resumed_session(("--session", str(journal))) is None

    def test_a_named_journal_outside_the_root_is_unnamed(self, tmp_path: Path) -> None:
        journal = tmp_path / "session.jsonl"
        journal.write_bytes(record_line(UserMessage(content="hi")))

        assert SagentAdapter().resumed_session((f"--session={journal}",)) == ""


def test_the_reader_reads_each_line_as_the_record_sagent_wrote() -> None:
    records = [UserMessage(content="hi"), AssistantMessage(content="hello")]
    reader = SagentAdapter().reader()

    read = [
        record
        for line in b"".join(record_line(r) for r in records).decode().splitlines(True)
        for record in reader.feed(line)
    ]
    read += reader.close()

    assert read == records


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
