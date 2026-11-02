"""Tests for the Claude kinds fixture capture."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import json
import sys

from treekle import ReadError

import pytest

from trackinizer.lib.agent.sessions.testdata import capture as session_capture
from trackinizer.lib.agent.sessions.testdata.kinds import capture
from trackinizer.lib.agent.sessions.testdata.kinds.capture import (
    _capture_claude_kinds,
    _record_lines,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.agent.sessions.testdata.capture import _Step


def test_record_lines_keeps_only_order_and_payload_witnesses(tmp_path: Path) -> None:
    records: list[dict[str, object]] = [
        {"type": "mode"},
        {"type": 1},
        {"type": "user"},
        {"type": "user", "turnOrigin": "human"},
        {"type": "user", "sourceToolUseID": "c"},
        {"type": "user", "interruptedMessageId": "m"},
        {"type": "user", "permissionDecision": {}},
        {"type": "user", "toolDenialKind": "user-rejected"},
        {"type": "assistant", "uuid": "not-call"},
        {"type": "assistant", "wireToolInputs": {}},
        {"type": "assistant", "attributionSkill": "probe"},
        {"type": "assistant", "attributionAgent": "agent"},
        {"type": "assistant", "attributionMcpServer": "probe"},
        {"type": "assistant", "attributionMcpTool": "echo"},
        {
            "type": "assistant",
            "uuid": "agent-call",
            "message": {
                "content": [None, {"type": "tool_use", "name": "Agent"}],
            },
        },
        {
            "type": "user",
            "sourceToolAssistantUUID": "agent-call",
            "toolUseResult": {"totalTokens": 1},
        },
        {"type": "attachment"},
        {"type": "system"},
        {"type": "attachment", "agentId": "a"},
        {"type": "attachment", "session_id": "s"},
        {"type": "attachment", "extra": True},
        {"type": "system", "extra": True},
        {
            "type": "user",
            "sourceToolAssistantUUID": "not-call",
            "toolUseResult": {"totalTokens": 0},
        },
    ]
    source = tmp_path / "source.jsonl"
    source.write_text("".join(f"{json.dumps(record)}\n" for record in records))

    written = _record_lines(source)

    assert written.read_text(encoding="utf-8").splitlines() == [
        json.dumps(records[index]) for index in range(2, 16)
    ] + [json.dumps(records[index]) for index in (16, 17, 18, 19)]


@pytest.mark.parametrize("name", ["Bash", "Edit", "Read", "Write", "Agent"])
def test_record_lines_keeps_payload_tool_calls_and_results(
    tmp_path: Path,
    name: str,
) -> None:
    records: list[dict[str, object]] = [
        {"type": "user"},
        {
            "type": "assistant",
            "uuid": "call",
            "message": {
                "content": [{"type": "tool_use", "name": name}],
            },
        },
        {
            "type": "user",
            "sourceToolAssistantUUID": "call",
            "toolUseResult": {},
        },
    ]
    source = tmp_path / "source.jsonl"
    source.write_text("".join(f"{json.dumps(record)}\n" for record in records))

    written = _record_lines(source)

    assert written.read_text(encoding="utf-8").splitlines() == [
        json.dumps(record) for record in records
    ]


def test_record_lines_rejects_a_non_object_record(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"[]\n")

    with pytest.raises(ReadError):
        _record_lines(source)


def test_capture_claude_kinds_serves_the_mcp_and_skill_and_emits_only_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = tmp_path / "user"
    real_claude_home = user / ".claude"
    real_claude_home.mkdir(parents=True)
    (real_claude_home / "auth.json").write_text("auth", encoding="utf-8")
    monkeypatch.setenv("HOME", str(user))
    calls: list[tuple[list[str], Sequence[_Step], Path, dict[str, str]]] = []
    skills: list[str] = []

    def fake_drive(
        argv: list[str],
        script: Sequence[_Step],
        *,
        cwd: Path,
        env: dict[str, str],
        turn_sec: float,
        timeout_sec: int,
    ) -> None:
        assert (turn_sec, timeout_sec) == (1.5, 9)
        calls.append((argv, script, cwd, env))
        assert (Path(env["CLAUDE_CONFIG_DIR"]) / "auth.json").read_text(
            encoding="utf-8",
        ) == "auth"
        skill = cwd / ".claude" / "skills" / "probe" / "SKILL.md"
        skills.append(skill.read_text(encoding="utf-8"))
        project = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "p"
        (project / "sid" / "subagents").mkdir(parents=True)
        (project / "main.jsonl").write_text(
            '{"type":"mode"}\n'
            f'{{"type":"user","cwd":"{cwd}","home":"{env["CLAUDE_CONFIG_DIR"]}"}}\n'
            f'{{"type":"assistant","text":"{"x" * 4066}"}}\n'
            f'{{"type":"system","text":"{"y" * 4068}"}}\n',
            encoding="utf-8",
        )
        (project / "sid" / "subagents" / "a.jsonl").write_text("x", encoding="utf-8")

    monkeypatch.setattr(capture, "_drive", fake_drive)
    out = tmp_path / "out" / "kinds"
    root = tmp_path / "root"

    written = _capture_claude_kinds(root, out, turn_sec=1.5, timeout_sec=9)

    assert written == [out / "claude_main.jsonl"]
    assert (out / "claude_main.jsonl").read_text(encoding="utf-8") == (
        f'{{"type":"user","cwd":"/workspace","home":"/config"}}\n'
        f'{{"type":"system","text":"{"y" * 4068}"}}\n'
    )
    assert skills == [
        (
            "---\nname: probe\ndescription: Replies with a fixed word.\n---\n\n"
            "Reply with exactly: skill-ran\n"
        ),
    ]
    servers = {
        "mcpServers": {
            "probe": {
                "command": sys.executable,
                "args": [
                    "-P",
                    str(Path(session_capture.__file__).parent / "echo_mcp.py"),
                ],
            },
        },
    }
    ((argv, script, cwd, env),) = calls
    assert argv == [
        "claude",
        "--permission-mode",
        "default",
        "--allowedTools",
        "Write",
        "Edit",
        "Read",
        "Bash(grep:*)",
        "Bash(sleep:*)",
        "mcp__probe__echo",
        "Agent",
        "Skill",
        "--mcp-config",
        json.dumps(servers),
        "--strict-mcp-config",
    ]
    assert [step.line for step in script] == [
        (
            "Do these in order: (1) use the Write tool to create note.txt "
            'containing "beta". (2) use the Edit tool to change beta to gamma. '
            "(3) use the Read tool to read note.txt. (4) run the shell command "
            '"grep zzz note.txt".'
        ),
        'Use the probe MCP server\'s echo tool to echo "hi".',
        (
            "Spawn one general-purpose subagent in the foreground, not in the "
            'background, whose entire job is to reply with the word "sync". '
            "Report what it said."
        ),
        "Use the probe skill.",
        'Run the shell command "touch rejected.txt".',
        'Run the shell command "sleep 60".',
    ]
    assert [step.line for step in script if step.escape] == [
        'Run the shell command "touch rejected.txt".',
        'Run the shell command "sleep 60".',
    ]
    assert len(script) == 6
    assert cwd == root / "work"
    assert env == {
        "CLAUDE_CONFIG_DIR": str(root / "home"),
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    }

    assert (
        _capture_claude_kinds(
            tmp_path / "second-root",
            out,
            turn_sec=1.5,
            timeout_sec=9,
        )
        == written
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
