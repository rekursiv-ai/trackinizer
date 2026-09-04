"""The record grammar every adapter obeys, whatever CLI wrote the file.

A session is its RECORDS -- no wrapping object, no metadata beside them -- so
everything a consumer needs is stated IN the stream, by a record that precedes
what it applies to:

* :class:`TurnContext` states the settings in force, and recurs when they
  change (a model swap, a different CLI, a new escaping convention).
* :class:`ContextClear` states the context a window opens with, and recurs at
  every reset.

These tests pin that shape across all five formats. They are the reason the
adapters can share one reader interface at all: a consumer that knows the
grammar never asks which provider it is reading.
"""

from __future__ import annotations

from io import StringIO

import json

import pytest

from trackinizer.lib.agent.sessions import claude, codex, gemini
from trackinizer.lib.agent.sessions.convert import _Adapter
from trackinizer.lib.agent.types.sessions import (
    ContextClear,
    SessionRecord,
    TurnContext,
)


_CLAUDE = (
    json.dumps(
        {
            "parentUuid": None,
            "isSidechain": False,
            "type": "user",
            "message": {"role": "user", "content": "hi"},
            "timestamp": "2026-01-01T00:00:00Z",
            "userType": "external",
            "cwd": "/w",
            "sessionId": "22222222-2222-2222-2222-222222222222",
            "version": "2.1.1",
            "uuid": "33333333-3333-3333-3333-333333333333",
        },
        separators=(",", ":"),
    )
    + "\n"
)

_CODEX = (
    '{"type":"session_meta","payload":{"session_id":"s1","id":"s1",'
    '"base_instructions":"XYZZY"}}\n'
    '{"type":"response_item","payload":{"type":"message","role":"user",'
    '"content":[{"type":"input_text","text":"hi"}]}}\n'
)

_GEMINI = '{"sessionId":"s1","messages":[{"type":"user","content":"hi"}]}'


def _adapters() -> tuple[tuple[str, _Adapter, str], ...]:
    """Every native format, with a minimal session in it.

    The CLI dialects only. A captured stream obeys the same grammar but is not
    a dialect -- it lives with the tool that spawns the process
    (``trackinizer.trax.run.adapters.scrape``), which ``lib`` cannot
    import, and its own tests pin the grammar there.
    """
    return (
        ("codex", codex, _CODEX),
        ("claude", claude, _CLAUDE),
        ("gemini", gemini, _GEMINI),
    )


def _cases() -> list[object]:
    """One parameter set per native format."""
    return [
        pytest.param(adapter, native, id=name) for name, adapter, native in _adapters()
    ]


def _read(adapter: _Adapter, native: str) -> list[SessionRecord]:
    """Every record a native session yields, drained from the iterator."""
    return list(adapter.normalize(StringIO(native)))


@pytest.mark.parametrize(("adapter", "native"), _cases())
def test_normalize_yields_records_rather_than_a_session(
    adapter: _Adapter, native: str
) -> None:
    """``normalize`` is a stream, so a tailer sees a record when it lands.

    Returning one object meant nothing reached the caller until EOF -- and a
    live session never reaches EOF, which is the whole reason a second reader
    interface existed. The iterator IS that interface.
    """
    records = _read(adapter, native)

    assert records
    assert not hasattr(records[0], "records")


@pytest.mark.parametrize(("adapter", "native"), _cases())
def test_settings_precede_the_acts_they_govern(adapter: _Adapter, native: str) -> None:
    """A ``TurnContext`` opens the stream: state before what it applies to."""
    records = _read(adapter, native)

    assert isinstance(records[0], TurnContext)


@pytest.mark.parametrize(("adapter", "native"), _cases())
def test_a_window_opens_with_the_context_it_begins_from(
    adapter: _Adapter, native: str
) -> None:
    """A ``ContextClear`` follows the settings and precedes the turns."""
    records = _read(adapter, native)

    assert isinstance(records[1], ContextClear)


@pytest.mark.parametrize(("adapter", "native"), _cases())
def test_the_opening_context_states_the_file_encoding(
    adapter: _Adapter, native: str
) -> None:
    """Encoding rides the settings, because a rewrite needs it and it moves.

    Claude spells non-ASCII by a MAJORITY over its lines, so the convention is
    not knowable at line one; a later ``TurnContext`` restates it and
    supersedes. Stating it here is what lets a writer reproduce the provider's
    own bytes without a session object to hang it on.
    """
    records = _read(adapter, native)

    opening = records[0]
    assert isinstance(opening, TurnContext)
    assert "newline_terminated" in opening.encoding


def test_a_declared_system_prompt_reaches_the_opening_clear() -> None:
    """Codex declares instructions on its launch line; the clear states them."""
    records = _read(codex, _CODEX)

    opening = records[1]
    assert isinstance(opening, ContextClear)
    assert opening.system_prompt == "XYZZY"


