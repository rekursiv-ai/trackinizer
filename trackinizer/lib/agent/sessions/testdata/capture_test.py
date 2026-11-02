"""Tests for the hermetic CLI-session capture script."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar, cast

import argparse
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import zlib

from treekle import ReadError, json_loads

import pytest

from trackinizer.lib.agent.sessions.testdata import capture
from trackinizer.lib.agent.sessions.testdata.capture import (
    _add_arguments,
    _await_logs,
    _await_marks,
    _await_turn,
    _capture_claude,
    _capture_codex,
    _completed_turns,
    _drive,
    _emit,
    _filler,
    _first_of_each_kind,
    _Flags,
    _isolate_environment,
    _log_size,
    _prepare,
    _reject_secrets,
    _scrub_secrets,
    _seed_onboarding,
    _session_script,
    _Step,
    _tiny_png,
    main,
)
from trackinizer.lib.testing.resources import ascii_locale


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


_MARKER = '{"subtype":"turn_duration"}\n'


class _Clock:
    """A fake clock whose sleep advances time and may mutate the world."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.on_sleep: Callable[[], None] | None = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, sec: float) -> None:
        self.slept.append(sec)
        self.now += sec
        if self.on_sleep is not None:
            self.on_sleep()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(
        capture,
        "time",
        SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep),
    )
    return fake


