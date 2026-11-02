"""The claude-kinds capture session, beside the fixture it writes.

``capture.py --cli claude-kinds`` runs it; see that script's docstring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import json
import sys

from treekle import json_loads

from trackinizer.lib.agent.sessions.testdata.capture import (
    _drive,
    _emit,
    _prepare,
    _Step,
)


_CWD: Final = Path(__file__).resolve().parent


# ``--strict-mcp-config`` so the one server is ``echo_mcp.py``: a server from the
# operator's settings would add tools, and their calls, the script never asked for.
# ``sys.executable`` is this script's interpreter, the one that has ``mcp``.
#
# ``CLAUDE_CODE_DISABLE_BACKGROUND_TASKS``: without it this CLI runs every subagent,
# and a long shell command, in the background whatever the prompt asks -- so no
# finished subagent report reaches a tool result, and no tool is running when the
# interrupt's Escape arrives (both measured, 2.1.309).
def _capture_claude_kinds(
    root: Path,
    out: Path,
    *,
    turn_sec: float,
    timeout_sec: int,
) -> list[Path]:
    """Drive the claude-kinds session and return the fixture path written."""
    # One line per record kind ``_session_script`` does not produce, run under the
    # default permission mode so a prompt exists to reject. ``allowed``
    # pre-approves every tool but the rejected one, so no other line stops on a
    # prompt the script never answers.
    #   1. the Write, Edit and Read payloads by name (the session script asks
    #      loosely, and the model answers with the shell), and a grep that finds
    #      nothing, which claude annotates with a return-code interpretation
    #   2. an MCP tool call, served by ``echo_mcp.py``
    #   3. a subagent run in the foreground, which answers with its finished report
    #   4. a skill invocation, from the workspace skill written below
    #   5. a rejected tool: ``touch`` is not pre-approved, so it prompts, and
    #      Escape answers no
    #   6. an interrupted turn: Escape while ``sleep`` runs.
    #
    # No background subagent: its finish reaches the log as a
    # ``<task-notification>`` user line, not as a tool result, and
    # ``claude_main.jsonl`` already holds both that and the launch.
    script = (
        _Step(
            line=(
                "Do these in order: (1) use the Write tool to create note.txt "
                'containing "beta". (2) use the Edit tool to change beta to gamma. '
                "(3) use the Read tool to read note.txt. (4) run the shell command "
                '"grep zzz note.txt".'
            ),
        ),
        _Step(line='Use the probe MCP server\'s echo tool to echo "hi".'),
        _Step(
            line=(
                "Spawn one general-purpose subagent in the foreground, not in the "
                'background, whose entire job is to reply with the word "sync". '
                "Report what it said."
            ),
        ),
        _Step(line="Use the probe skill."),
        _Step(line='Run the shell command "touch rejected.txt".', escape=True),
        _Step(line='Run the shell command "sleep 60".', escape=True),
    )
    allowed = (
        "Write",
        "Edit",
        "Read",
        "Bash(grep:*)",
        "Bash(sleep:*)",
        "mcp__probe__echo",
        "Agent",
        "Skill",
    )
    home, work = _prepare(root, real_home=Path.home() / ".claude")  # noqa: TID251 -- vendor fixed path, not ours (AGENTS.md rule 3)  # house-ignore[xdg-literal] -- Vendor CLI's fixed home path, not ours (AGENTS.md rule 3).
    skill = work / ".claude" / "skills" / "probe" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(
        b"---\nname: probe\ndescription: Replies with a fixed word.\n---\n\n"
        b"Reply with exactly: skill-ran\n",
    )
    servers = {
        "mcpServers": {
            "probe": {
                "command": sys.executable,
                "args": ["-P", str(_CWD.parent / "echo_mcp.py")],
            },
        },
    }
    _drive(
        [
            "claude",
            "--permission-mode",
            "default",
            "--allowedTools",
            *allowed,
            "--mcp-config",
            json.dumps(servers),
            "--strict-mcp-config",
        ],
        script,
        cwd=work,
        env={
            "CLAUDE_CONFIG_DIR": str(home),
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        },
        turn_sec=turn_sec,
        timeout_sec=timeout_sec,
    )
    out.mkdir(parents=True, exist_ok=True)
    return [
        _record_lines(
            _emit(path, out / "claude_main.jsonl", home=home, work=work),
        )
        for path in sorted((home / "projects").rglob("*.jsonl"))
        if path.parent.name != "subagents"
    ]


# The kinds session is captured for its record lines' key orders, so its fixture keeps
# only the lines that witness one: the first of each kind, the first carrying each
# order-bearing field, and each payload tool's call and result. The CLI's request
# mirrors and large context attachments are most of the bytes and none of the purpose.
def _record_lines(path: Path) -> Path:
    """Keep one line per kind and each order field the fixture asserts."""
    kinds = {"user", "assistant", "attachment", "system"}
    fields = {
        "agentId",
        "session_id",
        "turnOrigin",
        "wireToolInputs",
        "permissionDecision",
        "sourceToolUseID",
        "interruptedMessageId",
        "toolDenialKind",
        "attributionSkill",
        "attributionAgent",
        "attributionMcpServer",
        "attributionMcpTool",
    }
    tools = {"Bash", "Edit", "Read", "Write", "Agent"}
    seen: set[str] = set()
    calls: set[str] = set()
    kept: list[bytes] = []
    for line in path.read_bytes().splitlines(keepends=True):
        if len(line) > 4096:
            continue
        record = json_loads(line, dict[str, object])
        kind = record.get("type")
        if not isinstance(kind, str):
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        tool_call = isinstance(content, list) and any(
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") in tools
            for block in content
        )
        uuid = record.get("uuid")
        if tool_call and isinstance(uuid, str):
            calls.add(uuid)
        source = record.get("sourceToolAssistantUUID")
        tool_result = isinstance(source, str) and source in calls
        new_fields = fields & record.keys()
        if (
            (kind in kinds and kind not in seen)
            or new_fields
            or tool_call
            or tool_result
        ):
            kept.append(line)
            seen.add(kind)
            fields.difference_update(new_fields)
    path.write_bytes(b"".join(kept))
    return path
