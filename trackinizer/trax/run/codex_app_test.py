"""Tests for the codex app-server driver, run against a scripted fake app-server.

The fake (:mod:`.codex_app_fake`) stands in for ``codex app-server``: it answers each
request from a script and logs every frame the driver sends, so a test asserts the
JSON-RPC the driver wrote and the events it produced. The frames the script plays are
the ones a live codex 0.163.0 sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import asyncio
import json
import logging
import re
import sys

from treekle import PlainTree, from_plain, json_loads, to_plain

import pytest

from trackinizer.trax.run.asks import (
    Answer,
    Ask,
    Driver,
    Option,
    Resolved,
    StateChanged,
)
from trackinizer.trax.run.codex_app import CodexAppDriver, CodexAppError


_THIS: Final = Path(__file__).resolve()
_CWD: Final = _THIS.parent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence


type _Event = Ask | Resolved | StateChanged
_Tree = dict[str, PlainTree]
type _Frame = _Tree | str
type _Frames = Mapping[str, Sequence[_Frame]]

_FAKE: Final = _THIS.with_name("codex_app_fake.py")
_THREAD: Final = "thr_1"
_TURN: Final = "turn_1"

_REPLIES: Final[dict[str, _Tree]] = {
    "thread/start": {"thread": {"id": _THREAD}},
    "thread/resume": {"thread": {"id": _THREAD}},
    "turn/start": {"turn": {"id": _TURN}},
}

_AMEND: Final[PlainTree] = {
    "acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["touch"]},
}
_ASK_ID: Final = re.compile(r"[0-9a-f]{8}-\d+")


def _amend() -> PlainTree:
    """Return a fresh amendment decision: ``to_plain`` writes a shared one as a ref."""
    return {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["touch"]}}


def _status(*flags: str, kind: str = "active") -> _Tree:
    """Return a ``thread/status/changed`` frame; only an active status has flags."""
    status: _Tree = {"type": kind}
    if kind == "active":
        status["activeFlags"] = list(flags)
    return {
        "method": "thread/status/changed",
        "params": {"threadId": _THREAD, "status": status},
    }


def _turn(method: str, turn: str) -> _Tree:
    """Return a ``turn/started`` or ``turn/completed`` frame."""
    return {"method": method, "params": {"threadId": _THREAD, "turn": {"id": turn}}}


def _command(request_id: int = 0, **extra: PlainTree) -> _Tree:
    return {
        "method": "item/commandExecution/requestApproval",
        "id": request_id,
        "params": {
            "kind": "command",
            "threadId": _THREAD,
            "turnId": _TURN,
            "itemId": "item_1",
            "startedAtMs": 1,
            "reason": "Outside the workspace",
            "command": "touch /x",
            "cwd": "/w",
            "availableDecisions": ["accept", _amend(), "cancel"],
            **extra,
        },
    }


def _item_started(kind: str, changes: Sequence[_Tree] | None = ()) -> _Tree:
    item: _Tree = {"type": kind, "id": "item_2"}
    if changes is not None:
        item["changes"] = list(changes)
    return {
        "method": "item/started",
        "params": {"item": item, "threadId": _THREAD, "turnId": _TURN},
    }


def _item_completed(kind: str = "fileChange", item_id: str = "item_2") -> _Tree:
    return {
        "method": "item/completed",
        "params": {"item": {"type": kind, "id": item_id}, "threadId": _THREAD},
    }


def _change(path: str, kind: str, diff: str) -> _Tree:
    return {"path": path, "kind": {"type": kind}, "diff": diff}


def _file_request(request_id: int = 1, **extra: PlainTree) -> _Tree:
    return {
        "method": "item/fileChange/requestApproval",
        "id": request_id,
        "params": {
            "threadId": _THREAD,
            "turnId": _TURN,
            "itemId": "item_2",
            "startedAtMs": 1,
            "reason": None,
            "grantRoot": None,
            **extra,
        },
    }


def _permissions(request_id: int = 2) -> _Tree:
    return {
        "method": "item/permissions/requestApproval",
        "id": request_id,
        "params": {
            "threadId": _THREAD,
            "turnId": _TURN,
            "itemId": "item_3",
            "cwd": "/w",
            "reason": "Write elsewhere",
            "permissions": {"network": {"enabled": True}, "fileSystem": None},
        },
    }


_QUESTIONS: Final[list[PlainTree]] = [
    {
        "id": "color_preference",
        "header": "Color",
        "question": "Which color?",
        "isOther": True,
        "isSecret": False,
        "options": [
            {"label": "Red", "description": "Choose red."},
            {"label": "Blue", "description": "Choose blue."},
        ],
    },
]


def _question(request_id: int = 3) -> _Tree:
    return {
        "method": "item/tool/requestUserInput",
        "id": request_id,
        "params": {
            "threadId": _THREAD,
            "turnId": _TURN,
            "itemId": "item_4",
            "isBlocking": False,
            "questions": _QUESTIONS,
        },
    }


def _elicitation(request_id: int = 4, **extra: PlainTree) -> _Tree:
    return {
        "method": "mcpServer/elicitation/request",
        "id": request_id,
        "params": {
            "threadId": _THREAD,
            "turnId": None,
            "serverName": "docs",
            "mode": "form",
            "message": "Which repo?",
            "requestedSchema": {"type": "object", "properties": {}},
            **extra,
        },
    }


def _resolved(request_id: int) -> _Tree:
    return {
        "method": "serverRequest/resolved",
        "params": {"threadId": _THREAD, "requestId": request_id},
    }


class _Rig:
    """A driver wired to a scripted fake app-server, and the log of what it sent."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        replies: Mapping[str, PlainTree] | None = None,
        after: _Frames | None = None,
        on_response: _Frames | None = None,
        argv: tuple[str, ...] | None = None,
    ) -> None:
        self.log = tmp_path / "frames.jsonl"
        self.log.touch()
        script = tmp_path / "script.json"
        script.write_text(
            json.dumps(
                to_plain(
                    {
                        "log": str(self.log),
                        "replies": {**_REPLIES, **(replies or {})},
                        "after": after or {},
                        "on_response": on_response or {},
                    },
                ),
            ),
        )
        self.cwd = tmp_path
        self.argv = argv or (sys.executable, str(_FAKE), str(script))
        self.driver = CodexAppDriver(argv=self.argv)

    async def start(
        self,
        *,
        resume: str = "",
        model: str = "",
        effort: str = "",
    ) -> str:
        return await self.driver.start(
            cwd=self.cwd,
            resume=resume,
            model=model,
            effort=effort,
        )

    def frames(self) -> list[_Tree]:
        return [
            from_plain(json_loads(line), _Tree)
            for line in self.log.read_text().splitlines()
        ]

    def called(self, method: str) -> list[_Tree]:
        """Return the requests of ``method`` the driver sent, as whole frames."""
        return [each for each in self.frames() if each.get("method") == method]

    def params(self, method: str) -> _Tree:
        """Return the params of the first request of ``method`` the driver sent."""
        return from_plain(self.called(method)[0]["params"], _Tree)

    def methods(self) -> list[str]:
        return [
            from_plain(each.get("method"), str, default="") for each in self.frames()
        ]

    def response(self, request_id: int) -> PlainTree:
        """Return the result the driver sent to the server request ``request_id``."""
        frame = next(
            each
            for each in self.frames()
            if each.get("id") == request_id and "method" not in each
        )
        return frame.get("result", frame.get("error"))

    def run[T](self, scenario: Coroutine[object, object, T]) -> T:
        """Run ``scenario`` to its end and reap the app-server, whatever it did."""

        async def bounded() -> T:
            try:
                return await asyncio.wait_for(scenario, 10)
            finally:
                await self.driver.stop()

        return asyncio.run(bounded())

    async def sent(self, frames: int) -> None:
        """Wait until the fake has logged ``frames`` frames."""
        while len(self.log.read_text().splitlines()) < frames:  # noqa: ASYNC110 -- The fake is another process; only its log file says what it got.
            await asyncio.sleep(0.002)


