"""``ToolLines`` reads, gates and describes each line an agent sends a host."""

from __future__ import annotations

from typing import Final, cast
from unittest.mock import Mock

import json

import pytest

from trackinizer.server.tool_lines import (
    Authorize,
    GatedCall,
    ResultIds,
    Routed,
    ToolLines,
    Verdict,
    authorize,
    refused_line,
)


_CAP: Final = 1 << 16
"""The cap these lines are read to: any will do, and a small one is quick."""

_HELLO: Final = b'{"py/object": "Hello", "version": 1, "token": "t", "session_id": "s1", "client": "c"}\n'


def _call(**fields: object) -> bytes:
    line = {
        "py/object": "Call",
        "id": "c1",
        "agent": "a",
        "window": "w",
        "tool": "Bash",
        "arguments": {"command": "ls"},
    } | fields
    return (json.dumps(line) + "\n").encode()


def _fresh(gate: Authorize = authorize) -> ToolLines:
    return ToolLines(
        asker="me@x",
        host="m1",
        authorize=gate,
        line_bytes=_CAP,
    )


def _lines(gate: Authorize = authorize) -> ToolLines:
    lines = _fresh(gate)
    routed, calls = lines.feed(_HELLO)
    assert routed.to_daemon == _HELLO
    assert not calls
    return lines


def _deny(call: GatedCall) -> Verdict:
    return Verdict(outcome="deny", reason=f"no {call.tool}")


def _ends_the_connection(
    lines: ToolLines,
    raw: bytes,
    match: str | None = None,
) -> None:
    """Feed ``raw`` after a clean call: nothing of the frame passes, nor anything later."""
    with pytest.raises(ValueError, match=match):
        lines.feed(_call(id="ok") + raw)
    with pytest.raises(ValueError, match="spent"):
        lines.feed(_call(id="after"))


def test_a_call_is_described_and_forwarded() -> None:
    lines = _lines()
    routed, calls = lines.feed(_call())
    shown = json.dumps({"command": "ls"}, sort_keys=True).encode()
    assert calls == [
        GatedCall(
            asker="me@x",
            agent="a",
            session="s1",
            host="m1",
            call_id="c1",
            tool="Bash",
            argument_bytes=len(shown),
            outcome="allow",
        ),
    ]
    assert routed == Routed(to_daemon=_call())
    assert authorize(calls[0]) == Verdict(outcome="allow")


def test_a_denied_call_is_answered_and_never_forwarded() -> None:
    lines = _lines(_deny)
    routed, calls = lines.feed(_call())
    assert [call.outcome for call in calls] == ["deny"]
    assert routed == Routed(to_agent=refused_line("c1", "no Bash"))


def test_a_future_verdict_other_than_allow_is_not_forwarded() -> None:
    def ask(call: GatedCall) -> Verdict:
        del call
        return cast(Verdict, Mock(outcome="ask", reason=""))

    routed, calls = _lines(ask).feed(_call())
    assert routed == Routed(to_agent=refused_line("c1", "The call is not allowed."))
    assert [call.outcome for call in calls] == ["deny"]


def test_a_call_before_hello_ends_the_connection() -> None:
    with pytest.raises(ValueError, match="follows a Hello"):
        _fresh().feed(_call())


@pytest.mark.parametrize(
    "hello",
    [
        _HELLO,
        b'{"py/object": "Hello", "version": 1}\n',
        b'{"py/object": "Hello", "session_id": 5}\n',
        b'{"py/object": "Hello", "session_id": ["s1"]}\n',
    ],
    ids=["second", "no-session", "number", "list"],
)
def test_a_hello_names_one_session_as_a_string_once(hello: bytes) -> None:
    with pytest.raises(ValueError, match="Hello"):
        _lines().feed(hello)


def test_the_session_is_not_read_beyond_being_a_string() -> None:
    lines = _fresh()
    hello = cast(dict[str, object], json.loads(_HELLO)) | {
        "session_id": "../not a session",
        "x": 1,
    }
    routed, _ = lines.feed((json.dumps(hello) + "\n").encode())
    assert json.loads(routed.to_daemon) == hello
    _, calls = lines.feed(_call())
    assert [call.session for call in calls] == ["../not a session"]


@pytest.mark.parametrize(
    "fields",
    [
        {"id": 5},
        {"id": None},
        {"agent": {}},
        {"agent": ["a"]},
        {"tool": 5},
        {"tool": None},
        {"tool": {"py/object": "Ping"}},
        {"arguments": []},
        {"arguments": "ls"},
        {"arguments": None},
    ],
)
def test_a_call_whose_logged_fields_are_not_their_type_ends_the_connection(
    fields: dict[str, object],
) -> None:
    _ends_the_connection(_lines(), _call(**fields), match="Call")


