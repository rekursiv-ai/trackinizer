"""Tests for Codex session-file discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import json

import pytest

from trackinizer.runs.adapters.codex import CodexAdapter
from trackinizer.runs.adapters.custom_types import Subagent


class TestCodexSessionsDir:
    """The sessions root honors ``$CODEX_HOME`` (hermetic launchers set it)."""

    def test_codex_home_env_locates_sessions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A run under ``CODEX_HOME=<dir>`` must discover ``<dir>/sessions``.

        Study launchers spawn codex with a throwaway ``$CODEX_HOME`` for
        hermeticity; an adapter hard-coded to ``~/.codex`` polls the wrong tree
        and captures nothing.
        """
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        day = tmp_path / "sessions" / "2026" / "08" / "01"
        day.mkdir(parents=True)
        fixture = day / "rollout-2026-08-01T00-00-00-abc.jsonl"
        fixture.write_text('{"timestamp": "2026-08-01T00:00:00Z"}\n')
        adapter = CodexAdapter()
        assert tuple(adapter.session_dirs()) == (tmp_path / "sessions",)
        assert adapter.matches_session_file(fixture)

    def test_falls_back_to_home_codex_without_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        sessions = tmp_path / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        assert tuple(CodexAdapter().session_dirs()) == (sessions,)


class TestCodexSessionId:
    """The rollout's filename carries the id ``codex resume`` takes.

    ``rollout-<ISO>-<uuid>.jsonl``, and the launch line repeats that uuid as
    ``payload.id`` -- verified against a captured rollout. Returning ``None``
    made the CLI look un-resumable when the id was in the name all along.
    """

    def test_the_trailing_uuid_is_the_session_id(self) -> None:
        found = CodexAdapter().session_id_from_path(
            Path(
                "rollout-2026-09-01T13-57-15-01a05ec3-08b9-7542-9870-2839ffdbf7f3.jsonl",
            ),
        )

        assert found == "01a05ec3-08b9-7542-9870-2839ffdbf7f3"

    def test_a_name_without_a_uuid_names_nothing(self) -> None:
        """Refused rather than guessed: a partial id resumes the wrong session."""
        assert (
            CodexAdapter().session_id_from_path(Path("rollout-2026-08-01-abc.jsonl"))
            is None
        )

    def test_only_the_trailing_field_counts(self) -> None:
        """The timestamp holds digits and dashes too, so anchoring matters."""
        found = CodexAdapter().session_id_from_path(
            Path(
                "rollout-2026-09-01T13-57-15-01a06088-22fc-7121-9c25-263a54eaf330.jsonl",
            ),
        )

        assert found == "01a06088-22fc-7121-9c25-263a54eaf330"


_ID: Final = "01a14f1f-cca1-7071-803c-ab02f8457212"


class TestCodexResumedSession:
    """Which session a codex command line continues (``codex resume --help``).

    ``resume`` takes a session id, a session NAME, or ``--last``; only an id
    names a rollout the runner can find before codex starts. ``fork`` writes a
    new rollout, so it starts a session rather than continuing one.
    """

    @pytest.mark.parametrize(
        "cli_args",
        [
            ("resume", _ID),
            ("resume", _ID, "a prompt"),
            ("-c", "model_reasoning_effort=low", "resume", _ID),
            ("--no-daemon", "resume", _ID),
            ("exec", "resume", _ID),
        ],
        ids=["bare", "with-prompt", "after-config", "after-flag", "exec"],
    )
    def test_a_resume_by_id_names_the_session(self, cli_args: tuple[str, ...]) -> None:
        assert CodexAdapter().resumed_session(cli_args) == _ID

    @pytest.mark.parametrize(
        "cli_args",
        [
            ("resume",),
            ("resume", "--last"),
            ("resume", "--last", _ID),
            ("resume", "my-session-name"),
            ("resume", _ID.upper()),
        ],
        ids=["picker", "last", "last-wins", "name", "uppercase"],
    )
    def test_a_resume_codex_picks_names_no_session(
        self,
        cli_args: tuple[str, ...],
    ) -> None:
        assert CodexAdapter().resumed_session(cli_args) == ""

    @pytest.mark.parametrize(
        "cli_args",
        [(), ("fork", _ID), ("-m", "resume"), ("resume me",), ("exec", "hello")],
        ids=["fresh", "fork", "option-value", "prompt", "exec-prompt"],
    )
    def test_a_new_session_is_not_a_resume(self, cli_args: tuple[str, ...]) -> None:
        assert CodexAdapter().resumed_session(cli_args) is None


_THREAD: Final = "01a17e8a-5ff5-7900-9d2a-23da192fd7d9"
"""A spawned thread's id, which its rollout's name repeats."""


class TestCodexSubagents:
    """A spawned thread is a session of its own, linked to the thread that spawned it.

    Measured on codex 0.163.0: the rollout opens with ``session_meta``, whose
    ``source.subagent.thread_spawn`` names the parent thread.
    """

    @classmethod
    def _rollout(cls, root: Path, source: object) -> tuple[CodexAdapter, Path]:
        folder = root / "sessions" / "2026" / "10" / "27"
        folder.mkdir(parents=True)
        path = folder / f"rollout-2026-10-27T14-06-00-{_THREAD}.jsonl"
        meta = {"type": "session_meta", "payload": {"id": _THREAD, "source": source}}
        _ = path.write_text(json.dumps(meta) + "\n" + json.dumps({"type": "x"}) + "\n")
        return CodexAdapter(), path

    def test_a_spawned_thread_names_its_parent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        spawn = {
            "parent_thread_id": "01a17e85-a9b7-7230-a4b8-c29f9762ba25",
            "depth": 1,
            "agent_path": "/root/synthesis",
            "agent_nickname": "Bohr",
            "agent_role": None,
        }
        codex, path = self._rollout(tmp_path, {"subagent": {"thread_spawn": spawn}})

        assert codex.subagent(path) == Subagent(
            child=_THREAD,
            parent="01a17e85-a9b7-7230-a4b8-c29f9762ba25",
            note="Bohr (/root/synthesis)",
        )

    # The note is the edge's, and the server takes text there, never null.
    @pytest.mark.parametrize(
        ("named", "note"),
        [
            ({"agent_nickname": "Bohr"}, "Bohr"),
            (
                {"agent_path": "/root/synthesis", "agent_nickname": None},
                "/root/synthesis",
            ),
            ({}, ""),
        ],
        ids=["nickname", "path", "neither"],
    )
    def test_a_spawn_is_noted_by_as_much_of_its_name_as_it_gives(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        named: dict[str, str | None],
        note: str,
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        spawn = {"parent_thread_id": "01a17e85-a9b7-7230-a4b8-c29f9762ba25", **named}
        codex, path = self._rollout(tmp_path, {"subagent": {"thread_spawn": spawn}})

        subagent = codex.subagent(path)

        assert subagent is not None
        assert subagent.note == note

    @pytest.mark.parametrize("source", ["vscode", "cli", {"other": {}}])
    def test_a_thread_a_person_started_is_no_subagent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        source: object,
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        codex, path = self._rollout(tmp_path, source)

        assert codex.subagent(path) is None

    def test_a_missing_or_torn_rollout_is_no_subagent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        codex = CodexAdapter()
        torn = tmp_path / "sessions" / f"rollout-2026-10-27T14-06-00-{_THREAD}.jsonl"
        torn.parent.mkdir(parents=True)
        _ = torn.write_text('{"type": "session_meta", "pay')

        assert codex.subagent(torn) is None
        assert codex.subagent(torn.with_name("rollout-gone.jsonl")) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
