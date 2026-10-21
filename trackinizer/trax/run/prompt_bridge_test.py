"""Tests for the prompt bridge: claude's prompts reach Chat and are answered from it.

Each hook is :func:`prompt_bridge.ask` on a thread of its own, over a real Unix
socket, as claude's hook process runs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import json
import socket
import threading

from treekle import PlainTree, from_plain, json_loads

import pytest

from trackinizer.trax.daemon.protocol import socket_address
from trackinizer.trax.run import prompt_bridge
from trackinizer.trax.run.prompt_bridge import PromptBridge


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from trackinizer.lib.agent.types.sessions import SystemMessage


_JOIN_SEC = 2.0
"""How long a test waits on a hook thread or a record before it fails."""

_QUESTIONS: PlainTree = [
    {
        "question": "Pick a color",
        "header": "Color",
        "options": [{"label": "red"}, {"label": "blue"}],
        "multiSelect": False,
    },
]


class _Records:
    """The records the bridge writes, with a wait for the next ones."""

    def __init__(self) -> None:
        self.records: list[SystemMessage] = []
        self._changed = threading.Condition()

    def append(self, record: SystemMessage) -> None:
        with self._changed:
            self.records.append(record)
            self._changed.notify_all()

    def wait_for(self, count: int) -> list[SystemMessage]:
        with self._changed:
            assert self._changed.wait_for(
                lambda: len(self.records) >= count,
                timeout=_JOIN_SEC,
            ), f"{len(self.records)} records, waited for {count}"
            return list(self.records)


class _Bridge:
    """A started bridge, what it wrote, and its pending flag."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records = _Records()
        self.pending = threading.Event()
        self.bridge = PromptBridge(
            path,
            emit=self.records.append,
            pending=self.pending,
        )
        self.bridge.start()


class _Hook:
    """One blocking hook process, waiting on the bridge on a thread of its own."""

    def __init__(self, path: Path, event: dict[str, PlainTree]) -> None:
        self._reply: list[str] = []
        self._thread = threading.Thread(
            target=lambda: self._reply.append(
                prompt_bridge.ask(path, json.dumps(event)),
            ),
            daemon=True,
        )
        self._thread.start()

    def reply(self) -> str:
        """Return what the hook printed, once the bridge let it go."""
        self._thread.join(timeout=_JOIN_SEC)
        assert not self._thread.is_alive(), "the hook is still held"
        return self._reply[0]


@pytest.fixture
def bridge(tmp_path: Path) -> Iterator[_Bridge]:
    """Start a bridge on a socket in the test's own directory; close it after."""
    started = _Bridge(socket_address(tmp_path / "bridge.sock"))
    try:
        yield started
    finally:
        started.bridge.close()


def _event(
    name: str,
    *,
    tool: str = "Bash",
    tool_input: PlainTree = None,
    tool_use_id: str = "",
    prompt: str = "p1",
) -> dict[str, PlainTree]:
    """One hook event as claude writes it on the hook's stdin."""
    event: dict[str, PlainTree] = {
        "session_id": "s1",
        "hook_event_name": name,
        "prompt_id": prompt,
    }
    if tool_input is not None:
        event |= {"tool_name": tool, "tool_input": tool_input}
    if tool_use_id:
        event["tool_use_id"] = tool_use_id
    return event


def _ask(bridge: _Bridge, event: dict[str, PlainTree]) -> str:
    """Run one hook that the bridge answers at once."""
    return prompt_bridge.ask(bridge.path, json.dumps(event))


def _open(
    bridge: _Bridge,
    *,
    tool: str = "Bash",
    tool_input: PlainTree = None,
    tool_use_id: str = "t1",
) -> tuple[_Hook, dict[str, PlainTree]]:
    """Run ``PreToolUse`` then a held ``PermissionRequest``; return it and its card."""
    tool_input = {"command": "ls"} if tool_input is None else tool_input
    seen = len(bridge.records.records)
    _ = _ask(
        bridge,
        _event("PreToolUse", tool=tool, tool_input=tool_input, tool_use_id=tool_use_id),
    )
    hook = _Hook(
        bridge.path,
        _event("PermissionRequest", tool=tool, tool_input=tool_input),
    )
    opened = bridge.records.wait_for(seen + 1)[-1]
    return hook, from_plain(opened.extra["$interaction"], dict[str, PlainTree])


