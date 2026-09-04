"""The resume tail: which targets are allowed, and how a lossy one is gated."""

from __future__ import annotations

from collections import Counter
from io import StringIO
from pathlib import Path
from typing import Final, cast
from uuid import UUID, uuid4

import pytest

from trackinizer.client.client import Client
from trackinizer.lib.agent.sessions import (
    claude as claude_ir,
    codex as codex_ir,
)
from trackinizer.lib.agent.types.sessions import UncategorizedRecord, UserMessage
from trackinizer.lib.custom_json import JSON, json_freeze
from trackinizer.trax.run.adapters.tail import Tail
from trackinizer.trax.run.errors import (
    LossyConversionError,
    NotResumableError,
)
from trackinizer.trax.run.resume import (
    _read_part,
    _undroppable,
    prepare_resume,
)
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.types.streams import TraxRecord
from trackinizer.wire.wire_session_ir import PartBody, RecordBody


# Asked of the MODULE that owns it, not counted in parents from here: the
# export republishes this tree one directory shallower, so a fixed hop
# count resolved outside the package and the fixtures vanished.
_TESTDATA: Final = Path(claude_ir.__file__).resolve().parent / "testdata"


def _records(name: str) -> tuple[list[TraxRecord], JSON]:
    """A corpus fixture's records and how its file spells its bytes.

    Fed a line at a time through :class:`Tail`, which is what capture does:
    the reader PULLS and the runner PUSHES, so driving the pull side directly
    would exercise a path the runner never takes.
    """
    reader = Tail((codex_ir if name.startswith("codex") else claude_ir).normalize)
    out: list[TraxRecord] = []
    with (_TESTDATA / name).open(encoding="utf-8") as handle:
        for line in handle:
            out.extend(reader.feed(line))
    out.extend(reader.close())
    return out, json_freeze(reader.encoding)


class _FakeClient:
    """Serves one stored session, recording what the resume stamps."""

    def __init__(self, name: str, *, session_format: str) -> None:
        self.records, self.encoding = _records(name)
        self._format = session_format
        self.stamped: list[str] = []

    def read_session_parts(self, session_id: UUID) -> list[PartBody]:
        del session_id
        return [
            PartBody(
                part=0,
                name="s.jsonl",
                format=self._format,
                records=len(self.records),
                metadata=self.encoding,
                ir_id=uuid4(),
            )
        ]

    def read_session_records(
        self, session_id: UUID, *, part: int = 0, **_: object
    ) -> list[RecordBody]:
        return [
            RecordBody.of(
                SessionRecordRow.of(
                    session_id=session_id, part=part, idx=idx, record=record
                )
            )
            for idx, record in enumerate(self.records)
        ]

    def set_cli_session_id(self, session_id: UUID, cli_session_id: str) -> None:
        del session_id
        self.stamped.append(cli_session_id)


@pytest.fixture(autouse=True)
def local_session_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Materialize into temp roots, never the operator's own.

    BOTH CLIs: each adapter derives its own directory, so isolating one leaves
    the other writing real rollouts into ``~/.codex`` -- which the operator's
    codex then offers in its resume picker, and which makes the suite's result
    depend on what is already on the machine.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


