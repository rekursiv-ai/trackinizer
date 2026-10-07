"""``trax helper`` answers each Chat conversation as one resumed CLI conversation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import json
import threading
import uuid

import pytest

from trackinizer.client.client import Client
from trackinizer.trax.run import helper
from trackinizer.trax.run.helper import Claude, Codex, HelperError, Memory, serve
from trackinizer.wire.wire_chats import (
    CHAT_HELPER_CLI,
    AwaitingChat,
    ChatMessage,
    ChatReply,
    ChatThread,
)
from trackinizer.wire.wire_sessions import (
    SessionEnd,
    SessionEndResponse,
    SessionStart,
    SessionStartResponse,
    WorkspaceMessageContext,
)


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


WORKSPACE = uuid.UUID("c5286865-67b6-4bd8-ab51-e06e10c326c5")
FIRST = uuid.UUID("11111111-1111-4111-8111-111111111111")
SECOND = uuid.UUID("22222222-2222-4222-8222-222222222222")
SESSION = uuid.UUID("33333333-3333-4333-8333-333333333333")


def test_claude_turns_print_json_and_resume_by_session_id() -> None:
    claude = Claude()
    first = claude.argv("Why?", resume=None, extra=["--model", "haiku"])
    assert first[:4] == ["claude", "-p", "--output-format", "json"]
    assert "--resume" not in first
    assert first[first.index("--append-system-prompt") + 1] == helper.GUIDE
    assert first[-3:] == ["--model", "haiku", "Why?"]
    again = claude.argv("And?", resume="abc", extra=[])
    assert again[again.index("--resume") + 1] == "abc"
    assert again[-1] == "And?"


def test_claude_output_is_the_answer_and_its_session() -> None:
    claude = Claude()
    out = json.dumps(
        {"type": "result", "is_error": False, "result": "Plum.", "session_id": "s1"},
    )
    assert claude.read(out) == helper.Turn(text="Plum.", cli_session_id="s1")
    failed = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "result": "Rate limited",
            "session_id": "s1",
        },
    )
    with pytest.raises(HelperError, match="Rate limited"):
        claude.read(failed)
    with pytest.raises(HelperError, match="no answer"):
        claude.read("not json")
    for wrong in ("[]", json.dumps({"result": 5, "session_id": "s1"})):
        with pytest.raises(HelperError, match="no answer"):
            claude.read(wrong)
    odd_session = json.dumps({"result": "Plum.", "session_id": 7})
    assert claude.read(odd_session) == helper.Turn(text="Plum.", cli_session_id=None)


def test_codex_turns_resume_by_thread_and_carry_the_guide_first() -> None:
    codex = Codex()
    first = codex.argv("Why?", resume=None, extra=["-m", "gpt-6-luna"])
    assert first[:3] == ["codex", "exec", "--json"]
    assert first[-3:-1] == ["-m", "gpt-6-luna"]
    assert first[-1].startswith(helper.GUIDE)
    assert first[-1].endswith("Why?")
    # `codex exec resume [OPTIONS] SESSION_ID PROMPT`; the guide is in the thread already.
    assert codex.argv("And?", resume="t1", extra=[]) == [
        "codex",
        "exec",
        "resume",
        "--json",
        "t1",
        "And?",
    ]


def test_codex_output_is_its_last_message_and_its_thread() -> None:
    codex = Codex()
    # Codex may print a warning line before its events; the events still count.
    out = "WARNING: proceeding, even though we could not update PATH\n" + "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "t1"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Looking."},
            },
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "trax issue 1"},
            },
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Plum."},
            },
            # Other items carry text too; only an agent message is the answer.
            {"type": "item.completed", "item": {"type": "reasoning", "text": "Done."}},
            # A wrong-typed item or thread id is read as absent, not fatal.
            {"type": "item.completed", "item": "agent_message"},
            {"type": "thread.started", "thread_id": 9},
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.completed"},
        )
    )
    assert codex.read(out) == helper.Turn(text="Plum.", cli_session_id="t1")
    # No thread named: there is nothing to resume next time.
    unnamed = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Hi."}},
    )
    assert codex.read(unnamed) == helper.Turn(text="Hi.", cli_session_id=None)
    with pytest.raises(HelperError, match=r"^codex printed no answer$"):
        codex.read(json.dumps({"type": "thread.started", "thread_id": "t1"}))


def test_each_conversation_is_one_cli_conversation_resumed_by_its_id(
    tmp_path: Path,
) -> None:
    client = _Server(
        batches=[
            [_chat("Why?", FIRST)],
            [
                _chat("And then?", FIRST),
                _chat("Hi", SECOND),
                ("from the console", "ada@x", None, None),
            ],
        ],
    )
    runner = _Runner(
        answers={
            "Why?": ("Because.", "c1"),
            "And then?": ("Then this.", "c1"),
            "Hi": ("Hello.", "c2"),
        },
    )
    _serve(client, runner, tmp_path)

    assert client.started == [
        SessionStart(
            cli=CHAT_HELPER_CLI,
            actor="helper",
            title="Chat helper (claude)",
            started=client.started[0].started,
        ),
    ]
    # The second line of a conversation resumes the CLI conversation the first began.
    assert runner.resumed == [None, "c1", None]
    assert client.replies == [
        (FIRST, ChatReply(kind="answer", text="Because.")),
        (FIRST, ChatReply(kind="answer", text="Then this.")),
        (SECOND, ChatReply(kind="answer", text="Hello.")),
    ]
    # A line from outside Chat names no conversation to answer into, and is left.
    assert all("from the console" not in prompt for prompt in runner.prompts)
    assert client.ended == [SESSION]


def test_a_prompt_carries_the_sender_and_the_canvas_commands(tmp_path: Path) -> None:
    client = _Server(batches=[[_chat("Why?", FIRST)]])
    runner = _Runner(answers={"Why?": ("Because.", "c1")})
    _serve(client, runner, tmp_path)
    (prompt,) = runner.prompts
    assert prompt.startswith("ada@x: Why?\n")
    assert f"trax workspace {WORKSPACE}" in prompt


def test_a_restart_resumes_each_conversation_it_remembers(tmp_path: Path) -> None:
    runner = _Runner(
        answers={"Why?": ("Because.", "c1"), "And then?": ("Then this.", "c1")},
    )
    _serve(_Server(batches=[[_chat("Why?", FIRST)]]), runner, tmp_path)
    _serve(_Server(batches=[[_chat("And then?", FIRST)]]), runner, tmp_path)
    assert runner.resumed == [None, "c1"]


def test_on_start_it_answers_what_it_owes(tmp_path: Path) -> None:
    owed = ChatThread(
        id=FIRST,
        title="Why?",
        partner_actor="helper",
        partner_session_id=SESSION,
        earlier=False,
        messages=[_line(1, "user", "Why?")],
    )
    client = _Server(
        batches=[],
        awaiting=[AwaitingChat(conversation_id=FIRST, workspace_id=WORKSPACE, seq=1)],
        threads={FIRST: owed},
    )
    runner = _Runner(answers={"Why?": ("Because.", "c1")})
    _serve(client, runner, tmp_path)
    assert client.replies == [(FIRST, ChatReply(kind="answer", text="Because."))]
    assert f"trax workspace {WORKSPACE}" in runner.prompts[0]


def test_a_failed_turn_says_so_in_its_conversation_and_the_helper_goes_on(
    tmp_path: Path,
) -> None:
    client = _Server(batches=[[_chat("Why?", FIRST), _chat("Hi", SECOND)]])
    runner = _Runner(
        answers={"Hi": ("Hello.", "c2")},
        failures={"Why?": "claude exited 1: rate limited"},
    )
    _serve(client, runner, tmp_path)
    assert client.replies == [
        (
            FIRST,
            ChatReply(
                kind="answer",
                text="The helper could not answer: claude exited 1: rate limited",
            ),
        ),
        (SECOND, ChatReply(kind="answer", text="Hello.")),
    ]


def test_a_conversation_whose_cli_session_is_gone_starts_a_new_one(
    tmp_path: Path,
) -> None:
    memory = Memory(tmp_path / "helper.json")
    memory.put(FIRST, "lost")
    runner = _Runner(
        answers={"Why?": ("Because.", "c9")},
        failures={"lost": "No conversation found"},
    )
    client = _Server(batches=[[_chat("Why?", FIRST)]])
    _serve(client, runner, tmp_path, memory=memory)
    assert runner.resumed == ["lost", None]
    assert client.replies == [(FIRST, ChatReply(kind="answer", text="Because."))]
    assert Memory(tmp_path / "helper.json").get(FIRST) == "c9"


def test_memory_survives_a_corrupt_or_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "helper.json"
    assert Memory(path).get(FIRST) is None
    path.write_text("{not json", encoding="utf-8")
    assert Memory(path).get(FIRST) is None
    path.write_text("[]", encoding="utf-8")
    assert Memory(path).get(FIRST) is None
    path.write_text(json.dumps({str(FIRST): 5}), encoding="utf-8")
    assert Memory(path).get(FIRST) is None
    Memory(path).put(FIRST, "c1")
    assert Memory(path).get(FIRST) == "c1"


def test_main_splits_its_own_flags_from_the_clis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, tuple[str, ...]]] = []

    def fake_serve(
        client: Client,
        cli: helper.HelperCli,
        *,
        actor: str,
        extra: Sequence[str],
        **_: object,
    ) -> None:
        del client
        seen.append((cli.name, actor, tuple(extra)))

    monkeypatch.setattr(helper, "serve", fake_serve)
    assert (
        helper.main(
            ["claude", "--as", "guide", "--", "--model", "haiku"],
            client_factory=lambda: cast(Client, object()),
        )
        == 0
    )
    assert helper.main(["codex"], client_factory=lambda: cast(Client, object())) == 0
    assert seen == [("claude", "guide", ("--model", "haiku")), ("codex", "helper", ())]
    with pytest.raises(SystemExit):
        helper.main(["gemini"], client_factory=lambda: cast(Client, object()))


def _chat(
    text: str,
    conversation: uuid.UUID,
) -> tuple[str, str | None, str | None, WorkspaceMessageContext | None]:
    """Build a Chat line as the server drains it, with the context naming its conversation."""
    context = WorkspaceMessageContext(
        workspace_id=WORKSPACE,
        visible_visuals=[],
        conversation_id=conversation,
    )
    return (text, "ada@x", None, context)


def _line(seq: int, role: str, text: str) -> ChatMessage:
    return ChatMessage.model_validate(
        {
            "id": str(uuid.uuid4()),
            "seq": seq,
            "role": role,
            "author": "ada@x",
            "text": text,
            "created": datetime.now(UTC).isoformat(),
        },
    )


def _serve(
    client: _Server,
    runner: _Runner,
    tmp_path: Path,
    *,
    memory: Memory | None = None,
) -> None:
    serve(
        cast(Client, client),
        Claude(),
        actor="helper",
        extra=[],
        memory=memory or Memory(tmp_path / "helper.json"),
        run=runner,
        stop=client.stop,
    )


class _Server:
    """The routes a helper calls, answering from what the test gives it."""

    def __init__(
        self,
        *,
        batches: list[
            list[tuple[str, str | None, str | None, WorkspaceMessageContext | None]]
        ],
        awaiting: list[AwaitingChat] | None = None,
        threads: dict[uuid.UUID, ChatThread] | None = None,
    ) -> None:
        self.batches = batches
        self.awaiting = awaiting or []
        self.threads = threads or {}
        self.started: list[SessionStart] = []
        self.replies: list[tuple[uuid.UUID, ChatReply]] = []
        self.ended: list[uuid.UUID] = []
        self.stop = threading.Event()

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        self.started.append(body)
        return SessionStartResponse(id=SESSION, seq=0, actor=body.actor)

    def awaiting_chats(self) -> list[AwaitingChat]:
        return self.awaiting

    def read_chat(self, conversation_id: uuid.UUID) -> ChatThread:
        return self.threads[conversation_id]

    def drain_inbound(
        self,
        session_id: uuid.UUID,
        *,
        wait_sec: float = 0.0,
    ) -> list[tuple[str, str | None, str | None, WorkspaceMessageContext | None]]:
        del session_id, wait_sec
        if not self.batches:
            self.stop.set()
            return []
        return self.batches.pop(0)

    def post_chat_reply(self, conversation_id: uuid.UUID, *, reply: ChatReply) -> None:
        if reply.kind == "answer":
            self.replies.append((conversation_id, reply))

    def session_end(
        self,
        session_id: uuid.UUID,
        body: SessionEnd | None = None,
    ) -> SessionEndResponse:
        del body
        self.ended.append(session_id)
        return SessionEndResponse(id=session_id, ended=datetime.now(UTC))


class _Runner:
    """A CLI that answers each prompt as told, keyed by the line it ends with."""

    def __init__(
        self,
        *,
        answers: dict[str, tuple[str, str]],
        failures: dict[str, str] | None = None,
    ) -> None:
        self.answers = answers
        self.failures = failures or {}
        self.prompts: list[str] = []
        self.resumed: list[str | None] = []

    def __call__(self, argv: Sequence[str]) -> str:
        prompt = argv[-1]
        resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
        self.prompts.append(prompt)
        self.resumed.append(resume)
        for key, reason in self.failures.items():
            if key in (resume, prompt.split("\n", 1)[0].split(": ", 1)[-1]):
                raise HelperError(reason)
        text, session = self.answers[prompt.split("\n", 1)[0].split(": ", 1)[-1]]
        return json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": text,
                "session_id": session,
            },
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
