"""Tests for the hook that tells a ``trax run`` agent about boxes drawn on its canvas."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar, override

import json
import threading
import time
import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.trax.cli import connect
from trackinizer.trax.run.box_hook import hook


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from treekle import PlainTree

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


def _command(text: str) -> str:
    """Return the one command the settings run before a prompt."""
    settings = from_plain(
        json_loads(text),
        dict[str, dict[str, list[dict[str, list[dict[str, str | int]]]]]],
    )
    [entry] = settings["hooks"]["UserPromptSubmit"]
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

    def test_a_run_gets_a_private_dir_with_the_file(self) -> None:
        prepared = hook.prepare(profile=None)

        assert prepared.name == "settings.json"
        assert prepared.parent.stat().st_mode & 0o777 == 0o700
        assert "UserPromptSubmit" in prepared.read_text()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
