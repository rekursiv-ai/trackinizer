"""Throwaway keys and the gate verbs over ssh, against a runner that fakes ssh."""

from __future__ import annotations

from pathlib import Path
from typing import IO, TYPE_CHECKING, override

import io
import json
import re
import shutil
import stat

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax import job_ssh
from trackinizer.trax.job_ssh import (
    Gate,
    GateError,
    SubprocessRunner,
    Tools,
    one_time_key,
    require_tool,
)
from trackinizer.wire.wire_job import Grant, NodeAddress


if TYPE_CHECKING:
    from collections.abc import Sequence


_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIPtWmP3ce7m3hrwpxvm9x5s3WnCClGsA5R5nJ9J25ir1"
_PUBLIC = f"ssh-ed25519 {_BLOB}"
_CERT = "ssh-ed25519-cert-v01@openssh.com AAAAIHNzaC1lZDI1NTE5LWNlcnQtdjAx"
_COMMIT = "a" * 40
_DIGEST = "b" * 64
_TOOLS = Tools(ssh="/usr/bin/ssh", ssh_keygen="/usr/bin/ssh-keygen")


class _FakeRunner:
    """Records every call; plays ssh-keygen; answers the rest from a script."""

    def __init__(self, *, code: int = 0, reply: bytes = b"") -> None:
        self.code = code
        self.reply = reply
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.key_dir_modes: list[int] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
    ) -> int:
        if argv[0] == _TOOLS.ssh_keygen:
            key = Path(argv[argv.index("-f") + 1])
            self.key_dir_modes.append(stat.S_IMODE(key.parent.stat().st_mode))
            key.write_text("PRIVATE\n")
            key.with_suffix(".pub").write_text(f"{_PUBLIC} trax-job\n")
            self.calls.append((list(argv), None))
            return 0
        sent = stdin if isinstance(stdin, bytes) or stdin is None else stdin.read()
        self.calls.append((list(argv), sent))
        if stdout is not None:
            _ = stdout.write(self.reply)
        return self.code


def _node(**fields: object) -> NodeAddress:
    return NodeAddress.model_validate(
        {
            "name": "gpu-node-b",
            "host": "203.0.113.7",
            "port": 2222,
            "user": "gj7",
            "host_key": f"ssh-ed25519 {_BLOB}",
            **fields,
        },
    )


def _grant(**fields: object) -> Grant:
    return Grant.model_validate(
        {
            "job_id": 7,
            "serial": 3,
            "role": "rw",
            "node": _node().model_dump(),
            "certificate": _CERT,
            "valid_before": "2026-10-21T12:00:00Z",
            "max_bundle_bytes": 1_000_000,
            **fields,
        },
    )


def _gate(runner: _FakeRunner, tmp_path: Path) -> Gate:
    key_dir = tmp_path / "keys"
    key_dir.mkdir(exist_ok=True)
    return Gate(
        job_ssh.OneTimeKey(directory=key_dir, public_key=_PUBLIC),
        _grant(),
        runner=runner,
        tools=_TOOLS,
    )


def _nothing_on_path(name: str) -> None:
    del name


def _everything_in_bin(name: str) -> str:
    return f"/bin/{name}"


def test_a_missing_tool_is_one_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _nothing_on_path)

    with pytest.raises(ClientError, match=r"^ssh was not found on PATH$"):
        require_tool("ssh")


def test_tools_are_found_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _everything_in_bin)

    assert Tools.find() == Tools(ssh="/bin/ssh", ssh_keygen="/bin/ssh-keygen")


def test_a_key_is_made_in_a_private_directory_and_removed_on_exit() -> None:
    runner = _FakeRunner()

    with one_time_key(runner, tools=_TOOLS) as key:
        directory = key.directory
        assert directory.is_dir()
        assert key.public_key == _PUBLIC
        assert runner.key_dir_modes == [0o700]

    assert not directory.exists()
    argv = runner.calls[0][0]
    assert argv[:9] == [
        "/usr/bin/ssh-keygen",
        "-q",
        "-t",
        "ed25519",
        "-N",
        "",
        "-C",
        "trax-job",
        "-f",
    ]
    assert argv[9] == str(directory / "key")


