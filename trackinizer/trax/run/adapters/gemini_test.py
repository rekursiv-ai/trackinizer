"""Tests for the gemini adapter: where it looks and what it claims."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import hashlib


if TYPE_CHECKING:
    import pytest

from trackinizer.lib.agent.types.sessions import UserMessage
from trackinizer.trax.run.adapters.gemini import GeminiAdapter


class TestSessionDiscovery:
    """What the adapter watches and what it claims off that watch."""

    def test_the_watch_is_the_tmp_root_not_each_project_leaf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gemini mints ``<sha>/chats`` AFTER the watch is armed.

        A watch on the leaves existing at arming time cannot adopt the new
        sibling, so a first-ever run in a workspace would capture nothing.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert tuple(GeminiAdapter().session_dirs()) == (tmp_path / ".gemini" / "tmp",)

    def test_the_watch_root_is_returned_before_it_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The runner mints these; withholding an absent root disables capture."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (root,) = GeminiAdapter().session_dirs()
        assert not root.exists()

    def test_only_a_session_document_under_chats_matches(self, tmp_path: Path) -> None:
        """Widening the watch to the root must not widen what is captured."""
        adapter = GeminiAdapter()
        assert adapter.matches_session_file(tmp_path / "chats" / "session-a.json")
        assert not adapter.matches_session_file(tmp_path / "chats" / "other.json")
        assert not adapter.matches_session_file(tmp_path / "logs" / "session-a.json")
        assert not adapter.matches_session_file(tmp_path / "chats" / "session-a.jsonl")

    def test_the_scope_is_the_hash_of_the_resolved_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI hashes what IT resolved, so a symlinked cwd names a
        directory that never receives a write.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        real = tmp_path / "workspace"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        monkeypatch.chdir(link)
        digest = hashlib.sha256(str(real.resolve()).encode()).hexdigest()
        assert GeminiAdapter().session_scope() == tmp_path / ".gemini" / "tmp" / digest


class TestResumeCorrelation:
    def test_a_gemini_session_is_not_yet_resumable(self, tmp_path: Path) -> None:
        """The stem carries an id, but correlation is not wired for it.

        Returning it would make ``trax run --resume`` claim a session it
        cannot re-attach to.
        """
        assert GeminiAdapter().session_id_from_path(tmp_path / "session-x.json") is None


class TestReader:
    def test_each_file_gets_its_own_reader(self) -> None:
        """The reading state lives on the reader, not the adapter.

        A per-adapter counter let two concurrent session documents share one
        count (#498), which silently dropped the second document's turns.
        """
        adapter = GeminiAdapter()
        assert adapter.reader() is not adapter.reader()

    def test_a_rewritten_document_reads_as_the_session_it_now_holds(self) -> None:
        """Gemini rewrites ONE document, so a chunk is the whole session again.

        Fed as a continuation instead, the second read would resume a reader
        already at the end of the previous document and yield nothing at all --
        so a session that gained a turn would report none.
        """
        reader = GeminiAdapter().reader()
        one = '{"sessionId":"s","messages":[{"type":"user","content":"one"}]}'
        two = (
            '{"sessionId":"s","messages":[{"type":"user","content":"one"},'
            '{"type":"user","content":"two"}]}'
        )

        _ = list(reader.feed(one))
        after = [r for r in reader.feed(two) if isinstance(r, UserMessage)]

        assert [r.content for r in after] == ["one", "two"]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
