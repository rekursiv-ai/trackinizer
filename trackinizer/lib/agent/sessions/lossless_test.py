"""Falsifying round-trip tests: every input must survive normalize/denormalize.

A captured corpus can only CONFIRM. Every shape it happens not to contain --
a blank line, an empty content list, an unknown block -- is a claim nobody
tested, and three separate losslessness bugs hid in exactly that gap. These
inputs are synthesized to contain what the corpus does not, and each asserts
byte equality on the whole stream rather than on a filtered subset.

The filtering is the specific trap: an assertion that drops blank lines before
comparing cannot fail on a dropped blank line.
"""

from __future__ import annotations

from io import StringIO

import pytest

from trackinizer.lib.agent.sessions import claude, codex, normalized
from trackinizer.lib.agent.sessions.convert import _Adapter


# Key order is the CLI's, not ours: across 40,695 corpus lines only one of 46
# distinct key-sets was ever written in two orders, so a hand-ordered record is
# a shape no provider emits and proves nothing about a real file.
CLAUDE_RECORD = (
    '{"parentUuid":null,"isSidechain":false,"type":"user",'
    '"message":{"role":"user","content":"Hi."},"uuid":"u1",'
    '"timestamp":"2026-08-24T19:34:41.000Z","userType":"external",'
    '"cwd":"/w","sessionId":"s1","version":"2.1.241"}'
)
CODEX_RECORD = (
    '{"timestamp":"2026-08-24T19:34:41.197Z","type":"response_item",'
    '"payload":{"type":"message","id":"u1","role":"user",'
    '"content":[{"type":"input_text","text":"Hi."}]}}'
)
CODEX_META = (
    '{"timestamp":"2026-08-24T19:34:39.215Z","type":"session_meta",'
    '"payload":{"session_id":"s1","id":"s1",'
    '"timestamp":"2026-08-24T19:34:39.072Z","cwd":"/w"}}'
)


def _claude_turn(role: str, content: str) -> str:
    """Return one claude transcript line carrying ``content``."""
    return (
        '{"parentUuid":null,"isSidechain":false,"type":"' + role + '",'
        '"message":{"role":"' + role + '","content":' + content + "},"
        '"uuid":"u1","timestamp":"2026-08-24T19:34:41.000Z",'
        '"userType":"external","cwd":"/w","sessionId":"s1","version":"2.1.241"}\n'
    )


def _round_trip(adapter: _Adapter, native: str) -> str:
    """Return ``native`` after one normalize/denormalize cycle."""
    records = list(adapter.normalize(StringIO(native)))
    out = StringIO()
    adapter.denormalize(records, out)
    return out.getvalue()


@pytest.mark.parametrize(
    "shape",
    [
        "{record}\n",
        "\n{record}\n",
        "{record}\n\n",
        "\n\n{record}\n\n\n",
        "{record}\n   \n{record}\n",
        "\t\n{record}\n",
    ],
)
def test_claude_preserves_blank_and_whitespace_lines(shape: str) -> None:
    native = shape.format(record=CLAUDE_RECORD)

    assert _round_trip(claude, native) == native


@pytest.mark.parametrize(
    "shape",
    [
        "{meta}\n{record}\n",
        "\n{meta}\n{record}\n",
        "{meta}\n\n{record}\n",
        "{meta}\n{record}\n\n",
    ],
)
def test_codex_preserves_blank_and_whitespace_lines(shape: str) -> None:
    native = shape.format(meta=CODEX_META, record=CODEX_RECORD)

    assert _round_trip(codex, native) == native


def test_a_stream_without_a_trailing_newline_round_trips() -> None:
    cases: tuple[tuple[_Adapter, str], ...] = (
        (claude, CLAUDE_RECORD),
        (codex, CODEX_META + "\n" + CODEX_RECORD),
    )
    for adapter, native in cases:
        assert _round_trip(adapter, native) == native


@pytest.mark.parametrize(
    "native",
    [
        _claude_turn("user", "[]"),
        _claude_turn("user", '""'),
        _claude_turn("assistant", "[]"),
        _claude_turn("user", '[{"type":"future_block","x":1}]'),
    ],
    ids=["user-empty-list", "user-empty-string", "assistant-empty", "unknown-block"],
)
def test_claude_preserves_records_with_no_semantic_content(native: str) -> None:
    # A record whose content maps to nothing must still replay: dropping it
    # loses a real turn, and no byte test over the corpus catches it because
    # the corpus has no empty-content records.
    assert _round_trip(claude, native) == native