def _fail_inside_a_key(holder: list[Path]) -> None:
    with one_time_key(_FakeRunner(), tools=_TOOLS) as key:
        holder.append(key.directory)
        raise RuntimeError("boom")


def _enter_a_key(runner: _FakeRunner) -> None:
    with one_time_key(runner, tools=_TOOLS):
        pass


def test_a_key_is_removed_when_the_body_raises() -> None:
    holder: list[Path] = []

    with pytest.raises(RuntimeError, match="boom"):
        _fail_inside_a_key(holder)

    assert not holder[0].exists()


def test_a_failed_keygen_leaves_no_directory() -> None:
    made: list[Path] = []

    class _Failing(_FakeRunner):
        @override
        def run(
            self,
            argv: Sequence[str],
            *,
            stdin: bytes | IO[bytes] | None = None,
            stdout: IO[bytes] | None = None,
        ) -> int:
            made.append(Path(argv[argv.index("-f") + 1]).parent)
            return 1

    with pytest.raises(ClientError, match=r"^ssh-keygen failed"):
        _enter_a_key(_Failing())

    assert not made[0].exists()


def test_a_gate_writes_the_certificate_and_one_known_hosts_line(
    tmp_path: Path,
) -> None:
    gate = _gate(_FakeRunner(), tmp_path)

    assert (tmp_path / "keys" / "key-cert.pub").read_text() == f"{_CERT}\n"
    assert (tmp_path / "keys" / "known_hosts").read_text() == (
        f"gpu-node-b ssh-ed25519 {_BLOB}\n"
    )
    assert gate.node.name == "gpu-node-b"


@pytest.mark.parametrize(
    "host",
    [
        # Ssh reads a host that starts with '-' as an option, and ProxyCommand
        # runs a local program: a grant must never choose this computer's argv.
        "-oProxyCommand=touch pwned",
        "-p1",
        "user@203.0.113.7",
        "node b",
        "node%h",
        "ssh://node",
    ],
)
def test_a_host_that_is_not_a_name_or_address_is_refused_before_ssh(
    tmp_path: Path,
    host: str,
) -> None:
    runner = _FakeRunner()
    key_dir = tmp_path / "keys"
    key_dir.mkdir()

    with pytest.raises(ClientError, match=r"^the grant names no usable node host: "):
        _ = Gate(
            job_ssh.OneTimeKey(directory=key_dir, public_key=_PUBLIC),
            _grant(node=_node(host=host).model_dump()),
            runner=runner,
            tools=_TOOLS,
        )

    assert runner.calls == []
    assert list(key_dir.iterdir()) == []


@pytest.mark.parametrize(
    "host",
    ["203.0.113.7", "gpu-node-b.example.com", "::1", "2001:db8::7", "a"],
)
def test_a_host_name_or_address_is_accepted(tmp_path: Path, host: str) -> None:
    runner = _FakeRunner()
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    gate = Gate(
        job_ssh.OneTimeKey(directory=key_dir, public_key=_PUBLIC),
        _grant(node=_node(host=host).model_dump()),
        runner=runner,
        tools=_TOOLS,
    )

    _ = gate.bases()

    assert runner.calls[0][0][-3:] == [host, "--", "bases"]


def test_the_ssh_argv_is_exactly_the_designed_one(tmp_path: Path) -> None:
    runner = _FakeRunner(reply=b"")
    gate = _gate(runner, tmp_path)
    keys = tmp_path / "keys"

    _ = gate.bases()

    assert runner.calls[0][0] == [
        "/usr/bin/ssh",
        "-F",
        "none",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"IdentityFile={keys / 'key'}",
        "-o",
        f"CertificateFile={keys / 'key-cert.pub'}",
        "-o",
        f"UserKnownHostsFile={keys / 'known_hosts'}",
        "-o",
        "GlobalKnownHostsFile=none",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "HostKeyAlias=gpu-node-b",
        "-o",
        "ControlPath=none",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-p",
        "2222",
        "-l",
        "gj7",
        "203.0.113.7",
        "--",
        "bases",
    ]


