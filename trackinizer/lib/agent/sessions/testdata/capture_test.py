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

from treekle import json_loads

import pytest

from trackinizer.lib.agent.sessions.testdata import capture
from trackinizer.lib.agent.sessions.testdata.capture import (
    _add_arguments,
    _await_logs,
    _await_marks,
    _await_turn,
    _capture_claude,
    _capture_claude_kinds,
    _capture_codex,
    _completed_turns,
    _drive,
    _emit,
    _filler,
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
    monkeypatch.setattr(
        os,
        "environ",
        {
            "HOME": "/h",
            "PATH": "/bin",
            "TERM": "xterm",
            "OPENAI_API_KEY": "secret",
            "PWD": "/x",
            "GITHUB_TOKEN": "t",
        },
    )

    _isolate_environment()

    assert dict(os.environ) == {"HOME": "/h", "PATH": "/bin", "TERM": "xterm"}


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
    assert struct.unpack(">II", chunks[b"IHDR"][:8]) == (2, 2)
    assert len(zlib.decompress(chunks[b"IDAT"])) == 12


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


def test_reject_secrets_accepts_scrubbed_filler(tmp_path: Path) -> None:
    text = _scrub_secrets("key sk-" + "A" * 30)

    assert _reject_secrets(text, source=tmp_path / "x.jsonl") is None


def test_capture_claude_splits_main_and_sidechain_fixtures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
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
        project = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "p"
        (project / "sid" / "subagents").mkdir(parents=True)
        (project / "main.jsonl").write_text(f"main {cwd}\n", encoding="utf-8")
        (project / "sid" / "subagents" / "a.jsonl").write_text(
            "side\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(capture, "_drive", fake_drive)
    out = tmp_path / "out"
    out.mkdir()
    root = tmp_path / "root"

    written = _capture_claude(root, out, turn_sec=1.5, timeout_sec=9)

    assert written == [out / "claude_main.jsonl", out / "claude_sidechain.jsonl"]
    assert (out / "claude_main.jsonl").read_text(
        encoding="utf-8",
    ) == "main /workspace\n"
    assert (out / "claude_sidechain.jsonl").read_text(encoding="utf-8") == "side\n"
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


def test_capture_claude_kinds_serves_the_mcp_and_skill_and_emits_only_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    calls: list[tuple[list[str], Sequence[_Step], Path, dict[str, str]]] = []
    skills: list[str] = []

    def fake_drive(
        argv: list[str],
        script: Sequence[_Step],
        *,
        cwd: Path,
        env: dict[str, str],
        turn_sec: float,
        timeout_sec: int,
    ) -> None:
        assert (turn_sec, timeout_sec) == (1.5, 9)
        calls.append((argv, script, cwd, env))
        skill = cwd / ".claude" / "skills" / "probe" / "SKILL.md"
        skills.append(skill.read_text(encoding="utf-8"))
        project = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "p"
        (project / "sid" / "subagents").mkdir(parents=True)
        (project / "main.jsonl").write_text(
            '{"type":"mode"}\n'
            f'{{"type":"user","cwd":"{cwd}"}}\n'
            # 4097 bytes with its newline, then 4096.
            f'{{"type":"assistant","text":"{"x" * 4066}"}}\n'
            f'{{"type":"system","text":"{"y" * 4068}"}}\n',
            encoding="utf-8",
        )
        (project / "sid" / "subagents" / "a.jsonl").write_text("x", encoding="utf-8")

    monkeypatch.setattr(capture, "_drive", fake_drive)
    out = tmp_path / "out" / "kinds"
    root = tmp_path / "root"

    written = _capture_claude_kinds(root, out, turn_sec=1.5, timeout_sec=9)

    assert written == [out / "claude_main.jsonl"]
    # Record lines alone, and only the small ones, redacted.
    assert (out / "claude_main.jsonl").read_text(encoding="utf-8") == (
        f'{{"type":"user","cwd":"/workspace"}}\n{{"type":"system","text":"{"y" * 4068}"}}\n'
    )
    assert skills == [
        (
            "---\nname: probe\ndescription: Replies with a fixed word.\n---\n\n"
            "Reply with exactly: skill-ran\n"
        ),
    ]
    servers = {
        "mcpServers": {
            "probe": {
                "command": sys.executable,
                "args": ["-P", str(Path(capture.__file__).parent / "echo_mcp.py")],
            },
        },
    }
    ((argv, script, cwd, env),) = calls
    # ``touch`` alone is left unapproved: its prompt is the one Escape rejects.
    assert argv == [
        "claude",
        "--permission-mode",
        "default",
        "--allowedTools",
        "Write",
        "Edit",
        "Read",
        "Bash(grep:*)",
        "Bash(sleep:*)",
        "mcp__probe__echo",
        "Agent",
        "Skill",
        "--mcp-config",
        json.dumps(servers),
        "--strict-mcp-config",
    ]
    assert [step.line for step in script if step.escape] == [
        'Run the shell command "touch rejected.txt".',
        'Run the shell command "sleep 60".',
    ]
    assert len(script) == 6
    assert cwd == root / "work"
    assert env == {
        "CLAUDE_CONFIG_DIR": str(root / "home"),
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    }


def test_capture_codex_emits_only_rollout_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    argvs: list[list[str]] = []
    knobs: list[tuple[Sequence[_Step], Path, float, int]] = []

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
        knobs.append((script, cwd, turn_sec, timeout_sec))
        sessions = Path(env["CODEX_HOME"]) / "sessions" / "2026"
        sessions.mkdir(parents=True)
        (sessions / "rollout-1.jsonl").write_text("roll\n", encoding="utf-8")
        (sessions / "other.jsonl").write_text("nope\n", encoding="utf-8")

    monkeypatch.setattr(capture, "_drive", fake_drive)
    out = tmp_path / "out"
    out.mkdir()

    written = _capture_codex(tmp_path / "root", out, turn_sec=1.0, timeout_sec=2)

    assert written == [out / "codex_main.jsonl"]
    assert (out / "codex_main.jsonl").read_text(encoding="utf-8") == "roll\n"
    assert knobs == [(_session_script(), tmp_path / "root" / "work", 1.0, 2)]
    assert argvs[0][0] == "codex"
    assert "model_reasoning_summary=detailed" in argvs[0]


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
    monkeypatch.setattr(capture, "_capture_claude_kinds", make("claude-kinds"))
    monkeypatch.setattr(capture, "_capture_codex", make("codex"))
    return calls


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
    assert "no session files captured" in captured.err


def test_main_help_describes_the_capture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "Capture hermetic agent-CLI sessions" in capsys.readouterr().out


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