def _resolved(record: SystemMessage) -> PlainTree:
    """Return a record's resolution, without its id."""
    resolved = from_plain(record.extra["$interaction_resolved"], dict[str, PlainTree])
    return {key: value for key, value in resolved.items() if key != "id"}


def _decision(reply: str) -> PlainTree:
    """Return the decision a hook reply hands claude."""
    output = json_loads(reply, dict[str, dict[str, PlainTree]])["hookSpecificOutput"]
    assert output["hookEventName"] == "PermissionRequest"
    return output["decision"]


class TestPermission:
    """A tool asks for permission; Chat or the terminal answers."""

    def test_a_prompt_opens_one_card_and_chat_answers_it(self, bridge: _Bridge) -> None:
        hook, card = _open(bridge)

        assert card == {
            "id": card["id"],
            "type": "permission",
            "tool": "Bash",
            "input": {"command": "ls"},
            "questions": None,
            "plan": None,
            "state": "pending",
        }
        assert bridge.pending.is_set()
        assert bridge.bridge.answer(
            {"prompt_id": card["id"], "decision": "allow", "updated_input": None},
        )
        assert _decision(hook.reply()) == {"behavior": "allow"}
        [_, resolved] = bridge.records.wait_for(2)
        assert resolved.extra["$interaction_resolved"] == {
            "id": card["id"],
            "decision": "allow",
            "by": "chat",
        }
        assert not bridge.pending.is_set()

    def test_a_denial_carries_its_message(self, bridge: _Bridge) -> None:
        hook, card = _open(bridge)

        assert bridge.bridge.answer(
            {"prompt_id": card["id"], "decision": "deny", "message": "not now"},
        )

        assert _decision(hook.reply()) == {"behavior": "deny", "message": "not now"}
        assert _resolved(bridge.records.wait_for(2)[-1]) == {
            "decision": "deny",
            "by": "chat",
        }

    def test_an_answer_in_the_terminal_releases_the_hook(self, bridge: _Bridge) -> None:
        hook, card = _open(bridge)

        # The tool ran, so its input now holds what it ran with too.
        _ = _ask(
            bridge,
            _event(
                "PostToolUse",
                tool_input={"command": "ls", "description": "list"},
                tool_use_id="t1",
            ),
        )

        assert hook.reply() == ""
        assert _resolved(bridge.records.wait_for(2)[-1]) == {
            "decision": "released",
            "by": "terminal",
        }
        assert not bridge.pending.is_set()
        assert not bridge.bridge.answer({"prompt_id": card["id"], "decision": "allow"})

    def test_a_prompt_with_no_call_before_it_is_released_by_its_tool(
        self,
        bridge: _Bridge,
    ) -> None:
        hook = _Hook(
            bridge.path,
            _event("PermissionRequest", tool_input={"command": "ls"}),
        )
        _ = bridge.records.wait_for(1)

        _ = _ask(
            bridge,
            _event("PostToolUse", tool_input={"command": "ls"}, tool_use_id="t9"),
        )

        assert hook.reply() == ""
        assert _resolved(bridge.records.wait_for(2)[-1]) == {
            "decision": "released",
            "by": "terminal",
        }

    def test_another_tool_finishing_leaves_the_prompt_open(
        self,
        bridge: _Bridge,
    ) -> None:
        hook, card = _open(bridge)

        _ = _ask(
            bridge,
            _event("PostToolUse", tool="Read", tool_input={}, tool_use_id="t2"),
        )

        assert bridge.pending.is_set()
        assert bridge.bridge.answer({"prompt_id": card["id"], "decision": "allow"})
        assert _decision(hook.reply()) == {"behavior": "allow"}

    def test_a_hook_that_goes_away_releases_its_prompt(self, bridge: _Bridge) -> None:
        # Claude ends a hook past its timeout, and on Esc in the TUI.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(bridge.path))
            event = _event("PermissionRequest", tool_input={"command": "ls"})
            conn.sendall(json.dumps(event).encode() + b"\n")
            _ = bridge.records.wait_for(1)

        assert _resolved(bridge.records.wait_for(2)[-1]) == {
            "decision": "released",
            "by": "terminal",
        }
        assert not bridge.pending.is_set()


