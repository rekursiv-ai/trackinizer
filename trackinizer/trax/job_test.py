"""The ``trax job`` command: verb routing, and every flow against fake ssh and server."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast, override

import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.lib.userdirs import data_dir
from trackinizer.trax import cli, job_source
from trackinizer.trax.context import CWD
from trackinizer.trax.job import JOB_WORD, Job, Rig
from trackinizer.trax.job_source import BundleInfo, Snapshot
from trackinizer.trax.job_ssh import SubprocessRunner, Tools
from trackinizer.wire.wire_job import (
    Grant,
    GrantStatus,
    GrantTicket,
    JobList,
    JobView,
    ReadGrantBody,
    SecretNames,
    StartBody,
    SubmitBody,
)


if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    import argparse

    from trackinizer.client.client import Client


_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIPtWmP3ce7m3hrwpxvm9x5s3WnCClGsA5R5nJ9J25ir1"
_PUBLIC = f"ssh-ed25519 {_BLOB}"
_CERT = "ssh-ed25519-cert-v01@openssh.com AAAAIHNzaC1lZDI1NTE5LWNlcnQtdjAx"
_COMMIT = "a" * 40
_BASE = "b" * 40
_URL = "https://trax.example"
_TOOLS = Tools(ssh="ssh", ssh_keygen="ssh-keygen")


def _no_client() -> Client:
    raise AssertionError("the command must not reach the server")


def _grant(role: str = "rw") -> Grant:
    return Grant.model_validate(
        {
            "job_id": 7,
            "serial": 3,
            "role": role,
            "node": {
                "name": "gpu-node-b",
                "host": "203.0.113.7",
                "port": 22,
                "user": "gj7",
                "host_key": _PUBLIC,
            },
            "certificate": _CERT,
            "valid_before": "2026-10-21T12:00:00Z",
            "max_bundle_bytes": 100_000,
        },
    )


def _job(state: str = "queued", **fields: object) -> JobView:
    return JobView.model_validate(
        {
            "id": 7,
            "state": state,
            "node": "gpu-node-b",
            "gpus": 1,
            "minutes": 5,
            "label": "try",
            "argv": ["python", "x.py"],
            "commit": _COMMIT,
            "bundle_sha256": None,
            "created_at": datetime(2026, 10, 21, 12, tzinfo=UTC),
            "started_at": None,
            "ended_at": None,
            "gpu_minutes": None,
            "usd": None,
            "reason": "",
            "upload_serial": 3,
            **fields,
        },
    )


class _Time:
    """A clock that only moves when something waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _Server:
    """The server's ``job_*`` methods, scripted, recording every call."""

    base_url = _URL

    def __init__(self, time: _Time) -> None:
        self.time = time
        self.calls: list[tuple[str, object]] = []
        self.grants = [GrantStatus(state="ready", grant=_grant())]
        self.read_grants = [GrantStatus(state="ready", grant=_grant("ro"))]
        self.views = [_job("queued")]
        self.names: list[str] = []

    def _next[T](self, queue: list[T]) -> T:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def job_submit(self, body: SubmitBody) -> JobView:
        self.calls.append(("submit", body))
        return _job("requested")

    def job_grant(self, job_id: int, *, wait: float) -> GrantStatus:
        self.calls.append(("grant", (job_id, wait)))
        status = self._next(self.grants)
        if status.state == "pending":
            self.time.now += wait
        return status

    def job_read_grant(self, job_id: int, body: ReadGrantBody) -> GrantTicket:
        self.calls.append(("read_grant", (job_id, body.public_key)))
        return GrantTicket(serial=9)

    def job_grant_serial(self, job_id: int, serial: int, *, wait: float) -> GrantStatus:
        self.calls.append(("grant_serial", (job_id, serial, wait)))
        return self._next(self.read_grants)

    def job_start(self, job_id: int, body: StartBody) -> JobView:
        self.calls.append(("start", (job_id, body)))
        return _job("starting")

    def job_view(self, job_id: int) -> JobView:
        self.calls.append(("job", job_id))
        return self._next(self.views)

    def job_list(self) -> JobList:
        self.calls.append(("jobs", None))
        return JobList(jobs=[_job("running"), _job("completed", id=6, node=None)])

    def job_cancel(self, job_id: int) -> JobView:
        self.calls.append(("cancel", job_id))
        return _job("cancelled")

    def job_secret_names(self) -> SecretNames:
        return SecretNames(names=self.names)

    def job_put_secret(self, name: str, value: str) -> None:
        self.calls.append(("put_secret", (name, value)))

    def job_delete_secret(self, name: str) -> None:
        self.calls.append(("delete_secret", name))


