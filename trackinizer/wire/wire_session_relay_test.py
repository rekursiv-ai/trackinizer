"""The session relay's wire contract: what each frame is, and what a body may hold."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pydantic
import pytest

from trackinizer.wire import wire_session_relay
from trackinizer.wire.wire_session_ir import RecordBody
from trackinizer.wire.wire_session_relay import (
    AgentProcess,
    AgentState,
    BackgroundJob,
    Command,
    EventLine,
    Hello,
    Input,
    InputSent,
    NewInput,
    Opened,
    ProcessEnded,
    ProcessFrame,
    ProcessLine,
    ProcessSpec,
    RecordLine,
    ServeSession,
    Setting,
    SettingsLine,
    StateLine,
    TeamMember,
    ViewerFrame,
)


_SESSION = UUID(int=1)
_PROCESS = UUID(int=2)
_SINCE = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_STATE = AgentState(line="[3s 1k↑ 20↓]", busy=True, since=_SINCE)
_JOB = BackgroundJob(
    call_id="w1",
    tool="Bash",
    title="make build",
    agent="kid",
    started=_SINCE,
    state="running",
)
_ROOT = TeamMember(label="Agent", busy=False, spend_usd=1.5, session=_SESSION)
_KID = TeamMember(label="kid", parent="Agent", busy=True)
_MODEL = Setting(
    name=("model",),
    help_short="The model that answers.",
    usage="NAME",
    value="opus",
    options=("opus", "sonnet"),
)
_HALT = Setting(name=("halt",), help_short="Stop the turn.")
_SERVING = AgentProcess(
    session_id=_SESSION,
    process_id=_PROCESS,
    host="lab-box",
    state="attached",
    since=_SINCE,
)


def _kind(frame: pydantic.BaseModel) -> str:
    return type(frame).__name__


@pytest.mark.parametrize(
    "frame",
    [
        Opened(state=_STATE, process=_SERVING, settings=(_MODEL, _HALT)),
        Opened(),
        EventLine(kind="ModelResponseTextStreamed", payload={"text": "Hi"}),
        StateLine(state=_STATE),
        SettingsLine(settings=(_MODEL, _HALT)),
        RecordLine(part=0, record=RecordBody(idx=4, kind="UserMessage")),
        ProcessLine(process=_SERVING),
    ],
    ids=_kind,
)
def test_a_viewer_frame_is_read_back_by_its_type_tag(frame: pydantic.BaseModel) -> None:
    read = pydantic.TypeAdapter(ViewerFrame).validate_json(frame.model_dump_json())

    assert read == frame


@pytest.mark.parametrize(
    "frame",
    [
        Hello(process_id=_PROCESS, version="0.4.2", host="lab-box"),
        EventLine(kind="ModelHalt", payload={}),
        StateLine(state=AgentState(line="", busy=False, since=_SINCE)),
        SettingsLine(settings=()),
    ],
    ids=_kind,
)
def test_a_process_frame_is_read_back_by_its_type_tag(
    frame: pydantic.BaseModel,
) -> None:
    read = pydantic.TypeAdapter(ProcessFrame).validate_json(frame.model_dump_json())

    assert read == frame


def test_a_process_may_not_send_what_only_the_relay_writes() -> None:
    relay_only = ProcessLine(process=_SERVING).model_dump_json()

    with pytest.raises(pydantic.ValidationError):
        pydantic.TypeAdapter(ProcessFrame).validate_json(relay_only)


@pytest.mark.parametrize(
    ("busy", "jobs", "team", "working"),
    [
        (False, (), (_ROOT,), False),
        (True, (), (_ROOT,), True),
        (False, (_JOB,), (_ROOT,), True),
        (False, (), (_ROOT, _KID), True),
        # A job that runs until it is stopped is no work that will end.
        (
            False,
            (_JOB.model_copy(update={"call_id": "w2", "runs_until_stopped": True}),),
            (_ROOT,),
            False,
        ),
        (
            False,
            (
                _JOB.model_copy(update={"call_id": "w2", "runs_until_stopped": True}),
                _JOB,
            ),
            (_ROOT,),
            True,
        ),
    ],
    ids=["idle", "busy", "a-job", "a-child", "a-watch", "a-watch-and-a-job"],
)
def test_a_state_works_while_the_root_a_job_or_a_child_does(
    busy: bool,
    jobs: tuple[BackgroundJob, ...],
    team: tuple[TeamMember, ...],
    working: bool,
) -> None:
    state = AgentState(line="", busy=busy, since=_SINCE, jobs=jobs, team=team)

    assert state.working == working
    assert AgentState.model_validate_json(state.model_dump_json()) == state


def test_a_state_from_a_process_too_old_to_name_its_team_says_none_and_works_while_busy() -> (
    None
):
    """Such a process sends neither field, or the labels of its children at work."""
    old = AgentState.model_validate(
        {
            "line": "",
            "busy": False,
            "since": "2026-11-10T12:00:00Z",
            "agents": ["kid"],
        },
    )

    assert (old.jobs, old.team, old.working) == (None, None, False)
    assert old.model_copy(update={"busy": True}).working


def test_a_frame_reads_past_a_field_a_newer_peer_added() -> None:
    read = Hello.model_validate(
        {
            "type": "hello",
            "process_id": str(_PROCESS),
            "version": "9",
            "host": "",
            "added_later": 1,
        },
    )

    assert read == Hello(process_id=_PROCESS, version="9", host="")


def test_what_a_socket_frame_is_written_as_is_unchanged() -> None:
    """The process socket is in no API schema, so nothing else holds its frames still."""
    stamped = Input(
        id=UUID(int=7),
        author="ada@example.com",
        kind="ModelHalt",
        payload={},
    )

    assert Hello(process_id=_PROCESS, version="0.4.2", host="lab-box").model_dump(
        mode="json",
    ) == {
        "type": "hello",
        "process_id": "00000000-0000-0000-0000-000000000002",
        "version": "0.4.2",
        "host": "lab-box",
        "protocol": 1,
    }
    assert EventLine(kind="ModelHalt", payload={}).model_dump(mode="json") == {
        "type": "event",
        "kind": "ModelHalt",
        "payload": {},
    }
    assert StateLine(state=_STATE).model_dump(mode="json") == {
        "type": "state",
        "state": {
            "line": "[3s 1k↑ 20↓]",
            "busy": True,
            "since": "2026-11-10T12:00:00Z",
            "pending": [],
            "jobs": None,
            "team": None,
            "turn_ended": None,
            "working": True,
        },
    }
    assert _JOB.model_dump(mode="json") == {
        "call_id": "w1",
        "tool": "Bash",
        "title": "make build",
        "agent": "kid",
        "started": "2026-11-10T12:00:00Z",
        "state": "running",
        "awaited": False,
        "runs_until_stopped": False,
    }
    assert [member.model_dump(mode="json") for member in (_ROOT, _KID)] == [
        {
            "label": "Agent",
            "parent": "",
            "busy": False,
            "spend_usd": 1.5,
            "session": "00000000-0000-0000-0000-000000000001",
        },
        {
            "label": "kid",
            "parent": "Agent",
            "busy": True,
            "spend_usd": 0.0,
            "session": None,
        },
    ]
    assert SettingsLine(settings=(_MODEL,)).model_dump(mode="json") == {
        "type": "settings",
        "settings": [
            {
                "name": ["model"],
                "help_short": "The model that answers.",
                "help_long": "",
                "usage": "NAME",
                "value": "opus",
                "options": ["opus", "sonnet"],
                "flags": [],
            },
        ],
    }
    assert stamped.model_dump(mode="json") == {
        "type": "input",
        "id": "00000000-0000-0000-0000-000000000007",
        "author": "ada@example.com",
        "kind": "ModelHalt",
        "payload": {},
        "recipient": "",
        "booked": None,
    }


def test_an_input_from_another_session_s_agent_names_its_agent_and_booking() -> None:
    """A process that predates the fields reads a person's input as before."""
    sent = Input(
        id=UUID(int=7),
        author="worker#415",
        kind="UserMessage",
        payload={"py/object": "UserMessage", "content": "status?"},
        recipient="lead",
        booked="ask",
    )
    older = {
        "type": "input",
        "id": str(UUID(int=7)),
        "author": "a@x.y",
        "kind": "ModelHalt",
    }

    assert Input.model_validate_json(sent.model_dump_json()) == sent
    assert Input.model_validate({**older, "payload": {}}).booked is None
    with pytest.raises(pydantic.ValidationError):
        _ = Input.model_validate({**older, "payload": {}, "booked": "shout"})


