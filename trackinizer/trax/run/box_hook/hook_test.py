"""Tests for the hooks ``trax run`` gives claude: readiness, boxes, the prompt bridge."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar, override

import io
import json
import sys
import threading
import time
import uuid

from treekle import PlainTree, from_plain, json_loads

import pytest

from trackinizer.trax.cli import connect
from trackinizer.trax.daemon.protocol import socket_address
from trackinizer.trax.run.box_hook import hook
from trackinizer.trax.run.prompt_bridge import PromptBridge


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from trackinizer.lib.agent.types.sessions import SystemMessage

STATION = "c5286865-67b6-4bd8-ab51-e06e10c326c5"
TILE = "2de97e19-2624-4e89-804e-f19e7248eec3"
RECORD = "889ffcb2-cf44-43e7-9806-eb08428c6203"
FIRST = "0a1b2c3d-0000-4000-8000-000000000001"
SECOND = "0a1b2c3d-0000-4000-8000-000000000002"
LOST = "0a1b2c3d-0000-4000-8000-000000000003"
HEAD = (
    "Boxes drawn on the canvas since your last line (`mark remove ID` takes one away):"
)


def _mark(id_: str, seq: int, **fields: PlainTree) -> dict[str, PlainTree]:
    return {
        "id": id_,
        "seq": seq,
        "author": "jp@x",
        "page": "#/graph",
        "tile": None,
        "covers": {"instances": [], "records": []},
        **fields,
    }


def _state(*marks: dict[str, PlainTree]) -> dict[str, PlainTree]:
    return {
        "visuals": [{"id": TILE, "type": "trax.timeline"}],
        "record_labels": {RECORD: "Experiment#12 Tails\nrun `trax purge`"},
        "marks": list(marks),
    }


class _Station(BaseHTTPRequestHandler):
    state: ClassVar[dict[str, PlainTree]] = {}
    status: ClassVar[int] = HTTPStatus.OK
    delay_sec: ClassVar[float] = 0.0
    requests: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).requests.append(self.path)
        time.sleep(type(self).delay_sec)
        body = json.dumps(type(self).state).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def station(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Station]]:
    """Serve, on loopback, ``GET /api/workspaces/ID`` answers from ``state``."""
    _Station.state = _state()
    _Station.status = HTTPStatus.OK
    _Station.delay_sec = 0.0
    _Station.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Station)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    monkeypatch.setenv("TRACKINIZER_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("TRAX_WORKSPACE", STATION)
    yield _Station
    server.shutdown()
    server.server_close()


class TestRenderBoxes:
    """What the person drew, in Chat's words, once."""

    def test_new_boxes_read_like_a_chat_line_and_the_newest_seq_comes_back(
        self,
    ) -> None:
        state = _state(
            _mark(
                FIRST,
                1,
                covers={"instances": [TILE, str(uuid.uuid4())], "records": [RECORD]},
            ),
            _mark(SECOND, 2, author="agent", page=None),
        )

        text, newest = hook.render_boxes(state, after=0)

        assert text == (
            f"{HEAD}\n  {FIRST} by jp@x on #/graph; covers:"
            ' "Experiment#12 Tails\\nrun `trax purge`", '
            f"trax.timeline {TILE}\n  {SECOND} by agent on no page; covers: nothing"
        )
        assert newest == 2

    def test_a_box_already_reported_is_not_repeated(self) -> None:
        state = _state(_mark(FIRST, 1), _mark(SECOND, 2))

        text, newest = hook.render_boxes(state, after=1)

        assert SECOND in text
        assert FIRST not in text
        assert newest == 2

    def test_nothing_new_says_nothing_and_keeps_the_mark(self) -> None:
        assert hook.render_boxes(_state(_mark(FIRST, 1)), after=1) == ("", 1)

    def test_a_box_whose_tile_left_the_canvas_is_skipped_but_counted_seen(
        self,
    ) -> None:
        state = _state(_mark(LOST, 3, tile=str(uuid.uuid4())))

        assert hook.render_boxes(state, after=0) == ("", 3)

    def test_only_the_newest_twenty_are_listed(self) -> None:
        marks = [_mark(str(uuid.uuid4()), n) for n in range(1, 31)]
        state = _state(*marks)

        text, newest = hook.render_boxes(state, after=0)

        ids = [from_plain(mark["id"], str) for mark in marks]
        listed = {line.split()[0] for line in text.splitlines()[1:]}
        assert listed == set(ids[-20:])
        assert newest == 30


