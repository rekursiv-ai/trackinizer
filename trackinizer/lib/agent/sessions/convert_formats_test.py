"""What each format can and cannot carry, measured through ``convert``.

Losslessness is not declared by an adapter -- ``_dropped`` re-normalizes the
converted text and compares record populations. These tests pin the claim
phase 3 rests on: gemini carries a session's acts.

Only the CLI dialects are here. A captured stream is not one -- it lives with
the tool that spawns the process, and its own tests pin what it can carry.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import inspect
import json

import pytest

from trackinizer.lib.agent.sessions import claude, codex, gemini
from trackinizer.lib.agent.sessions.convert import (
    _Adapter,
    _adapter,
    _dropped,
    _session_files,
    convert_file,
    detect_format,
    main,
)
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    ContextClear,
    SessionRecord,
    ToolCall,
    UserMessage,
)


def _session() -> list[SessionRecord]:
    """A small session carrying prose and a call."""
    return [
        UserMessage(content="do the thing"),
        AssistantMessage(content="running it"),
        ToolCall(call_id="t1", name="read_file", arguments={"path": "x"}),
    ]


def test_gemini_carries_every_record() -> None:
    """A session converted to gemini loses nothing measurable."""
    out = StringIO()
    gemini.denormalize(_session(), out)

    assert _dropped(_session(), out.getvalue(), "gemini") == ()


def test_a_gemini_document_is_sniffed() -> None:
    """A whole-document format needs its own detection.

    Every other native format is JSONL, so the sniffer walks lines. Gemini's
    first line is a fragment of one object and parses as nothing, which would
    otherwise fall through as unrecognized.

    Sniffed from a document gemini actually WROTE (round-tripped through the
    reader), not one synthesized here: a session built in memory declares no
    ``sessionId``, and it is the pair of keys that identifies the format.
    """
    native = '{"sessionId": "s1", "messages": [{"type": "user", "content": "hi"}]}'

    out = StringIO()
    gemini.denormalize(gemini.normalize(StringIO(native)), out)

    assert detect_format(out.getvalue()) == "gemini"


def test_unrecognized_text_names_no_format() -> None:
    """Detection declines rather than guessing.

    Plain text is not a session dialect, and answering with one would hand a
    caller an adapter that rewrites the file into a shape it never had.
    """
    assert detect_format("just some output\n") == ""


def test_every_offered_format_resolves_to_an_adapter() -> None:
    """The CLI's format list and the adapter table agree.

    They are separate declarations, so a format offered but missing from
    ``_adapter`` would silently fall through to the normalized JSON adapter
    and rewrite a session into the wrong shape. Read off ``main``'s own
    default so the list under test is the one users are given.
    """
    offered = inspect.signature(main).parameters["formats"].default

    for name in offered:
        adapter = _adapter(name)
        assert hasattr(adapter, "normalize")
        assert hasattr(adapter, "denormalize")
    assert set(offered) == {"claude", "codex", "gemini", "json"}


_CLAUDE_LINE = json.dumps(
    {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/w",
        "sessionId": "22222222-2222-2222-2222-222222222222",
        "version": "2.1.1",
        "type": "user",
        "message": {"role": "user", "content": "hi"},
        "uuid": "33333333-3333-3333-3333-333333333333",
        "timestamp": "2026-01-01T00:00:00Z",
    }
)


@pytest.mark.parametrize("target", ["gemini", "codex", "json"])
def test_every_offered_target_lands_a_file(tmp_path: Path, target: str) -> None:
    """A convert that reports success must have written something.

    An empty rendering is indistinguishable here from one the conversion
    already streamed to disk itself, so the empty case was skipped -- and a
    target whose rendering of a given session IS empty reported success having
    produced no file at all.
    """
    source = tmp_path / "s.jsonl"
    _ = source.write_text(f"{_CLAUDE_LINE}\n")
    out_dir = tmp_path / "out"

    code = main(
        [
            "convert",
            str(source),
            "--to",
            target,
            "--out-dir",
            str(out_dir),
            "-q",
            "--lossy",
        ]
    )

    assert code == 0
    assert [path.name for path in out_dir.iterdir()]


def test_a_gemini_document_is_found_when_a_tree_is_walked(tmp_path: Path) -> None:
    """A session is discovered by being a session, not by its extension.

    Every other format is JSONL, so the walk globbed ``*.jsonl`` -- and a
    gemini document, which is one ``.json`` object, was invisible to it: a
    directory of them reported no session files at all.
    """
    _ = (tmp_path / "s.json").write_text(
        '{"sessionId":"s1","messages":[{"type":"user","content":"hi"}]}'
    )

    assert [path.name for path in _session_files([tmp_path])] == ["s.json"]


def test_a_conversion_that_only_renumbers_contexts_is_not_lossy() -> None:
    """``context_id`` is an INDEX, so renumbering it loses no record.

    Axiom 5: a record names the settings that applied BY INDEX, and never
    copies them. A session carrying no :class:`TurnContext` -- every gemini one
    and every hand-built one -- gains one when written as claude, moving every
    record's ``context_id`` from ``None`` to ``0``. The comparator read that as
    three records lost, so the CLI refused a conversion that dropped nothing.
    """
    session = _session()

    out = StringIO()
    claude.denormalize(session, out)

    assert _dropped(session, out.getvalue(), "claude") == ()


@pytest.mark.parametrize("body", [b"\xff\xfe\x00binary", b"{}"], ids=["binary", "text"])
def test_an_unreadable_json_does_not_abort_the_walk(
    tmp_path: Path, body: bytes
) -> None:
    """Discovery must survive a file it cannot read, not die on it.

    Widening the walk to ``.json`` made it OPEN each candidate to sniff, and
    discovery runs before the per-file error handling ``convert_file`` has --
    so one non-UTF-8 blob or one chmod-000 file anywhere under a corpus root
    raised out of ``main`` and took every other session with it.
    """
    project = tmp_path / "project"
    project.mkdir()
    _ = (project / "s.jsonl").write_text('{"type":"user","sessionId":"s","uuid":"u"}\n')
    blocked = project / "blob.json"
    _ = blocked.write_bytes(body)
    if body == b"{}":
        blocked.chmod(0o000)
    try:
        assert [path.name for path in _session_files([tmp_path])] == ["s.jsonl"]
    finally:
        blocked.chmod(0o644)


def test_a_named_unreadable_file_reports_why_not_a_wrong_shape(
    tmp_path: Path,
) -> None:
    """Declining to DISCOVER a file is not the same as failing to read one.

    Sniffing answers ``""`` for a file it could not open as well as for one
    whose content names no format, so a file the caller named outright was
    reported as "unrecognized session format" -- telling an operator their
    unreadable file was the wrong shape.
    """
    path = tmp_path / "blob.json"
    _ = path.write_bytes(b"\xff\xfe\x00binary")

    error = convert_file(path, "auto", "json", False).error

    assert error is not None
    assert error.startswith("UnicodeDecodeError")


def test_a_json_sidecar_beside_a_transcript_is_not_a_session(tmp_path: Path) -> None:
    """Widening the walk to ``.json`` must not sweep in the CLI's own files.

    Claude keeps ``sessions-index.json`` and ``agent-<id>.meta.json`` beside
    the transcripts, so an extension-only rule reported 46 of them as sessions
    that then failed to convert -- because they never were any.
    """
    # In a SUBDIRECTORY: a directory holding transcripts directly is itself one
    # session, and the walk that widened to ``.json`` is the recursive branch.
    project = tmp_path / "project"
    project.mkdir()
    _ = (project / "sessions-index.json").write_text('{"sessions": []}')
    _ = (project / "s.jsonl").write_text('{"type":"user","sessionId":"s","uuid":"u"}\n')

    assert [path.name for path in _session_files([tmp_path])] == ["s.jsonl"]


@pytest.mark.parametrize(
    ("adapter", "native"),
    [
        pytest.param(
            codex,
            '{"type":"session_meta","payload":{"session_id":"s1","id":"s1",'
            '"base_instructions":"XYZZY"}}\n'
            '{"type":"response_item","payload":{"type":"message","role":"user",'
            '"content":[{"type":"input_text","text":"hi"}]}}\n',
            id="codex",
        ),
        pytest.param(claude, _CLAUDE_LINE + "\n", id="claude"),
        pytest.param(
            gemini,
            '{"sessionId":"s1","messages":[{"type":"user","content":"hi"}]}',
            id="gemini",
        ),
    ],
)
def test_every_session_opens_with_a_clear(adapter: _Adapter, native: str) -> None:
    """One rule for every vendor: a session BEGINS by stating its context.

    The IR exists so a consumer answers "what did the model start from"
    without knowing which CLI wrote the file. Implemented for codex alone,
    that question had a different answer per provider -- which is the
    asymmetry the IR is for.
    """
    records = list(adapter.normalize(StringIO(native)))

    # After the ``TurnContext``: settings are stated before the acts they
    # govern, and the clear is one of those acts.
    assert isinstance(records[1], ContextClear)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