async def _next(events: AsyncIterator[_Event]) -> _Event:
    return await asyncio.wait_for(anext(events), 5)


async def _ask(events: AsyncIterator[_Event]) -> Ask:
    """Return the next ask, skipping the state changes before it."""
    while True:
        event = await _next(events)
        if isinstance(event, Ask):
            return event


def _of[T: Ask | Resolved | StateChanged](seen: list[_Event], kind: type[T]) -> list[T]:
    return [each for each in seen if isinstance(each, kind)]


async def _collect(events: AsyncIterator[_Event]) -> list[_Event]:
    return [each async for each in events]


async def _asked(
    rig: _Rig,
    *,
    option: str,
    updated_input: dict[str, PlainTree] | None = None,
    stop_after: int = 5,
) -> tuple[Ask, list[_Event]]:
    """Run a turn that asks, answer its first ask, and return it with the events after."""
    await rig.start()
    events = rig.driver.events()
    await rig.driver.send_line("go")
    ask = await _ask(events)
    await rig.driver.answer(
        Answer(ask_id=ask.id, option=option, updated_input=updated_input, who="jp"),
    )
    await rig.sent(stop_after)
    await rig.driver.stop()
    return ask, await _collect(events)


async def _stopped(rig: _Rig) -> None:
    await rig.start()
    await rig.driver.stop()


# ---------------------------------------------------------------- start and resume.


