"""Writing a stored session back out as a file a CLI can resume."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import json

import pytest

from trackinizer.lib.agent.sessions import claude as claude_ir
from trackinizer.lib.agent.sessions.convert import detect_format
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    IncompleteRecord,
    SessionRecord,
    Thinking,
    TurnContext,
    UserMessage,
)
from trackinizer.lib.custom_json import JSON, DictCodec, json_freeze
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.errors import (
    CiphertextDroppedError,
    NotResumableError,
)
from trackinizer.trax.run.materialize import (
    RESUMABLE_TARGETS,
    materialize,
    materialize_claude,
)


# Asked of the MODULE that owns it, not counted in parents from here: the
# export republishes this tree one directory shallower, so a fixed hop count
# resolved outside the package and the fixture vanished.
_FIXTURE: Final = (
    Path(claude_ir.__file__).resolve().parent / "testdata" / "claude_sidechain.jsonl"
)


def _records() -> list[SessionRecord]:
    """A real captured transcript's records.

    Real rather than synthetic: claude's writer reconstructs LINES from the
    provider-native envelope each record carries in ``extra`` (``uuid``,
    ``parentUuid``, the key order), so hand-built records with no envelope
    merge into one line and the rewrite proves nothing about a real replay.
    """
    with _FIXTURE.open(encoding="utf-8") as handle:
        return list(claude_ir.normalize(handle))


def _encoding() -> JSON:
    """How that fixture spells its bytes, which the rewrite needs verbatim.

    The LAST context stating one: the escaping convention is a majority over
    the lines read, so the reader restates it as it moves and the final
    statement is the one in force for the whole file.
    """
    return next(
        record.encoding
        for record in reversed(_records())
        if isinstance(record, TurnContext) and record.encoding
    )


@pytest.fixture(autouse=True)
def local_session_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point BOTH CLIs' session roots at temp dirs for every test here.

    Each adapter derives its own, so isolating one leaves the other writing
    real files into the operator's home -- rollouts codex then offers in its
    own resume picker, and which make this suite's result depend on what is
    already on the machine.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


class TestTheFileIsThisMachinesOwn:
    """Three things make the written file resumable HERE, not where captured."""

    def test_the_session_id_is_minted_fresh(self) -> None:
        """Reusing the captured id would collide with the original locally.

        And for a codex capture it would name an id claude never issued, so
        the CLI would refuse a session it has no record of.
        """
        first = materialize_claude(records=_records(), encoding=_encoding())
        second = materialize_claude(records=_records(), encoding=_encoding())

        assert first.cli_session_id != second.cli_session_id

    def test_the_file_is_named_for_the_minted_id(self) -> None:
        """``--resume <uuid>`` finds the file by that name, so they must agree."""
        written = materialize_claude(records=_records(), encoding=_encoding())

        assert written.path.stem == str(written.cli_session_id)
        assert written.path.suffix == ".jsonl"

    def test_the_id_inside_the_file_is_rewritten(self) -> None:
        """The contents must agree with the name, or the CLI reads a stranger.

        Claude repeats ``sessionId`` on every line; a file whose lines still
        name the CAPTURED session is one the CLI will not associate with the
        id it was asked to resume.
        """
        written = materialize_claude(records=_records(), encoding=_encoding())

        declared = {
            json.loads(line)["sessionId"]
            for line in written.path.read_text(encoding="utf-8").splitlines()
        }
        assert declared == {str(written.cli_session_id)}

    def test_a_record_stating_no_id_is_still_named_by_the_file(self) -> None:
        """``_renamed`` rewrites an id a record HAS; the writer fills one it lacks.

        The writer's fallback is ``_FOREIGN_SEED`` -- a fixed namespace uuid --
        so a record carrying no ``sessionId`` lands under
        ``6ba7b811-9dad-11d1-80b4-00c04fd430c8`` rather than the file's own id.
        Measured on a real resume: a materialized transcript declared two ids,
        and claude, which filters its transcript by ``sessionId``, opened the
        session showing NONE of the conversation.

        Not every record carries one: the IR holds no identity, so a record the
        server returned without a provider residual states nothing to rewrite.
        """
        written = materialize_claude(
            records=[UserMessage(content="hi"), AssistantMessage(content="hello")],
            # A FOREIGN encoding, which is what makes the writer synthesize
            # claude's identity keys at all: a codex-captured session states
            # one, and that is the crossing this whole path exists for.
            encoding=json_freeze({"newline_terminated": True}),
        )

        declared = {
            json.loads(line).get("sessionId")
            for line in written.path.read_text(encoding="utf-8").splitlines()
        }
        assert declared == {str(written.cli_session_id)}

    def test_the_path_derives_from_the_local_cwd(self, tmp_path: Path) -> None:
        """Claude encodes the working directory in its project directory name.

        Resuming in a different directory is expected: the file lands in the
        project THIS cwd names, which is where the CLI will look for it.
        """
        written = materialize_claude(records=_records(), encoding=_encoding())

        assert written.path.parent.parent == tmp_path / "claude" / "projects"


class TestTheRewriteIsReadableBack:
    """What is written must normalize back to what went in."""

    def test_the_records_survive_the_round_trip(self) -> None:
        written = materialize_claude(records=_records(), encoding=_encoding())

        with written.path.open(encoding="utf-8") as handle:
            reread = list(claude_ir.normalize(handle))

        before = [r.content for r in _records() if isinstance(r, UserMessage)]
        after = [r.content for r in reread if isinstance(r, UserMessage)]
        assert before, "the fixture carries no user turns to compare"
        assert after == before, "the rewrite lost a turn"

    def test_the_reread_file_declares_the_minted_id(self) -> None:
        """The reader reads back the id the rewrite stamped in.

        Read off the records themselves: identity is not in the IR, so the id
        is only where claude states it -- the ``sessionId`` each line carries
        in its own residual.
        """
        written = materialize_claude(records=_records(), encoding=_encoding())

        with written.path.open(encoding="utf-8") as handle:
            declared = {
                DictCodec.coerce(getattr(record, "extra", None)).get("sessionId")
                for record in claude_ir.normalize(handle)
            }

        assert declared - {None} == {str(written.cli_session_id)}


class TestCiphertext:
    """Sealed reasoning is spliced at materialization, or the replay refuses."""

    def test_ciphertext_is_spliced_back_into_its_record(self) -> None:
        """The bytes live in another table; the file needs them inline."""
        sealed = "c2VhbGVkLXJlYXNvbmluZw=="
        written = materialize_claude(
            records=[Thinking(encrypted="")],
            encoding=json_freeze({}),
            sealed=[sealed],
        )

        assert sealed in written.path.read_text(encoding="utf-8")

    def test_a_dropped_ciphertext_refuses_rather_than_writing_a_hollow_file(
        self,
    ) -> None:
        """Retention can drop the bytes; a hollow transcript fails at the CLI.

        Raising names the cause. Writing an empty ``encrypted`` would fail
        inside the provider with nothing pointing back at retention.
        """
        with pytest.raises(CiphertextDroppedError, match="no longer stored"):
            materialize_claude(
                records=[Thinking(encrypted="")],
                encoding=json_freeze({}),
                sealed=[None],
            )

    def test_nothing_is_written_when_the_ciphertext_is_missing(
        self, tmp_path: Path
    ) -> None:
        """A half-written file would be captured as this run's own transcript."""
        with pytest.raises(CiphertextDroppedError):
            materialize_claude(
                records=[UserMessage(content="a"), Thinking(encrypted="")],
                encoding=json_freeze({}),
                sealed=[None, None],
            )

        assert not list((tmp_path / "claude" / "projects").rglob("*.jsonl"))

    def test_readable_thinking_needs_no_ciphertext(self) -> None:
        """A summarized reasoning block was never sealed, so it replays fine."""
        written = materialize_claude(
            records=[Thinking(content="visible reasoning")],
            encoding=json_freeze({}),
            sealed=[None],
        )

        assert "visible reasoning" in written.path.read_text(encoding="utf-8")