def test_the_opening_clear_states_what_the_fresh_context_was_given() -> None:
    """EVERY provider's, not only the one that names a prompt field.

    The clear is what delineates a session -- "what was the model looking at"
    is this record plus everything after it -- so an adapter whose CLI spreads
    the opening instructions over several lines must ASSEMBLE them. Claude
    injects its skills, subagents, and tool availability as ``attachment``
    lines at the head of the file; left as loose state records, the one record
    a consumer reads to delineate the session carried nothing at all.
    """
    native = (
        json.dumps(
            {
                "type": "attachment",
                "attachment": {"type": "skill_listing", "content": "- tdd: write it"},
                "uuid": "11111111-1111-1111-1111-111111111111",
                "timestamp": "2026-01-01T00:00:00Z",
            },
            separators=(",", ":"),
        )
        + "\n"
        + _CLAUDE
    )

    records = _read(claude, native)

    opening = records[1]
    assert isinstance(opening, ContextClear)
    assert opening.system_prompt is not None
    assert "- tdd: write it" in opening.system_prompt


def test_the_opening_clear_gathers_every_instruction_not_just_a_declared_one() -> None:
    """Codex names ``base_instructions`` AND sends more before the first turn.

    Its skills block and role prompt arrive as ordinary system messages ahead
    of any act -- given to the fresh context exactly as the declared prompt
    was. Taking only the declared one made the delineating record describe
    part of what the model was looking at, which is worse than none: a
    consumer cannot tell the difference.
    """
    native = (
        '{"type":"session_meta","payload":{"session_id":"s1","id":"s1",'
        '"base_instructions":"XYZZY"}}\n'
        '{"type":"response_item","payload":{"type":"message","role":"system",'
        '"content":[{"type":"input_text","text":"<skills>tdd</skills>"}]}}\n'
        '{"type":"response_item","payload":{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"hi"}]}}\n'
    )

    records = _read(codex, native)

    opening = records[1]
    assert isinstance(opening, ContextClear)
    assert opening.system_prompt is not None
    assert "XYZZY" in opening.system_prompt
    assert "<skills>tdd</skills>" in opening.system_prompt


def test_a_compaction_within_one_file_opens_a_window_too() -> None:
    """One rule for every reset, whether or not the file changed.

    Claude compacts IN PLACE as well as across a seam: it writes the summary
    as a user turn flagged ``isCompactSummary``. Only the seam case reached a
    ``ContextClear``, so a compaction inside one transcript left "the last
    clear plus everything after it" naming the whole session -- the summary
    was in the stream but nothing said a window had opened.
    """
    native = _CLAUDE + json.dumps(
        {
            "parentUuid": "33333333-3333-3333-3333-333333333333",
            "isSidechain": False,
            "type": "user",
            "message": {"role": "user", "content": "Summary: we said hi."},
            "isCompactSummary": True,
            "timestamp": "2026-01-01T00:00:01Z",
            "userType": "external",
            "cwd": "/w",
            "sessionId": "22222222-2222-2222-2222-222222222222",
            "version": "2.1.1",
            "uuid": "44444444-4444-4444-4444-444444444444",
        },
        separators=(",", ":"),
    )

    records = _read(claude, native)

    kinds = [type(record).__name__ for record in records]
    assert "ContextCompaction" in kinds, kinds
    at = kinds.index("ContextCompaction")
    assert kinds[at + 1] == "ContextClear"
    window = records[at + 1]
    assert isinstance(window, ContextClear)
    assert window.summary == "Summary: we said hi."


def test_a_compaction_is_followed_by_the_window_it_opened() -> None:
    """The event, then the context that replaced the history.

    One rule for "what was the model looking at": the last ``ContextClear``
    plus every record after it. A compaction that carried its own summary
    instead would need a second rule, and a consumer would have to know which
    kind of reset it was looking at.
    """
    native = (
        '{"type":"session_meta","payload":{"session_id":"s1","id":"s1"}}\n'
        '{"type":"compacted","payload":{"message":"","replacement_history":['
        '{"type":"message","role":"user","content":[{"type":"input_text",'
        '"text":"keep me"}]},'
        '{"type":"compaction","encrypted_content":"sealed"}]}}\n'
    )

    records = _read(codex, native)

    kinds = [type(record).__name__ for record in records]
    assert "ContextCompaction" in kinds
    at = kinds.index("ContextCompaction")
    assert kinds[at + 1] == "ContextClear"
    window = records[at + 1]
    assert isinstance(window, ContextClear)
    assert window.summary == "sealed"
    assert [type(r).__name__ for r in window.history] == ["UserMessage"]


@pytest.mark.parametrize(("adapter", "native"), _cases())
def test_a_session_rewrites_to_the_bytes_it_was_read_from(
    adapter: _Adapter, native: str
) -> None:
    """The grammar costs no bytes: every added record is derived."""
    out = StringIO()

    adapter.denormalize(_read(adapter, native), out)

    assert out.getvalue() == native


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