def test_start_runs_the_handshake_then_starts_a_thread(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> str:
        try:
            return await rig.start(model="gpt-6-luna")
        finally:
            await rig.driver.stop()

    assert rig.run(scenario()) == _THREAD
    assert rig.methods() == ["initialize", "initialized", "thread/start"]
    assert [each.get("id") for each in rig.frames()] == [1, None, 2]
    assert Path(f"{rig.log}.cwd").read_text() == str(tmp_path)
    init = rig.params("initialize")
    assert init["clientInfo"] == {
        "name": "trax-run",
        "title": "trax run",
        "version": "1",
    }
    capabilities = from_plain(init["capabilities"], _Tree)
    assert capabilities["experimentalApi"] is True
    quiet = from_plain(capabilities["optOutNotificationMethods"], list[str])
    assert "item/agentMessage/delta" in quiet
    assert rig.params("thread/start") == {
        "cwd": str(tmp_path),
        "model": "gpt-6-luna",
        "approvalPolicy": "on-request",
        "sandbox": "workspace-write",
        "approvalsReviewer": "user",
    }


def test_start_sends_the_policy_the_driver_was_built_with(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.driver = CodexAppDriver(
        argv=rig.argv,
        approval_policy="never",
        sandbox="danger-full-access",
        reviewer="auto_review",
    )
    rig.run(_stopped(rig))
    assert rig.params("thread/start") == {
        "cwd": str(tmp_path),
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
        "approvalsReviewer": "auto_review",
    }


def test_resume_resumes_the_thread_by_id(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> str:
        try:
            return await rig.start(resume="thr_old", model="m")
        finally:
            await rig.driver.stop()

    assert rig.run(scenario()) == _THREAD
    assert "thread/start" not in rig.methods()
    assert rig.params("thread/resume") == {
        "threadId": "thr_old",
        "cwd": str(tmp_path),
        "model": "m",
        "approvalPolicy": "on-request",
        "sandbox": "workspace-write",
        "approvalsReviewer": "user",
    }


def test_a_refused_resume_raises_and_ends_the_app_server(tmp_path: Path) -> None:
    error: PlainTree = {"code": -32_600, "message": "invalid session id: bad length"}
    rig = _Rig(tmp_path, replies={"thread/resume": {"$error": error}})

    async def scenario() -> list[_Event]:
        with pytest.raises(CodexAppError, match="invalid session id") as raised:
            await rig.start(resume="x")
        assert raised.value.code == -32_600
        return await _collect(rig.driver.events())

    assert rig.run(scenario()) == []
    assert "thread/start" not in rig.methods()


def test_a_resume_with_no_rollout_starts_a_new_thread_in_its_place(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error: PlainTree = {"code": -32_600, "message": "no rollout found for thread id x"}
    rig = _Rig(tmp_path, replies={"thread/resume": {"$error": error}})

    async def scenario() -> str:
        try:
            return await rig.start(resume="x", model="m")
        finally:
            await rig.driver.stop()

    assert rig.run(scenario()) == _THREAD
    assert rig.methods()[2:] == ["thread/resume", "thread/start"]
    assert rig.params("thread/start") == {
        "cwd": str(tmp_path),
        "model": "m",
        "approvalPolicy": "on-request",
        "sandbox": "workspace-write",
        "approvalsReviewer": "user",
    }
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "thread x has no rollout to resume; starting a new thread"
    )


def test_a_driver_starts_once(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> None:
        await rig.start()
        await rig.driver.stop()
        with pytest.raises(RuntimeError, match=r"^a CodexAppDriver starts once"):
            await rig.start()

    rig.run(scenario())
    assert rig.methods().count("initialize") == 1


def test_the_driver_meets_the_driver_contract() -> None:
    driver: Driver = CodexAppDriver()
    assert isinstance(driver, CodexAppDriver)


def test_a_missing_binary_raises(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, argv=("codex-app-fake-that-does-not-exist",))
    with pytest.raises(FileNotFoundError):
        rig.run(rig.start())


def test_model_and_effort_go_through_the_thread_settings(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> None:
        await rig.start(model="m", effort="low")
        await rig.sent(4)
        await rig.driver.stop()

    rig.run(scenario())
    assert rig.params("thread/settings/update") == {
        "threadId": _THREAD,
        "model": "m",
        "effort": "low",
    }


def test_a_call_before_start_raises(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    with pytest.raises(CodexAppError, match=r"^the codex app-server is not running$"):
        rig.run(rig.driver.send_line("go"))


# ---------------------------------------------------------- lines, commands, settings.


def test_a_line_starts_a_turn_and_steers_the_turn_in_progress(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> None:
        await rig.start()
        await rig.driver.send_line("first")
        await rig.driver.send_line("second")
        await rig.driver.stop()

    rig.run(scenario())
    assert rig.methods()[-2:] == ["turn/start", "turn/steer"]
    text: PlainTree = [{"type": "text", "text": "first"}]
    assert rig.params("turn/start") == {"threadId": _THREAD, "input": text}
    assert rig.params("turn/steer") == {
        "threadId": _THREAD,
        "expectedTurnId": _TURN,
        "input": [{"type": "text", "text": "second"}],
    }


def test_a_refused_steer_falls_back_to_a_new_turn(tmp_path: Path) -> None:
    stale: PlainTree = {"code": -32_600, "message": "expected turn id is stale"}
    rig = _Rig(tmp_path, replies={"turn/steer": {"$error": stale}})

    async def scenario() -> None:
        await rig.start()
        await rig.driver.send_line("first")
        await rig.driver.send_line("second")
        await rig.driver.stop()

    rig.run(scenario())
    assert rig.methods()[-3:] == ["turn/start", "turn/steer", "turn/start"]


@pytest.mark.parametrize(
    ("completed", "steered"),
    [(_TURN, False), ("turn_other", True)],
)
def test_only_the_turn_that_completed_stops_being_steered(
    tmp_path: Path,
    completed: str,
    steered: bool,
) -> None:
    rig = _Rig(
        tmp_path,
        after={"turn/start": [_turn("turn/completed", completed), _status()]},
    )

    async def scenario() -> None:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("first")
        assert await _next(events) == StateChanged(state="running")
        await rig.driver.send_line("second")
        await rig.driver.stop()

    rig.run(scenario())
    assert rig.methods()[-1] == ("turn/steer" if steered else "turn/start")


def test_a_turn_the_app_server_reports_is_steered_and_interrupted(
    tmp_path: Path,
) -> None:
    rig = _Rig(
        tmp_path,
        replies={"turn/start": {}},
        after={"turn/start": [_turn("turn/started", "turn_9"), _status()]},
    )

    async def scenario() -> None:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("first")
        assert await _next(events) == StateChanged(state="running")
        await rig.driver.send_line("second")
        await rig.driver.interrupt()
        await rig.driver.stop()

    rig.run(scenario())
    assert rig.params("turn/steer")["expectedTurnId"] == "turn_9"
    assert rig.params("turn/interrupt") == {"threadId": _THREAD, "turnId": "turn_9"}


def test_compact_and_goal_commands_map_to_their_methods(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> None:
        await rig.start()
        await rig.driver.command("/compact", "")
        await rig.driver.command("goal", "ship the driver")
        await rig.driver.command("/goal", "pause")
        await rig.driver.command("/goal", "resume")
        await rig.driver.command("/goal", "clear")
        await rig.driver.command("/goal", "")
        await rig.driver.stop()

    rig.run(scenario())
    assert rig.methods()[3:] == [
        "thread/compact/start",
        "thread/goal/set",
        "thread/goal/set",
        "thread/goal/set",
        "thread/goal/clear",
        "thread/goal/get",
    ]
    thread: _Tree = {"threadId": _THREAD}
    user: _Tree = {**thread, "origin": "user"}
    sets = [from_plain(each["params"], _Tree) for each in rig.called("thread/goal/set")]
    assert sets == [
        {**user, "objective": "ship the driver", "status": "active"},
        {**user, "status": "paused"},
        {**user, "status": "active"},
    ]
    assert rig.params("thread/compact/start") == thread
    assert rig.params("thread/goal/clear") == user
    assert rig.params("thread/goal/get") == thread


def test_an_unknown_command_is_refused(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> None:
        await rig.start()
        try:
            with pytest.raises(ValueError, match="/review"):
                await rig.driver.command("/review", "")
        finally:
            await rig.driver.stop()

    rig.run(scenario())


def test_settings_change_the_model_and_effort_it_is_given(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> None:
        await rig.start()
        await rig.driver.settings(model="gpt-6-luna", effort="")
        await rig.driver.settings(model="", effort="high")
        await rig.driver.stop()

    rig.run(scenario())
    updates = [
        from_plain(each["params"], _Tree)
        for each in rig.called("thread/settings/update")
    ]
    assert updates == [
        {"threadId": _THREAD, "model": "gpt-6-luna"},
        {"threadId": _THREAD, "effort": "high"},
    ]


def test_interrupt_stops_the_turn_in_progress_and_does_nothing_between_turns(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> None:
        await rig.start()
        await rig.driver.interrupt()
        await rig.driver.send_line("go")
        await rig.driver.interrupt()
        await rig.driver.stop()

    rig.run(scenario())
    assert rig.methods().count("turn/interrupt") == 1
    assert rig.params("turn/interrupt") == {"threadId": _THREAD, "turnId": _TURN}


def test_calls_in_flight_together_each_get_their_own_reply(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> None:
        await rig.start()
        await asyncio.gather(
            rig.driver.send_line("go"),
            rig.driver.command("/compact", ""),
        )
        await rig.driver.stop()

    rig.run(scenario())
    calls = rig.called("turn/start") + rig.called("thread/compact/start")
    assert len({each["id"] for each in calls}) == 2


def test_replies_that_name_no_call_of_ours_are_ignored(tmp_path: Path) -> None:
    stray: list[_Frame] = [
        {"id": None, "error": {"code": -32_700, "message": "parse error"}},
        {"id": "x", "result": {}},
        {"id": 999, "result": {}},
        {"id": [1], "result": {}},
    ]
    rig = _Rig(tmp_path, after={"thread/start": stray})

    async def scenario() -> None:
        await rig.start()
        await rig.driver.send_line("go")
        await rig.driver.stop()

    rig.run(scenario())
    assert rig.methods()[-1] == "turn/start"


def test_an_error_reply_carries_its_code_and_message(tmp_path: Path) -> None:
    rig = _Rig(
        tmp_path,
        replies={"thread/settings/update": {"$error": {"message": "no such model"}}},
    )

    async def scenario() -> CodexAppError:
        await rig.start()
        try:
            with pytest.raises(CodexAppError) as raised:
                await rig.driver.settings(model="nope", effort="")
        finally:
            await rig.driver.stop()
        return raised.value

    error = rig.run(scenario())
    assert (str(error), error.code) == ("no such model", 0)


# ------------------------------------------------------------------ command asks.


def test_a_command_approval_becomes_an_ask_with_the_agents_own_options(
    tmp_path: Path,
) -> None:
    rig = _Rig(
        tmp_path,
        after={"turn/start": [_status("waitingOnApproval"), _command()]},
    )

    async def scenario() -> tuple[_Event, Ask]:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("touch x")
        first = await _next(events)
        ask = await _ask(events)
        await rig.driver.stop()
        return first, ask

    first, ask = rig.run(scenario())
    assert first == StateChanged(state="requires_action")
    assert _ASK_ID.fullmatch(ask.id)
    assert ask == Ask(
        id=ask.id,
        provider="codex",
        kind="command",
        title="Run a command",
        tool="shell",
        tool_call_id="item_1",
        command="touch /x",
        cwd="/w",
        reason="Outside the workspace",
        input={"command": "touch /x", "cwd": "/w"},
        options=(
            Option(id="accept", label="Yes", kind="allow_once"),
            Option(
                id="acceptWithExecpolicyAmendment",
                label="Yes, and do not ask again for commands like this",
                kind="allow_always",
            ),
            Option(id="decline", label="No", kind="deny"),
            Option(id="cancel", label="No, and stop the turn", kind="deny_stop"),
        ),
    )


@pytest.mark.parametrize(
    ("option", "decision", "ended"),
    [
        ("accept", "accept", "allow"),
        ("acceptWithExecpolicyAmendment", _AMEND, "allow"),
        ("decline", "decline", "deny"),
        ("cancel", "cancel", "deny"),
        ("allow_once", "accept", "allow"),
        ("deny", "decline", "deny"),
    ],
)
def test_an_answer_sends_the_decision_its_option_stands_for(
    tmp_path: Path,
    option: str,
    decision: PlainTree,
    ended: str,
) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [_command()]})
    ask, after = rig.run(_asked(rig, option=option))
    assert rig.response(0) == {"decision": decision}
    resolved = _of(after, Resolved)[0]
    assert resolved.ask_id == ask.id
    assert (resolved.by, resolved.who, resolved.decision) == ("chat", "jp", ended)


def test_an_answer_naming_no_option_is_refused_and_the_ask_stays_open(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [_command()]})

    async def scenario() -> list[_Event]:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("go")
        ask = await _ask(events)
        with pytest.raises(ValueError, match="allow_always_forever"):
            await rig.driver.answer(
                Answer(ask_id=ask.id, option="allow_always_forever"),
            )
        await rig.driver.answer(Answer(ask_id=ask.id, option="accept"))
        await rig.sent(5)
        await rig.driver.stop()
        return await _collect(events)

    after = rig.run(scenario())
    assert [each.by for each in _of(after, Resolved)] == ["chat"]
    assert rig.response(0) == {"decision": "accept"}


def test_a_second_answer_to_one_ask_is_a_no_op(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [_command()]})

    async def scenario() -> list[_Event]:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("go")
        ask = await _ask(events)
        await rig.driver.answer(Answer(ask_id=ask.id, option="accept"))
        await rig.driver.answer(Answer(ask_id=ask.id, option="decline"))
        await rig.driver.answer(Answer(ask_id="never-asked", option="accept"))
        await rig.sent(5)
        await asyncio.sleep(0.02)
        await rig.driver.stop()
        return await _collect(events)

    after = rig.run(scenario())
    answers = [each for each in rig.frames() if "method" not in each]
    assert answers == [{"id": 0, "result": {"decision": "accept"}}]
    assert len(_of(after, Resolved)) == 1


def test_a_decline_is_offered_when_the_agent_lists_none(tmp_path: Path) -> None:
    only: PlainTree = ["accept", "cancel"]
    rig = _Rig(tmp_path, after={"turn/start": [_command(availableDecisions=only)]})
    ask, _after = rig.run(_asked(rig, option="deny"))
    assert [each.id for each in ask.options] == ["accept", "decline", "cancel"]
    assert rig.response(0) == {"decision": "decline"}


def test_a_decline_follows_the_choices_when_there_is_no_cancel(tmp_path: Path) -> None:
    only: PlainTree = ["accept", "acceptForSession"]
    rig = _Rig(tmp_path, after={"turn/start": [_command(availableDecisions=only)]})
    ask, _after = rig.run(_asked(rig, option="deny"))
    assert [each.id for each in ask.options] == [
        "accept",
        "acceptForSession",
        "decline",
    ]


def test_the_usual_choices_are_offered_when_the_agent_lists_none(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [_command(availableDecisions=None)]})
    ask, _after = rig.run(_asked(rig, option="allow_session"))
    assert ask.options == (
        Option(id="accept", label="Yes", kind="allow_once"),
        Option(
            id="acceptForSession",
            label="Yes, for this session",
            kind="allow_session",
        ),
        Option(id="decline", label="No", kind="deny"),
        Option(id="cancel", label="No, and stop the turn", kind="deny_stop"),
    )
    assert rig.response(0) == {"decision": "acceptForSession"}


def test_a_choice_the_driver_does_not_know_is_not_offered(tmp_path: Path) -> None:
    offered: list[PlainTree] = [
        "accept",
        "approveEverything",
        {"someNewAmendment": {}},
        7,
    ]
    rig = _Rig(tmp_path, after={"turn/start": [_command(availableDecisions=offered)]})
    ask, _after = rig.run(_asked(rig, option="accept"))
    assert [each.id for each in ask.options] == ["accept", "decline"]


def test_a_network_approval_names_the_host_and_its_policy_choices(
    tmp_path: Path,
) -> None:
    allow: PlainTree = {"action": "allow", "host": "pypi.org"}
    block: PlainTree = {"action": "deny", "host": "pypi.org"}
    request = _command(
        command=None,
        networkApprovalContext={"host": "pypi.org", "protocol": "https"},
        availableDecisions=[
            "accept",
            {"applyNetworkPolicyAmendment": {"network_policy_amendment": allow}},
            {"applyNetworkPolicyAmendment": {"network_policy_amendment": block}},
            "decline",
        ],
    )
    rig = _Rig(tmp_path, after={"turn/start": [request]})
    ask, _after = rig.run(
        _asked(rig, option="applyNetworkPolicyAmendment:allow:pypi.org"),
    )
    assert ask == Ask(
        id=ask.id,
        provider="codex",
        kind="command",
        title="Allow network access to pypi.org",
        tool="shell",
        tool_call_id="item_1",
        cwd="/w",
        reason="Outside the workspace",
        input={"host": "pypi.org", "protocol": "https", "cwd": "/w"},
        options=(
            Option(id="accept", label="Yes", kind="allow_once"),
            Option(
                id="applyNetworkPolicyAmendment:allow:pypi.org",
                label="Yes, and always allow pypi.org",
                kind="allow_always",
            ),
            Option(
                id="applyNetworkPolicyAmendment:deny:pypi.org",
                label="No, and always block pypi.org",
                kind="deny",
            ),
            Option(id="decline", label="No", kind="deny"),
        ),
    )
    assert rig.response(0) == {
        "decision": {
            "applyNetworkPolicyAmendment": {"network_policy_amendment": allow},
        },
    }


_FILE_OPTIONS: Final = (
    Option(id="accept", label="Yes", kind="allow_once"),
    Option(id="acceptForSession", label="Yes, for this session", kind="allow_session"),
    Option(id="decline", label="No", kind="deny"),
    Option(id="cancel", label="No, and stop the turn", kind="deny_stop"),
)
_HI: Final = _change("/x/a.txt", "add", "hi\n")


@pytest.mark.parametrize(
    ("frames", "diff"),
    [
        ([_item_started("fileChange", [_HI])], "/x/a.txt (add)\nhi\n"),
        (
            [
                _item_started(
                    "fileChange",
                    [_HI, _change("/x/b.txt", "delete", "-b\n")],
                ),
            ],
            "/x/a.txt (add)\nhi\n\n/x/b.txt (delete)\n-b\n",
        ),
        ([_item_started("fileChange")], ""),
        ([_item_started("fileChange", None)], ""),
        ([_item_completed("agentMessage", "msg_1")], ""),
        ([], ""),
        ([_item_started("commandExecution", [_HI])], ""),
        ([_item_started("fileChange", [_HI]), _item_completed()], ""),
    ],
)
def test_a_file_change_ask_carries_the_diff_its_item_started_with(
    tmp_path: Path,
    frames: list[_Tree],
    diff: str,
) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [*frames, _file_request()]})
    ask, _after = rig.run(_asked(rig, option="acceptForSession"))
    assert ask == Ask(
        id=ask.id,
        provider="codex",
        kind="file_change",
        title="Edit files",
        tool="apply_patch",
        tool_call_id="item_2",
        diff=diff,
        options=_FILE_OPTIONS,
    )
    assert rig.response(1) == {"decision": "acceptForSession"}


def test_a_file_change_ask_names_the_root_it_wants_to_write_under(
    tmp_path: Path,
) -> None:
    request = _file_request(reason="Needs more room", grantRoot="/srv")
    rig = _Rig(tmp_path, after={"turn/start": [request]})
    ask, _after = rig.run(_asked(rig, option="accept"))
    assert (ask.reason, ask.input) == ("Needs more room", {"grant_root": "/srv"})


# --------------------------------------------- permissions, questions, elicitations.


@pytest.mark.parametrize(
    ("option", "granted", "scope"),
    [
        ("allow_once", {"network": {"enabled": True}, "fileSystem": None}, "turn"),
        (
            "allow_session",
            {"network": {"enabled": True}, "fileSystem": None},
            "session",
        ),
        ("deny", {}, "turn"),
    ],
)
def test_a_permissions_ask_grants_what_was_asked_or_nothing(
    tmp_path: Path,
    option: str,
    granted: PlainTree,
    scope: str,
) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [_permissions()]})
    ask, _after = rig.run(_asked(rig, option=option))
    assert ask == Ask(
        id=ask.id,
        provider="codex",
        kind="permissions",
        title="Grant more access",
        tool="request_permissions",
        tool_call_id="item_3",
        cwd="/w",
        reason="Write elsewhere",
        input={"permissions": {"network": {"enabled": True}, "fileSystem": None}},
        options=(
            Option(id="turn", label="Yes", kind="allow_once"),
            Option(id="session", label="Yes, for this session", kind="allow_session"),
            Option(id="decline", label="No", kind="deny"),
        ),
    )
    assert rig.response(2) == {"permissions": granted, "scope": scope}


@pytest.mark.parametrize(
    ("answers", "sent"),
    [
        ({"Color": "Red"}, {"color_preference": {"answers": ["Red"]}}),
        (
            {"color_preference": ["Red", "Blue"]},
            {"color_preference": {"answers": ["Red", "Blue"]}},
        ),
    ],
)
def test_a_question_is_answered_by_header_or_id(
    tmp_path: Path,
    answers: PlainTree,
    sent: PlainTree,
) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [_question()]})
    ask, after = rig.run(
        _asked(rig, option="allow_once", updated_input={"answers": answers}),
    )
    assert ask == Ask(
        id=ask.id,
        provider="codex",
        kind="question",
        tool="request_user_input",
        tool_call_id="item_4",
        questions=_QUESTIONS,
        options=(Option(id="answer", label="Send answers", kind="allow_once"),),
    )
    assert rig.response(3) == {"answers": sent}
    resolved = _of(after, Resolved)[0]
    assert (resolved.option, resolved.decision) == ("answer", "answered")


def test_a_question_with_no_questions_has_an_empty_list(tmp_path: Path) -> None:
    request = _question()
    request["params"] = {
        key: value
        for key, value in from_plain(request["params"], _Tree).items()
        if key != "questions"
    }
    rig = _Rig(tmp_path, after={"turn/start": [request]})
    ask, _after = rig.run(_asked(rig, option="answer"))
    assert ask.questions == []
    assert rig.response(3) == {"answers": {}}


def test_a_question_answer_naming_no_question_is_refused(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [_question()]})

    async def scenario() -> None:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("go")
        ask = await _ask(events)
        updated: dict[str, PlainTree] = {"answers": {"Size": "L"}}
        with pytest.raises(ValueError, match="Size"):
            await rig.driver.answer(
                Answer(ask_id=ask.id, option="answer", updated_input=updated),
            )
        await rig.driver.answer(Answer(ask_id=ask.id, option="answer"))
        await rig.sent(5)
        await rig.driver.stop()

    rig.run(scenario())
    assert rig.response(3) == {"answers": {}}


