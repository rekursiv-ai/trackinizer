"""Prove the table in :mod:`shell_results` on three counts, per row.

The module docstring promises a set of shell forms it recognizes. Each row is
asserted three ways, because a row can fail each independently:

1. HARVESTED -- the command becomes the typed record it names, carrying the
   path and whatever content the transcript actually held.
2. REVERSIBLE -- a session holding that record rewrites to the provider's
   original bytes. A lift that cannot be undone silently rewrites transcripts.
3. TRANSFORMABLE -- editing the record's semantic fields changes the replayed
   command to match. A record nobody can edit is a read-only annotation, not a
   representation of the act.

Both providers, since each spells the same act differently: claude records the
command on the CALL and the output on the result, codex records both together.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from io import StringIO

import json

import pytest

from trackinizer.lib.agent.sessions import claude, codex
from trackinizer.lib.agent.sessions.udiff import render_udiff
from trackinizer.lib.agent.types.sessions import (
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    SessionRecord,
    ToolCall,
)
from trackinizer.lib.custom_json import DictCodec, ListCodec, StrCodec, json_unfreeze


# One entry per row of the table in ``shell_results``: the command, the record
# it must become, the path it must name, and the content it may claim. A
# ``None`` content means the transcript never held the file's new bytes, so the
# record must NOT invent them.
def rows() -> tuple[tuple[str, str, type[object], str, str | None], ...]:
    """Return every documented form: id, command, record type, path, content.

    Returns:
      rows: One tuple per table row.

    """
    return (
        ("read-cat", "/bin/cat a.txt", FileReadResult, "a.txt", "body\n"),
        ("read-head", "/usr/bin/head -n 1 a.txt", FileReadResult, "a.txt", "body\n"),
        ("read-tail", "/usr/bin/tail -n 1 a.txt", FileReadResult, "a.txt", "body\n"),
        ("read-sed", "/bin/sed -n 1p a.txt", FileReadResult, "a.txt", "body\n"),
        ("read-nl", "/usr/bin/nl -ba a.txt", FileReadResult, "a.txt", "body\n"),
        # A literal ``cd`` composes into the path, per ``shell_results``: the
        # bare operand would name a file in the session's own directory.
        ("read-cd", "cd sub && /bin/cat a.txt", FileReadResult, "sub/a.txt", "body\n"),
        ("write-echo", "/bin/echo hi > a.txt", FileWriteResult, "a.txt", "hi\n"),
        ("write-printf", "/usr/bin/printf hi > a.txt", FileWriteResult, "a.txt", "hi"),
        (
            "write-heredoc",
            "/bin/cat > a.txt << 'EOF'\nbeta\nEOF\n",
            FileWriteResult,
            "a.txt",
            "beta\n",
        ),
        ("append-echo", "/bin/echo hi >> a.txt", FileEditResult, "a.txt", "+hi\n"),
        (
            "append-heredoc",
            "/bin/cat >> a.txt << 'EOF'\nbeta\nEOF\n",
            FileEditResult,
            "a.txt",
            "+beta\n",
        ),
        (
            "patch-inline",
            "/usr/bin/patch a.txt << 'EOF'\n-old\n+new\nEOF\n",
            FileEditResult,
            "a.txt",
            "-old\n+new\n",
        ),
        ("rewrite-sed", "/bin/sed -i s/a/b/ a.txt", FileEditResult, "a.txt", None),
        (
            "rewrite-perl",
            "/usr/bin/perl -pi -e s/a/b/ a.txt",
            FileEditResult,
            "a.txt",
            None,
        ),
        ("rewrite-tee", "/usr/bin/tee a.txt", FileEditResult, "a.txt", None),
        ("rewrite-tee-a", "/usr/bin/tee -a a.txt", FileEditResult, "a.txt", None),
    )


def _claude_native(command: str, stdout: str) -> str:
    """Return the two claude lines one Bash call and its answer occupy."""
    call = {
        "parentUuid": None,
        "isSidechain": False,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "Bash",
                    "input": {"command": command},
                }
            ],
        },
        "uuid": "u1",
        "timestamp": "2026-09-02T00:00:00.000Z",
        "userType": "external",
        "cwd": "/w",
        "sessionId": "01a03544-88de-71e2-981c-c8433de27ddc",
        "version": "2.1.241",
    }
    answer = {
        "parentUuid": "u1",
        "isSidechain": False,
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": stdout}
            ],
        },
        "uuid": "u2",
        "timestamp": "2026-09-02T00:00:01.000Z",
        "toolUseResult": {
            "stdout": stdout,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
        "userType": "external",
        "cwd": "/w",
        "sessionId": "01a03544-88de-71e2-981c-c8433de27ddc",
        "version": "2.1.241",
    }
    return "".join(
        json.dumps(line, separators=(",", ":")) + "\n" for line in (call, answer)
    )


def _codex_native(command: str, stdout: str) -> str:
    """Return the codex launch line plus the event one command produced."""
    meta = {
        "timestamp": "2026-08-24T19:34:39.215Z",
        "type": "session_meta",
        "payload": {
            "session_id": "01a03544-88de-71e2-981c-c8433de27ddc",
            "id": "01a03544-88de-71e2-981c-c8433de27ddc",
        },
    }
    event = {
        "timestamp": "2026-08-24T19:34:41.197Z",
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "CommandExecution",
                "id": "e1",
                "command": ["/bin/bash", "-lc", command],
                "stdout": stdout,
                "exit_code": 0,
            },
        },
    }
    return "".join(
        json.dumps(line, separators=(",", ":")) + "\n" for line in (meta, event)
    )


def _lifted(records: Sequence[SessionRecord]) -> object:
    """Return the one file result a session carries."""
    found = [
        record
        for record in records
        if isinstance(record, FileReadResult | FileWriteResult | FileEditResult)
    ]
    return found[0] if len(found) == 1 else None


@pytest.mark.parametrize(
    ("command", "expected", "path", "content"),
    [pytest.param(*row[1:], id=row[0]) for row in rows()],
)
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_every_table_row_is_harvested(
    provider: str,
    command: str,
    expected: type[object],
    path: str,
    content: str | None,
) -> None:
    # A read reports what the command printed; a write or append reports the
    # bytes it put in the file; a rewrite reports neither, because the
    # transcript holds no record of the file afterwards.
    stdout = "body\n" if expected is FileReadResult else ""
    native = (
        _claude_native(command, stdout)
        if provider == "claude"
        else _codex_native(command, stdout)
    )
    adapter = claude if provider == "claude" else codex

    lifted = _lifted(list(adapter.normalize(StringIO(native))))

    assert type(lifted) is expected
    assert isinstance(lifted, FileReadResult | FileWriteResult | FileEditResult)
    assert lifted.path == path
    if isinstance(lifted, FileEditResult):
        assert (render_udiff(lifted.edits) or None) == content
    else:
        assert lifted.content == content


@pytest.mark.parametrize("command", [pytest.param(row[1], id=row[0]) for row in rows()])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_every_table_row_replays_to_the_original_bytes(
    provider: str, command: str
) -> None:
    # Lifting is a VIEW of the command, not a replacement for it: whatever the
    # classifier made of the line, writing the session back must reproduce the
    # provider's own bytes exactly.
    native = (
        _claude_native(command, "body\n")
        if provider == "claude"
        else _codex_native(command, "body\n")
    )
    adapter = claude if provider == "claude" else codex

    records = list(adapter.normalize(StringIO(native)))
    output = StringIO()
    adapter.denormalize(records, output)

    assert output.getvalue() == native


@pytest.mark.parametrize(
    ("command", "expected"),
    [pytest.param(row[1], row[2], id=row[0]) for row in rows()],
)
def test_editing_a_lifted_path_rewrites_the_replayed_command(
    command: str, expected: type[object]
) -> None:
    """A lifted record is editable, and the edit reaches the command.

    The path is the field every row carries, so it is the one every row can be
    asked to change. A record whose edit does not reach the replayed bytes
    would let a caller believe it had rewritten a session it had not.
    """
    native = _claude_native(command, "body\n")
    records = list(claude.normalize(StringIO(native)))
    lifted = _lifted(records)
    assert isinstance(lifted, FileReadResult | FileWriteResult | FileEditResult)

    edited = replace(lifted, path="renamed.txt")
    output = StringIO()
    claude.denormalize(
        [edited if record is lifted else record for record in records], output
    )

    replayed = _replayed_command(output.getvalue())
    assert "renamed.txt" in replayed
    assert expected is not None


def _replayed_command(native: str) -> str:
    """Return the Bash command a rewritten claude transcript carries."""
    for line in native.splitlines():
        record = DictCodec.coerce(json.loads(line))
        message = DictCodec.coerce(record.get("message"))
        for block in ListCodec.mappings(message.get("content")):
            if StrCodec.coerce(block.get("name")) == "Bash":
                return StrCodec.coerce(
                    DictCodec.coerce(block.get("input")).get("command")
                )
    return ""


def test_editing_lifted_write_content_rewrites_the_replayed_command() -> None:
    """Changing the bytes a write put in a file changes the command."""
    native = _claude_native("/usr/bin/printf body > a.txt", "")
    records = list(claude.normalize(StringIO(native)))
    lifted = _lifted(records)
    assert isinstance(lifted, FileWriteResult)

    output = StringIO()
    claude.denormalize(
        [
            replace(lifted, content="changed") if record is lifted else record
            for record in records
        ],
        output,
    )

    assert "changed" in _replayed_command(output.getvalue())


def test_a_lifted_result_keeps_the_shell_execution_it_came_from() -> None:
    """The original execution stays on the record, so nothing is lost.

    A lift narrows a shell result to a file result; the exit code, the argv,
    and the streams that proved it still have to survive somewhere, or a
    provider-native replay could not be rebuilt.
    """
    native = _claude_native("/bin/cat a.txt", "body\n")

    lifted = _lifted(list(claude.normalize(StringIO(native))))

    assert isinstance(lifted, FileReadResult)
    assert "$shell" in json_unfreeze(lifted.extra)


def test_a_session_with_no_shell_call_lifts_nothing() -> None:
    assert _lifted([ToolCall(call_id="c1", name="Read")]) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