def test_bases_keeps_only_commit_shas(tmp_path: Path) -> None:
    runner = _FakeRunner(reply=f"{_COMMIT}\nnoise\n\n{'c' * 40}\n{'D' * 40}\n".encode())

    assert _gate(runner, tmp_path).bases() == [_COMMIT, "c" * 40]


def test_upload_streams_the_bundle_to_ssh_stdin_and_parses_the_reply(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "x.bundle"
    bundle.write_bytes(b"BUNDLE-BYTES")
    reply = {"commit": _COMMIT, "bundle_sha256": _DIGEST, "bytes": 12, "files": 3}
    runner = _FakeRunner(reply=json.dumps(reply).encode() + b"\n")

    done = _gate(runner, tmp_path).upload(bundle, sha256=_DIGEST, commit=_COMMIT)

    argv, sent = runner.calls[0]
    assert argv[-5:] == ["--", "upload", "12", _DIGEST, _COMMIT]
    assert sent == b"BUNDLE-BYTES"
    assert (done.commit, done.files) == (_COMMIT, 3)


def test_a_malformed_upload_reply_is_refused_without_quoting_it(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "x.bundle"
    bundle.write_bytes(b"x")
    runner = _FakeRunner(reply=b'{"commit": "SECRET-LOOKING"}\n')

    with pytest.raises(ClientError, match="reply to upload was malformed") as err:
        _ = _gate(runner, tmp_path).upload(bundle, sha256=_DIGEST, commit=_COMMIT)

    assert "SECRET-LOOKING" not in str(err.value)


@pytest.mark.parametrize(
    ("code", "text"),
    [
        (2, "the node refused status (exit 2)"),
        (3, "the node failed an integrity check on status (exit 3)"),
        (4, "the node reports not found for status (exit 4)"),
        (5, "the node hit an internal error on status (exit 5)"),
        (255, "ssh could not connect to the node for status (exit 255)"),
        (1, "ssh exited 1 running status"),
    ],
)
def test_a_gate_exit_code_becomes_a_plain_error_that_carries_the_code(
    tmp_path: Path,
    code: int,
    text: str,
) -> None:
    gate = _gate(_FakeRunner(code=code), tmp_path)

    with pytest.raises(GateError, match=f"^{re.escape(text)}$") as err:
        _ = gate.status()

    assert err.value.exit_code == code
    assert isinstance(err.value, ClientError)


def test_status_and_manifest_parse_the_nodes_json_lines(tmp_path: Path) -> None:
    status = _FakeRunner(reply=b'{"job": 7, "state": "running", "exit_code": null}\n')
    entries = [
        {"path": "a.txt", "size": 3, "sha256": _DIGEST},
        {"path": "b/c.bin", "size": 0, "sha256": None},
    ]
    manifest = _FakeRunner(
        reply=("\n".join(json.dumps(entry) for entry in entries) + "\n").encode(),
    )

    assert _gate(status, tmp_path).status().state == "running"
    assert status.calls[0][0][-2:] == ["--", "status"]
    got = _gate(manifest, tmp_path).manifest()
    assert manifest.calls[0][0][-2:] == ["--", "manifest"]
    assert [(e.path, e.size, e.sha256) for e in got] == [
        ("a.txt", 3, _DIGEST),
        ("b/c.bin", 0, None),
    ]


def test_a_malformed_manifest_line_is_one_plain_error(tmp_path: Path) -> None:
    runner = _FakeRunner(reply=b"not json\n")

    with pytest.raises(ClientError, match=r"^the node's manifest was malformed$"):
        _ = _gate(runner, tmp_path).manifest()


def test_the_last_line_of_a_reply_is_the_answer(tmp_path: Path) -> None:
    reply = b'banner\n{"job": 7, "state": "done", "exit_code": 0}\n\n'

    status = _gate(_FakeRunner(reply=reply), tmp_path).status()

    assert (status.state, status.exit_code) == ("done", 0)


@pytest.mark.parametrize("reply", [b"", b"\n\n"])
def test_an_empty_reply_is_malformed_not_a_crash(tmp_path: Path, reply: bytes) -> None:
    with pytest.raises(
        ClientError,
        match=r"^the node's reply to status was malformed$",
    ):
        _ = _gate(_FakeRunner(reply=reply), tmp_path).status()


def test_bytes_that_are_not_text_are_replaced_not_fatal(tmp_path: Path) -> None:
    bases = _FakeRunner(reply=b"\xff\xfe\n" + _COMMIT.encode() + b"\n")
    manifest = _FakeRunner(
        reply=b"\xff\n"
        + json.dumps(
            {"path": "a", "size": 1, "sha256": None},
        ).encode(),
    )
    status = _FakeRunner(reply=b"\xff\n")

    assert _gate(bases, tmp_path).bases() == [_COMMIT]
    with pytest.raises(ClientError, match="manifest was malformed"):
        _ = _gate(manifest, tmp_path).manifest()
    with pytest.raises(ClientError, match="reply to status was malformed"):
        _ = _gate(status, tmp_path).status()


def test_logs_pass_the_follow_flag_and_copy_the_stream(tmp_path: Path) -> None:
    runner = _FakeRunner(reply=b"line one\nline two\n")
    sink = io.BytesIO()

    _gate(runner, tmp_path).logs(sink, follow=True)
    _gate(runner, tmp_path).logs(sink, follow=False)

    assert runner.calls[0][0][-3:] == ["--", "logs", "--follow"]
    assert runner.calls[1][0][-2:] == ["--", "logs"]
    assert sink.getvalue() == b"line one\nline two\n" * 2


def test_download_sends_the_wanted_paths_on_stdin(tmp_path: Path) -> None:
    runner = _FakeRunner(reply=b"TAR")
    sink = io.BytesIO()
    gate = _gate(runner, tmp_path)

    gate.download(["a.txt", "dir/b file.bin"], sink)
    gate.download([], sink)

    assert runner.calls[0][0][-2:] == ["--", "download"]
    assert runner.calls[0][1] == b"a.txt\ndir/b file.bin\n"
    assert runner.calls[1][1] == b""
    assert sink.getvalue() == b"TARTAR"


# -- The real runner, against tiny shell children.


def test_the_real_runner_feeds_stdin_and_streams_stdout() -> None:
    sink = io.BytesIO()

    code = SubprocessRunner().run(["cat"], stdin=b"hello", stdout=sink)

    assert (code, sink.getvalue()) == (0, b"hello")


def test_the_real_runner_moves_more_than_a_pipe_buffer_both_ways(
    tmp_path: Path,
) -> None:
    payload = b"x" * (1 << 20)
    source = tmp_path / "in.bin"
    source.write_bytes(payload)
    sink = io.BytesIO()

    with source.open("rb") as file:
        code = SubprocessRunner().run(["cat"], stdin=file, stdout=sink)

    assert (code, sink.getvalue()) == (0, payload)


def test_the_real_runner_returns_the_exit_code_and_discards_unwanted_output() -> None:
    code = SubprocessRunner().run(["sh", "-c", "echo noise; exit 3"])

    assert code == 3


def test_output_nobody_wants_never_blocks_the_child() -> None:
    code = SubprocessRunner().run(["sh", "-c", "head -c 300000 /dev/zero"])

    assert code == 0


def test_a_child_that_exits_before_reading_its_stdin_is_not_an_error() -> None:
    code = SubprocessRunner().run(
        ["sh", "-c", "exit 4"],
        stdin=b"y" * (1 << 20),
    )

    assert code == 4


def test_the_real_runner_kills_the_child_when_the_sink_fails() -> None:
    class _Broken(io.BytesIO):
        @override
        def write(self, buffer: object, /) -> int:
            raise BrokenPipeError("closed")

    with pytest.raises(BrokenPipeError):
        _ = SubprocessRunner().run(
            ["sh", "-c", "echo x; exec sleep 30"],
            stdout=_Broken(),
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