_ELICITATION_OPTIONS: Final = (
    Option(id="accept", label="Accept", kind="allow_once"),
    Option(id="decline", label="Decline", kind="deny"),
    Option(id="cancel", label="Cancel", kind="deny_stop"),
)


@pytest.mark.parametrize(
    ("option", "content", "result"),
    [
        ("accept", {"repo": "loop"}, {"action": "accept", "content": {"repo": "loop"}}),
        ("decline", {"repo": "loop"}, {"action": "decline", "content": None}),
        ("cancel", None, {"action": "cancel", "content": None}),
    ],
)
def test_an_elicitation_accepts_with_content_or_declines(
    tmp_path: Path,
    option: str,
    content: dict[str, PlainTree] | None,
    result: PlainTree,
) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [_elicitation()]})
    ask, _after = rig.run(_asked(rig, option=option, updated_input=content))
    assert ask == Ask(
        id=ask.id,
        provider="codex",
        kind="elicitation",
        title="Which repo?",
        input={
            "server": "docs",
            "mode": "form",
            "requestedSchema": {"type": "object", "properties": {}},
        },
        options=_ELICITATION_OPTIONS,
    )
    assert rig.response(4) == result


def test_an_elicitation_by_url_shows_the_url(tmp_path: Path) -> None:
    request = _elicitation(
        mode="url",
        url="https://example.com/auth",
        requestedSchema=None,
    )
    rig = _Rig(tmp_path, after={"turn/start": [request]})
    ask, _after = rig.run(_asked(rig, option="accept"))
    assert ask.input == {
        "server": "docs",
        "mode": "url",
        "url": "https://example.com/auth",
    }