class _FakeRelay:
    # What each submitted line appends to the log, after its tool call.
    turn_marker: ClassVar[str] = _MARKER
    # How long ``run`` outlives ``/exit`` unless terminated, as a wedged CLI does.
    hang_sec: ClassVar[float] = 0.0

    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.submitted: list[str] = []
        self.keys: list[tuple[int, bytes]] = []
        self.terminated = threading.Event()
        self.ran = threading.Event()
        self.daemon: bool | None = None
        self.stopped_by_terminate: bool | None = None
        self.logs = Path(next(iter(env.values())))

    def run(self) -> int:
        self.daemon = threading.current_thread().daemon
        self.stopped_by_terminate = self.terminated.wait(self.hang_sec)
        self.ran.set()
        return 0

    def submit(self, line: str) -> None:
        self.submitted.append(line)
        with (self.logs / "s.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"type":"tool_use"}\n' + self.turn_marker)

    def write(self, data: bytes) -> None:
        self.keys.append((len(self.submitted), data))

    def terminate(self) -> None:
        self.terminated.set()


@pytest.fixture
def relays(monkeypatch: pytest.MonkeyPatch) -> list[_FakeRelay]:
    made: list[_FakeRelay] = []

    def factory(argv: list[str], *, cwd: Path, env: dict[str, str]) -> _FakeRelay:
        relay = _FakeRelay(argv, cwd=cwd, env=env)
        made.append(relay)
        return relay

    monkeypatch.setattr(capture, "ThreadedRelay", factory)
    return made


def test_log_size_sums_only_jsonl_files_at_any_depth(tmp_path: Path) -> None:
    (tmp_path / "a.jsonl").write_text("12345", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.jsonl").write_text("678", encoding="utf-8")
    (tmp_path / "c.txt").write_text("ignored!", encoding="utf-8")
    (tmp_path / "dir.jsonl").mkdir()

    assert _log_size(tmp_path) == 8


def test_completed_turns_counts_markers_but_not_subagent_turns(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.jsonl").write_text(_MARKER * 2 + "other\n", encoding="utf-8")
    (tmp_path / "second.jsonl").write_text(_MARKER, encoding="utf-8")
    (tmp_path / "subagents").mkdir()
    (tmp_path / "subagents" / "a.jsonl").write_text(_MARKER * 5, encoding="utf-8")

    assert _completed_turns(tmp_path) == 3


def test_await_logs_waits_for_the_prompt_once_a_log_exists(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    (tmp_path / "s.jsonl").write_text("x", encoding="utf-8")

    _await_logs(tmp_path, deadline_sec=10.0)

    assert clock.slept == [3.0]


def test_await_logs_gives_up_at_the_deadline_and_says_so(
    tmp_path: Path,
    clock: _Clock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _await_logs(tmp_path, deadline_sec=2.0)

    assert clock.slept == [0.5] * 4
    assert f"no session log under {tmp_path} yet; proceeding" in capsys.readouterr().err


def test_await_turn_returns_the_new_count_when_a_turn_completes(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    log = tmp_path / "s.jsonl"
    log.write_text("x", encoding="utf-8")

    def finish() -> None:
        if len(clock.slept) == 2:
            log.write_text("x" + _MARKER, encoding="utf-8")

    clock.on_sleep = finish

    result = _await_turn(tmp_path, done=0, quiet_sec=100.0, deadline_sec=100.0)

    assert result == 1
    assert clock.slept == [0.5, 0.5]


def test_await_turn_returns_done_after_the_log_goes_quiet(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    (tmp_path / "s.jsonl").write_text("x", encoding="utf-8")

    result = _await_turn(tmp_path, done=3, quiet_sec=1.0, deadline_sec=100.0)

    assert result == 3
    assert clock.slept == [0.5, 0.5]


def test_await_turn_restarts_the_quiet_window_when_the_log_grows(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    log = tmp_path / "s.jsonl"
    log.write_text("x", encoding="utf-8")

    def grow_once() -> None:
        if len(clock.slept) == 1:
            log.write_text("xyz", encoding="utf-8")

    clock.on_sleep = grow_once

    result = _await_turn(tmp_path, done=0, quiet_sec=1.0, deadline_sec=100.0)

    assert result == 0
    assert clock.slept == [0.5, 0.5, 0.5]


def test_await_turn_gives_up_at_the_deadline_while_the_log_keeps_growing(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    log = tmp_path / "s.jsonl"
    log.write_text("x", encoding="utf-8")

    def grow() -> None:
        log.write_text("x" * (len(clock.slept) + 1), encoding="utf-8")

    clock.on_sleep = grow

    result = _await_turn(tmp_path, done=7, quiet_sec=100.0, deadline_sec=2.0)

    assert result == 7
    assert clock.slept == [0.5] * 4


def test_isolate_environment_keeps_only_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept = {
        "HOME": "/h",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LOGNAME": "op",
        "PATH": "/bin",
        "SHELL": "/bin/sh",
        "TERM": "xterm",
        "TMPDIR": "/t",
        "USER": "op",
    }
    monkeypatch.setattr(
        os,
        "environ",
        {**kept, "OPENAI_API_KEY": "secret", "PWD": "/x", "GITHUB_TOKEN": "t"},
    )

    _isolate_environment()

    assert dict(os.environ) == kept


def test_drive_skips_a_cli_that_is_not_installed(
    tmp_path: Path,
    relays: list[_FakeRelay],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing(name: str) -> str | None:
        assert name == "nosuchcli"
        return None

    monkeypatch.setattr(shutil, "which", missing)

    _drive(
        ["nosuchcli"],
        _session_script(),
        cwd=tmp_path,
        env={"CLAUDE_CONFIG_DIR": str(tmp_path)},
        turn_sec=1.0,
        timeout_sec=1,
    )

    assert relays == []
    assert "nosuchcli: not found in PATH; skipping" in capsys.readouterr().err


def test_drive_submits_every_scripted_turn_then_exits_and_terminates(
    tmp_path: Path,
    clock: _Clock,
    relays: list[_FakeRelay],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def found(name: str) -> str | None:
        return f"/bin/{name}"

    monkeypatch.setattr(shutil, "which", found)
    monkeypatch.setattr(os, "environ", {"HOME": "/h", "SECRET_KEY": "s"})
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "s.jsonl").write_text("boot\n", encoding="utf-8")

    _drive(
        ["claude", "--flag"],
        _session_script(),
        cwd=tmp_path,
        env={"CLAUDE_CONFIG_DIR": str(logs)},
        turn_sec=60.0,
        timeout_sec=5,
    )

    (relay,) = relays
    assert relay.argv == ["claude", "--flag"]
    assert relay.cwd == tmp_path
    assert relay.submitted == [*(step.line for step in _session_script()), "/exit"]
    assert relay.keys == []
    assert relay.terminated.is_set()
    # The CLI runs on a daemon thread, so a wedged one cannot keep this alive.
    assert relay.ran.wait(5.0)
    assert relay.daemon is True
    assert dict(os.environ) == {"HOME": "/h"}
    err = capsys.readouterr().err
    assert "turn 7/7" in err
    assert clock.slept[0] == 3.0


def test_drive_terminates_a_cli_still_running_past_the_timeout(
    tmp_path: Path,
    clock: _Clock,
    relays: list[_FakeRelay],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def found(name: str) -> str | None:
        return f"/bin/{name}"

    monkeypatch.setattr(shutil, "which", found)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(_FakeRelay, "hang_sec", 5.0)
    (tmp_path / "s.jsonl").write_text("boot\n", encoding="utf-8")

    _drive(
        ["claude"],
        (),
        cwd=tmp_path,
        env={"CLAUDE_CONFIG_DIR": str(tmp_path)},
        turn_sec=60.0,
        timeout_sec=0,
    )

    (relay,) = relays
    assert relay.ran.wait(5.0)
    assert relay.stopped_by_terminate is True
    assert clock.slept == [3.0]


def test_drive_presses_escape_once_an_escape_steps_tool_call_is_logged(
    tmp_path: Path,
    clock: _Clock,
    relays: list[_FakeRelay],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def found(name: str) -> str | None:
        return f"/bin/{name}"

    monkeypatch.setattr(shutil, "which", found)
    monkeypatch.setattr(os, "environ", {})
    # No turn marker: an interrupted turn writes none, so each turn ends on quiet.
    monkeypatch.setattr(_FakeRelay, "turn_marker", "")
    (tmp_path / "s.jsonl").write_text("boot\n", encoding="utf-8")
    long = "Run the shell command that takes a whole minute, then stop."

    _drive(
        ["claude"],
        (_Step(line="plain"), _Step(line=long, escape=True)),
        cwd=tmp_path,
        env={"CLAUDE_CONFIG_DIR": str(tmp_path)},
        turn_sec=60.0,
        timeout_sec=5,
    )

    (relay,) = relays
    assert relay.submitted == ["plain", long, "/exit"]
    assert relay.keys == [(2, b"\x1b")]
    # Boot wait; 20s of quiet ends each turn; the 2s that lets the prompt draw.
    assert clock.slept == [3.0, *[0.5] * 40, 2.0, *[0.5] * 40]
    assert capsys.readouterr().err.splitlines() == [
        f"$ claude ... (cwd={tmp_path}, 2 turns)",
        "  turn 1/2: plain",
        "  turn 2/2: Run the shell command that takes a whole minute,",
    ]


def test_await_marks_returns_once_the_marker_count_passes_above(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    log = tmp_path / "s.jsonl"
    log.write_text("mm", encoding="utf-8")
    (tmp_path / "subagents").mkdir()
    (tmp_path / "subagents" / "a.jsonl").write_text("mmmm", encoding="utf-8")

    def add() -> None:
        log.write_text("mmm", encoding="utf-8")

    clock.on_sleep = add

    _await_marks(tmp_path, "m", above=2, deadline_sec=10.0)

    assert clock.slept == [0.5]


def test_await_marks_gives_up_at_the_deadline_and_says_so(
    tmp_path: Path,
    clock: _Clock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _await_marks(tmp_path, "m", above=0, deadline_sec=1.0)

    assert clock.slept == [0.5, 0.5]
    assert f"no m under {tmp_path} yet; proceeding" in capsys.readouterr().err


def test_prepare_copies_only_existing_credentials_and_writes_the_image(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "auth.json").write_text("AUTH", encoding="utf-8")
    (real / "settings.json").write_text("SET", encoding="utf-8")

    home, work = _prepare(tmp_path / "root", real_home=real)

    assert home == tmp_path / "root" / "home"
    assert work == tmp_path / "root" / "work"
    assert sorted(p.name for p in home.iterdir()) == ["auth.json", "settings.json"]
    assert (home / "auth.json").read_text(encoding="utf-8") == "AUTH"
    assert (work / "tiny.png").read_bytes() == _tiny_png()


def test_prepare_seeds_claudes_onboarding_for_its_own_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = tmp_path / "user"
    (user / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user))
    (user / ".claude.json").write_text('{"theme":"dark"}', encoding="utf-8")

    home, work = _prepare(tmp_path / "root", real_home=user / ".claude")

    seeded = json_loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert seeded == {
        "theme": "dark",
        "projects": {
            str(work): {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
                "projectOnboardingSeenCount": 1,
            },
        },
    }


def test_seed_onboarding_reads_the_state_file_as_utf8_whatever_the_locale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text('{"name":"Zoë"}', encoding="utf-8")
    home = tmp_path / "cfg"
    home.mkdir()

    with ascii_locale():
        _seed_onboarding(home, tmp_path / "w", real_home=tmp_path / ".claude")

    seeded = json_loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert seeded == {
        "name": "Zoë",
        "projects": {
            str(tmp_path / "w"): {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
                "projectOnboardingSeenCount": 1,
            },
        },
    }


def test_seed_onboarding_refuses_a_state_file_that_is_not_an_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text("[]", encoding="utf-8")

    home = tmp_path / "cfg"
    home.mkdir()

    with pytest.raises(ReadError):
        _seed_onboarding(home, tmp_path / "w", real_home=tmp_path / ".claude")


def test_seed_onboarding_keeps_every_key_but_projects_and_trusts_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    (fake_home / ".claude.json").write_text(
        json.dumps({"theme": "dark", "projects": {"/secret": {}}, "n": 3}),
        encoding="utf-8",
    )
    home = tmp_path / "cfg"
    home.mkdir()
    work = tmp_path / "work"

    _seed_onboarding(home, work, real_home=fake_home / ".claude")

    seeded = json_loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert seeded == {
        "theme": "dark",
        "n": 3,
        "projects": {
            str(work): {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
                "projectOnboardingSeenCount": 1,
            },
        },
    }


def test_seed_onboarding_is_claude_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    (fake_home / ".claude.json").write_text("{}", encoding="utf-8")
    home = tmp_path / "cfg"
    home.mkdir()

    _seed_onboarding(home, tmp_path, real_home=fake_home / ".codex")

    assert list(home.iterdir()) == []


def test_seed_onboarding_does_nothing_without_a_claude_state_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    home = tmp_path / "cfg"
    home.mkdir()

    _seed_onboarding(home, tmp_path, real_home=fake_home / ".claude")

    assert list(home.iterdir()) == []


def test_tiny_png_is_a_valid_two_by_two_png() -> None:
    data = _tiny_png()

    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks: dict[bytes, bytes] = {}
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(tag + body)
        chunks[tag] = body
        pos += 12 + length
    assert list(chunks) == [b"IHDR", b"IDAT", b"IEND"]
    # 2x2, 8-bit truecolor; deflate, no filter, not interlaced.
    assert chunks[b"IHDR"] == struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    assert len(zlib.decompress(chunks[b"IDAT"])) == 12
    assert chunks[b"IEND"] == b""


def test_emit_redacts_paths_account_ids_and_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = tmp_path / "user"
    home = tmp_path / "cfg"
    work = tmp_path / "ws"
    monkeypatch.setenv("HOME", str(user))
    scratch = f"{tempfile.gettempdir()}/claude-1000/-tmp-run-scratch-work/x"
    source = tmp_path / "in.jsonl"
    source.write_text(
        f'{{"w":"{work}/a","h":"{home}/b","u":"{user}/c","t":"{scratch}",'
        '"ownerAccountUuid": "real-account","ownerOrganizationUuid":"real-org",'
        '"e":"The user\'s email address is Jo.Q+x@Corp.example.co.uk. Use it",'
        f'"k":"sk-{"A" * 30}"}}\n',
        encoding="utf-8",
    )
    target = tmp_path / "out.jsonl"

    assert _emit(source, target, home=home, work=work) == target

    zero = "00000000-0000-0000-0000-000000000000"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "w": "/workspace/a",
        "h": "/config/b",
        "u": "/home/user/c",
        "t": "/workspace/.claude",
        "ownerAccountUuid": zero,
        "ownerOrganizationUuid": zero,
        "e": "The user's email address is user@example.com. Use it",
        "k": "sk-" + ("deadbeef" * 4)[:30],
    }


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        ("AKIA" + "B" * 16, ("deadbeef" * 3)[:20]),
        ("ghp_" + "A" * 20, "ghp_" + "deadbeefdeadbeefdead"),
        ("-----BEGIN RSA PRIVATE KEY-----", "-----" + ("deadbeef" * 4)[:26]),
        ('API_KEY="' + "x" * 20 + '"', "API_" + ("deadbeef" * 4)[:26]),
    ],
)
def test_scrub_secrets_keeps_length_and_issuer_prefix(
    secret: str,
    expected: str,
) -> None:
    assert _scrub_secrets(secret) == expected


def test_filler_without_an_issuer_prefix_is_all_filler() -> None:
    found = re.search(r"AKIA[0-9A-Z]{16}", "AKIA" + "C" * 16)

    assert found is not None
    assert _filler(found) == ("deadbeef" * 3)[:20]


def test_reject_secrets_raises_on_a_surviving_credential(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"

    with pytest.raises(ValueError, match="credential-shaped text survived"):
        _reject_secrets("key sk-" + "A" * 30, source=source)


def test_reject_secrets_names_a_credential_that_follows_scrubbed_filler(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rollout.jsonl"
    text = _scrub_secrets("key sk-" + "A" * 30) + " sk-" + "B" * 30

    with pytest.raises(ValueError, match="credential-shaped") as raised:
        _reject_secrets(text, source=source)

    assert str(raised.value) == (
        f"{source}: credential-shaped text survived redaction "
        "(sk-BBBBBBBBB...); fixture not written"
    )


def _unscrubbed(text: str) -> str:
    """Return ``text`` as a scrubber that missed every secret would."""
    return text


def test_emit_refuses_a_secret_the_scrubber_missed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A scrubber regression: the backstop must still keep the fixture unwritten.
    monkeypatch.setattr(capture, "_scrub_secrets", _unscrubbed)
    source = tmp_path / "rollout.jsonl"
    source.write_text('{"k":"sk-' + "A" * 30 + '"}\n', encoding="utf-8")
    target = tmp_path / "out.jsonl"

    with pytest.raises(ValueError, match="credential-shaped") as raised:
        _emit(source, target, home=tmp_path / "h", work=tmp_path / "w")

    assert str(raised.value) == (
        f"{source}: credential-shaped text survived redaction "
        "(sk-AAAAAAAAA...); fixture not written"
    )
    assert not target.exists()


def test_reject_secrets_accepts_scrubbed_filler(tmp_path: Path) -> None:
    text = _scrub_secrets("key sk-" + "A" * 30)

    assert _reject_secrets(text, source=tmp_path / "x.jsonl") is None


def _vendor_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """Make a user home whose ``name`` directory holds every credential file."""
    user = tmp_path / "user"
    (user / name).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user))
    for credential in (
        "auth.json",
        "config.toml",
        ".credentials.json",
        "settings.json",
    ):
        (user / name / credential).write_text("{}", encoding="utf-8")
    return user


def test_capture_claude_splits_main_and_sidechain_fixtures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _vendor_home(tmp_path, monkeypatch, ".claude")
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
        home = env["CLAUDE_CONFIG_DIR"]
        project = Path(home) / "projects" / "p"
        (project / "sid" / "subagents").mkdir(parents=True)
        (project / "main.jsonl").write_text(
            f"main {cwd} {home} {user} é\n",
            encoding="utf-8",
        )
        (project / "sid" / "subagents" / "a.jsonl").write_text(
            "side\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(capture, "_drive", fake_drive)
    out = tmp_path / "out"
    out.mkdir()
    root = tmp_path / "root"

    with ascii_locale():
        written = _capture_claude(root, out, turn_sec=1.5, timeout_sec=9)

    assert written == [out / "claude_main.jsonl", out / "claude_sidechain.jsonl"]
    assert (out / "claude_main.jsonl").read_text(
        encoding="utf-8",
    ) == "main /workspace /config /home/user é\n"
    assert (out / "claude_sidechain.jsonl").read_text(encoding="utf-8") == "side\n"
    assert sorted(path.name for path in (root / "home").iterdir()) == [
        ".credentials.json",
        "auth.json",
        "config.toml",
        "projects",
        "settings.json",
    ]
    assert calls == [
        (
            ["claude", "--permission-mode", "bypassPermissions"],
            _session_script(),
            root / "work",
            {"CLAUDE_CONFIG_DIR": str(root / "home")},
            1.5,
            9,
        ),
    ]


def test_capture_codex_emits_only_rollout_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vendor_home(tmp_path, monkeypatch, ".codex")
    argvs: list[list[str]] = []
    knobs: list[tuple[Sequence[_Step], Path, dict[str, str], float, int]] = []

    def fake_drive(
        argv: list[str],
        script: Sequence[_Step],
        *,
        cwd: Path,
        env: dict[str, str],
        turn_sec: float,
        timeout_sec: int,
    ) -> None:
        argvs.append(argv)
        knobs.append((script, cwd, env, turn_sec, timeout_sec))
        home = env["CODEX_HOME"]
        sessions = Path(home) / "sessions" / "2026"
        sessions.mkdir(parents=True)
        (sessions / "rollout-1.jsonl").write_text(
            f"roll {cwd} {home}\n",
            encoding="utf-8",
        )
        (sessions / "other.jsonl").write_text("nope\n", encoding="utf-8")

    monkeypatch.setattr(capture, "_drive", fake_drive)
    out = tmp_path / "out"
    out.mkdir()
    root = tmp_path / "root"

    written = _capture_codex(root, out, turn_sec=1.0, timeout_sec=2)

    assert written == [out / "codex_main.jsonl"]
    assert (out / "codex_main.jsonl").read_text(
        encoding="utf-8",
    ) == "roll /workspace /config\n"
    assert knobs == [
        (_session_script(), root / "work", {"CODEX_HOME": str(root / "home")}, 1.0, 2),
    ]
    assert sorted(path.name for path in (root / "home").iterdir()) == [
        ".credentials.json",
        "auth.json",
        "config.toml",
        "sessions",
        "settings.json",
    ]
    # Codex 0.153 refuses ``--skip-git-repo-check`` in its TUI, so trust is configured.
    assert argvs == [
        [
            "codex",
            "-c",
            f'projects."{tmp_path / "root" / "work"}".trust_level="trusted"',
            "-c",
            "model_reasoning_summary=detailed",
            "--dangerously-bypass-approvals-and-sandbox",
        ],
    ]


def test_add_arguments_defaults() -> None:
    parser = argparse.ArgumentParser()
    _add_arguments(parser)

    flags = cast(_Flags, parser.parse_args([]))

    assert flags.cli == "both"
    assert flags.out == Path(capture.__file__).resolve().parent
    assert flags.timeout_sec == 300
    assert flags.turn_sec == 180.0


def _fake_captures(
    monkeypatch: pytest.MonkeyPatch,
    *,
    empty: bool = False,
) -> list[tuple[str, Path, float, int]]:
    calls: list[tuple[str, Path, float, int]] = []

    def make(name: str) -> Callable[..., list[Path]]:
        def fake(
            root: Path,
            out: Path,
            *,
            turn_sec: float,
            timeout_sec: int,
        ) -> list[Path]:
            calls.append((name, root, turn_sec, timeout_sec))
            os.chdir("/")
            if empty:
                return []
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"{name}.jsonl"
            path.write_text("abc", encoding="utf-8")
            return [path]

        return fake

    monkeypatch.setattr(capture, "_capture_claude", make("claude"))
    # The name ``main`` dispatches through: its lazy proxy binds the target on first
    # call and keeps it, so patching the kinds module reached only the first test.
    monkeypatch.setattr(capture, "_capture_claude_kinds", make("claude-kinds"))
    monkeypatch.setattr(capture, "_capture_codex", make("codex"))
    monkeypatch.setattr(capture, "_capture_codex_kinds", make("codex-kinds"))
    return calls


def test_main_codex_kinds_runs_only_the_codex_kinds_session_into_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = _fake_captures(monkeypatch)
    out = tmp_path / "o"

    assert main(["--cli", "codex-kinds", "--out", str(out)]) == 0

    assert calls == [("codex-kinds", out / "scratch" / "codex-kinds", 180.0, 300)]
    assert Path.cwd() == tmp_path
    assert capsys.readouterr().out.splitlines() == [
        f"{out / 'kinds' / 'codex-kinds.jsonl'} (3 bytes)",
    ]


def test_capture_codex_kinds_drives_the_session_then_forks_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vendor_home(tmp_path, monkeypatch, ".codex")
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
        written = capture._capture_codex_kinds(root, out, turn_sec=1.5, timeout_sec=9)
        # Into a directory that already exists, as every capture after the first.
        again = capture._capture_codex_kinds(
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
        ".credentials.json",
        "auth.json",
        "config.toml",
        "sessions",
        "settings.json",
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


def test_main_claude_kinds_runs_only_the_kinds_session_into_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = _fake_captures(monkeypatch)
    out = tmp_path / "o"

    assert main(["--cli", "claude-kinds", "--out", str(out)]) == 0

    assert calls == [("claude-kinds", out / "scratch" / "claude-kinds", 180.0, 300)]
    assert Path.cwd() == tmp_path
    assert capsys.readouterr().out.splitlines() == [
        f"{out / 'kinds' / 'claude-kinds.jsonl'} (3 bytes)",
    ]


def test_main_both_runs_each_cli_clears_scratch_and_restores_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = _fake_captures(monkeypatch)
    out = tmp_path / "deep" / "out"
    (out / "scratch").mkdir(parents=True)
    (out / "scratch" / "stale").write_text("old", encoding="utf-8")

    code = main(["--out", str(out), "--turn-sec", "5", "--timeout-sec", "7"])

    assert code == 0
    assert calls == [
        ("claude", out / "scratch" / "claude", 5.0, 7),
        ("codex", out / "scratch" / "codex", 5.0, 7),
    ]
    assert not (out / "scratch" / "stale").exists()
    assert Path.cwd() == tmp_path
    assert capsys.readouterr().out.splitlines() == [
        f"{out / 'claude.jsonl'} (3 bytes)",
        f"{out / 'codex.jsonl'} (3 bytes)",
    ]


def test_main_claude_only_skips_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = _fake_captures(monkeypatch)

    assert main(["--cli", "claude", "--out", str(tmp_path / "o")]) == 0

    assert [name for name, *_ in calls] == ["claude"]


def test_main_fails_when_nothing_was_captured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = _fake_captures(monkeypatch, empty=True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["capture", "--cli", "codex", "--out", str(tmp_path / "o")],
    )

    assert main() == 1

    assert [name for name, *_ in calls] == ["codex"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "no session files captured\n"


def test_main_help_describes_the_capture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    # The docstring after its shell preamble, whole.
    description = out.partition("Capture hermetic agent-CLI sessions")[2]
    assert "Costs real API tokens" in description
    assert "exec uv" not in out
    assert "Which CLI to drive (default: both)" in out
    assert "Cap on how long one scripted turn may run" in out


def test_main_help_has_no_description_when_docstrings_are_stripped(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # As ``python -OO`` runs it.
    monkeypatch.setattr(capture, "__doc__", None)

    with pytest.raises(SystemExit):
        main(["--help"])

    # Usage, then straight to the arguments.
    after_usage = capsys.readouterr().out.split("\n\n")[1]
    assert after_usage.startswith("options:")


def test_the_session_script_is_the_capture_spec() -> None:
    assert _session_script() == (
        _Step(
            line=(
                'Do these in order, briefly: (1) run the shell command "echo alpha". '
                '(2) run the shell command "exit 7", which fails. (3) create note.txt '
                'containing "beta", then change beta to gamma. (4) read tiny.png and '
                "describe it in three words."
            ),
        ),
        _Step(
            line=(
                "Now search the web for the current stable Python version, then fetch "
                "https://example.com and quote its heading."
            ),
        ),
        _Step(
            line=(
                "Spawn one general-purpose subagent whose entire job is to reply with "
                'the word "sub". Report what it said.'
            ),
        ),
        _Step(line="/model"),
        _Step(line="Say exactly: after-model"),
        _Step(line="/compact"),
        _Step(line="Say exactly: after-compact"),
    )


def test_first_of_each_kind_refuses_a_line_that_is_not_an_object(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text('{"type":"a"}\n[]\n', encoding="utf-8")

    with pytest.raises(ReadError):
        _first_of_each_kind(path)


def test_main_makes_the_output_directory_and_its_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _ = _fake_captures(monkeypatch, empty=True)

    assert main(["--cli", "both", "--out", str(tmp_path / "a" / "b")]) == 1

    assert (tmp_path / "a" / "b").is_dir()


def test_main_refuses_a_cli_it_does_not_drive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--cli", "gemini", "--out", str(tmp_path)])

    assert exit_info.value.code == 2


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