@pytest.mark.parametrize(
    "native",
    [
        _claude_turn("user", "[{}]"),
        _claude_turn("user", '[{"type":"text","text":""}]'),
        _claude_turn(
            "user",
            '[{"type":"tool_result","tool_use_id":"c1","content":"one"},'
            '{"type":"tool_result","tool_use_id":"c2","content":"two"}]',
        ),
        _claude_turn(
            "user",
            '[{"type":"tool_result","tool_use_id":"c1","content":['
            '{"type":"image","source":{"type":"base64","data":"!",'
            '"media_type":"image/png"}}]}]',
        ),
        _claude_turn(
            "assistant",
            '[{"type":"thinking","thinking":"","signature":""}]',
        ),
        (
            '{"parentUuid":null,"isSidechain":false,"type":"assistant",'
            '"message":{"role":"assistant","content":[]},"uuid":"u1",'
            '"timestamp":"2026-08-24T19:34:41.000Z","effort":"future",'
            '"userType":"external","cwd":"/w","sessionId":"s1",'
            '"version":"2.1.241"}\n'
        ),
        (
            '{"parentUuid":null,"isSidechain":false,"type":"user",'
            '"message":{"role":"user","content":"Hi."},"uuid":"u1",'
            '"timestamp":null,"userType":"external","cwd":"/w",'
            '"sessionId":"s1","version":"2.1.241"}\n'
        ),
    ],
    ids=[
        "empty-object-block",
        "empty-text-block",
        "multiple-tool-results",
        "malformed-base64",
        "empty-signature",
        "unknown-effort",
        "null-timestamp",
    ],
)
def test_claude_preserves_edge_shape_bytes(native: str) -> None:
    assert _round_trip(claude, native) == native


def _codex_item(payload: str) -> str:
    """Return one codex response-item line carrying ``payload``."""
    return (
        '{"timestamp":"2026-08-24T19:34:41.197Z","type":"response_item",'
        '"payload":' + payload + "}\n"
    )


@pytest.mark.parametrize(
    "native",
    [
        CODEX_META
        + "\n"
        + _codex_item('{"type":"message","id":"u","role":"user","content":[]}'),
        CODEX_META + "\n" + _codex_item('{"type":"future"}'),
    ],
    ids=["empty-content", "unknown-item"],
)
def test_codex_preserves_records_with_no_semantic_content(native: str) -> None:
    assert _round_trip(codex, native) == native


def test_codex_survives_a_malformed_nested_arguments_string() -> None:
    # ``arguments`` is provider-supplied JSON inside JSON. A malformed one
    # must not abort the whole file, and must replay as the provider wrote it
    # rather than as the empty object it parses to.
    native = (
        CODEX_META
        + "\n"
        + _codex_item(
            '{"type":"function_call","call_id":"c","name":"n","arguments":"{"}'
        )
    )

    assert _round_trip(codex, native) == native


@pytest.mark.parametrize(
    "arguments",
    ['{\\"cmd\\":\\"ls\\",\\"n\\":1}', '{\\"cmd\\": \\"ls\\", \\"n\\": 1}'],
    ids=["compact", "spaced"],
)
def test_codex_keeps_the_spacing_of_a_nested_argument_string(arguments: str) -> None:
    # ``arguments`` is a STRING in the line, so its own separator spacing is
    # part of the bytes. The corpus writes both: 3587 compact against 41
    # spaced, so re-spacing one silently rewrites the other.
    # Key order is codex's own: every one of 6518 captured ``function_call``
    # payloads states the name and arguments before the call they answer.
    native = (
        CODEX_META
        + "\n"
        + _codex_item(
            '{"type":"function_call","name":"n",'
            '"arguments":"' + arguments + '","call_id":"c"}'
        )
    )

    assert _round_trip(codex, native) == native


@pytest.mark.parametrize(
    ("adapter", "native"),
    # Named: pytest builds an id from the VALUE otherwise, and these values
    # are whole JSONL records.
    [
        pytest.param(claude, "\n" + CLAUDE_RECORD + "\n\n", id="claude"),
        pytest.param(codex, CODEX_META + "\n\n" + CODEX_RECORD + "\n", id="codex"),
    ],
)
def test_the_normalized_json_form_preserves_every_native_byte(
    adapter: _Adapter, native: str
) -> None:
    # native -> records -> JSON -> records -> native, the conversion path the
    # CLI exposes. Whatever the native adapters keep, JSON must keep too.
    records = list(adapter.normalize(StringIO(native)))
    as_json = StringIO()
    normalized.denormalize(records, as_json)
    as_json.seek(0)
    rebuilt = StringIO()
    adapter.denormalize(list(normalized.normalize(as_json)), rebuilt)

    assert rebuilt.getvalue() == native


@pytest.mark.parametrize(
    "text",
    ["2017\\u20132022", "2017–2022", "caf\\u00e9", "café"],  # codespell:ignore caf
)
def test_the_claude_ascii_escaping_convention_survives(text: str) -> None:
    # Claude writes either convention, and a file escaped at all is escaped
    # throughout -- so it is the whole stream's, and re-emitting it in the
    # other form changes every line.
    stream = _claude_turn("user", f'"{text}"')

    assert _round_trip(claude, stream) == stream