# ----------------------------------------------- who ended an ask, state, and exit.


def test_a_request_resolved_without_our_answer_was_resolved_in_the_terminal(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [_command(), _resolved(0), _status()]})

    async def scenario() -> tuple[Ask, list[_Event]]:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("go")
        ask = await _ask(events)
        rest = [await _next(events) for _ in range(4)]
        await rig.driver.stop()
        return ask, rest

    ask, rest = rig.run(scenario())
    assert rest == [
        StateChanged(state="requires_action"),
        Resolved(ask_id=ask.id, option="", by="terminal", decision="answered"),
        StateChanged(state="idle"),
        StateChanged(state="running"),
    ]


def test_a_request_the_interrupt_cleared_was_ended_by_the_run(tmp_path: Path) -> None:
    rig = _Rig(
        tmp_path,
        after={
            "turn/start": [_command()],
            "turn/interrupt": [
                _turn("turn/started", "turn_2"),
                _command(5, turnId="turn_2"),
                _resolved(5),
                _resolved(0),
            ],
        },
    )

    async def scenario() -> list[_Event]:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("go")
        await _ask(events)
        await rig.driver.interrupt()
        await rig.sent(5)
        await asyncio.sleep(0.05)
        await rig.driver.stop()
        return await _collect(events)

    seen = rig.run(scenario())
    ended = {
        each.ask_id.split("-")[1]: (each.by, each.decision)
        for each in _of(seen, Resolved)
    }
    assert ended == {"5": ("terminal", "answered"), "0": ("run", "released")}