def test_a_call_missing_a_logged_field_ends_the_connection() -> None:
    for key in ("id", "agent", "tool", "arguments"):
        line = cast(dict[str, object], json.loads(_call()))
        del line[key]
        _ends_the_connection(_lines(), (json.dumps(line) + "\n").encode(), match="Call")


@pytest.mark.parametrize(
    "fields",
    [
        {"window": 1},
        {"address_book": {"py/object": "Ping"}},
        {"granted": ["Bash", "Write"]},
        {"extra": 1},
        {"arguments": {"a": {"py/object": "DaemonControl", "stop": True}}},
        {"arguments": {'json://{"py/object": "Hello"}': 1}},
        {"tool": "Ba\x00sh"},
        {"agent": "x" * 1000},
    ],
)
def test_what_the_relay_does_not_read_is_forwarded_as_parsed(
    fields: dict[str, object],
) -> None:
    routed, calls = _lines().feed(_call(**fields))
    assert len(calls) == 1
    assert routed == Routed(to_daemon=_call(**fields))


def test_a_line_split_across_frames_is_one_call() -> None:
    lines = _lines()
    line = _call()
    routed, calls = lines.feed(line[:20])
    assert (routed, calls) == (Routed(), [])
    routed, calls = lines.feed(line[20:-1])
    assert (routed, calls) == (Routed(), [])
    routed, calls = lines.feed(line[-1:])
    assert routed.to_daemon == line
    assert len(calls) == 1


def test_a_split_multibyte_character_is_read_whole() -> None:
    lines = _lines()
    line = _call(tool="Bäsh").replace(b"\\u00e4", "ä".encode())
    cut = line.index("ä".encode()) + 1
    lines.feed(line[:cut])
    routed, calls = lines.feed(line[cut:])
    assert [call.tool for call in calls] == ["Bäsh"]
    assert routed.to_daemon == line.replace("ä".encode(), b"\\u00e4")


def test_two_lines_in_one_frame_are_two_calls() -> None:
    lines = _lines()
    routed, calls = lines.feed(
        _call(id="1") + b'{"py/object": "Ping"}\n' + _call(id="2"),
    )
    assert [call.tool for call in calls] == ["Bash", "Bash"]
    assert routed.to_daemon == _call(id="1") + b'{"py/object": "Ping"}\n' + _call(
        id="2",
    )


def test_a_duplicate_tool_key_is_forwarded_as_the_key_logged() -> None:
    lines = _lines()
    raw = (
        b'{"py/object": "Call", "id": "c1", "agent": "a", "window": "w",'
        b' "tool": "Read", "tool": "Bash", "arguments": {}}\n'
    )
    routed, calls = lines.feed(raw)
    forwarded = cast(dict[str, object], json.loads(routed.to_daemon))
    assert [call.tool for call in calls] == [forwarded["tool"]] == ["Bash"]
    assert routed.to_daemon.count(b'"tool"') == 1


def test_a_duplicate_kind_key_is_forwarded_as_the_kind_judged() -> None:
    lines = _lines()
    raw = b'{"py/object": "Call", "py/object": "Note", "id": "n1"}\n'
    routed, calls = lines.feed(raw)
    assert calls == []
    assert routed == Routed(
        to_agent=refused_line("n1", "Note is not offered."),
        dropped=(len(raw) - 1,),
    )
    raw = b'{"py/object": "Note", "py/object": "Ping"}\n'
    routed, _ = lines.feed(raw)
    assert routed.to_daemon == b'{"py/object": "Ping"}\n'


def test_an_escaped_key_is_the_key_it_spells() -> None:
    lines = _lines()
    raw = (
        b'{"py/object": "Call", "id": "c1", "agent": "a", "window": "w",'
        b' "\\u0074ool": "Bash", "arguments": {}}\n'
    )
    routed, calls = lines.feed(raw)
    assert [call.tool for call in calls] == ["Bash"]
    assert cast(dict[str, object], json.loads(routed.to_daemon))["tool"] == "Bash"
    assert b"\\u0074" not in routed.to_daemon


@pytest.mark.parametrize("kind", ["Note", "Peers", "DaemonControl", "Pause", "Tools"])
def test_a_kind_not_offered_never_reaches_the_host(kind: str) -> None:
    lines = _lines()
    routed, calls = lines.feed(
        (json.dumps({"py/object": kind, "id": "x1"}) + "\n").encode(),
    )
    assert calls == []
    assert routed.to_agent == refused_line("x1", f"{kind} is not offered.")
    assert routed.to_daemon == b""