@pytest.mark.parametrize(
    "text",
    ["2017–2022", "café"],  # codespell:ignore caf
)
def test_codex_writes_raw_utf8(text: str) -> None:
    # Codex has ONE convention, unlike claude: all 13138 captured non-ASCII
    # lines are raw UTF-8, so escaping is not a per-file choice to preserve.
    stream = (
        CODEX_META
        + "\n"
        + _codex_item(
            '{"type":"message","id":"u","role":"user",'
            '"content":[{"type":"input_text","text":"' + text + '"}]}'
        )
    )

    assert _round_trip(codex, stream) == stream


@pytest.mark.parametrize(
    "line",
    ["42", '"text"', "[]", "null"],
    ids=["number", "string", "array", "null"],
)
def test_codex_preserves_valid_non_object_lines(line: str) -> None:
    native = CODEX_META + "\n" + line + "\n"

    assert _round_trip(codex, native) == native


@pytest.mark.parametrize(
    "line",
    ["42", '"text"', "[]", "null"],
    ids=["number", "string", "array", "null"],
)
def test_claude_preserves_valid_non_object_lines(line: str) -> None:
    assert _round_trip(claude, line + "\n") == line + "\n"


@pytest.mark.parametrize(
    "payload",
    [
        (
            '{"type":"message","role":"assistant","content":['
            '{"type":"input_text","text":"hi","metadata":{"x":1}},7]}'
        ),
        '{"type":"function_call","arguments":null,"name":7}',
        '{"type":"function_call","call_id":"c"}',
        '{"type":"function_call_output","call_id":"c"}',
        '{"type":"function_call_output","call_id":"c","output":null}',
        '{"type":"reasoning","summary":null}',
        '{"type":"reasoning"}',
        '{"type":"message","role":"user"}',
        '{"type":"message","role":"user","content":null}',
        '{"type":"message","role":"user","content":7}',
    ],
    ids=[
        "block-metadata-and-scalar",
        "malformed-function-fields",
        "absent-function-fields",
        "absent-output",
        "null-output",
        "null-reasoning-summary",
        "absent-reasoning-summary",
        "absent-message-content",
        "null-message-content",
        "malformed-message-content",
    ],
)
def test_codex_preserves_field_state_and_malformed_content(payload: str) -> None:
    native = CODEX_META + "\n" + _codex_item(payload)

    assert _round_trip(codex, native) == native


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"token_count","info":7,"rate_limits":[1]}',
        # A null usage object, which codex writes on the first count of a
        # turn. ``TokenUsage.info`` is an object field, so a reader that let
        # it become ``{}`` rewrote the line as one -- 1033 captured rollouts.
        '{"type":"token_count","info":null,"rate_limits":{"a":1}}',
        '{"type":"token_count","info":{"a":1},"rate_limits":null}',
        (
            '{"type":"item_completed","item":{"type":"CommandExecution",'
            '"id":"e","command":["ok",7]}}'
        ),
        (
            '{"type":"web_search_end","call_id":"c","results":['
            '{"url":7,"title":null,"snippet":{"x":1}}]}'
        ),
    ],
    ids=["usage", "null-usage", "null-rate-limits", "command", "search-row"],
)
def test_codex_preserves_malformed_event_fields(payload: str) -> None:
    native = (
        CODEX_META
        + "\n"
        + (
            '{"timestamp":"2026-08-24T19:34:41.197Z","type":"event_msg",'
            '"payload":' + payload + "}\n"
        )
    )

    assert _round_trip(codex, native) == native


def test_codex_preserves_null_compaction_summary() -> None:
    native = (
        CODEX_META
        + "\n"
        + (
            '{"timestamp":"2026-08-24T19:34:41.197Z","type":"compacted",'
            '"payload":{"message":null}}\n'
        )
    )

    assert _round_trip(codex, native) == native


def test_codex_preserves_malformed_timestamps() -> None:
    native = (
        '{"timestamp":7,"type":"session_meta","payload":{"session_id":"s"}}\n'
        '{"timestamp":{"raw":true},"type":"world_state","payload":{}}\n'
    )

    assert _round_trip(codex, native) == native


def test_codex_ordinals_count_malformed_lines() -> None:
    native = (
        '{"ordinal":0,"type":"session_meta","payload":{"session_id":"s"}}\n'
        "{broken\n"
        '{"ordinal":2,"type":"world_state","payload":{}}\n'
    )

    assert _round_trip(codex, native) == native


def test_codex_preserves_provider_dollar_keys() -> None:
    native = (
        CODEX_META
        + "\n"
        + _codex_item(
            '{"type":"message","role":"user","content":[],'
            '"$order":"provider","$parts":[99],"$foreign":true}'
        )
    )

    assert _round_trip(codex, native) == native


def test_an_empty_stream_round_trips() -> None:
    adapters: tuple[_Adapter, ...] = (claude, codex)
    for adapter in adapters:
        assert _round_trip(adapter, "") == ""


def test_a_session_carries_no_records_for_an_empty_stream() -> None:
    assert list(claude.normalize(StringIO(""))) == []


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