class TestQuestionAndPlan:
    """``AskUserQuestion`` and ``ExitPlanMode`` ask through the same hook."""

    def test_answers_go_back_in_the_updated_input(self, bridge: _Bridge) -> None:
        hook, card = _open(
            bridge,
            tool="AskUserQuestion",
            tool_input={"questions": _QUESTIONS},
        )

        assert (card["type"], card["questions"], card["plan"]) == (
            "question",
            _QUESTIONS,
            None,
        )
        answers: PlainTree = {"Pick a color": "blue"}
        assert bridge.bridge.answer(
            {
                "prompt_id": card["id"],
                "decision": "allow",
                "updated_input": {"answers": answers},
                "message": "",
            },
        )
        assert _decision(hook.reply()) == {
            "behavior": "allow",
            "updatedInput": {"questions": _QUESTIONS, "answers": answers},
        }
        assert _resolved(bridge.records.wait_for(2)[-1]) == {
            "decision": "answered",
            "by": "chat",
        }
        assert "Pick a color" in (bridge.records.records[0].content or "")

    def test_a_plan_shows_its_text(self, bridge: _Bridge) -> None:
        hook, card = _open(
            bridge,
            tool="ExitPlanMode",
            tool_input={"plan": "Write ok.txt", "planFilePath": "/p.md"},
        )

        assert (card["type"], card["plan"], card["questions"]) == (
            "plan",
            "Write ok.txt",
            None,
        )
        assert bridge.bridge.answer({"prompt_id": card["id"], "decision": "allow"})
        assert _decision(hook.reply()) == {"behavior": "allow"}


class TestRelease:
    """The turn or the run ending lets every held hook go."""

    @pytest.mark.parametrize("ending", ["Stop", "SessionEnd"])
    def test_the_turn_ending_releases_every_prompt(
        self,
        bridge: _Bridge,
        ending: str,
    ) -> None:
        first, _ = _open(bridge, tool_input={"command": "ls"}, tool_use_id="t1")
        second, _ = _open(bridge, tool_input={"command": "pwd"}, tool_use_id="t2")

        _ = _ask(bridge, _event(ending))

        assert (first.reply(), second.reply()) == ("", "")
        records = bridge.records.wait_for(4)
        assert [_resolved(record) for record in records[2:]] == [
            {"decision": "released", "by": "run"},
        ] * 2
        assert not bridge.pending.is_set()

    def test_closing_releases_every_prompt_and_stops_listening(
        self,
        bridge: _Bridge,
    ) -> None:
        hook, _ = _open(bridge)

        bridge.bridge.close()

        assert hook.reply() == ""
        assert _resolved(bridge.records.wait_for(2)[-1]) == {
            "decision": "released",
            "by": "run",
        }
        assert _ask(bridge, _event("Stop")) == ""
        bridge.bridge.close()


class TestAnswer:
    """What Chat sends is checked before it reaches a hook."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"prompt_id": "nobody", "decision": "allow"},
            {"prompt_id": "x", "decision": "maybe"},
            {"decision": "allow"},
            {"prompt_id": "x", "decision": "allow", "extra": 1},
        ],
    )
    def test_an_answer_to_no_open_prompt_is_refused(
        self,
        bridge: _Bridge,
        payload: dict[str, object],
    ) -> None:
        assert not bridge.bridge.answer(payload)


class TestAsk:
    """The hook's side: whatever fails prints nothing, so claude asks in its TUI."""

    def test_no_bridge_listening_prints_nothing(self, tmp_path: Path) -> None:
        path = socket_address(tmp_path / "none.sock")

        assert prompt_bridge.ask(path, json.dumps(_event("Stop"))) == ""

    def test_what_is_not_an_event_is_not_sent(self, bridge: _Bridge) -> None:
        assert prompt_bridge.ask(bridge.path, "not json") == ""
        assert bridge.records.records == []

    def test_an_event_the_bridge_cannot_read_is_let_go(self, bridge: _Bridge) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(bridge.path))
            conn.sendall(b"[1]\n")
            assert conn.recv(16) == b""


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