def test_an_action_is_a_setting_with_no_value_and_a_name_of_at_least_one_part() -> None:
    assert (_HALT.value, _HALT.options, _HALT.flags) == (None, (), ())
    with pytest.raises(pydantic.ValidationError):
        Setting(name=(), help_short="Nameless.")


def test_a_state_says_when_it_began_and_holds_no_pending_line_unless_given() -> None:
    idle = AgentState(line="[done]", busy=False, since=_SINCE)

    assert idle.pending == ()
    with pytest.raises(pydantic.ValidationError):
        AgentState.model_validate({"line": "[done]", "busy": False})


def test_an_input_carries_what_the_relay_stamped_on_a_persons_post() -> None:
    posted = NewInput(kind="Command", payload={"line": "/model opus"})
    stamped = Input(
        id=UUID(int=7),
        author="ada@example.com",
        kind=posted.kind,
        payload=posted.payload,
    )

    assert Input.model_validate_json(stamped.model_dump_json()) == stamped
    assert InputSent(id=stamped.id).model_dump(mode="json") == {"id": str(UUID(int=7))}


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "SessionQuit", "payload": {}},
        {"kind": "SessionCompact", "payload": {}},
        {"kind": "ModelHalt", "payload": {}, "author": "mallory@example.com"},
        {"kind": "ModelHalt"},
        {"kind": "Command", "payload": {"line": "model opus"}},
        {"kind": "Command", "payload": {}},
    ],
    ids=[
        "a kind no person may send",
        "a verb only a terminal sends",
        "a field only the relay sets",
        "no payload",
        "a command that is no slash line",
        "a command with no line",
    ],
)
def test_a_persons_post_is_refused_outside_its_three_kinds_and_three_fields(
    body: dict[str, object],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        NewInput.model_validate(body)


def test_a_post_keeps_its_senders_id_and_is_given_one_when_it_names_none() -> None:
    named = NewInput.model_validate(
        {"id": str(UUID(int=7)), "kind": "ModelHalt", "payload": {}},
    )
    first = NewInput(kind="ModelHalt", payload={})
    second = NewInput(kind="ModelHalt", payload={})

    assert named.id == UUID(int=7)
    assert first.id != second.id
    assert NewInput.model_validate_json(first.model_dump_json()) == first


def test_a_commands_body_is_one_slash_line() -> None:
    posted = NewInput(kind="Command", payload={"line": "/model opus"})

    assert Command.model_validate(posted.payload) == Command(line="/model opus")


@pytest.mark.parametrize(
    "body",
    [
        {"line": ""},
        {"line": "halt"},
        {"line": "/halt", "to": "child_1"},
        {"text": "/halt"},
    ],
    ids=["an empty line", "a line with no slash", "a second field", "no line"],
)
def test_a_commands_body_is_refused_without_exactly_its_line(
    body: dict[str, str],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        Command.model_validate(body)


def test_a_process_spec_names_no_place_by_default() -> None:
    """The server's own host runs the agent, on that host's login, from its home."""
    spec = ProcessSpec.model_validate({})

    assert (spec.host, spec.login_host, spec.cwd, spec.args) == ("", "", "~", ())


@pytest.mark.parametrize(
    ("host", "login_host"),
    [("", ""), ("lab-box", ""), ("", "laptop"), ("lab-box", "laptop")],
    ids=[
        "the server's host, on its own login",
        "a named host, on its own login",
        "the server's host, on another host's login",
        "a named host, on another host's login",
    ],
)
def test_where_an_agent_runs_and_whose_login_it_uses_are_named_apart(
    host: str,
    login_host: str,
) -> None:
    spec = ProcessSpec(host=host, login_host=login_host, args=("--model", "opus"))

    assert (spec.host, spec.login_host) == (host, login_host)
    assert ProcessSpec.model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        ({"host": "lab-box", "cwd": "relative/path"}, "cwd"),
        ({"host": "lab-box", "model": "opus"}, "model"),
        ({"provider": "rai"}, "provider"),
        ({"login": "laptop"}, "login"),
    ],
    ids=[
        "a relative folder",
        "a field that is a sagent flag now",
        "a field this wire does not have",
        "the login host under its old name, which read as a command to log in",
    ],
)
def test_a_process_spec_is_refused_for_a_bad_folder_or_an_unknown_field(
    spec: dict[str, object],
    why: str,
) -> None:
    with pytest.raises(pydantic.ValidationError) as raised:
        ProcessSpec.model_validate(spec)

    assert why in str(raised.value)