def test_our_own_answer_is_not_reported_again_when_the_server_confirms_it(
    tmp_path: Path,
) -> None:
    rig = _Rig(
        tmp_path,
        after={"turn/start": [_command()]},
        on_response={"0": [_resolved(0), _status()]},
    )
    ask, after = rig.run(_asked(rig, option="accept"))
    assert after == [
        StateChanged(state="requires_action"),
        Resolved(ask_id=ask.id, option="accept", by="chat", who="jp", decision="allow"),
        StateChanged(state="idle"),
        StateChanged(state="running"),
        StateChanged(state="idle"),
    ]


def test_state_follows_the_thread_status_and_each_change_is_reported_once(
    tmp_path: Path,
) -> None:
    frames: list[_Frame] = [
        _status(),
        _status(),
        _status("waitingOnUserInput"),
        _status("waitingOnApproval"),
        _status(),
        _status(kind="idle"),
        _status(),
        _status(kind="systemError"),
        {"$exit": 0},
    ]
    rig = _Rig(tmp_path, after={"turn/start": frames})

    async def scenario() -> list[_Event]:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("go")
        return await _collect(events)

    assert rig.run(scenario()) == [
        StateChanged(state="running"),
        StateChanged(state="requires_action"),
        StateChanged(state="running"),
        StateChanged(state="idle"),
        StateChanged(state="running"),
        StateChanged(state="idle"),
    ]