class TestWhichTargetsResume:
    """Only a CLI that names a session by a stable id can be re-entered."""

    @pytest.mark.parametrize("target", ["gemini", "sh"])
    def test_a_non_resumable_target_is_refused_by_name(self, target: str) -> None:
        """Refused with the target named, not a generic failure.

        Each still DOWNLOADS in its own format; what it lacks is a way to be
        handed back to a running process -- gemini rewrites one document in
        place, and a scrape has no native log at all.
        """
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        with pytest.raises(NotResumableError, match=target):
            prepare_resume(cast_client(client), uuid4(), target)

    def test_claude_resumes(self) -> None:
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        written = prepare_resume(cast_client(client), uuid4(), "claude")

        assert written.path.exists()

    def test_codex_resumes(self) -> None:
        """``codex resume <uuid>`` re-enters a rollout, so codex is a target."""
        client = _FakeClient("codex_main.jsonl", session_format="codex")

        written = prepare_resume(cast_client(client), uuid4(), "codex")

        assert written.path.exists()
        assert written.path.name.startswith("rollout-")

    def test_a_claude_capture_resumes_as_codex(self) -> None:
        """The crossing that was previously impossible in this direction.

        The target is chosen at RESUME time, not by what captured the session,
        so both directions have to work -- ``--lossy`` because a claude
        transcript holds acts a rollout cannot state.
        """
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        written = prepare_resume(cast_client(client), uuid4(), "codex", lossy=True)

        assert written.path.exists()
        assert client.stamped == [str(written.cli_session_id)]

    def test_a_session_with_no_native_part_is_refused(self) -> None:
        """An ``sh`` scrape is searchable and has no format to hand back."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="")

        with pytest.raises(NotResumableError, match="no part with a native format"):
            prepare_resume(cast_client(client), uuid4(), "claude")


class TestTheStampPrecedesTheRun:
    """The id is stamped BEFORE the runner opens its session."""

    def test_the_minted_id_is_stamped_on_the_row(self) -> None:
        """Without it the resumed run forks a second AgentSession."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        written = prepare_resume(cast_client(client), uuid4(), "claude")

        assert client.stamped == [str(written.cli_session_id)]

    def test_the_stamped_id_names_the_written_file(self) -> None:
        """``--resume <id>`` and the file on disk have to agree."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        written = prepare_resume(cast_client(client), uuid4(), "claude")

        assert written.path.stem == client.stamped[0]


class TestLossyConversion:
    """A cross-format resume that DROPS records needs the flag."""

    def test_a_claude_session_is_never_lossy(self) -> None:
        """Same format in and out: nothing to drop, no flag needed."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        assert prepare_resume(cast_client(client), uuid4(), "claude").path.exists()

    def test_a_lossy_cross_format_resume_is_refused_without_the_flag(self) -> None:
        """Measured by rewriting, not predicted from the format pair.

        A silently shortened transcript is a conversation the model never had,
        and nothing in the file says anything is missing.
        """
        client = _FakeClient("codex_main.jsonl", session_format="codex")

        with pytest.raises(LossyConversionError, match="--lossy"):
            prepare_resume(cast_client(client), uuid4(), "claude")

    def test_the_flag_accepts_the_loss(self) -> None:
        client = _FakeClient("codex_main.jsonl", session_format="codex")

        written = prepare_resume(cast_client(client), uuid4(), "claude", lossy=True)

        assert written.path.exists()

    def test_the_gate_measures_the_file_that_gets_written(self) -> None:
        """The loss report has to describe the FILE, not a dry run beside it.

        Materialization stamps identity before writing -- codex needs a
        ``session_meta`` line, synthesized when the source carried none -- and
        that line is what a ``ContextClear`` rides out on. Measuring the
        unstamped records reported a ``ContextClear`` drop that the written
        file does not have, so the gate refused a resume over a record it was
        about to preserve.
        """
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        written = prepare_resume(cast_client(client), uuid4(), "codex", lossy=True)

        rebuilt = Counter(
            type(record).__name__
            for record in codex_ir.normalize(
                StringIO(written.path.read_text(encoding="utf-8"))
            )
        )
        records, _ = _read_part(cast_client(client), uuid4(), 0)
        dropped = _undroppable(records, "claude", "codex")

        assert "ContextClear" not in dropped, (
            "the gate names a loss the written file does not have"
        )
        assert rebuilt["ContextClear"] == 1

    def test_derived_state_is_not_counted_as_loss(self) -> None:
        """A ``TurnContext`` is DERIVED, so a differing count is not a drop.

        Each adapter states settings in its own shape -- claude repeats an
        envelope per line, codex declares once per turn -- so the counts
        legitimately differ across a crossing while the conversation is
        untouched. Counting them made every claude-to-codex resume look lossy
        and demand ``--lossy`` for a transcript that loses no turn at all.
        """
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")
        records, _ = _read_part(cast_client(client), uuid4(), 0)

        dropped = _undroppable(records, "claude", "codex")

        assert "TurnContext" not in dropped

    def test_a_lost_act_is_still_reported(self) -> None:
        """The gate must still bite on an act the target cannot state.

        Loosening it to ignore derived state is only correct if a real loss
        still trips it, so this pins the half that must not move. An
        ``UncategorizedRecord`` is a line claude wrote and codex has no shape
        for at all -- measured, it writes nothing and reads back as nothing.
        """
        records = [
            UserMessage(content="kept"),
            UncategorizedRecord(kind="queue-operation"),
        ]

        dropped = _undroppable(records, "claude", "codex")

        assert "UncategorizedRecord" in dropped
        assert "UserMessage" not in dropped

    def test_a_refused_resume_stamps_nothing(self) -> None:
        """The gate runs BEFORE the stamp, so a refusal leaves the row alone.

        Stamping first would repoint the session at a file that was never
        written, and the next real resume would re-attach to nothing.
        """
        client = _FakeClient("codex_main.jsonl", session_format="codex")

        with pytest.raises(LossyConversionError):
            prepare_resume(cast_client(client), uuid4(), "claude")

        assert client.stamped == []


def cast_client(fake: _FakeClient) -> Client:
    """The fake, as the ``Client`` the resume path declares."""
    return cast(Client, fake)


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
