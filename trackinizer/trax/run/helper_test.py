"""``trax helper`` serves each science chat as one session and one resumed CLI conversation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast, override

import json
import uuid

import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.lib.agent.types.sessions import AgentToAgentMessage, AssistantMessage
from trackinizer.trax.run import helper
from trackinizer.trax.run.chat_sessions import ChatSessions
from trackinizer.trax.run.fake_chat_server import FakeChatServer, Queued
from trackinizer.trax.run.helper import Claude, Codex, HelperError, Memory, serve
from trackinizer.trax.run.redact import Redactor
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_science_chat import (
    CHAT_HELPER_CLI,
    FORK_EDGE_LABEL,
    SCIENCE_CHAT_LABEL,
    ChatForkAt,
    fork_point_label,
)
from trackinizer.wire.wire_session_ir import RecordBody
from trackinizer.wire.wire_sessions import (
    SessionEnd,
    SessionEndResponse,
    SessionStart,
    WorkspaceMessageContext,
)


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from trackinizer.lib.codec import PlainTree
    from trackinizer.wire.filters import Filter
    from trackinizer.wire.wire_session_ir import ManifestBody, PartBody


WORKSPACE = uuid.UUID("c5286865-67b6-4bd8-ab51-e06e10c326c5")
FIRST = uuid.UUID("11111111-1111-4111-8111-111111111111")
SECOND = uuid.UUID("22222222-2222-4222-8222-222222222222")
ADA = "ada@x"
GRACE = "grace@x"


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


def test_each_conversation_is_one_session_and_one_cli_conversation(
    tmp_path: Path,
) -> None:
    server = FakeChatServer(
        batches=[
            [_chat("Why?", FIRST)],
            [
                _chat("And then?", FIRST, source=GRACE),
                _chat("Hi", SECOND),
                ("from the console", ADA, None, None),
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
    _serve(server, runner, tmp_path)

    # The second line of a conversation resumes the CLI conversation the first began.
    assert runner.resumed == [None, "c1", None]
    # A line from outside Chat names no conversation to answer into, and is left.
    assert all("from the console" not in prompt for prompt in runner.prompts)
    # Each conversation is its own session, whose records are the conversation.
    assert server.lines(FIRST) == [
        (ADA, "Why?"),
        ("answer", "Because."),
        (GRACE, "And then?"),
        ("answer", "Then this."),
    ]
    assert server.lines(SECOND) == [(ADA, "Hi"), ("answer", "Hello.")]
    assert len(server.sessions) == 3
    # Every session ends with the helper, the service's too.
    assert [held.ended for held in server.sessions.values()] == [1, 1, 1]


def test_the_service_session_is_the_assistants_and_each_chat_is_a_science_chat(
    tmp_path: Path,
) -> None:
    server = FakeChatServer(
        batches=[[_chat("Why   is it?", FIRST)], [_chat("And?", FIRST, source=GRACE)]],
    )
    runner = _Runner(
        answers={"Why   is it?": ("Because.", "c1"), "And?": ("So.", "c1")},
    )
    _serve(server, runner, tmp_path)

    service = server.service()
    # It names itself, so a restart resumes it and hears the lines posted meanwhile.
    assert service.start == SessionStart(
        cli=CHAT_HELPER_CLI,
        cli_session_id="trax-helper:helper",
        actor="helper",
        title="Chat helper (claude)",
        started=service.start.started,
    )
    chat = server.chat(FIRST).start
    assert (chat.cli, chat.cli_session_id, chat.actor) == (
        "sagent",
        f"chat:{FIRST}",
        "chat-111111111111",
    )
    # The first poster is the account and the title is their line; the second poster
    # is labelled and changes neither.
    assert (chat.account, chat.title) == (ADA, "Why is it?")
    assert server.chat(FIRST).labels == [
        SCIENCE_CHAT_LABEL,
        f"poster:{ADA}",
        f"poster:{GRACE}",
    ]
    # Each label is added once, however many lines its poster posts.
    assert server.label_actors == ["helper"] * 3


def test_a_fork_opens_with_the_lines_it_starts_from_and_tells_its_first_turn_them(
    tmp_path: Path,
) -> None:
    server = FakeChatServer()
    original = server.seed(
        FIRST,
        records=[
            _said(0, ADA, "q1"),
            _answered(1, "a1"),
            _said(2, GRACE, "q2"),
            _answered(3, "a2"),
        ],
    )
    fork = ChatForkAt(session_id=original, part=0, idx=1)
    server.batches = [
        [_chat("what if?", SECOND, source=GRACE, fork=fork)],
        [_chat("and then?", SECOND, source=GRACE)],
    ]
    runner = _Runner(
        answers={"what if?": ("Then this.", "c9"), "and then?": ("So.", "c9")},
    )
    _serve(server, runner, tmp_path)

    # The fork's session holds the lines up to a1, then the forker's own.
    assert server.lines(SECOND) == [
        (ADA, "q1"),
        ("answer", "a1"),
        (GRACE, "what if?"),
        ("answer", "Then this."),
        (GRACE, "and then?"),
        ("answer", "So."),
    ]
    held = server.chat(SECOND)
    assert (held.start.account, held.labels) == (
        GRACE,
        [SCIENCE_CHAT_LABEL, f"poster:{GRACE}"],
    )
    assert server.edges == [
        (
            held.id,
            original,
            "produced_by",
            "helper",
            [FORK_EDGE_LABEL, fork_point_label(part=0, idx=1)],
        ),
    ]
    # Only the first turn is told the lines; the CLI conversation keeps them after.
    assert runner.prompts[0].startswith("Earlier lines")
    assert "\n> ada@x: q1\n> answer: a1\n\ngrace@x: what if?" in runner.prompts[0]
    assert "fork of another chat" not in runner.prompts[1]
    assert len(server.lines(FIRST)) == 4


def test_a_fork_whose_original_cannot_be_read_opens_no_session_and_the_helper_goes_on(
    tmp_path: Path,
) -> None:
    # The first read of a session's parts is the start's look for what is owed; the
    # fork's own reads follow, and the second line's too.
    server = _Flaky(batches=[], fails={"read_session_parts": {2, 3}})
    original = server.seed(FIRST, records=[_said(0, ADA, "q1"), _answered(1, "a1")])
    fork = ChatForkAt(session_id=original, part=0, idx=0)
    server.batches = [
        [_chat("what if?", SECOND, source=GRACE, fork=fork)],
        [_chat("and then?", SECOND, source=GRACE)],
        [_chat("hello", FIRST, source=GRACE)],
    ]
    runner = _Runner(answers={"hello": ("Hi.", "c1")})
    _serve(server, runner, tmp_path)

    # A fork answered without the lines it starts from would be answered as if the
    # conversation had begun with its first line, and so would the next line of it.
    assert len(runner.prompts) == 1
    assert "hello" in runner.prompts[0]
    assert server.edges == []
    assert all(
        held.start.cli_session_id != f"chat:{SECOND}"
        for held in server.sessions.values()
    )
    assert server.lines(FIRST)[-1] == ("answer", "Hi.")


def test_a_fork_whose_original_could_not_be_read_is_opened_by_the_next_line(
    tmp_path: Path,
) -> None:
    server = _Flaky(batches=[], fails={"read_session_parts": {2}})
    original = server.seed(FIRST, records=[_said(0, ADA, "q1"), _answered(1, "a1")])
    fork = ChatForkAt(session_id=original, part=0, idx=1)
    server.batches = [
        [_chat("what if?", SECOND, source=GRACE, fork=fork)],
        [_chat("and then?", SECOND, source=GRACE)],
    ]
    runner = _Runner(answers={"and then?": ("So.", "c2")})
    _serve(server, runner, tmp_path)

    assert server.lines(SECOND) == [
        (ADA, "q1"),
        ("answer", "a1"),
        (GRACE, "and then?"),
        ("answer", "So."),
    ]
    assert [edge[:3] for edge in server.edges] == [
        (server.chat(SECOND).id, original, "produced_by"),
    ]
    assert runner.prompts[0].startswith("Earlier lines")


def test_a_fork_line_for_a_conversation_that_has_lines_is_a_plain_line(
    tmp_path: Path,
) -> None:
    server = FakeChatServer()
    other = server.seed(SECOND, records=[_said(0, ADA, "secret q"), _answered(1, "a")])
    fork = ChatForkAt(session_id=other, part=0, idx=1)
    # FIRST is served by this helper already; THIRD was left by an earlier process.
    third = uuid.UUID("33333333-3333-4333-8333-333333333333")
    _ = server.seed(third, records=[_said(0, GRACE, "mine"), _answered(1, "b")])
    server.batches = [
        [_chat("hello", FIRST, source=GRACE)],
        [_chat("sneak", FIRST, source=ADA, fork=fork)],
        [_chat("sneak too", third, source=ADA, fork=fork)],
    ]
    runner = _Runner(
        answers={
            "hello": ("Hi.", "c1"),
            "sneak": ("No.", "c1"),
            "sneak too": ("Nor.", "c3"),
        },
    )
    _serve(server, runner, tmp_path)

    assert server.lines(FIRST) == [
        (GRACE, "hello"),
        ("answer", "Hi."),
        (ADA, "sneak"),
        ("answer", "No."),
    ]
    assert server.lines(third) == [
        (GRACE, "mine"),
        ("answer", "b"),
        (ADA, "sneak too"),
        ("answer", "Nor."),
    ]
    assert server.edges == []
    assert all("secret q" not in each for each in runner.prompts)
    assert server.lines(SECOND) == [(ADA, "secret q"), ("answer", "a")]


def test_a_prompt_carries_the_sender_and_the_canvas_commands(tmp_path: Path) -> None:
    server = FakeChatServer(batches=[[_chat("Why?", FIRST)]])
    runner = _Runner(answers={"Why?": ("Because.", "c1")})
    _serve(server, runner, tmp_path)
    (prompt,) = runner.prompts
    assert prompt.startswith("ada@x: Why?\n")
    assert f"trax workspace {WORKSPACE}" in prompt


def test_a_restart_resumes_each_conversation_and_session_it_remembers(
    tmp_path: Path,
) -> None:
    server = FakeChatServer(batches=[[_chat("Why?", FIRST)]])
    runner = _Runner(
        answers={"Why?": ("Because.", "c1"), "And then?": ("Then this.", "c1")},
    )
    _serve(server, runner, tmp_path)
    server.batches.append([_chat("And then?", FIRST)])
    server.stop.clear()
    _serve(server, runner, tmp_path)

    assert runner.resumed == [None, "c1"]
    # The conversation's session is resumed, not opened again, and takes its records
    # under a file of its own.
    assert (
        len([held for held in server.sessions.values() if held.start.cli == "sagent"])
        == 1
    )
    assert len(server.chat(FIRST).parts) == 2
    assert [text for _, text in server.lines(FIRST)] == [
        "Why?",
        "Because.",
        "And then?",
        "Then this.",
    ]


def test_on_start_it_answers_what_it_owes(tmp_path: Path) -> None:
    server = FakeChatServer(key="k1")
    _ = server.seed(FIRST, records=[_said(0, ADA, "Why?")])
    _ = server.seed(SECOND, records=[_said(0, ADA, "Hi"), _answered(1, "Hello.")])
    runner = _Runner(answers={"Why?": ("Because.", "c1")})
    _serve(server, runner, tmp_path)

    # Only the conversation whose last line is a person's is answered, once.
    assert runner.prompts == ["ada@x: Why?"]
    assert server.lines(FIRST) == [(ADA, "Why?"), ("answer", "Because.")]
    assert server.lines(SECOND) == [(ADA, "Hi"), ("answer", "Hello.")]
    [opener] = [
        each for each in server.listings[0] if each.field == "opened_by_api_key_id"
    ]
    assert (opener.op, opener.value) == ("is", "k1")


def test_a_line_behind_a_long_run_of_other_records_is_still_owed(
    tmp_path: Path,
) -> None:
    server = FakeChatServer()
    steps = [
        RecordBody(idx=index, kind="ToolCall", payload={"name": "Read"})
        for index in range(1, 80)
    ]
    _ = server.seed(FIRST, records=[_said(0, ADA, "Why?"), *steps])
    runner = _Runner(answers={"Why?": ("Because.", "c1")})
    _serve(server, runner, tmp_path)
    assert server.lines(FIRST)[-1] == ("answer", "Because.")


def test_closing_twice_ends_each_session_once() -> None:
    server = FakeChatServer()
    chats = ChatSessions(cast(Client, server), actor="helper")
    chats.hear(FIRST, poster=ADA, text="Why?")
    chats.close()
    chats.close()
    assert server.chat(FIRST).ended == 1


def test_a_failed_turn_says_so_in_its_conversation_and_the_helper_goes_on(
    tmp_path: Path,
) -> None:
    server = FakeChatServer(batches=[[_chat("Why?", FIRST), _chat("Hi", SECOND)]])
    runner = _Runner(
        answers={"Hi": ("Hello.", "c2")},
        failures={"Why?": "claude exited 1: rate limited"},
    )
    _serve(server, runner, tmp_path)
    assert server.lines(FIRST)[-1] == (
        "answer",
        "The helper could not answer: claude exited 1: rate limited",
    )
    assert server.lines(SECOND)[-1] == ("answer", "Hello.")


def test_a_line_that_cannot_be_recorded_is_left_and_the_helper_goes_on(
    tmp_path: Path,
) -> None:
    server = _Unrecordable(batches=[[_chat("Why?", FIRST), _chat("Hi", SECOND)]])
    runner = _Runner(answers={"Hi": ("Hello.", "c2")})
    _serve(server, runner, tmp_path)
    assert [prompt.split("\n")[0] for prompt in runner.prompts] == ["ada@x: Hi"]
    assert server.lines(SECOND) == [(ADA, "Hi"), ("answer", "Hello.")]


def test_a_restarted_helper_resumes_its_service_session(tmp_path: Path) -> None:
    server = FakeChatServer(batches=[[_chat("Why?", FIRST)]])
    runner = _Runner(
        answers={"Why?": ("Because.", "c1"), "And?": ("So.", "c1")},
    )
    _serve(server, runner, tmp_path)
    first = server.service().id
    server.batches.append([_chat("And?", FIRST)])
    server.stop.clear()
    _serve(server, runner, tmp_path)

    assert server.service().id == first
    # Ended by each run, and no second service session was opened.
    assert [
        held.ended
        for held in server.sessions.values()
        if held.start.cli == CHAT_HELPER_CLI
    ] == [2]


def test_an_answer_that_cannot_be_recorded_is_left_and_the_helper_goes_on(
    tmp_path: Path,
) -> None:
    # The second append is the answer to the first line.
    server = _Flaky(
        batches=[[_chat("Why?", FIRST), _chat("Hi", SECOND)]],
        fails={"append_records": {2}},
    )
    runner = _Runner(answers={"Why?": ("Because.", "c1"), "Hi": ("Hello.", "c2")})
    _serve(server, runner, tmp_path)
    assert server.lines(FIRST) == [(ADA, "Why?")]
    assert server.lines(SECOND) == [(ADA, "Hi"), ("answer", "Hello.")]


def test_a_failed_turn_whose_report_cannot_be_recorded_does_not_stop_the_helper(
    tmp_path: Path,
) -> None:
    server = _Flaky(
        batches=[[_chat("Why?", FIRST), _chat("Hi", SECOND)]],
        fails={"append_records": {2}},
    )
    runner = _Runner(
        answers={"Hi": ("Hello.", "c2")},
        failures={"Why?": "claude exited 1"},
    )
    _serve(server, runner, tmp_path)
    assert server.lines(SECOND)[-1] == ("answer", "Hello.")


def test_a_failed_listing_at_start_owes_nothing_and_the_helper_serves(
    tmp_path: Path,
) -> None:
    server = _Flaky(batches=[[_chat("Hi", SECOND)]], fails={"list_kind": {1}})
    _ = server.seed(FIRST, records=[_said(0, ADA, "Why?")])
    runner = _Runner(answers={"Hi": ("Hello.", "c2")})
    _serve(server, runner, tmp_path)
    assert server.lines(FIRST) == [(ADA, "Why?")]
    assert server.lines(SECOND) == [(ADA, "Hi"), ("answer", "Hello.")]


def test_one_chat_that_cannot_be_read_does_not_hide_the_others_it_owes(
    tmp_path: Path,
) -> None:
    server = _Flaky(fails={"read_session_parts": {1}})
    _ = server.seed(FIRST, records=[_said(0, ADA, "Why?")])
    _ = server.seed(SECOND, records=[_said(0, ADA, "Hi")])
    runner = _Runner(answers={"Why?": ("Because.", "c1"), "Hi": ("Hello.", "c2")})
    _serve(server, runner, tmp_path)
    assert [prompt.split("\n")[0] for prompt in runner.prompts] == ["ada@x: Hi"]


def test_a_failed_drain_is_retried_and_the_helper_goes_on(tmp_path: Path) -> None:
    server = _Flaky(batches=[[_chat("Hi", SECOND)]], fails={"drain_inbound": {1, 2}})
    runner = _Runner(answers={"Hi": ("Hello.", "c2")})
    _serve(server, runner, tmp_path)
    assert server.lines(SECOND) == [(ADA, "Hi"), ("answer", "Hello.")]


def test_a_label_that_cannot_be_added_does_not_lose_the_line(tmp_path: Path) -> None:
    server = _Flaky(batches=[[_chat("Why?", FIRST)]], fails={"add_label": {1}})
    runner = _Runner(answers={"Why?": ("Because.", "c1")})
    _serve(server, runner, tmp_path)
    assert server.lines(FIRST) == [(ADA, "Why?"), ("answer", "Because.")]
    # The label is tried again with the next record, not forgotten.
    assert set(server.chat(FIRST).labels) == {SCIENCE_CHAT_LABEL, f"poster:{ADA}"}


def test_a_session_that_cannot_be_ended_does_not_keep_the_others_open() -> None:
    server = _Flaky(fails={"session_end": {1}})
    chats = ChatSessions(cast(Client, server), actor="helper")
    chats.hear(FIRST, poster=ADA, text="Why?")
    chats.hear(SECOND, poster=ADA, text="Hi")
    chats.close()
    assert server.chat(SECOND).ended == 1


def test_a_server_that_names_no_key_owes_by_chat_alone(tmp_path: Path) -> None:
    """A ``--no-auth`` server stamps no opener, so no listing can filter on one."""
    server = FakeChatServer(key=None)
    _ = server.seed(FIRST, records=[_said(0, ADA, "Why?")])
    runner = _Runner(answers={"Why?": ("Because.", "c1")})
    _serve(server, runner, tmp_path)
    assert runner.prompts == ["ada@x: Why?"]
    assert [each.field for each in server.listings[0]] == [
        "cli_session_id",
        "labels",
        "modified",
    ]


def test_a_name_in_a_chats_first_line_is_masked_in_its_title(tmp_path: Path) -> None:
    server = FakeChatServer(batches=[[_chat("Key hunter2hunter2?", FIRST)]])
    runner = _Runner(answers={"Key hunter2hunter2?": ("No.", "c1")})
    _serve(
        server,
        runner,
        tmp_path,
        redactor=Redactor({"TRAX_API_KEY": "hunter2hunter2"}),
    )
    assert server.chat(FIRST).start.title == "Key [redacted:TRAX_API_KEY]?"


def test_a_conversation_whose_cli_session_is_gone_starts_a_new_one(
    tmp_path: Path,
) -> None:
    memory = Memory(tmp_path / "helper.json")
    memory.put(FIRST, "lost")
    runner = _Runner(
        answers={"Why?": ("Because.", "c9")},
        failures={"lost": "No conversation found"},
    )
    server = FakeChatServer(batches=[[_chat("Why?", FIRST)]])
    _serve(server, runner, tmp_path, memory=memory)
    assert runner.resumed == ["lost", None]
    assert server.lines(FIRST)[-1] == ("answer", "Because.")
    assert Memory(tmp_path / "helper.json").get(FIRST) == "c9"


def test_a_secret_in_an_answer_is_masked_in_the_chat_session(tmp_path: Path) -> None:
    server = FakeChatServer(batches=[[_chat("Key?", FIRST)]])
    runner = _Runner(answers={"Key?": ("It is hunter2hunter2.", "c1")})
    _serve(
        server,
        runner,
        tmp_path,
        redactor=Redactor({"TRAX_API_KEY": "hunter2hunter2"}),
    )
    [_, (_, answer)] = server.lines(FIRST)
    assert "hunter2" not in answer
    assert "hunter2" not in " ".join(record.text for record in server.records(FIRST))


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
    *,
    source: str = ADA,
    fork: ChatForkAt | None = None,
) -> Queued:
    """Build a Chat line as the server drains it, with the context naming its conversation."""
    context = WorkspaceMessageContext(
        workspace_id=WORKSPACE,
        visible_visuals=[],
        conversation_id=conversation,
        fork=fork,
    )
    return (text, source, None, context)


def _said(idx: int, sender: str, text: str) -> RecordBody:
    return RecordBody.of(
        SessionRecordRow.of(
            session_id=uuid.UUID(int=0),
            part=0,
            idx=idx,
            record=AgentToAgentMessage(
                sender=sender,
                content=text,
                timestamp=datetime.now(UTC).isoformat(),
            ),
        ),
    )


def _answered(idx: int, text: str) -> RecordBody:
    return RecordBody.of(
        SessionRecordRow.of(
            session_id=uuid.UUID(int=0),
            part=0,
            idx=idx,
            record=AssistantMessage(
                content=text,
                timestamp=datetime.now(UTC).isoformat(),
            ),
        ),
    )


def _serve(
    server: FakeChatServer,
    runner: _Runner,
    tmp_path: Path,
    *,
    memory: Memory | None = None,
    redactor: Redactor | None = None,
) -> None:
    serve(
        cast(Client, server),
        Claude(),
        actor="helper",
        extra=[],
        memory=memory or Memory(tmp_path / "helper.json"),
        run=runner,
        stop=server.stop,
        redactor=redactor,
        retry_sec=0.0,
    )


class _Unrecordable(FakeChatServer):
    """A server that refuses the first record it is given."""

    def __init__(self, *, batches: list[list[Queued]]) -> None:
        super().__init__(batches=batches)
        self.refused = False

    @override
    def append_records(
        self,
        session_id: uuid.UUID,
        *,
        name: str,
        manifest: ManifestBody,
        records: list[RecordBody],
    ) -> None:
        if not self.refused:
            self.refused = True
            raise ClientError("the server is down")
        super().append_records(
            session_id,
            name=name,
            manifest=manifest,
            records=records,
        )


class _Flaky(FakeChatServer):
    """A server whose calls fail as told: by call name, the 1-based calls that fail."""

    def __init__(
        self,
        *,
        batches: list[list[Queued]] | None = None,
        fails: dict[str, set[int]],
    ) -> None:
        super().__init__(batches=batches)
        self.fails = fails
        self.calls: dict[str, int] = {}

    def _call(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.calls[name] in self.fails.get(name, set()):
            raise ClientError(f"{name} failed")

    @override
    def append_records(
        self,
        session_id: uuid.UUID,
        *,
        name: str,
        manifest: ManifestBody,
        records: list[RecordBody],
    ) -> None:
        self._call("append_records")
        super().append_records(
            session_id,
            name=name,
            manifest=manifest,
            records=records,
        )

    @override
    def list_kind(
        self,
        kind: str,
        *,
        limit: int,
        offset: int,
        filters: list[Filter],
    ) -> list[dict[str, PlainTree]]:
        self._call("list_kind")
        return super().list_kind(kind, limit=limit, offset=offset, filters=filters)

    @override
    def read_session_parts(self, session_id: uuid.UUID) -> list[PartBody]:
        self._call("read_session_parts")
        return super().read_session_parts(session_id)

    @override
    def drain_inbound(
        self,
        session_id: uuid.UUID,
        *,
        wait_sec: float = 0.0,
    ) -> list[Queued]:
        self._call("drain_inbound")
        return super().drain_inbound(session_id, wait_sec=wait_sec)

    @override
    def add_label(self, target_id: uuid.UUID, label: str, *, actor: str) -> None:
        self._call("add_label")
        super().add_label(target_id, label, actor=actor)

    @override
    def session_end(
        self,
        session_id: uuid.UUID,
        body: SessionEnd | None = None,
    ) -> SessionEndResponse:
        self._call("session_end")
        return super().session_end(session_id, body)


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
        # A fork's first prompt quotes the lines it opened with, then a blank line.
        message = (
            prompt.partition("\n\n")[2] if prompt.startswith("Earlier") else prompt
        )
        asked = message.split("\n", 1)[0].split(": ", 1)[-1]
        for key, reason in self.failures.items():
            if key in (resume, asked):
                raise HelperError(reason)
        text, session = self.answers[asked]
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