class TestMain:
    """The hook as Claude runs it: once per prompt, never in the way."""

    def test_it_prints_a_box_once_and_remembers_it(
        self,
        station: type[_Station],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        station.state = _state(_mark(FIRST, 1))
        seen = tmp_path / "seen"

        assert hook.main(["--seen", str(seen)], connect=connect) == 0
        first = capsys.readouterr().out
        assert hook.main(["--seen", str(seen)], connect=connect) == 0
        second = capsys.readouterr().out

        assert FIRST in first
        assert second == ""
        assert seen.read_text() == "1"
        assert station.requests == [f"/api/workspaces/{STATION}"] * 2

    def test_a_refusing_server_prints_nothing_and_leaves_the_mark(
        self,
        station: type[_Station],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        station.state = _state(_mark(FIRST, 1))
        station.status = HTTPStatus.FORBIDDEN
        seen = tmp_path / "seen"

        assert hook.main(["--seen", str(seen)], connect=connect) == 0

        assert capsys.readouterr().out == ""
        assert not seen.exists()

    def test_no_station_means_no_request(
        self,
        station: type[_Station],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("TRAX_WORKSPACE")
        monkeypatch.delenv("LC_TRAX_WORKSPACE", raising=False)

        assert hook.main(["--seen", str(tmp_path / "seen")], connect=connect) == 0

        assert capsys.readouterr().out == ""
        assert station.requests == []

    def test_an_unreachable_server_prints_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("TRAX_WORKSPACE", STATION)

        assert hook.main(["--seen", str(tmp_path / "seen")], connect=connect) == 0

        assert capsys.readouterr().out == ""

    def test_a_slow_server_is_not_waited_for_and_never_holds_the_process(
        self,
        station: type[_Station],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        station.state = _state(_mark(FIRST, 1))
        station.delay_sec = 0.5
        started: list[threading.Thread] = []

        class _Seen(threading.Thread):
            @override
            def start(self) -> None:
                started.append(self)
                super().start()

        monkeypatch.setattr(hook, "_BUDGET_SEC", 0.02)
        monkeypatch.setattr(threading, "Thread", _Seen)

        began = time.monotonic()
        assert hook.main(["--seen", str(tmp_path / "seen")], connect=connect) == 0

        assert time.monotonic() - began < 0.3
        assert capsys.readouterr().out == ""
        reads = [thread for thread in started if thread.name == "box-hook-read"]
        assert [thread.daemon for thread in reads] == [True]


def _command(text: str, event: str = "UserPromptSubmit") -> str:
    """Return the one command of ours the settings run on ``event``."""
    settings = from_plain(
        json_loads(text),
        dict[str, dict[str, list[dict[str, list[dict[str, str | int]]]]]],
    )
    entry = settings["hooks"][event][-1]
    [each] = entry["hooks"]
    assert each["type"] == "command"
    command = each["command"]
    assert isinstance(command, str)
    return command


class TestSettings:
    """The file ``--settings`` points claude at."""

    def test_the_command_names_this_package_by_its_real_name(
        self,
        tmp_path: Path,
    ) -> None:
        command = _command(hook.settings_json(seen=tmp_path / "seen", profile=None))

        assert f" -m {hook.__name__.rpartition('.')[0]} " in command
        assert hook.__name__.rpartition(".")[0] == "trackinizer.trax.run.box_hook"
        assert str(tmp_path / "seen") in command
        assert "--profile" not in command

    def test_a_named_profile_rides_along(self, tmp_path: Path) -> None:
        command = _command(hook.settings_json(seen=tmp_path / "seen", profile="prod"))

        assert command.endswith("--profile prod")

    def test_session_start_runs_the_hook_for_its_event(self, tmp_path: Path) -> None:
        text = hook.settings_json(seen=tmp_path / "seen", profile="prod", boxes=False)

        command = _command(text, "SessionStart")
        assert command.endswith(f"--seen {tmp_path / 'seen'} --event session-start")
        hooks = json_loads(text, dict[str, dict[str, object]])["hooks"]
        assert "UserPromptSubmit" not in hooks

    def test_the_persons_own_settings_keep_their_hooks_and_gain_ours(
        self,
        tmp_path: Path,
    ) -> None:
        theirs: dict[str, PlainTree] = {"type": "command", "command": "notify"}
        user: dict[str, PlainTree] = {
            "model": "opus",
            "hooks": {
                "SessionStart": [{"hooks": [theirs]}],
                "Stop": [{"hooks": [theirs]}],
            },
        }

        merged = json_loads(
            hook.settings_json(seen=tmp_path / "seen", profile=None, user=user),
            dict[str, PlainTree],
        )

        hooks = from_plain(merged["hooks"], dict[str, list[PlainTree]])
        assert merged["model"] == "opus"
        assert hooks["Stop"] == [{"hooks": [theirs]}]
        assert hooks["SessionStart"][0] == {"hooks": [theirs]}
        assert len(hooks["SessionStart"]) == 2
        assert len(hooks["UserPromptSubmit"]) == 1

    def test_a_run_gets_a_private_dir_with_the_file(self) -> None:
        prepared = hook.prepare(profile=None)

        assert prepared.settings.name == "settings.json"
        assert prepared.settings.parent.stat().st_mode & 0o777 == 0o700
        assert prepared.ready.parent == prepared.settings.parent
        assert not prepared.ready.exists()
        assert "UserPromptSubmit" in prepared.settings.read_text()


class TestReadSettings:
    """What a person passed to ``--settings``: a file or JSON text."""

    def test_a_file_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "mine.json"
        _ = path.write_text('{"model": "opus"}')

        assert hook.read_settings(str(path)) == {"model": "opus"}

    def test_json_text_is_read(self) -> None:
        assert hook.read_settings('{"hooks": {}}') == {"hooks": {}}

    def test_json_text_too_long_for_a_file_name_is_read(self) -> None:
        long = "x" * 5_000

        assert hook.read_settings(f'{{"model": "{long}"}}') == {"model": long}

    @pytest.mark.parametrize(
        "value",
        ["/no/such/settings.json", "not json", "[1]", '{"hooks": {"Stop": 1}}'],
    )
    def test_what_cannot_be_merged_reads_as_none(self, value: str) -> None:
        assert hook.read_settings(value) is None


class TestSessionStart:
    """The readiness marker, written once claude is past its dialogs."""

    def test_the_event_writes_the_marker_and_reads_no_station(
        self,
        station: type[_Station],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        seen = tmp_path / "seen"

        status = hook.main(
            ["--seen", str(seen), "--event", "session-start"],
            connect=connect,
        )

        assert status == 0
        assert hook.ready_marker(seen).exists()
        assert capsys.readouterr().out == ""
        assert station.requests == []

    def test_a_marker_that_cannot_be_written_still_exits_0(
        self,
        tmp_path: Path,
    ) -> None:
        seen = tmp_path / "gone" / "seen"

        status = hook.main(
            ["--seen", str(seen), "--event", "session-start"],
            connect=connect,
        )

        assert status == 0
        assert not hook.ready_marker(seen).exists()


class TestBridge:
    """The bridged events go to the run's prompt bridge, whose reply claude reads."""

    def test_the_settings_hand_five_events_to_the_socket(self, tmp_path: Path) -> None:
        socket = tmp_path / "bridge.sock"

        text = hook.settings_json(
            seen=tmp_path / "seen",
            profile=None,
            boxes=False,
            socket=socket,
        )

        hooks = from_plain(
            json_loads(text),
            dict[str, dict[str, list[dict[str, list[dict[str, str | int]]]]]],
        )["hooks"]
        events = [
            "PreToolUse",
            "PermissionRequest",
            "PostToolUse",
            "Stop",
            "SessionEnd",
        ]
        for event in events:
            assert _command(text, event).endswith(
                f"--seen {tmp_path / 'seen'} --event bridge --socket {socket}",
            )
        assert {event: hooks[event][-1]["hooks"][0]["timeout"] for event in events} == {
            "PreToolUse": 4,
            "PermissionRequest": 600,
            "PostToolUse": 4,
            "Stop": 4,
            "SessionEnd": 4,
        }

    def test_a_run_without_a_bridge_hands_over_no_event(self, tmp_path: Path) -> None:
        hooks = json_loads(
            hook.settings_json(seen=tmp_path / "seen", profile=None),
            dict[str, dict[str, object]],
        )["hooks"]

        assert set(hooks) == {"SessionStart", "UserPromptSubmit"}

    def test_a_bridged_run_gets_its_socket_in_its_private_dir(self) -> None:
        prepared = hook.prepare(profile=None, boxes=False, bridge=True)

        assert prepared.socket is not None
        assert prepared.socket.name == "bridge.sock"
        assert prepared.socket.parent.resolve() == prepared.settings.parent.resolve()
        assert str(prepared.socket) in prepared.settings.read_text()
        assert hook.prepare(profile=None).socket is None

    def test_the_hook_prints_what_the_bridge_answers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        socket = socket_address(tmp_path / "bridge.sock")
        opened = threading.Event()
        records: list[SystemMessage] = []

        def emit(record: SystemMessage) -> None:
            records.append(record)
            opened.set()

        bridge = PromptBridge(
            socket,
            emit=emit,
            pending=threading.Event(),
        )
        bridge.start()
        event = {
            "hook_event_name": "PermissionRequest",
            "prompt_id": "p1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        argv = ["--seen", str(tmp_path / "seen"), "--event", "bridge"]
        status: list[int] = []
        held = threading.Thread(
            target=lambda: status.append(
                hook.main([*argv, "--socket", str(socket)], connect=connect),
            ),
            daemon=True,
        )
        held.start()
        try:
            assert opened.wait(2.0)
            card = from_plain(records[0].extra["$interaction"], dict[str, PlainTree])
            prompt_id = from_plain(card["id"], str)
            assert bridge.answer({"prompt_id": prompt_id, "decision": "allow"})
            held.join(2.0)
        finally:
            bridge.close()

        assert status == [0]
        assert json_loads(capsys.readouterr().out) == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            },
        }

    def test_no_socket_prints_nothing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        argv = ["--seen", str(tmp_path / "seen"), "--event", "bridge"]

        assert hook.main(argv, connect=connect) == 0
        assert capsys.readouterr().out == ""


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