class _Node:
    """Plays ssh-keygen and the node's gate for every verb."""

    def __init__(self) -> None:
        self.words: list[list[str]] = []
        self.keygen_dirs: list[Path] = []
        self.bases: list[str] = []
        self.upload_codes = [0]
        self.uploaded: list[bytes] = []
        self.reply_commit: str | None = None
        self.files: dict[str, bytes] = {"a.txt": b"alpha", "d/b.bin": b"beta"}
        self.interrupt = False
        self.log_chunks = ["é".encode()[:1], "é".encode()[1:] + b" line\n"]

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
    ) -> int:
        if argv[0] == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            self.keygen_dirs.append(key.parent)
            key.write_text("PRIVATE\n")
            key.with_suffix(".pub").write_text(f"{_PUBLIC} trax-job\n")
            return 0
        assert stdout is not None
        words = list(argv[argv.index("--") + 1 :])
        self.words.append(words)
        match words[0]:
            case "bases":
                _ = stdout.write("".join(f"{each}\n" for each in self.bases).encode())
            case "upload":
                assert stdin is not None
                assert not isinstance(stdin, bytes)
                data = stdin.read()
                self.uploaded.append(data)
                if code := self.upload_codes.pop(0):
                    return code
                reply = {
                    "commit": self.reply_commit or words[3],
                    "bundle_sha256": words[2],
                    "bytes": len(data),
                    "files": 2,
                }
                _ = stdout.write(json.dumps(reply).encode() + b"\n")
            case "logs":
                if self.interrupt:
                    raise KeyboardInterrupt
                for chunk in self.log_chunks:
                    _ = stdout.write(chunk)
            case "manifest":
                for path, data in self.files.items():
                    entry = {
                        "path": path,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    _ = stdout.write(json.dumps(entry).encode() + b"\n")
            case "download":
                assert isinstance(stdin, bytes)
                wanted = stdin.decode().split()
                with tarfile.open(fileobj=stdout, mode="w|") as tar:
                    for path in wanted:
                        info = tarfile.TarInfo(path)
                        info.size = len(self.files[path])
                        tar.addfile(info, io.BytesIO(self.files[path]))
            case _:
                raise AssertionError(words)
        return 0


class _Rig:
    def __init__(self) -> None:
        self.time = _Time()
        self.server = _Server(self.time)
        self.node = _Node()

    def factory(self) -> Client:
        return cast("Client", self.server)

    @property
    def rig(self) -> Rig:
        return Rig(
            client=cast("Client", self.server),
            runner=self.node,
            tools=_TOOLS,
            sleep=self.time.sleep,
            clock=self.time.clock,
        )


@pytest.fixture
def world() -> _Rig:
    return _Rig()


outs: list[str] = []
"""The file names the fake bundler was asked to write."""

snapshotted: list[Path] = []
"""The directories the fake snapshotter was asked to snapshot."""

allowed: list[bool] = []
"""Whether each fake snapshot was told to send secret-named untracked files."""


def _fake_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    size: int = 12,
    base: str | None = None,
    untracked: tuple[str, ...] = (),
) -> list[list[str]]:
    """Replace git: the snapshot is a commit, the bundle a 12-byte file."""
    bundled: list[list[str]] = []
    outs.clear()
    snapshotted.clear()
    allowed.clear()
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)

    def snapshot(
        repo: Path,
        tmp: Path,
        *,
        allow_secret_names: bool = False,
    ) -> Snapshot:
        del tmp
        snapshotted.append(repo)
        allowed.append(allow_secret_names)
        return Snapshot(
            commit=_COMMIT,
            private_git_dir=private,
            files=5,
            untracked=untracked,
        )

    def bundle(snap: Snapshot, bases: Sequence[str], out: Path) -> BundleInfo:
        assert snap.commit == _COMMIT
        outs.append(out.name)
        bundled.append(list(bases))
        data = b"b" * size
        out.write_bytes(data)
        return BundleInfo(
            path=out,
            bytes=size,
            sha256=hashlib.sha256(data).hexdigest(),
            base=base if bases else None,
        )

    monkeypatch.setattr(job_source, "snapshot", snapshot)
    monkeypatch.setattr(job_source, "bundle", bundle)
    return bundled


def _submit(
    world: _Rig,
    *,
    command: tuple[str, ...] = ("true",),
    gpus: int = 1,
    label: str = "",
    follow: bool = False,
    include_untracked: bool = False,
) -> None:
    Job.run_submit(
        world.rig,
        command=command,
        gpus=gpus,
        minutes=10,
        label=label,
        follow=follow,
        include_untracked=include_untracked,
    )


# -- Verb routing.


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    seen: list[object] = []

    def run(cls: type[Job], verb: str, args: object, factory: object) -> None:
        del cls, verb, factory
        seen.append(args)

    monkeypatch.setattr(Job, "run", classmethod(run))
    return seen


