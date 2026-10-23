"""Doctor checks stay read-only and repairs preserve the caller's setup."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.lib.userdirs import config_dir
from trackinizer.trax import cli, connection
from trackinizer.trax.context import CWD
from trackinizer.trax.install import TARGETS, install


if TYPE_CHECKING:
    from collections.abc import Mapping

    from treekle import PlainTree

    from trackinizer.trax.client_cache import Target


@pytest.fixture(autouse=True)
def isolated_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep user-wide and project repairs inside the test directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)


class _Server:
    """Supply the HTTP boundary without network or shared profiles."""

    def __init__(self) -> None:
        self.base_url = "http://server:8765"
        self.calls: list[tuple[str, float | None]] = []
        self.error: ClientError | None = None
        self.version: PlainTree = {"sha": "a" * 40}

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> PlainTree:
        del params
        self.calls.append((path, timeout))
        if self.error is not None:
            raise self.error
        return self.version if path == "/api/version" else {"role": "writer"}


def test_check_reports_missing_skills_without_creating_directories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _Server()
    with pytest.raises(ClientError, match="doctor"):
        cli.parse_and_run(
            ["doctor", "--target", "codex", "--project"],
            client_factory=lambda: cast(Client, server),
        )
    output = capsys.readouterr().out
    assert "missing" in output
    assert str(tmp_path / ".agents/skills/trax") in output
    assert "--fix" in output
    assert "server: ok" in output
    assert "auth: ok" in output
    assert not (tmp_path / ".agents").exists()
    assert server.calls == [("/api/version", 3.0), ("/api/me/profile", 3.0)]


def test_fix_then_check_and_uninstall_restores_original_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["doctor", "--target", "cursor", "--project", "--offline"]
    cli.parse_and_run([*args, "--fix"], client_factory=_unexpected_server)
    destination = tmp_path / ".cursor/skills/trax"
    assert (destination / "belief/SKILL.md").is_file()
    assert (destination / "grammar.lark").is_file()
    assert "current" in capsys.readouterr().out
    cli.parse_and_run(args, client_factory=_unexpected_server)
    assert "current" in capsys.readouterr().out
    cli.parse_and_run([*args, "--uninstall"], client_factory=_unexpected_server)
    assert "removed" in capsys.readouterr().out
    assert not (tmp_path / ".cursor").exists()


def test_fix_refuses_unmanaged_skills_and_reports_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / ".claude/skills/trax"
    destination.mkdir(parents=True)
    skill = destination / "SKILL.md"
    skill.write_text("user guidance")
    with pytest.raises(ClientError, match="doctor found setup problems"):
        cli.parse_and_run(
            ["doctor", "--fix", "--project", "--offline"],
            client_factory=_unexpected_server,
        )
    output = capsys.readouterr().out
    assert "skills: error:" in output
    assert "unmanaged" in output
    assert str(destination) in output
    assert skill.read_text() == "user guidance"


def test_uninstall_refuses_unmanaged_skills_and_preserves_content(
    tmp_path: Path,
) -> None:
    destination = tmp_path / ".claude/skills/trax"
    destination.mkdir(parents=True)
    skill = destination / "SKILL.md"
    skill.write_text("user guidance")
    with pytest.raises(ClientError, match="unmanaged") as error:
        cli.parse_and_run(
            ["doctor", "--uninstall", "--project"],
            client_factory=_unexpected_server,
        )
    assert str(destination) in str(error.value)
    assert skill.read_text() == "user guidance"


@pytest.mark.parametrize(
    ("project", "repair"),
    [
        (False, "repair: trax doctor --fix --target claude"),
        (True, "repair: trax doctor --fix --target claude --project"),
    ],
)
def test_missing_skills_report_the_scope_specific_repair_command(
    *,
    project: bool,
    repair: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["doctor", "--offline"]
    if project:
        args.append("--project")
    with pytest.raises(ClientError, match="doctor"):
        cli.parse_and_run(args, client_factory=_unexpected_server)
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert "Skills are missing; run --fix." in lines
    assert repair in lines


def test_stale_skills_report_the_repair_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = tmp_path / "package"
    source = package / "docs/skills/trax"
    source.mkdir(parents=True)
    for path in (
        source / "SKILL.md",
        package / "docs/grammar.lark",
        package / "docs/GRAMMAR.md",
    ):
        path.write_text("original guidance")
    monkeypatch.setattr("trackinizer.trax.install._CWD", package)
    install(TARGETS["claude"], project=True, root=tmp_path)
    (source / "SKILL.md").write_text("updated guidance")
    with pytest.raises(ClientError, match="doctor"):
        cli.parse_and_run(
            ["doctor", "--project", "--offline"],
            client_factory=_unexpected_server,
        )
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert "repair: trax doctor --fix --target claude --project" in lines


def test_fix_succeeds_locally_even_when_profile_cannot_resolve(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(ClientError, match="doctor"):
        cli.parse_and_run(
            ["doctor", "--fix", "--project"],
            client_factory=_missing_profile,
        )
    assert (tmp_path / ".claude/skills/trax/SKILL.md").is_file()
    output = capsys.readouterr().out
    assert "current" in output
    assert "profile: error" in output
    assert "missing-profile" in output


def test_uninstall_never_needs_profile_or_server(tmp_path: Path) -> None:
    install(TARGETS["codex"], project=True, root=tmp_path)
    cli.parse_and_run(
        ["doctor", "--target", "codex", "--project", "--uninstall"],
        client_factory=_unexpected_server,
    )
    assert not (tmp_path / ".agents").exists()


def test_daemon_repair_uses_caller_directory(tmp_path: Path) -> None:
    caller = tmp_path / "caller"
    caller.mkdir()
    token = CWD.set(str(caller))
    try:
        cli.parse_and_run(
            ["doctor", "--fix", "--project", "--offline"],
            client_factory=_unexpected_server,
        )
    finally:
        CWD.reset(token)
    assert (caller / ".claude/skills/trax/SKILL.md").is_file()
    assert not (tmp_path / ".claude").exists()


def test_existing_source_link_is_healthy_and_preserved(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("source guidance")
    destination = tmp_path / ".agents/skills/trax"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(source, target_is_directory=True)
    args = ["doctor", "--target", "codex", "--project", "--offline"]
    for operation in (list[str](), ["--fix"], ["--uninstall"]):
        cli.parse_and_run([*args, *operation], client_factory=_unexpected_server)
        assert destination.is_symlink()
        assert destination.resolve() == source
    assert "source" in capsys.readouterr().out
    assert (source / "SKILL.md").read_text() == "source guidance"


@pytest.mark.parametrize("payload", [None, {}, {"sha": 123}, ["wrong"]])
def test_malformed_server_response_is_a_controlled_diagnostic(
    payload: PlainTree,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install(TARGETS["claude"], project=True, root=tmp_path)
    server = _Server()
    server.version = payload
    with pytest.raises(ClientError, match="doctor"):
        cli.parse_and_run(
            ["doctor", "--project"],
            client_factory=lambda: cast(Client, server),
        )
    assert "server: error" in capsys.readouterr().out


def test_server_failure_does_not_hide_skill_diagnostics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _Server()
    server.error = ClientError("connection refused")
    with pytest.raises(ClientError, match="doctor"):
        cli.parse_and_run(
            ["doctor", "--project"],
            client_factory=lambda: cast(Client, server),
        )
    output = capsys.readouterr().out
    assert "missing" in output
    assert "server: error" in output
    assert "connection refused" in output
    assert not (tmp_path / ".claude").exists()


def test_doctor_uses_the_existing_profile_resolution_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install(TARGETS["claude"], project=True, root=tmp_path)
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "doctor-target").write_text(
        "url=https://target.example\napi_key=secret\n",
    )
    server = _Server()
    heard: list[Target] = []

    def shared(target: Target) -> Client:
        heard.append(target)
        return cast(Client, server)

    monkeypatch.setattr(connection, "shared_client", shared)
    cli.parse_and_run(["--profile", "doctor-target", "doctor", "--project"])
    assert len(heard) == 1
    assert heard[0].url == "https://target.example"
    assert "secret" not in capsys.readouterr().out


@pytest.mark.parametrize("suffix", [[], ["help"], ["--help"]])
def test_doctor_help_is_available_without_connection(
    suffix: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["doctor", *suffix] if suffix else ["help", "doctor"]
    cli.parse_and_run(args, client_factory=_unexpected_server)
    assert "trax doctor" in capsys.readouterr().out


def test_fix_and_uninstall_are_mutually_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.parse_and_run(
            ["doctor", "--fix", "--uninstall"],
            client_factory=_unexpected_server,
        )
    assert stopped.value.code == 2
    assert "not allowed with argument --fix" in capsys.readouterr().err


def _unexpected_server() -> Client:
    pytest.fail("local doctor operation must not resolve or contact the server")


def _missing_profile() -> Client:
    raise ClientError("profile 'missing-profile' not found")


@pytest.mark.parametrize("target_name", ["claude", "codex", "cursor"])
@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("daemon", [False, True])
def test_target_scope_and_daemon_round_trip(
    target_name: str,
    *,
    project: bool,
    daemon: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    caller = tmp_path / "caller"
    process = tmp_path / "process"
    caller.mkdir()
    process.mkdir()
    monkeypatch.chdir(process if daemon else caller)
    args = ["doctor", "--target", target_name, "--offline"]
    if project:
        args.append("--project")
    token = CWD.set(str(caller) if daemon else "")
    destination = (caller if project else tmp_path / "home") / TARGETS[
        target_name
    ].project
    try:
        cli.parse_and_run([*args, "--fix"], client_factory=_unexpected_server)
        assert (destination / "trax/SKILL.md").is_file()
        cli.parse_and_run(args, client_factory=_unexpected_server)
        cli.parse_and_run([*args, "--uninstall"], client_factory=_unexpected_server)
    finally:
        CWD.reset(token)
    assert not destination.parent.exists()


def test_malformed_url_reports_profile_error_after_local_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TRACKINIZER_URL", "http://[")
    assert cli.main(["doctor", "--fix", "--project"]) == 2
    output = capsys.readouterr()
    assert "profile: error" in output.out
    assert "doctor found setup problems" in output.err
    assert (tmp_path / ".claude/skills/trax/SKILL.md").is_file()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
