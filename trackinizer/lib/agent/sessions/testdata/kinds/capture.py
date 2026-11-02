"""The claude-kinds and codex-kinds capture sessions, beside the fixtures they write.

``capture.py --cli claude-kinds`` and ``--cli codex-kinds`` run them; see that
script's docstring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import json
import sys

from treekle import from_plain, json_dumps, json_loads

from trackinizer.lib.agent.sessions.testdata.capture import (
    _codex_flags,
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
def capture_claude_kinds(
    root: Path,
    out: Path,
    *,
    turn_sec: float,
    timeout_sec: int,
) -> list[Path]:
    """Drive the claude-kinds session and return the fixture path written.

    Args:
      root: The scratch directory the session's home and workspace go in.
      out: The directory the fixture is written to.
      turn_sec: How long each scripted turn waits for its answer.
      timeout_sec: The bound on the whole session.

    Returns:
      written: The fixture path for the session's main transcript.

    """
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


# One line per payload type ``_session_script`` left uncaptured, then a fork of the
# session, which ``codex fork --last`` starts as a rollout of its own naming the thread
# it forked from:
#   1. a function call, ``update_plan``, and its output
#   2. a web search, which only ``web_search="live"`` lets the model make
#   3. ``apply_patch``, which records a patch event and a file-change item
#   4. ``/compact``, which records the compaction.
def capture_codex_kinds(
    root: Path,
    out: Path,
    *,
    turn_sec: float,
    timeout_sec: int,
) -> list[Path]:
    """Drive the codex-kinds session and its fork and return the fixture paths written.

    Args:
      root: The scratch directory the session's home and workspace go in.
      out: The directory the fixtures are written to.
      turn_sec: How long each scripted turn waits for its answer.
      timeout_sec: The bound on each session.

    Returns:
      written: The fixture paths, one per rollout: the session's and its fork's.

    """
    home, work = _prepare(root, real_home=Path.home() / ".codex")  # noqa: TID251 -- vendor fixed path, not ours (AGENTS.md rule 3)  # house-ignore[xdg-literal] -- Vendor CLI's fixed home path, not ours (AGENTS.md rule 3).
    flags = [*_codex_flags(work), "-c", 'web_search="live"']
    env = {"CODEX_HOME": str(home)}
    _drive(
        ["codex", *flags],
        (
            _Step(
                line=(
                    "Use your update_plan tool to record a two-step plan, then mark "
                    "both steps completed."
                ),
            ),
            _Step(
                line=(
                    "Search the web for the current stable Python version and state "
                    "it in one line."
                ),
            ),
            _Step(
                line=(
                    'Use apply_patch to create note.txt containing "beta", then '
                    "apply_patch again to change beta to gamma."
                ),
            ),
            _Step(line="/compact"),
        ),
        cwd=work,
        env=env,
        turn_sec=turn_sec,
        timeout_sec=timeout_sec,
    )
    _drive(
        ["codex", "fork", "--last", *flags],
        (_Step(line="Say exactly: forked"),),
        cwd=work,
        env=env,
        turn_sec=turn_sec,
        timeout_sec=timeout_sec,
    )
    out.mkdir(parents=True, exist_ok=True)
    return [
        _first_of_each_kind(
            _emit(
                path,
                out
                / (
                    "codex_fork.jsonl"
                    if "forked_from_id"
                    in path.read_text(encoding="utf-8").partition("\n")[0]
                    else "codex_main.jsonl"
                ),
                home=home,
                work=work,
            ),
        )
        for path in sorted((home / "sessions").rglob("rollout-*.jsonl"))
    ]


# A kinds rollout is captured for each payload type's key order, which one line of a
# type shows as well as twenty: the rest repeat it, and the fork replays its parent.
# Codex writes compact JSON, so rewriting a line changes only its cut strings.
def _first_of_each_kind(path: Path) -> Path:
    """Keep the first line of each line, payload and item type, strings cut short."""
    seen: set[tuple[object, object, object]] = set()
    kept: list[str] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        record = json_loads(line, dict[str, object])
        payload = from_plain(record.get("payload"), dict[str, object], default={})
        # An ``item_completed`` event names its item's own type one level down.
        item = from_plain(payload.get("item"), dict[str, object], default={})
        kind = (record.get("type"), payload.get("type"), item.get("type"))
        if kind not in seen:
            seen.add(kind)
            kept.append(
                json_dumps(_trimmed(record), ensure_ascii=False, separators=(",", ":"))
                + "\n",
            )
    path.write_text("".join(kept), encoding="utf-8")
    return path


# Instructions, compaction histories and tool outputs run to tens of KB a string and
# no key order hangs on their text; uncut, the fixture outgrows the testdata ceiling.
def _trimmed(value: object) -> object:
    """Return ``value`` with every string in it cut to its first 256 characters."""
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, dict):
        return {key: _trimmed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trimmed(item) for item in value]
    return value


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
        message = from_plain(record.get("message"), dict[str, object], default={})
        # A message's content is its text, or a list of blocks.
        blocks = (
            from_plain(message["content"], list[dict[str, object] | None])
            if isinstance(message.get("content"), list)
            else []
        )
        tool_call = any(
            block is not None
            and block.get("type") == "tool_use"
            and block.get("name") in tools
            for block in blocks
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