def test_a_process_says_whose_login_it_uses_when_not_its_own_hosts() -> None:
    elsewhere = _SERVING.model_copy(update={"login_host": "laptop"})

    assert _SERVING.login_host == ""
    assert AgentProcess.model_validate_json(elsewhere.model_dump_json()) == elsewhere


def test_a_serve_session_does_not_print_its_token() -> None:
    token = f"key-{_PROCESS.hex}"
    serve = ServeSession(
        session_id=_SESSION,
        process_id=_PROCESS,
        token=token,
        spec=ProcessSpec(host="lab-box", args=("--model", "opus")),
    )

    assert token not in repr(serve)
    assert ServeSession.model_validate_json(serve.model_dump_json()) == serve


def test_a_serve_session_says_whether_it_was_placed_for_remote_agents() -> None:
    """One from a server that does not say is no remote agent's."""
    plain = {
        "session_id": str(_SESSION),
        "process_id": str(_PROCESS),
        "token": "t",
        "spec": {"host": "remote-agents"},
    }
    remote = ServeSession.model_validate({**plain, "remote": True})

    assert ServeSession.model_validate(plain).remote is False
    assert remote.remote is True
    assert ServeSession.model_validate_json(remote.model_dump_json()) == remote


def test_a_process_that_never_started_ends_with_no_exit_code() -> None:
    ended = ProcessEnded(process_id=_PROCESS, exit_code=None, reason="no client")

    assert ProcessEnded.model_validate_json(ended.model_dump_json()) == ended