@pytest.mark.parametrize(
    "raw",
    [
        b'{"py/object": "Peers", "labels": []}\n',
        b'{"py/object": "Peers", "id": 5}\n',
        b'{"id": 5}\n',
        b'{"py/object": 5}\n',
        b'{"py/object": ["Call"]}\n',
        b'{"py/object": {"py/object": "Call"}}\n',
    ],
    ids=[
        "no-id",
        "id-not-a-string",
        "no-kind",
        "kind-number",
        "kind-list",
        "kind-object",
    ],
)
def test_a_kind_not_offered_and_without_an_id_is_dropped(raw: bytes) -> None:
    lines = _lines()
    assert lines.feed(raw) == (Routed(dropped=(len(raw) - 1,)), [])
    assert lines.feed(_call())[0] == Routed(to_daemon=_call())


def test_a_line_without_a_kind_is_answered_by_its_id() -> None:
    routed, calls = _lines().feed(b'{"id": "x1"}\n')
    assert (routed, calls) == (
        Routed(
            to_agent=refused_line("x1", "None is not offered."),
            dropped=(len(b'{"id": "x1"}'),),
        ),
        [],
    )


def test_each_line_dropped_in_a_frame_is_sized_and_the_rest_go_on() -> None:
    peers = b'{"py/object": "Peers", "labels": []}'
    note = b'{"py/object": "Note", "id": "n1"}'
    routed, calls = _lines().feed(peers + b"\n" + _call() + note + b"\n")
    assert routed.dropped == (len(peers), len(note))
    assert routed.to_daemon == _call()
    assert [call.call_id for call in calls] == ["c1"]


@pytest.mark.parametrize(
    "line",
    [
        {"py/object": "Cancel", "id": "c1"},
        {"py/object": "Ack", "id": "c1"},
        {"py/object": "Describe", "id": "c1", "tool": "Bash"},
        {"py/object": "Ping"},
        {"py/object": "Bye"},
        {"py/object": "Ack", "id": 5, "x": 1},
    ],
)
def test_the_other_kinds_are_forwarded_unlogged(line: dict[str, object]) -> None:
    raw = (json.dumps(line) + "\n").encode()
    acks = ("c1",) if line == {"py/object": "Ack", "id": "c1"} else ()
    assert _lines().feed(raw) == (Routed(to_daemon=raw, acks=acks), [])


def _padded(size: int) -> bytes:
    """Return a ``Call`` line of ``size`` bytes, newline excluded."""
    head = b'{"py/object": "Call", "id": "big", "agent": "a", "window": "w", "tool": "Bash", "arguments": {"x": "'
    tail = b'"}}'
    return head + b"y" * (size - len(head) - len(tail)) + tail


def test_a_line_at_the_cap_is_read() -> None:
    routed, calls = _lines().feed(_padded(_CAP) + b"\n")
    assert len(calls) == 1
    assert routed.to_agent == b""


def test_a_line_one_byte_over_the_cap_ends_the_connection() -> None:
    _ends_the_connection(
        _lines(),
        _padded(_CAP + 1) + b"\n",
        match=f"over {_CAP} bytes",
    )


