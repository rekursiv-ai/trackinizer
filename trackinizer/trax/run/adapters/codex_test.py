"""Tests for Codex session-file discovery."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    import pytest

from trackinizer.trax.run.adapters.codex import CodexAdapter


class TestCodexSessionsDir:
    """The sessions root honors ``$CODEX_HOME`` (hermetic launchers set it)."""

    def test_codex_home_env_locates_sessions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
                "rollout-2026-09-01T13-57-15-01a05ec3-08b9-7542-9870-2839ffdbf7f3.jsonl"
            )
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
                "rollout-2026-09-01T13-57-15-01a06088-22fc-7121-9c25-263a54eaf330.jsonl"
            )
        )

        assert found == "01a06088-22fc-7121-9c25-263a54eaf330"


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