def test_an_end_report_is_refused_past_the_reasons_bound() -> None:
    with pytest.raises(pydantic.ValidationError):
        ProcessEnded(
            process_id=_PROCESS,
            exit_code=1,
            reason="x" * (wire_session_relay.MAX_REASON_CHARS + 1),
        )


def test_each_path_names_its_session_or_host() -> None:
    assert [
        wire_session_relay.SESSION_LINES_PATH.format(session_id=_SESSION),
        wire_session_relay.SESSION_INPUTS_PATH.format(session_id=_SESSION),
        wire_session_relay.SESSION_PROCESS_PATH.format(session_id=_SESSION),
        wire_session_relay.SESSION_PROCESS_WS_PATH.format(session_id=_SESSION),
        wire_session_relay.HOST_SESSIONS_WAIT_PATH.format(host_id=_PROCESS),
        wire_session_relay.HOST_SESSIONS_ENDED_PATH.format(host_id=_PROCESS),
    ] == [
        f"/api/sessions/{_SESSION}/lines",
        f"/api/sessions/{_SESSION}/inputs",
        f"/api/sessions/{_SESSION}/process",
        f"/api/sessions/{_SESSION}/process/ws",
        f"/api/hosts/{_PROCESS}/sessions/wait",
        f"/api/hosts/{_PROCESS}/sessions/ended",
    ]


def test_a_host_says_each_thing_it_offers_by_a_fact_of_its_own() -> None:
    """A daemon and a server of different versions must agree on these names."""
    assert (
        wire_session_relay.SESSIONS_FACT,
        wire_session_relay.REMOTE_AGENTS_FACT,
        wire_session_relay.LOGINS_FACT,
    ) == ("sessions", "remote_agents", "logins")


def test_a_remote_agent_takes_a_campaign_lead_s_flags() -> None:
    """The lead's time, label, model, effort, budget, rounds and prompt, each valued."""
    lead = (
        "--time",
        "2:00:00",
        "--name",
        "lead-tiles-20261115-1800",
        "--model",
        "opus",
        "--model.thinking.effort",
        "high",
        "--max-budget-usd",
        "5",
        "--max-tool-call-rounds",
        "200",
        "--system-prompt",
        "# Research lead\n\n- run the loop",
        "--append-system-prompt",
        "Brief: Issue#7",
    )

    assert wire_session_relay.remote_flags_refused(lead) == ""
    assert wire_session_relay.remote_flags_refused(()) == ""


@pytest.mark.parametrize(
    ("args", "why"),
    [
        (("--tools", "Bash"), "a remote agent takes no --tools"),
        (("--add-dir", "/"), "a remote agent takes no --add-dir"),
        (("--model=opus",), "a remote agent takes no --model=opus"),
        (("--name", "a", "--time", "1:00"), "a remote agent takes --time first"),
        (("--model",), "a remote agent takes --model with a value"),
        (
            ("--name", "a", "--model", "--tools"),
            "a remote agent takes --model with a value",
        ),
        (("--model", "a", "--model", "b"), "a remote agent takes --model once"),
        (("opus",), "a remote agent takes no opus"),
    ],
    ids=[
        "a confining flag",
        "another",
        "a joined value",
        "a time not first",
        "a missing value",
        "a flag as a value",
        "a flag twice",
        "a bare word",
    ],
)
def test_a_remote_agent_refuses_any_other_flag_saying_which(
    args: tuple[str, ...],
    why: str,
) -> None:
    """A flag such as ``--tools`` or ``--add-dir`` would reach past the confinement."""
    assert wire_session_relay.remote_flags_refused(args) == why


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