def test_when_the_app_server_exits_open_asks_are_released(tmp_path: Path) -> None:
    rig = _Rig(
        tmp_path,
        after={
            "turn/start": [
                _status("waitingOnApproval"),
                _command(),
                _file_request(),
                {"$exit": 0},
            ],
        },
    )

    async def scenario() -> list[_Event]:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("go")
        return await _collect(events)

    seen = rig.run(scenario())
    asks = _of(seen, Ask)
    released = _of(seen, Resolved)
    assert [each.ask_id for each in released] == [each.id for each in asks]
    assert all(
        (each.by, each.decision) == ("released", "released") for each in released
    )
    assert seen[-1] == StateChanged(state="idle")
    assert _of(seen, StateChanged) == [
        StateChanged(state="requires_action"),
        StateChanged(state="idle"),
    ]


@pytest.mark.parametrize(
    ("stderr", "message"),
    [
        (b"", "the codex app-server ended before it answered turn/start"),
        (b"boom\n", "the codex app-server ended (boom) before it answered turn/start"),
        (
            b"\xffbad",
            "the codex app-server ended (�bad) before it answered turn/start",
        ),
    ],
)
def test_a_call_in_flight_when_the_app_server_exits_raises(
    tmp_path: Path,
    stderr: bytes,
    message: str,
) -> None:
    frames: list[_Frame] = [{"$stderr": stderr.hex()}] if stderr else []
    rig = _Rig(
        tmp_path,
        replies={"turn/start": {"$silent": True}},
        after={"turn/start": [*frames, {"$exit": 3}]},
    )

    async def scenario() -> CodexAppError:
        await rig.start()
        with pytest.raises(CodexAppError) as raised:
            await rig.driver.send_line("go")
        return raised.value

    error = rig.run(scenario())
    assert (str(error), error.code) == (message, 0)