class TestResumableTargets:
    """A target is resumable when it names a session by a stable id."""

    def test_claude_is_resumable(self) -> None:
        assert "claude" in RESUMABLE_TARGETS

    def test_codex_is_resumable(self) -> None:
        """``codex resume <SESSION_ID>`` takes a uuid, and the rollout has one.

        Measured against the installed CLI (``codex resume --help``: "Session
        id (UUID) or session name") and a captured rollout, whose filename
        ends in the same uuid its launch line states as ``payload.id``. The
        earlier refusal cited ``session_id_from_path`` returning ``None`` --
        which was this adapter declining to parse a name it could read, not a
        property of codex.
        """
        assert "codex" in RESUMABLE_TARGETS

    def test_gemini_and_sh_are_not(self) -> None:
        """Neither names a session to re-enter.

        Gemini rewrites one document in place, and a scrape has no native log
        at all -- so there is no id to hand back, whatever the writer can
        produce.
        """
        assert not {"gemini", "sh"} & RESUMABLE_TARGETS


class TestMaterializingCodex:
    """A stored session written back as a rollout the codex CLI resumes."""

    def test_the_file_is_named_by_the_id_codex_resumes(self) -> None:
        """``codex resume`` finds a rollout by the uuid in its FILENAME.

        The whole stem, not just the uuid: codex globs ``rollout-*`` and the
        adapter's own ``matches_session_file`` requires that prefix, so a file
        named by the bare id is invisible to both.
        """
        written = materialize(
            target="codex",
            records=[UserMessage(content="hi")],
            encoding=json_freeze({}),
        )

        assert written.path.name.startswith("rollout-")
        assert str(written.cli_session_id) in written.path.name
        assert CodexAdapter().matches_session_file(written.path)

    def test_the_rollout_reads_back_as_codex(self) -> None:
        """Written in codex's own dialect, not claude's under a codex name."""
        written = materialize(
            target="codex",
            records=[UserMessage(content="hi")],
            encoding=json_freeze({}),
        )

        assert detect_format(written.path.read_text(encoding="utf-8")) == "codex"

    def test_the_declared_id_matches_the_filename(self) -> None:
        """A rollout states its own id; the CLI rejects one that disagrees.

        The same rule claude's ``_renamed`` enforces, for the same reason: a
        transcript naming the session it came from, under a filename naming a
        fresh one, resumes a session the local machine does not have.
        """
        written = materialize(
            target="codex",
            records=[UserMessage(content="hi")],
            encoding=json_freeze({}),
        )

        declared = json.loads(written.path.read_text(encoding="utf-8").splitlines()[0])
        payload = DictCodec.coerce(declared["payload"])
        assert payload["id"] == str(written.cli_session_id)

    def test_the_launch_payload_is_one_codex_will_load(self) -> None:
        """A rollout declaring only its id is refused by the CLI.

        Measured against the installed binary: a materialized rollout whose
        ``session_meta`` payload held ``{id, session_id}`` answered "No saved
        session found", while the same file with a real declaration -- cwd,
        originator, cli_version, source, thread_source, model_provider, and a
        top-level ``ordinal`` -- resumed. The payload is not decoration; it is
        what codex validates a session by.
        """
        written = materialize(
            target="codex",
            records=[UserMessage(content="hi")],
            encoding=json_freeze({}),
        )

        declared = json.loads(written.path.read_text(encoding="utf-8").splitlines()[0])
        payload = DictCodec.coerce(declared["payload"])
        assert declared["ordinal"] == 0
        assert set(payload) >= {
            "cli_version",
            "cwd",
            "id",
            "model_provider",
            "originator",
            "session_id",
            "source",
            "thread_source",
            "timestamp",
        }
        assert payload["cwd"] == str(Path.cwd())

    def test_a_record_with_no_timestamp_field_still_materializes(self) -> None:
        """Two members of the union declare no fields the others share.

        ``IncompleteRecord`` is a raw line the reader could not parse -- it
        holds ``text`` and nothing else, no ``timestamp`` and no ``extra`` --
        and a real session carried one, so stamping crashed the resume with
        ``AttributeError: 'IncompleteRecord' object has no attribute
        'timestamp'`` before anything reached disk. ``_renamed`` already reads
        its residual through ``getattr`` for the same reason.
        """
        written = materialize(
            target="codex",
            records=[IncompleteRecord(text="{"), UserMessage(content="hi")],
            encoding=json_freeze({}),
        )

        assert written.path.exists()

    def test_another_providers_ciphertext_is_not_replayed(self) -> None:
        """Reasoning bytes are sealed BY the provider that issued them.

        Claude's signature crossed into codex's ``encrypted_content`` and the
        resumed session failed on its first request: ``invalid_encrypted_content
        -- The encrypted content CAIS...AQ== could not be verified``. OpenAI
        cannot decrypt an Anthropic seal, so the bytes are not portable; the
        readable summary is, and it is what survives the crossing.
        """
        written = materialize(
            target="codex",
            records=[
                Thinking(summary="weighing it", encrypted="CAISsgIKpgEIERgCKkBjy88X"),
                UserMessage(content="hi"),
            ],
            encoding=json_freeze({}),
            # A claude-captured session, which is the only case that carries a
            # seal codex did not issue.
            source="claude",
        )

        assert "CAISsgIKpgEIERgCKkBjy88X" not in written.path.read_text(
            encoding="utf-8"
        )

    def test_an_unparsed_line_is_not_replayed_into_another_format(self) -> None:
        """``IncompleteRecord`` holds one provider's bytes, verbatim.

        Written into a codex rollout it landed mid-file with no newline and no
        outer envelope, so the line held THREE concatenated objects and no JSON
        reader could parse it. It replays only into the format it was read
        from; crossing, it is a drop the loss gate counts.
        """
        written = materialize(
            target="codex",
            records=[
                UserMessage(content="hi"),
                IncompleteRecord(text='{"type":"cost-state"}{"type":"atis-latch"}'),
            ],
            encoding=json_freeze({}),
            source="claude",
        )

        for line in written.path.read_text(encoding="utf-8").splitlines():
            _ = json.loads(line)

    def test_an_unparsed_record_holding_several_lines_is_dropped(self) -> None:
        """One record is one line, whatever format wrote it.

        A real capture stored 4498 characters -- a ``cost-state``, an
        ``atis-latch`` and a ``turn_context`` concatenated, no separators, no
        trailing newline -- under a single :class:`IncompleteRecord`, and no
        crossing was involved: the part's own format was codex, so a per-part
        rule replayed it verbatim and the rollout carried a line that parsed as
        none.

        Verbatim replay is what makes a same-format rewrite byte-exact, so a
        record that IS one line still replays whatever it says, valid or not.
        Only a record that cannot BE a line is dropped.
        """
        written = materialize(
            target="codex",
            records=[
                UserMessage(content="hi"),
                IncompleteRecord(text='{"type":"cost-state"}{"type":"atis-latch"}'),
            ],
            encoding=json_freeze({}),
            # NOT a crossing: the format that captured it is the one written.
            source="codex",
        )

        for line in written.path.read_text(encoding="utf-8").splitlines():
            _ = json.loads(line)

    def test_the_launch_line_carries_the_stamp_not_just_the_payload(self) -> None:
        """The OUTER ``timestamp``, which is the one that decides resumability.

        Measured against the installed binary on two copies of one rollout
        differing in this field alone: ``"timestamp":null`` answered "No saved
        session found", the ISO string resumed. Codex indexes a session by the
        stamp on the line, not the one inside the declaration, so the writer
        must be given the record's own field -- setting it in ``extra`` alone
        left the emitted line null.
        """
        written = materialize(
            target="codex",
            records=[UserMessage(content="hi")],
            encoding=json_freeze({}),
        )

        declared = json.loads(written.path.read_text(encoding="utf-8").splitlines()[0])
        assert isinstance(declared["timestamp"], str)
        assert declared["timestamp"].endswith("Z")

    def test_every_line_is_stamped_not_only_the_launch(self) -> None:
        """An unstamped transcript line is one codex does NOT replay.

        Measured against the installed binary on three rollouts of one
        conversation: stamping only the launch line resumed the session but
        answered "Unknown" when asked what the transcript said, while stamping
        every line answered from it. The session opens either way, so this is
        invisible to a check that only asks whether the CLI started.

        A record crossed in from another CLI may carry no stamp of its own --
        claude's transcript stamps its lines, but the IR does not require one --
        so the stamp is supplied here rather than assumed.
        """
        written = materialize(
            target="codex",
            records=[UserMessage(content="hi"), AssistantMessage(content="hello")],
            encoding=json_freeze({}),
        )

        lines = [
            json.loads(line)
            for line in written.path.read_text(encoding="utf-8").splitlines()
        ]
        assert all(isinstance(line.get("timestamp"), str) for line in lines)

    def test_a_captured_declaration_keeps_its_own_fields(self) -> None:
        """A codex capture already declares itself; only identity is restated.

        The synthesized payload is for a session crossed in from another CLI.
        Overwriting a real declaration would discard the cwd and provider the
        rollout was actually recorded under.
        """
        captured = TurnContext(
            extra=json_freeze(
                {
                    "payload": {
                        "id": "old",
                        "session_id": "old",
                        "cwd": "/elsewhere",
                        "originator": "codex-tui",
                        "cli_version": "0.150.1",
                        "source": "cli",
                        "thread_source": "user",
                        "model_provider": "openai",
                        "timestamp": "2026-01-01T00:00:00.000Z",
                    }
                }
            )
        )

        written = materialize(
            target="codex",
            records=[captured, UserMessage(content="hi")],
            encoding=json_freeze({}),
        )

        payload = DictCodec.coerce(
            json.loads(written.path.read_text(encoding="utf-8").splitlines()[0])[
                "payload"
            ]
        )
        assert payload["cwd"] == "/elsewhere"
        assert payload["id"] == str(written.cli_session_id)

    def test_the_session_is_added_to_the_index_codex_resumes_from(self) -> None:
        """``codex resume <id>`` looks the session up in an INDEX.

        Measured: a rollout sitting in ``sessions/`` with no
        ``session_index.jsonl`` entry answers "No saved session found", and a
        real rollout resumes in a fresh ``CODEX_HOME`` given only its index
        line. Writing the file is half the job.
        """
        written = materialize(
            target="codex",
            records=[UserMessage(content="hi")],
            encoding=json_freeze({}),
        )

        index = next(iter(CodexAdapter().session_dirs())).parent / "session_index.jsonl"
        entries = [
            json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()
        ]
        assert [entry["id"] for entry in entries] == [str(written.cli_session_id)]

    def test_the_index_is_appended_never_rewritten(self) -> None:
        """The operator's other sessions must survive a materialization."""
        index = next(iter(CodexAdapter().session_dirs())).parent / "session_index.jsonl"
        index.parent.mkdir(parents=True, exist_ok=True)
        _ = index.write_text(
            '{"id":"kept","thread_name":"prior","updated_at":"2026-01-01T00:00:00Z"}\n',
            encoding="utf-8",
        )

        written = materialize(
            target="codex",
            records=[UserMessage(content="hi")],
            encoding=json_freeze({}),
        )

        ids = [
            json.loads(line)["id"]
            for line in index.read_text(encoding="utf-8").splitlines()
        ]
        assert ids == ["kept", str(written.cli_session_id)]

    def test_an_unknown_target_is_refused(self) -> None:
        """Naming a target with no writer is a caller error, not an empty file."""
        with pytest.raises(NotResumableError, match="gemini"):
            _ = materialize(
                target="gemini",
                records=[UserMessage(content="hi")],
                encoding=json_freeze({}),
            )


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