def test_a_line_over_the_cap_ends_the_connection_before_its_newline() -> None:
    lines = _lines()
    big = _padded(_CAP + 5000)
    assert lines.feed(big[: _CAP // 2]) == (Routed(), [])
    with pytest.raises(ValueError, match="over"):
        lines.feed(big[_CAP // 2 :])
    with pytest.raises(ValueError, match="spent"):
        lines.feed(b"\n")


@pytest.mark.parametrize(
    "raw",
    [
        b"not json\n",
        b"\n",
        b"[1, 2]\n",
        b'"Call"\n',
        b'\xef\xbb\xbf{"py/object": "Ping"}\n',
        b'{"py/object": "Ping"}\xff\n',
        b'{"py/object": "Ping", "x": NaN}\n',
        b'{"py/object": "Ping", "x": -Infinity}\n',
        b'{"py/object": "Ping", "x": 1e999}\n',
        b'{"py/object": "Ping", "x": ' + b"1" * 5000 + b"}\n",
        b"[" * 100_000 + b"\n",
    ],
    ids=[
        "not-json",
        "blank",
        "array",
        "string",
        "bom",
        "bad-utf8",
        "nan",
        "infinity",
        "overflow",
        "long-integer",
        "deeply-nested",
    ],
)
def test_unreadable_input_ends_the_connection_and_forwards_nothing(raw: bytes) -> None:
    _ends_the_connection(_lines(), raw)


@pytest.mark.parametrize("number", [b"NaN", b"1e999", b"[-Infinity]"])
def test_a_number_json_lacks_in_arguments_ends_the_connection(number: bytes) -> None:
    _ends_the_connection(_lines(), _call().replace(b'"ls"', number))


# Where ``json`` gives out depends on the platform's stack, so every depth either side of
# it is tried, as one object and inside a call's arguments: each is read or ends the tool connection.
@pytest.mark.parametrize("depth", [500, 900, 990, 1000, 1010, 1100, 5000, 100_000])
@pytest.mark.parametrize("where", ["line", "arguments", "kind"])
def test_a_line_nested_any_depth_is_read_or_ends_the_connection(
    depth: int,
    where: str,
) -> None:
    nested = b"[" * depth + b"]" * depth
    raw = {
        "line": b'{"py/object": "Ping", "x": ' + nested + b"}\n",
        "arguments": _call().replace(b'"ls"', nested),
        "kind": b'{"id": "x1", "py/object": ' + nested + b"}\n",
    }[where]
    lines = _lines()
    try:
        routed, _ = lines.feed(raw)
    except ValueError:
        with pytest.raises(ValueError, match="spent"):
            lines.feed(_call())
    else:
        assert routed.to_daemon or routed.to_agent


@pytest.mark.parametrize("dumps", ["loads", "dumps"])
def test_a_line_nested_past_the_stack_ends_the_connection(
    monkeypatch: pytest.MonkeyPatch,
    dumps: str,
) -> None:
    """Where the parser gives out depends on the platform's stack, so it is made to."""

    def overflow(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RecursionError

    lines = _lines()
    monkeypatch.setattr(json, dumps, overflow)
    with pytest.raises(ValueError, match="nests too deep"):
        lines.feed(b'{"py/object": "Ping"}\n')
    monkeypatch.undo()
    with pytest.raises(ValueError, match="spent"):
        lines.feed(_call(id="after"))


def test_a_paired_surrogate_is_text() -> None:
    routed, calls = _lines().feed(
        _call(tool="😀".encode("utf-16", "surrogatepass").decode("utf-16")),
    )
    assert [call.tool for call in calls] == ["\U0001f600"]
    assert cast(dict[str, object], json.loads(routed.to_daemon))["tool"] == "\U0001f600"


def test_a_surrogate_in_arguments_is_forwarded_and_sized() -> None:
    routed, calls = _lines().feed(_call(arguments={"a": "\ud800"}))
    shown = json.dumps({"a": "\ud800"}, sort_keys=True).encode()
    assert calls[0].argument_bytes == len(shown)
    assert routed.to_daemon == _call(arguments={"a": "\ud800"})


def test_the_wire_form_of_arguments_is_what_is_sized() -> None:
    arguments = {"py/dict": {"n": {"py/float": "nan"}, "l": {"py/list": [1]}}}
    routed, calls = _lines().feed(_call(arguments=arguments))
    shown = json.dumps(arguments, sort_keys=True).encode()
    assert calls[0].argument_bytes == len(shown)
    assert routed.to_daemon == _call(arguments=arguments)


def test_the_size_is_over_sorted_arguments() -> None:
    lines = _lines()
    _, first = lines.feed(_call(arguments={"b": 1, "a": 2}))
    _, second = lines.feed(_call(arguments={"a": 2, "b": 1}))
    assert first[0].argument_bytes == second[0].argument_bytes
    assert first[0].argument_bytes == len(b'{"a": 2, "b": 1}')


def test_refused_line_is_one_json_line() -> None:
    assert json.loads(refused_line("c1", "no")) == {
        "py/object": "Result",
        "id": "c1",
        "record": {
            "py/object": "ToolErrorResult",
            "content": "no",
            "call_id": "",
            "is_error": True,
            "extra": {},
        },
    }
    assert refused_line("c1", "no").endswith(b"\n")


def test_the_hosts_results_name_their_calls_however_the_frames_cut_them() -> None:
    results = ResultIds(line_bytes=64)
    first = b'{"py/object": "Result", "id": "c1", "record": {}}\n'
    second = b'{"py/object": "Result", "id": "c2"}\n'

    assert results.feed(first[:10]) == []
    assert results.feed(first[10:] + second[:5]) == ["c1"]
    assert results.feed(second[5:]) == ["c2"]


def test_a_host_line_that_is_no_result_unreadable_or_too_long_is_passed_over() -> None:
    results = ResultIds(line_bytes=64)
    long = b'{"py/object": "Result", "id": "c3", "pad": "' + b"x" * 64 + b'"}\n'

    assert (
        results.feed(b'{"py/object": "Ping"}\n{"py/object": "Result", "id": 7}\n') == []
    )
    assert results.feed(b"not json\n[1]\n" + b"\xff\n" + b"[" * 100_000 + b"\n") == []
    assert results.feed(long[:40]) == []
    assert results.feed(long[40:80]) == []
    assert results.feed(long[80:]) == []
    assert results.feed(b'{"py/object": "Result", "id": "c4"}\n') == ["c4"]


def test_an_ack_names_the_result_the_agent_holds() -> None:
    lines = _lines()

    routed, _ = lines.feed(b'{"py/object": "Ack", "id": "c1"}\n{"py/object": "Ack"}\n')

    assert routed.acks == ("c1",)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