def test_only_the_end_of_a_long_stderr_is_kept(tmp_path: Path) -> None:
    stderr = (b"a" * 1_500 + b"\n") * 2 + b"END"
    rig = _Rig(
        tmp_path,
        replies={"turn/start": {"$silent": True}},
        after={"turn/start": [{"$stderr": stderr.hex()}, {"$exit": 3}]},
    )

    async def scenario() -> str:
        await rig.start()
        with pytest.raises(CodexAppError) as raised:
            await rig.driver.send_line("go")
        return str(raised.value)

    message = rig.run(scenario())
    kept = message.removeprefix("the codex app-server ended (").removesuffix(
        ") before it answered turn/start",
    )
    assert kept == stderr[-2_000:].decode().strip()
    assert len(stderr) > 2_000


# ---------------------------------------------------- frames the driver cannot use.


def test_a_request_whose_params_do_not_read_gets_an_error_reply(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _Rig(tmp_path, after={"turn/start": [_command(0, command=5), _command(1)]})
    ask, _after = rig.run(_asked(rig, option="accept", stop_after=6))
    assert ask.kind == "command"
    assert rig.response(0) == {
        "code": -32_602,
        "message": "invalid params for item/commandExecution/requestApproval",
    }
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith(
        "refusing item/commandExecution/requestApproval whose params do not read: ",
    )
    assert record.args is not None
    method, error = record.args
    assert method == "item/commandExecution/requestApproval"
    assert isinstance(error, ValueError)


def test_a_server_request_the_driver_cannot_answer_gets_an_error_reply(
    tmp_path: Path,
) -> None:
    unknown: _Tree = {"method": "currentTime/read", "id": 9, "params": {}}
    rig = _Rig(tmp_path, after={"turn/start": [unknown, _command()]})
    ask, _after = rig.run(_asked(rig, option="accept", stop_after=6))
    assert ask.kind == "command"
    assert rig.response(9) == {
        "code": -32_601,
        "message": "unsupported request currentTime/read",
    }


def test_a_line_that_is_not_a_frame_is_skipped_and_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _Rig(tmp_path, after={"turn/start": ["not json {", _command()]})
    ask, _after = rig.run(_asked(rig, option="accept"))
    assert ask.kind == "command"
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("skipping a line that is not a frame: ")
    assert record.args is not None
    [error] = record.args
    assert isinstance(error, ValueError)


def test_output_on_stderr_does_not_stop_the_reading(tmp_path: Path) -> None:
    rig = _Rig(
        tmp_path,
        after={"turn/start": [{"$stderr": b"warning\n".hex()}, _command()]},
    )
    ask, _after = rig.run(_asked(rig, option="accept"))
    assert ask.kind == "command"


def test_a_turn_named_twice_is_one_turn(tmp_path: Path) -> None:
    rig = _Rig(
        tmp_path,
        after={
            "turn/start": [
                _turn("turn/started", _TURN),
                _turn("turn/completed", _TURN),
                _status(),
            ],
        },
    )

    async def scenario() -> None:
        await rig.start()
        events = rig.driver.events()
        await rig.driver.send_line("first")
        assert await _next(events) == StateChanged(state="running")
        await rig.driver.send_line("second")
        await rig.driver.stop()

    rig.run(scenario())
    assert rig.methods()[-2:] == ["turn/start", "turn/start"]


def test_stop_ends_the_events_and_may_be_called_again(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    async def scenario() -> list[list[_Event]]:
        await rig.driver.stop()
        await rig.start()
        collected = asyncio.create_task(_collect(rig.driver.events()))
        await rig.driver.stop()
        await rig.driver.stop()
        return [await collected, await _collect(rig.driver.events())]

    assert rig.run(scenario()) == [[], []]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