@pytest.mark.parametrize(
    "rest",
    [
        [],
        ["help"],
        ["--help"],
        ["-h"],
        ["submit", "--help"],
        ["submit", "--gpus", "2", "-h"],
        ["logs", "7", "help"],
        ["help", "extra"],
        ["submit", "--help", "--", "python", "x.py"],
    ],
)
def test_help_is_shown_without_running_anything(
    rest: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = _capture_run(monkeypatch)

    Job.dispatch(JOB_WORD, rest, _no_client)

    out = capsys.readouterr().out
    assert out.startswith("Usage: trax job submit")
    assert seen == []


def test_a_help_flag_after_the_double_dash_belongs_to_the_job(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = _capture_run(monkeypatch)

    Job.dispatch(
        JOB_WORD,
        ["submit", "--gpus", "2", "--label", "a b", "--", "python", "x.py", "--help"],
        _no_client,
    )

    (args,) = seen
    assert capsys.readouterr().out == ""
    parsed = vars(cast("argparse.Namespace", args))
    assert parsed["action"] == "submit"
    assert parsed["command"] == ("python", "x.py", "--help")
    assert (parsed["gpus"], parsed["label"]) == (2, "a b")


@pytest.mark.parametrize(
    "rest",
    [["submit", "--label", "help", "--", "true"], ["results", "7", "--to", "help"]],
)
def test_the_word_help_as_a_flags_value_is_not_a_request_for_help(
    rest: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = _capture_run(monkeypatch)

    Job.dispatch(JOB_WORD, rest, _no_client)

    assert len(seen) == 1
    assert capsys.readouterr().out == ""


def test_help_before_a_double_dash_wins_over_what_follows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = _capture_run(monkeypatch)

    Job.dispatch(JOB_WORD, ["help", "--", "x"], _no_client)

    assert seen == []
    assert capsys.readouterr().out.startswith("Usage: trax job submit")


def test_a_second_double_dash_stays_in_the_job_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture_run(monkeypatch)

    Job.dispatch(JOB_WORD, ["submit", "--", "sh", "-c", "x", "--", "y"], _no_client)

    parsed = vars(cast("argparse.Namespace", seen[0]))
    assert parsed["command"] == ("sh", "-c", "x", "--", "y")


def test_submit_defaults_to_one_gpu_and_ten_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture_run(monkeypatch)

    Job.dispatch(JOB_WORD, ["submit", "--", "true"], _no_client)

    args = seen[0]
    names = ("gpus", "minutes", "label", "follow", "include_untracked")
    assert [getattr(args, name) for name in names] == [1, 10, "", False, False]


def test_submit_takes_include_untracked_as_a_flag_before_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture_run(monkeypatch)

    Job.dispatch(
        JOB_WORD,
        ["submit", "--include-untracked", "--", "true", "--include-untracked"],
        _no_client,
    )

    parsed = vars(cast("argparse.Namespace", seen[0]))
    assert parsed["include_untracked"] is True
    assert parsed["command"] == ("true", "--include-untracked")


def test_the_other_verbs_never_include_untracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture_run(monkeypatch)

    Job.dispatch(JOB_WORD, ["status"], _no_client)

    assert vars(cast("argparse.Namespace", seen[0]))["include_untracked"] is False


@pytest.mark.parametrize(
    "rest",
    [
        ["nonsense"],
        ["logs"],
        ["logs", "seven"],
        ["secret", "HF_TOKEN"],
        ["secret", "HF_TOKEN", "set", "a-literal-value"],
        ["secret", "HF_TOKEN", "wipe"],
        ["results", "7", "--to"],
    ],
)
def test_a_malformed_command_is_refused_before_any_request(rest: list[str]) -> None:
    with pytest.raises(SystemExit) as err:
        Job.dispatch(JOB_WORD, rest, _no_client)

    assert err.value.code == 2


def test_submit_needs_a_command() -> None:
    with pytest.raises(ClientError, match="after --"):
        Job.dispatch(JOB_WORD, ["submit"], _no_client)


def test_only_submit_takes_a_command() -> None:
    with pytest.raises(ClientError, match="only submit"):
        Job.dispatch(JOB_WORD, ["status", "--", "x"], _no_client)


def test_the_verb_is_listed_and_routed_by_the_top_level_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert Job in cli.DISPATCHERS
    assert Job.matches("job")

    cli.parse_and_run(["help", "job"], client_factory=_no_client)

    assert capsys.readouterr().out.startswith("Usage: trax job submit")


# -- Secrets: the value comes from stdin and nowhere else.


def test_a_secret_value_is_read_from_stdin_and_one_newline_is_stripped(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("hf_value\n"))

    Job.dispatch(
        JOB_WORD,
        ["secret", "HF_TOKEN", "set"],
        world.factory,
    )

    assert world.server.calls == [("put_secret", ("HF_TOKEN", "hf_value"))]
    assert capsys.readouterr().out == "set: job secret HF_TOKEN\n"


@pytest.mark.parametrize(
    ("stdin", "value"),
    [("hf\r\n", "hf"), ("hf\n\n", "hf\n"), ("hf", "hf"), ("a b\n", "a b")],
)
def test_one_trailing_line_ending_is_stripped_from_a_secret_and_no_more(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    stdin: str,
    value: str,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))

    Job.dispatch(JOB_WORD, ["secret", "WANDB_API_KEY", "set"], world.factory)

    assert world.server.calls == [("put_secret", ("WANDB_API_KEY", value))]


@pytest.mark.parametrize("stdin", ["", "\n", "\r\n"])
def test_an_empty_secret_is_refused(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    stdin: str,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))

    with pytest.raises(ClientError, match=r"^stdin held no value for HF_TOKEN$"):
        Job.dispatch(
            JOB_WORD,
            ["secret", "HF_TOKEN", "set"],
            world.factory,
        )

    assert world.server.calls == []


def test_a_secret_name_outside_the_two_known_is_refused_before_reading_stdin(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", None)

    with pytest.raises(
        ClientError,
        match=r"^a secret name must be one of HF_TOKEN, WANDB_API_KEY$",
    ):
        Job.dispatch(
            JOB_WORD,
            ["secret", "AWS_KEY", "set"],
            world.factory,
        )


def test_secrets_lists_names_and_del_removes_one(
    world: _Rig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = world.factory

    Job.dispatch(JOB_WORD, ["secrets"], factory)
    empty = capsys.readouterr().out
    world.server.names = ["HF_TOKEN", "WANDB_API_KEY"]
    Job.dispatch(JOB_WORD, ["secrets"], factory)
    listed = capsys.readouterr().out
    Job.dispatch(JOB_WORD, ["secret", "WANDB_API_KEY", "del"], factory)
    deleted = capsys.readouterr().out

    assert empty == "(no secrets)\n"
    assert listed == "HF_TOKEN\nWANDB_API_KEY\n"
    assert deleted == "deleted: job secret WANDB_API_KEY\n"
    assert world.server.calls == [("delete_secret", "WANDB_API_KEY")]


# -- status and cancel.


def test_status_lists_jobs_in_aligned_columns_with_the_command_cut(
    world: _Rig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Job.dispatch(JOB_WORD, ["status"], world.factory)

    assert capsys.readouterr().out == (
        "7  running    gpu-node-b  python x.py\n6  completed  -           python x.py\n"
    )


def test_a_long_command_is_cut_to_sixty_characters_in_the_listing(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    long = _job("running", argv=["x" * 70])
    monkeypatch.setattr(world.server, "job_list", lambda: JobList(jobs=[long]))

    Job.dispatch(JOB_WORD, ["status"], world.factory)

    assert capsys.readouterr().out == f"7  running  gpu-node-b  {'x' * 60}\n"


def test_status_of_one_job_shows_every_field(
    world: _Rig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world.server.views = [
        _job(
            "completed",
            started_at=datetime(2026, 10, 21, 12, 1, tzinfo=UTC),
            ended_at=datetime(2026, 10, 21, 12, 5, tzinfo=UTC),
            gpu_minutes=4,
            usd=0.2,
            reason="finished",
        ),
    ]

    Job.dispatch(JOB_WORD, ["status", "7"], world.factory)

    assert capsys.readouterr().out == (
        "job:      7\n"
        "state:    completed\n"
        "node:     gpu-node-b\n"
        "gpus:     1 for up to 5 min\n"
        "label:    try\n"
        "command:  python x.py\n"
        f"commit:   {_COMMIT}\n"
        "created:  2026-10-21T12:00:00+00:00\n"
        "started:  2026-10-21T12:01:00+00:00\n"
        "ended:    2026-10-21T12:05:00+00:00\n"
        "charged:  4 gpu-min, usd 0.2\n"
        "reason:   finished\n"
    )
    assert world.server.calls == [("job", 7)]


def test_status_of_a_job_not_yet_started_shows_dashes(
    world: _Rig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world.server.views = [_job("requested", node=None, label="")]

    Job.dispatch(JOB_WORD, ["status", "7"], world.factory)

    out = capsys.readouterr().out.splitlines()
    assert out[2:3] == ["node:     -"]
    assert out[4:5] == ["label:    -"]
    assert out[8:] == [
        "started:  -",
        "ended:    -",
        "charged:  -, -",
        "reason:   -",
    ]


def test_status_with_no_jobs_says_so(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(world.server, "job_list", lambda: JobList(jobs=[]))

    Job.dispatch(JOB_WORD, ["status"], world.factory)

    assert capsys.readouterr().out == "(no jobs)\n"


def test_cancel_reports_the_state_the_server_gave(
    world: _Rig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Job.dispatch(JOB_WORD, ["cancel", "7"], world.factory)

    assert world.server.calls == [("cancel", 7)]
    assert capsys.readouterr().out == "job 7: cancelled\n"


# -- submit.


def test_submit_uploads_the_bundle_starts_the_job_and_prints_only_its_id(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundled = _fake_source(monkeypatch, tmp_path, base=_BASE)
    world.node.bases = [_BASE]
    world.server.grants = [
        GrantStatus(state="pending"),
        GrantStatus(state="ready", grant=_grant()),
    ]
    saved = data_dir() / "rekursiv-ai" / "trax" / "job" / "trax.example" / "7"
    saved.mkdir(parents=True)

    with _caller_in(tmp_path):
        _submit(world, command=("python", "x.py"), label="try", gpus=2)

    assert snapshotted == [tmp_path]
    captured = capsys.readouterr()
    assert captured.out == "7\n"
    assert captured.err == (
        "snapshot: 5 files at aaaaaaaaaaaa\n"
        "job 7: requested, waiting for a node\n"
        "bundle: 12 bytes, on base bbbbbbbbbbbb\n"
    )
    assert [name for name, _ in world.server.calls] == [
        "submit",
        "grant",
        "grant",
        "start",
        "job",
    ]
    submit = cast("SubmitBody", world.server.calls[0][1])
    assert (submit.gpus, submit.minutes, submit.label) == (2, 10, "try")
    assert (submit.argv, submit.commit) == (["python", "x.py"], _COMMIT)
    assert submit.public_key == _PUBLIC
    assert world.server.calls[1:3] == [("grant", (7, 25)), ("grant", (7, 25))]
    start = cast("tuple[int, StartBody]", world.server.calls[3][1])
    assert start[0] == 7
    assert start[1].bundle_sha256 == hashlib.sha256(b"b" * 12).hexdigest()
    assert (start[1].commit, start[1].bundle_bytes) == (_COMMIT, 12)
    assert world.server.calls[4] == ("job", 7)
    assert bundled == [[_BASE]]
    assert outs == ["thin.bundle"]
    assert world.node.uploaded == [b"b" * 12]
    assert [words[0] for words in world.node.words] == ["bases", "upload"]
    assert (saved / "src.bundle").read_bytes() == b"b" * 12
    assert not world.node.keygen_dirs[0].exists()
    assert world.time.slept == [2]
    assert world.time.now == 25 + 2


@pytest.mark.parametrize("include", [False, True])
def test_submit_tells_the_snapshot_whether_to_send_secret_named_files(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    include: bool,
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)

    _submit(world, include_untracked=include)

    assert allowed == [include]


def test_submit_says_which_untracked_files_the_snapshot_took(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ = _fake_source(monkeypatch, tmp_path, untracked=("new.py", "sub/c.txt"))

    _submit(world)

    lines = capsys.readouterr().err.splitlines()
    assert lines[:2] == [
        "snapshot: 5 files at aaaaaaaaaaaa",
        "untracked files sent (2): new.py, sub/c.txt",
    ]


def test_submit_names_at_most_ten_untracked_files_and_counts_the_rest(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    names = tuple(f"f{number:02d}.py" for number in range(13))
    _ = _fake_source(monkeypatch, tmp_path, untracked=names)

    _submit(world)

    line = capsys.readouterr().err.splitlines()[1]
    assert line == (
        "untracked files sent (13): " + ", ".join(names[:10]) + " and 3 more"
    )


def test_submit_shows_an_untracked_name_that_holds_a_control_character_escaped(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ = _fake_source(monkeypatch, tmp_path, untracked=("a\x1b[2Jb.py",))

    _submit(world)

    captured = capsys.readouterr().err
    assert "\x1b" not in captured
    assert "untracked files sent (1): a\\x1b[2Jb.py" in captured


def _repo_with_a_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stand in a real repository with an untracked ``.env``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("print(1)\n")
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = t\n\temail = t@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for args in (["init", "--quiet"], ["add", "-A"], ["commit", "--quiet", "-m", "x"]):
        _ = subprocess.run(  # noqa: S603 -- fixed git argv on a tmp_path repo.
            ["git", "-C", str(repo), *args],  # noqa: S607 -- git is the tool under test.
            check=True,
            capture_output=True,
        )
    (repo / ".env").write_text("TOKEN=x\n")
    monkeypatch.chdir(repo)


def test_a_snapshot_that_refuses_a_secret_ends_the_submit_before_any_job_is_asked_for(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_with_a_secret(monkeypatch, tmp_path)

    with pytest.raises(ClientError, match=r"look like secrets: \.env\. "):
        _submit(world)

    assert world.server.calls == []


def test_include_untracked_sends_the_secret_named_file_and_says_so(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _repo_with_a_secret(monkeypatch, tmp_path)

    _submit(world, include_untracked=True)

    captured = capsys.readouterr()
    assert captured.out == "7\n"
    assert "untracked files sent (1): .env\n" in captured.err


def test_a_bad_flag_is_named_before_any_job_is_asked_for(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)

    with pytest.raises(
        ClientError,
        match=(
            r"^invalid job: gpus: Input should be less than or equal to 8; "
            r"minutes: Input should be greater than or equal to 1$"
        ),
    ):
        Job.run_submit(
            world.rig,
            command=("true",),
            gpus=9,
            minutes=0,
            label="",
            follow=False,
            include_untracked=False,
        )

    assert world.server.calls == []


def test_a_bad_argument_is_named_by_its_position(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)

    with pytest.raises(
        ClientError,
        match=r"^invalid job: argv\.1: String should match",
    ):
        _submit(world, command=("python", "a\x00b"))


def test_a_refused_grant_prints_the_reason_and_exits_one_without_uploading(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)
    world.server.grants = [GrantStatus(state="refused", reason="no credit left")]

    with pytest.raises(SystemExit) as err:
        _submit(world)

    assert err.value.code == 1
    assert capsys.readouterr().err.endswith("refused: no credit left\n")
    assert world.node.words == []
    assert not world.node.keygen_dirs[0].exists()


def test_a_refusal_with_no_reason_says_so(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)
    world.server.grants = [GrantStatus(state="refused")]

    with pytest.raises(SystemExit):
        _submit(world)

    assert capsys.readouterr().err.endswith("refused: no reason given\n")


def test_a_grant_that_is_ready_without_a_certificate_is_polled_again(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)
    world.server.grants = [
        GrantStatus(state="ready"),
        GrantStatus(state="ready", grant=_grant()),
    ]

    _submit(world)

    assert [name for name, _ in world.server.calls].count("grant") == 2


def test_no_grant_in_ten_minutes_names_the_job_to_cancel(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)
    world.server.grants = [GrantStatus(state="pending")]

    with pytest.raises(
        ClientError,
        match=r"^no certificate was granted within 10 minutes$",
    ):
        _submit(world)

    assert world.time.now == 600
    assert capsys.readouterr().err.endswith(
        "trax: job 7 was submitted; 'trax job cancel 7' releases it\n",
    )


def test_a_bundle_over_the_nodes_limit_is_refused_before_the_upload(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _ = _fake_source(monkeypatch, tmp_path, size=100_001)

    with pytest.raises(
        ClientError,
        match=r"^the bundle is 100001 bytes; this node takes at most 100000$",
    ):
        _submit(world)

    assert [words[0] for words in world.node.words] == ["bases"]


def test_a_bundle_exactly_at_the_nodes_limit_is_sent(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _ = _fake_source(monkeypatch, tmp_path, size=100_000)

    _submit(world)

    assert len(world.node.uploaded[0]) == 100_000


def test_an_integrity_failure_on_a_thin_bundle_retries_once_with_a_full_one(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundled = _fake_source(monkeypatch, tmp_path, base=_BASE)
    world.node.bases = [_BASE]
    world.node.upload_codes = [3, 0]

    _submit(world)

    assert bundled == [[_BASE], []]
    assert outs == ["thin.bundle", "full.bundle"]
    assert [words[0] for words in world.node.words] == ["bases", "upload", "upload"]
    assert capsys.readouterr().err.splitlines()[2:5] == [
        "bundle: 12 bytes, on base bbbbbbbbbbbb",
        "the node rejected the thin bundle; sending a full bundle",
        "bundle: 12 bytes, full",
    ]


@pytest.mark.parametrize("code", [2, 5, 255])
def test_any_other_upload_failure_is_not_retried(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    code: int,
) -> None:
    bundled = _fake_source(monkeypatch, tmp_path, base=_BASE)
    world.node.bases = [_BASE]
    world.node.upload_codes = [code]

    with pytest.raises(ClientError):
        _submit(world)

    assert len(bundled) == 1


def test_an_integrity_failure_on_a_full_bundle_stops(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)
    world.node.upload_codes = [3]

    with pytest.raises(ClientError, match="integrity"):
        _submit(world)

    assert [words[0] for words in world.node.words] == ["bases", "upload"]
    assert "trax job cancel 7" in capsys.readouterr().err


def test_a_reply_for_another_commit_is_refused(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)
    world.node.reply_commit = "c" * 40

    with pytest.raises(
        ClientError,
        match=(
            r"^the node stored something other than what was sent; "
            r"nothing was started$"
        ),
    ):
        _submit(world)

    assert not any(name == "start" for name, _ in world.server.calls)


@pytest.mark.parametrize("waiting", ["requested", "placed", "granted", "starting"])
def test_the_job_is_waited_on_until_it_is_queued(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    waiting: str,
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)
    world.server.views = [_job(waiting), _job("queued")]

    _submit(world)

    assert capsys.readouterr().out == "7\n"
    assert [name for name, _ in world.server.calls].count("job") == 2
    assert world.time.slept == [2, 2]


@pytest.mark.parametrize(
    ("state", "reason", "line"),
    [
        ("failed", "no node has room", "job 7 failed: no node has room"),
        ("expired", "", "job 7 expired: no reason given"),
        ("cancelled", "stopped", "job 7 cancelled: stopped"),
    ],
)
def test_a_job_that_ends_after_the_start_prints_why_and_exits_one(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    state: str,
    reason: str,
    line: str,
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)
    world.server.views = [_job("starting"), _job(state, reason=reason)]

    with pytest.raises(SystemExit) as err:
        _submit(world)

    assert err.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith(f"{line}\n")


def test_a_job_that_completed_before_anyone_looked_is_a_success(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)
    world.server.views = [_job("completed")]

    _submit(world)

    assert capsys.readouterr().out == "7\n"


def test_a_job_that_never_queues_is_reported_after_five_minutes(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)
    world.server.views = [_job("starting")]

    with pytest.raises(
        ClientError,
        match=r"^job 7 was not queued within 5 minutes$",
    ):
        _submit(world)

    assert world.time.slept == [2] * 150
    assert world.time.now == 300


def test_follow_streams_the_log_after_the_id_is_printed(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ = _fake_source(monkeypatch, tmp_path)

    _submit(world, follow=True)

    assert capsys.readouterr().out == "7\né line\n"
    assert world.node.words[-1] == ["logs", "--follow"]
    assert ("read_grant", (7, _PUBLIC)) in world.server.calls


def test_submit_end_to_end_on_a_real_repository(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("print(1)\n")
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = t\n\temail = t@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for args in (["init", "--quiet"], ["add", "-A"], ["commit", "--quiet", "-m", "x"]):
        _ = subprocess.run(  # noqa: S603 -- fixed git argv on a tmp_path repo.
            ["git", "-C", str(repo), *args],  # noqa: S607 -- git is the tool under test.
            check=True,
            capture_output=True,
        )
    (repo / "new.py").write_text("print(2)\n")
    monkeypatch.chdir(repo)

    _submit(world, command=("python", "new.py"))

    assert capsys.readouterr().out == "7\n"
    submit = cast("SubmitBody", world.server.calls[0][1])
    assert len(submit.commit) == 40
    assert world.node.uploaded[0].startswith(b"# v2 git bundle")


# -- logs and results.


def test_logs_ask_for_a_read_certificate_and_decode_the_stream_as_text(
    world: _Rig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Job.run_logs(world.rig, 7, follow=False)

    assert capsys.readouterr().out == "é line\n"
    assert world.server.calls[:2] == [
        ("read_grant", (7, _PUBLIC)),
        ("grant_serial", (7, 9, 25)),
    ]
    assert world.node.words == [["logs"]]
    assert not world.node.keygen_dirs[0].exists()


def test_bytes_in_a_log_that_are_not_text_are_replaced(
    world: _Rig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world.node.log_chunks = [b"ok \xff", b"\n"]

    Job.run_logs(world.rig, 7, follow=False)

    assert capsys.readouterr().out == "ok \ufffd\n"


# The log is the job's own bytes: a colour may reach the terminal, a command to it
# (set the clipboard, move the cursor, a one-character C1 CSI) is shown instead.
def test_a_log_shows_terminal_commands_instead_of_sending_them(
    world: _Rig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world.node.log_chunks = [
        b"\x1b[31mred\x1b[0m\t\x1b]52;c;aGk=\x07 \x1b[2J\x1b[A ",
        "\x9b6n\r\n".encode(),
    ]

    Job.run_logs(world.rig, 7, follow=False)

    assert capsys.readouterr().out == (
        "\x1b[31mred\x1b[0m\t\\x1b]52;c;aGk=\\x07 \\x1b[2J\\x1b[A \\x9b6n\r\n"
    )


def test_a_refused_read_grant_exits_one(
    world: _Rig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world.server.read_grants = [GrantStatus(state="refused", reason="too many grants")]

    with pytest.raises(SystemExit) as err:
        Job.run_logs(world.rig, 7, follow=False)

    assert err.value.code == 1
    assert "too many grants" in capsys.readouterr().err


class _Impatient(_Server):
    """Answers a read grant ``pending`` after ``took`` seconds, not the wait given."""

    def __init__(self, time: _Time, *, took: float) -> None:
        super().__init__(time)
        self.took = took

    @override
    def job_grant_serial(self, job_id: int, serial: int, *, wait: float) -> GrantStatus:
        polls = [name for name, _ in self.calls].count("grant_serial")
        # Bounds the test when nothing paces the polls; the deadline never moves.
        assert polls < 1_000, "the grant was polled in a tight loop"
        self.time.now += self.took
        return super().job_grant_serial(job_id, serial, wait=wait)


@pytest.mark.parametrize(("took", "slept"), [(0, [1] * 600), (1, [])])
def test_a_server_that_answers_early_is_polled_at_most_once_a_second(
    world: _Rig,
    took: float,
    slept: list[float],
) -> None:
    world.server = _Impatient(world.time, took=took)
    world.server.read_grants = [GrantStatus(state="pending")]

    with pytest.raises(
        ClientError,
        match=r"^no certificate was granted within 10 minutes$",
    ):
        Job.run_logs(world.rig, 7, follow=False)

    assert world.time.slept == slept


def test_ctrl_c_while_following_ends_quietly_and_removes_the_key(
    world: _Rig,
) -> None:
    world.node.interrupt = True

    with pytest.raises(SystemExit) as err:
        Job.run_logs(world.rig, 7, follow=True)

    assert err.value.code == 130
    assert not world.node.keygen_dirs[0].exists()


def test_results_download_into_the_default_directory_and_write_job_json(
    world: _Rig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Job.run_results(world.rig, 7, to=None)

    saved = data_dir() / "rekursiv-ai" / "trax" / "job" / "trax.example" / "7"
    assert (saved / "out" / "a.txt").read_bytes() == b"alpha"
    assert (saved / "out" / "d" / "b.bin").read_bytes() == b"beta"
    text = (saved / "job.json").read_text()
    assert text == f"{_job('queued').model_dump_json(indent=2)}\n"
    assert "public_key" not in json.loads(text)
    assert capsys.readouterr().out == (
        f"results: {saved / 'out'} (2 downloaded, 0 already held)\n"
    )
    assert [words[0] for words in world.node.words] == ["manifest", "download"]
    assert world.server.calls == [
        ("job", 7),
        ("read_grant", (7, _PUBLIC)),
        ("grant_serial", (7, 9, 25)),
    ]


def test_results_to_a_directory_skip_what_it_already_holds(
    world: _Rig,
    tmp_path: Path,
) -> None:
    target = tmp_path / "mine"
    Job.run_results(world.rig, 7, to=str(target))
    world.node.words.clear()

    Job.run_results(world.rig, 7, to=str(target))

    assert (target / "out" / "a.txt").read_bytes() == b"alpha"
    assert [words[0] for words in world.node.words] == ["manifest"]


@contextmanager
def _caller_in(directory: Path) -> Generator[None]:
    """Make ``directory`` the caller's, as the daemon does, not this process's."""
    token = CWD.set(str(directory))
    try:
        yield
    finally:
        CWD.reset(token)


def test_results_to_a_directory_inside_the_repository_are_refused(
    world: _Rig,
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "repo").resolve()
    (repo / "sub").mkdir(parents=True)
    _ = subprocess.run(  # noqa: S603 -- fixed git argv on a tmp_path repo.
        ["git", "-C", str(repo), "init", "--quiet"],  # noqa: S607 -- git is the tool under test.
        check=True,
        capture_output=True,
    )
    with _caller_in(repo / "sub"):
        for inside in ("here", "../deeper/still", str(repo / "x")):
            with pytest.raises(
                ClientError,
                match=(
                    rf"^--to {re.escape(inside)} is inside the repository at "
                    rf"{re.escape(str(repo))}; results are not kept in a worktree$"
                ),
            ):
                Job.run_results(world.rig, 7, to=inside)

    assert world.server.calls == []
    assert world.node.words == []


def test_results_outside_any_repository_may_go_anywhere(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    with _caller_in(plain):
        Job.run_results(world.rig, 7, to="relative")
        Job.run_results(world.rig, 7, to="~/from-home")

    assert (plain / "relative" / "out" / "a.txt").exists()
    assert (tmp_path / "home" / "from-home" / "out" / "a.txt").exists()


# -- The real rig: found tools, or one plain error before any job exists.


def _nothing_on_path(name: str) -> None:
    del name


def _everything_in_bin(name: str) -> str:
    return f"/bin/{name}"


@pytest.mark.parametrize(
    "rest",
    [["submit", "--", "true"], ["logs", "7"], ["results", "7"]],
)
def test_a_missing_ssh_fails_before_the_server_hears_of_a_job(
    rest: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _nothing_on_path)

    with pytest.raises(ClientError, match=r"^ssh was not found on PATH$"):
        Job.dispatch(JOB_WORD, rest, _no_client)


def test_the_real_rig_runs_ssh_through_a_subprocess_runner(
    world: _Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _everything_in_bin)
    seen: list[Rig] = []

    def run_logs(cls: type[Job], rig: Rig, job: int, *, follow: bool) -> None:
        del cls, job, follow
        seen.append(rig)

    monkeypatch.setattr(Job, "run_logs", classmethod(run_logs))

    Job.dispatch(JOB_WORD, ["logs", "7"], world.factory)

    (rig,) = seen
    assert isinstance(rig.runner, SubprocessRunner)
    assert rig.sleep is time.sleep
    assert rig.clock is time.monotonic
    assert rig.tools == Tools(ssh="/bin/ssh", ssh_keygen="/bin/ssh-keygen")
    assert rig.client is world.server


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
