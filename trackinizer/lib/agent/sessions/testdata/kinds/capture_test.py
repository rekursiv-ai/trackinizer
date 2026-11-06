"""Tests for the Claude kinds fixture capture."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import json
import sys

from treekle import ReadError

import pytest

from trackinizer.lib.agent.sessions.testdata import capture as session_capture
from trackinizer.lib.agent.sessions.testdata.capture import _Step
from trackinizer.lib.agent.sessions.testdata.kinds import capture
from trackinizer.lib.agent.sessions.testdata.kinds.capture import (
    _first_of_each_kind,
    _record_lines,
    capture_claude_kinds,
    capture_codex_kinds,
)
from trackinizer.lib.testing.resources import ascii_locale


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


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


@pytest.fixture(scope="module")
def claude_kinds_capture(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[
    tuple[
        Path,
        Path,
        Path,
        list[tuple[list[str], Sequence[_Step], Path, dict[str, str]]],
        list[str],
        list[Path],
    ]
]:
    tmp_path = tmp_path_factory.mktemp("claude-kinds")
    user = tmp_path / "user"
    real_claude_home = user / ".claude"
    real_claude_home.mkdir(parents=True)
    (real_claude_home / "auth.json").write_text("auth", encoding="utf-8")
    monkeypatch = pytest.MonkeyPatch()
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

    written = capture_claude_kinds(root, out, turn_sec=1.5, timeout_sec=9)
    try:
        yield tmp_path, root, out, calls, skills, written
    finally:
        monkeypatch.undo()


def test_capture_claude_kinds_serves_the_mcp_and_skill_and_emits_only_main(
    claude_kinds_capture: tuple[
        Path,
        Path,
        Path,
        list[tuple[list[str], Sequence[_Step], Path, dict[str, str]]],
        list[str],
        list[Path],
    ],
) -> None:
    _, root, out, calls, skills, written = claude_kinds_capture
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


def test_capture_claude_kinds_reuses_existing_output(
    claude_kinds_capture: tuple[
        Path,
        Path,
        Path,
        list[tuple[list[str], Sequence[_Step], Path, dict[str, str]]],
        list[str],
        list[Path],
    ],
) -> None:
    tmp_path, _, out, _, _, written = claude_kinds_capture
    assert (
        capture_claude_kinds(
            tmp_path / "second-root",
            out,
            turn_sec=1.5,
            timeout_sec=9,
        )
        == written
    )


def test_capture_codex_kinds_drives_the_session_then_forks_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = tmp_path / "user"
    (user / ".codex").mkdir(parents=True)
    (user / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(user))
    calls: list[
        tuple[list[str], Sequence[_Step], Path, dict[str, str], float, int]
    ] = []

    def fake_drive(
        argv: list[str],
        script: Sequence[_Step],
        *,
        cwd: Path,
        env: dict[str, str],
        turn_sec: float,
        timeout_sec: int,
    ) -> None:
        calls.append((argv, script, cwd, env, turn_sec, timeout_sec))
        home = env["CODEX_HOME"]
        sessions = Path(home) / "sessions" / "2026"
        sessions.mkdir(parents=True, exist_ok=True)
        forked = argv[1] == "fork"
        meta = '{"forked_from_id":"p"}' if forked else "{}"
        name = "rollout-2.jsonl" if forked else "rollout-1.jsonl"
        (sessions / name).write_text(
            f'{{"type":"session_meta","payload":{meta}}}\n'
            f'{{"type":"event_msg","payload":{{"type":"a","cwd":"{cwd}"}}}}\n'
            '{"type":"event_msg","payload":{"type":"a"}}\n'
            '{"type":"event_msg","payload":{"type":"b"}}\n'
            '{"type":"compacted","payload":{"message":""}}\n'
            '{"type":"compacted","payload":{"message":"again"}}\n'
            '{"type":"bare"}\n'
            '{"type":"event_msg","payload":{"type":"b","item":{"type":"x"}}}\n'
            '{"type":"event_msg","payload":{"type":"b","item":{"type":"x","n":2}}}\n'
            # A later line may name the key; only the first line says a fork.
            '{"type":"event_msg","payload":'
            f'{{"type":"c","text":"forked_from_id é {home}"}}}}\n'
            '{"type":"event_msg","payload":{"type":"d"}}\n',
            encoding="utf-8",
        )

    monkeypatch.setattr(capture, "_drive", fake_drive)
    out = tmp_path / "out" / "kinds"
    root = tmp_path / "root"

    with ascii_locale():
        written = capture_codex_kinds(root, out, turn_sec=1.5, timeout_sec=9)
        # Into a directory that already exists, as every capture after the first.
        again = capture_codex_kinds(
            tmp_path / "again",
            out,
            turn_sec=1.5,
            timeout_sec=9,
        )

    assert written == again == [out / "codex_main.jsonl", out / "codex_fork.jsonl"]
    # The first line of each kind alone, redacted.
    assert (out / "codex_fork.jsonl").read_text(encoding="utf-8") == (
        '{"type":"session_meta","payload":{"forked_from_id":"p"}}\n'
        '{"type":"event_msg","payload":{"type":"a","cwd":"/workspace"}}\n'
        '{"type":"event_msg","payload":{"type":"b"}}\n'
        '{"type":"compacted","payload":{"message":""}}\n'
        '{"type":"bare"}\n'
        '{"type":"event_msg","payload":{"type":"b","item":{"type":"x"}}}\n'
        '{"type":"event_msg","payload":'
        '{"type":"c","text":"forked_from_id é /config"}}\n'
        '{"type":"event_msg","payload":{"type":"d"}}\n'
    )
    assert sorted(path.name for path in (root / "home").iterdir()) == [
        "auth.json",
        "sessions",
    ]
    flags = [
        "-c",
        f'projects."{root / "work"}".trust_level="trusted"',
        "-c",
        "model_reasoning_summary=detailed",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        'web_search="live"',
    ]
    env = {"CODEX_HOME": str(root / "home")}
    # The script is the capture's spec: each line drives a different live session.
    session = (
        _Step(
            line=(
                "Use your update_plan tool to record a two-step plan, then mark both "
                "steps completed."
            ),
        ),
        _Step(
            line=(
                "Search the web for the current stable Python version and state it "
                "in one line."
            ),
        ),
        _Step(
            line=(
                'Use apply_patch to create note.txt containing "beta", then '
                "apply_patch again to change beta to gamma."
            ),
        ),
        _Step(line="/compact"),
    )
    assert calls[:2] == [
        (["codex", *flags], session, root / "work", env, 1.5, 9),
        (
            ["codex", "fork", "--last", *flags],
            (_Step(line="Say exactly: forked"),),
            root / "work",
            env,
            1.5,
            9,
        ),
    ]


def test_first_of_each_kind_cuts_every_string_and_keeps_key_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    long = "é" * 300
    path.write_text(
        json.dumps(
            {"type": "m", "payload": {"z": long, "a": [long, 7, None], "b": True}},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    _first_of_each_kind(path)

    cut = "é" * 256
    assert path.read_text(encoding="utf-8") == (
        f'{{"type":"m","payload":{{"z":"{cut}","a":["{cut}",7,null],"b":true}}}}\n'
    )


def test_first_of_each_kind_refuses_a_line_that_is_not_an_object(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text('{"type":"a"}\n[]\n', encoding="utf-8")

    with pytest.raises(ReadError):
        _first_of_each_kind(path)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
