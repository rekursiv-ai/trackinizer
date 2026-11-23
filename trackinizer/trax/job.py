"""The ``trax job`` command: run a job on a GPU node, straight from this computer.

``submit`` snapshots the repository you are in, asks the server for a job and a
one-job ssh certificate, and uploads the code as a git bundle directly to the node.
``results`` and ``logs`` read the job back from the node the same way. The server
only brokers the job; no code or result bytes pass through it, and this computer
holds no long-lived key: every command makes a throwaway key and deletes it when it
ends. It needs ``ssh``, ``ssh-keygen`` and ``git`` on ``PATH``.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast, override

import argparse
import codecs
import io
import re
import shutil
import sys
import tempfile
import time

from trackinizer.client.errors import ClientError
from trackinizer.lib.userdirs import data_dir
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.context import cwd, out_stream
from trackinizer.trax.render import echo


if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from typing import TextIO

    from _typeshed import ReadableBuffer

    import pydantic

    from trackinizer.client.client import Client
    from trackinizer.trax import (
        job_fetch,
        job_source,
        job_ssh,
        machine_host,
    )
    from trackinizer.trax.job_source import BundleInfo, Snapshot
    from trackinizer.trax.job_ssh import Gate, Runner, Tools
    from trackinizer.wire import wire_job
    from trackinizer.wire.wire_job import Grant, GrantStatus
else:
    from wrapt import lazy_import

    # The pydantic models, ssh and git machinery load only when a ``job`` verb runs,
    # so they stay off the cold-start path of every other verb.
    pydantic = lazy_import("pydantic")
    wire_job = lazy_import("trackinizer.wire.wire_job")
    job_fetch = lazy_import("trackinizer.trax.job_fetch")
    job_source = lazy_import("trackinizer.trax.job_source")
    job_ssh = lazy_import("trackinizer.trax.job_ssh")
    machine_host = lazy_import("trackinizer.trax.machine_host")


JOB_WORD: Final = "job"
"""The verb that selects this command, in matching, help and usage."""

_SUBMIT: Final = (
    "trax job submit [--gpus N] [--minutes M] [--label TEXT] [--follow] "
    "[--include-untracked] -- CMD..."
)
_UNTRACKED_SHOWN: Final = 10


class Rig:
    """What a flow talks to: the server, ssh, and the clock."""

    def __init__(
        self,
        *,
        client: Client,
        runner: Runner,
        tools: Tools,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        self.client = client
        self.runner = runner
        self.tools = tools
        self.sleep = sleep
        self.clock = clock


class Job(Command):
    """Run jobs on a GPU node, and read their logs and results."""

    names = (JOB_WORD,)

    help = HelpPage(
        usage=_SUBMIT,
        summary=(
            "Runs CMD on a GPU node. 'submit' snapshots the repository you are in "
            "(uncommitted and untracked files included, ignored files left out), "
            "says which untracked files it took, uploads it straight to the node "
            "over ssh, and prints the job id. "
            "'results' downloads the job's output straight from the node. The "
            "server only brokers the job and signs a one-job certificate; no code "
            "or result bytes pass through it, and this computer keeps no key."
        ),
        arguments=(
            ("JOB", "a job id, as 'submit' printed it"),
            ("CMD...", "the command, run without a shell in the snapshot's root"),
            ("NAME", "HF_TOKEN or WANDB_API_KEY"),
        ),
        options=(
            ("--gpus N", "GPUs for the job, 1 to 8 (default 1)"),
            ("--minutes M", "wall-clock limit, 1 to 360 (default 10)"),
            ("--label TEXT", "a short name for the job"),
            ("--follow", "with submit: stream the log until the job ends"),
            (
                "--include-untracked",
                (
                    "with submit: also send untracked files named like secrets "
                    "(.env*, *.pem, id_*, *credentials*, .netrc, *.key), which are "
                    "refused unless .gitignore covers them"
                ),
            ),
            ("-f", "with logs: stream the log until the job ends"),
            ("--to DIR", "with results: download here, outside the repository"),
        ),
        examples=(
            f"{_SUBMIT}",
            f"trax {JOB_WORD} status [JOB]                  list jobs, or show one",
            f"trax {JOB_WORD} logs JOB [-f]                 print the job's log",
            f"trax {JOB_WORD} results JOB [--to DIR]        download the job's out/",
            f"trax {JOB_WORD} cancel JOB                    stop a job",
            f"trax {JOB_WORD} secrets                       list your secrets' names",
            f"trax {JOB_WORD} secret NAME set < FILE        set a secret from stdin",
            f"trax {JOB_WORD} secret NAME del               remove a secret",
        ),
        notes=(
            "Everything after the first '--' is the job's command, even '--help'.",
            (
                "A secret's value is read from stdin only. The job never sees it: "
                "the node adds it to requests to Hugging Face and Weights & Biases."
            ),
            (
                "Results go to a directory of your own data folder unless --to names "
                "one; a directory inside the repository is refused. 'src.bundle', the "
                "exact code that ran, is kept beside them for a later pull request."
            ),
            "Needs ssh, ssh-keygen and git on PATH.",
        ),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog=f"trax {JOB_WORD}",
            description=cls.__doc__,
            add_help=False,
        )
        # A verb sets only its own flags; the rest read as these, so ``run`` reads all.
        parser.set_defaults(
            gpus=1,
            minutes=10,
            label="",
            follow=False,
            include_untracked=False,
            job=None,
            to=None,
            name="",
            op="",
        )
        verbs = parser.add_subparsers(dest="action", required=True, metavar="VERB")
        submit = verbs.add_parser("submit", add_help=False)
        submit.add_argument("--gpus", type=int, default=1)
        submit.add_argument("--minutes", type=int, default=10)
        submit.add_argument("--label", default="")
        submit.add_argument("--follow", action="store_true")
        submit.add_argument("--include-untracked", action="store_true")
        status = verbs.add_parser("status", add_help=False)
        status.add_argument("job", nargs="?", type=int, default=None)
        logs = verbs.add_parser("logs", add_help=False)
        logs.add_argument("job", type=int)
        logs.add_argument("-f", "--follow", action="store_true")
        results = verbs.add_parser("results", add_help=False)
        results.add_argument("job", type=int)
        results.add_argument("--to", default=None)
        cancel = verbs.add_parser("cancel", add_help=False)
        cancel.add_argument("job", type=int)
        _ = verbs.add_parser("secrets", add_help=False)
        secret = verbs.add_parser("secret", add_help=False)
        secret.add_argument("name")
        secret.add_argument("op", choices=("set", "del"))
        return parser

    @classmethod
    @override
    def dispatch(
        cls,
        verb: str,
        rest: list[str],
        client_factory: Callable[[], Client],
    ) -> None:
        # The job's command is split off first: ``trax job submit -- python x.py
        # --help`` must reach the job, and the base class would show help instead.
        head, command = _split(rest)
        if _asks_for_help(head):
            echo(cls.help_text(), nl=False)
            return
        args = cls.make_parser().parse_args(head)
        args.command = tuple(command)
        cls.run(verb, args, client_factory)

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb
        flags = cast(_Flags, args)
        if flags.action != "submit" and flags.command:
            raise ClientError("only submit takes a command after --")
        if flags.action == "submit" and not flags.command:
            raise ClientError(f"submit needs a command after --: {_SUBMIT}")
        job = flags.job
        if flags.action == "submit":
            cls.run_submit(
                _rig(client_factory),
                command=flags.command,
                gpus=flags.gpus,
                minutes=flags.minutes,
                label=flags.label,
                follow=flags.follow,
                include_untracked=flags.include_untracked,
            )
        elif flags.action == "logs" and isinstance(job, int):
            cls.run_logs(_rig(client_factory), job, follow=flags.follow)
        elif flags.action == "results" and isinstance(job, int):
            cls.run_results(_rig(client_factory), job, to=flags.to)
        elif flags.action == "cancel" and isinstance(job, int):
            cls.run_cancel(client_factory(), job)
        elif flags.action == "status":
            cls.run_status(client_factory(), job)
        elif flags.action == "secrets":
            cls.run_secrets(client_factory())
        else:
            cls.run_secret(client_factory(), flags.name, op=flags.op)

    @classmethod
    def run_submit(
        cls,
        rig: Rig,
        *,
        command: Sequence[str],
        gpus: int,
        minutes: int,
        label: str,
        follow: bool,
        include_untracked: bool,
    ) -> None:
        """Snapshot the repository, upload it to a node, and start the job.

        Prints the job id once the server says the job is queued or later.

        Args:
          rig: The server, ssh and the clock.
          command: The job's command line.
          gpus: How many GPUs.
          minutes: The wall-clock limit.
          label: A short name for the job.
          follow: Stream the log until the job ends.
          include_untracked: Send untracked files named like secrets, which the
            snapshot refuses otherwise.

        """
        with tempfile.TemporaryDirectory(prefix="job-") as scratch:
            snap = job_source.snapshot(
                cwd(),
                Path(scratch),
                allow_secret_names=include_untracked,
            )
            echo(f"snapshot: {snap.files} files at {snap.commit[:12]}", err=True)
            _say_untracked(snap)
            with job_ssh.one_time_key(rig.runner, tools=rig.tools) as key:
                body = _submit_body(
                    key.public_key,
                    command=command,
                    gpus=gpus,
                    minutes=minutes,
                    label=label,
                    commit=snap.commit,
                )
                job = rig.client.job_submit(body)
                echo(f"job {job.id}: requested, waiting for a node", err=True)
                try:
                    grant = _await_grant(
                        rig,
                        lambda wait: rig.client.job_grant(job.id, wait=wait),
                    )
                    gate = job_ssh.Gate(key, grant, runner=rig.runner, tools=rig.tools)
                    info = _upload(gate, snap, Path(scratch), grant)
                    results = _results_dir(rig.client.base_url, job.id, None)
                    results.mkdir(parents=True, exist_ok=True)
                    _ = shutil.copyfile(info.path, results / "src.bundle")
                    _await_queued(rig, job.id, snap, info)
                except ClientError:
                    echo(
                        f"trax: job {job.id} was submitted; "
                        f"'trax job cancel {job.id}' releases it",
                        err=True,
                    )
                    raise
        echo(job.id)
        if follow:
            cls.run_logs(rig, job.id, follow=True)

    @classmethod
    def run_logs(cls, rig: Rig, job: int, *, follow: bool) -> None:
        """Print a job's log; with ``follow``, until the job ends.

        Args:
          rig: The server, ssh and the clock.
          job: The job id.
          follow: Keep streaming until the job ends.

        """
        with _read_gate(rig, job) as gate:
            try:
                gate.logs(_TextSink(out_stream()), follow=follow)
            except KeyboardInterrupt:
                raise SystemExit(130) from None

    @classmethod
    def run_results(cls, rig: Rig, job: int, *, to: str | None) -> None:
        """Download a job's output into a local directory.

        Args:
          rig: The server, ssh and the clock.
          job: The job id.
          to: The directory to fill; ``None`` uses the per-server data directory.

        """
        results = _results_dir(rig.client.base_url, job, to)
        view = rig.client.job_view(job)
        with _read_gate(rig, job) as gate:
            done = job_fetch.fetch(gate, gate.manifest(), results=results)
        (results / "job.json").write_text(f"{view.model_dump_json(indent=2)}\n")
        echo(
            f"results: {results / 'out'} "
            f"({done.downloaded} downloaded, {done.skipped} already held)",
        )

    @classmethod
    def run_status(cls, client: Client, job: int | None) -> None:
        """List the caller's jobs, or show one.

        Args:
          client: The server.
          job: The job id; ``None`` lists every job.

        """
        if job is not None:
            view = client.job_view(job)
            charged = "-" if view.gpu_minutes is None else f"{view.gpu_minutes} gpu-min"
            money = "-" if view.usd is None else f"usd {view.usd}"
            fields = {
                "job": view.id,
                "state": view.state,
                "node": view.node or "-",
                "gpus": f"{view.gpus} for up to {view.minutes} min",
                "label": view.label or "-",
                "command": " ".join(view.argv),
                "commit": view.commit,
                "created": view.created_at.isoformat(),
                "started": view.started_at.isoformat() if view.started_at else "-",
                "ended": view.ended_at.isoformat() if view.ended_at else "-",
                "charged": f"{charged}, {money}",
                "reason": view.reason or "-",
            }
            for name, value in fields.items():
                echo(f"{f'{name}:':<9} {value}")
            return
        jobs = client.job_list().jobs
        if not jobs:
            echo("(no jobs)")
            return
        rows = [
            ((str(each.id), each.state, each.node or "-"), " ".join(each.argv))
            for each in jobs
        ]
        widths = [max(len(head[col]) for head, _ in rows) for col in range(3)]
        for head, command in rows:
            padded = [cell.ljust(widths[col]) for col, cell in enumerate(head)]
            echo("  ".join((*padded, command[:60])))

    @classmethod
    def run_cancel(cls, client: Client, job: int) -> None:
        """Ask the server to stop a job; minutes that already ran are charged."""
        echo(f"job {job}: {client.job_cancel(job).state}")

    @classmethod
    def run_secrets(cls, client: Client) -> None:
        """Print the names of the caller's job secrets."""
        names = client.job_secret_names().names
        if not names:
            echo("(no secrets)")
        for name in names:
            echo(name)

    @classmethod
    def run_secret(cls, client: Client, name: str, *, op: str) -> None:
        """Set a job secret from stdin, or delete it.

        Args:
          client: The server.
          name: ``HF_TOKEN`` or ``WANDB_API_KEY``.
          op: ``set`` reads the value from stdin; ``del`` removes it.

        """
        if name not in wire_job.SECRET_NAMES:
            names = ", ".join(sorted(wire_job.SECRET_NAMES))
            raise ClientError(f"a secret name must be one of {names}")
        if op == "del":
            client.job_delete_secret(name)
            echo(f"deleted: {JOB_WORD} secret {name}")
            return
        value = sys.stdin.read().removesuffix("\n").removesuffix("\r")
        if not value:
            raise ClientError(f"stdin held no value for {name}")
        client.job_put_secret(name, value)
        echo(f"set: {JOB_WORD} secret {name}")


class _Flags(Protocol):
    """Parsed ``trax job`` flags; each verb sets only its own."""

    action: str
    command: tuple[str, ...]
    gpus: int
    minutes: int
    label: str
    follow: bool
    include_untracked: bool
    job: int | None
    to: str | None
    name: str
    op: str


# A colour (SGR), or a control character other than tab, newline and carriage return.
# ESC (0x1b) and the C1 range (0x80-0x9f, which holds a one-character CSI) are in it.
_TERMINAL_COMMAND: Final = re.compile(
    r"(\x1b\[[0-9;]*m)|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]",
)


class _TextSink(io.BytesIO):
    r"""A byte sink that writes UTF-8 text to a stream, whole characters at a time.

    The bytes are the job's own, so they may colour the text but never command the
    terminal: every other escape or control character is written as ``\xNN``. A
    colour cut in two by a read is shown that way too.
    """

    def __init__(self, stream: TextIO) -> None:
        super().__init__()
        self._stream = stream
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @override
    def write(self, buffer: ReadableBuffer, /) -> int:
        data = bytes(buffer)
        text = _TERMINAL_COMMAND.sub(_shown, self._decoder.decode(data))
        _ = self._stream.write(text)
        self._stream.flush()
        return len(data)


def _shown(found: re.Match[str]) -> str:
    r"""Keep a colour; spell any other control character as ``\xNN``."""
    text = found.group(0)
    return text if found.start(1) != -1 else f"\\x{ord(text):02x}"


def _say_untracked(snap: Snapshot) -> None:
    """Name the untracked files the snapshot took, so a stray one is seen before it goes."""
    if not snap.untracked:
        return
    shown = ", ".join(
        job_source.plain_name(n) for n in snap.untracked[:_UNTRACKED_SHOWN]
    )
    more = len(snap.untracked) - _UNTRACKED_SHOWN
    names = f"{shown} and {more} more" if more > 0 else shown
    echo(f"untracked files sent ({len(snap.untracked)}): {names}", err=True)


def _split(rest: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split ``rest`` at its first ``--`` into the flags and the job's command."""
    if "--" not in rest:
        return list(rest), []
    at = rest.index("--")
    return list(rest[:at]), list(rest[at + 1 :])


# A last word of ``help`` is the usual trailing form, except as the value of a flag:
# ``--label help`` names a job.
def _asks_for_help(head: Sequence[str]) -> bool:
    """Whether the tokens before ``--`` ask for help rather than an action."""
    if not head or head[0] == "help" or {"--help", "-h"} & set(head):
        return True
    return head[-1] == "help" and head[-2] not in {"--label", "--to"}


def _rig(client_factory: Callable[[], Client]) -> Rig:
    """Build the real rig; a missing ssh fails before any job is asked for."""
    tools = job_ssh.Tools.find()
    return Rig(
        client=client_factory(),
        runner=job_ssh.SubprocessRunner(),
        tools=tools,
        sleep=time.sleep,
        clock=time.monotonic,
    )


def _submit_body(
    public_key: str,
    *,
    command: Sequence[str],
    gpus: int,
    minutes: int,
    label: str,
    commit: str,
) -> wire_job.SubmitBody:
    """Build the submit request, or raise an error naming each bad field."""
    try:
        return wire_job.SubmitBody.model_validate(
            {
                "public_key": public_key,
                "gpus": gpus,
                "minutes": minutes,
                "label": label,
                "argv": list(command),
                "commit": commit,
            },
        )
    except pydantic.ValidationError as err:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in err.errors()
        )
        raise ClientError(f"invalid job: {problems}") from None


# Exits 1 with the server's reason when it refuses. A server that answers before the
# wait it was given is asked again a second later, not in a tight loop.
def _await_grant(rig: Rig, poll: Callable[[float], GrantStatus]) -> Grant:
    """Long-poll for a certificate, for up to ten minutes."""
    deadline = rig.clock() + 10 * 60
    while True:
        asked = rig.clock()
        status = poll(25)
        if status.state == "refused":
            echo(f"refused: {status.reason or 'no reason given'}", err=True)
            raise SystemExit(1)
        if status.state == "ready" and status.grant is not None:
            return status.grant
        if rig.clock() >= deadline:
            raise ClientError("no certificate was granted within 10 minutes")
        if rig.clock() - asked < 1:
            rig.sleep(1)


@contextmanager
def _read_gate(rig: Rig, job: int) -> Generator[Gate]:
    """Get a read certificate for ``job`` and yield a gate that uses it."""
    with job_ssh.one_time_key(rig.runner, tools=rig.tools) as key:
        ticket = rig.client.job_read_grant(
            job,
            wire_job.ReadGrantBody(public_key=key.public_key),
        )
        grant = _await_grant(
            rig,
            lambda wait: rig.client.job_grant_serial(job, ticket.serial, wait=wait),
        )
        yield job_ssh.Gate(key, grant, runner=rig.runner, tools=rig.tools)


def _upload(
    gate: Gate,
    snap: Snapshot,
    scratch: Path,
    grant: Grant,
) -> BundleInfo:
    """Send the snapshot as a thin bundle, or a full one if the node rejects it."""
    info = _bundle(snap, gate.bases(), scratch / "thin.bundle", grant)
    try:
        reply = gate.upload(info.path, sha256=info.sha256, commit=snap.commit)
    except job_ssh.GateError as err:
        if err.exit_code != 3 or info.base is None:
            raise
        echo("the node rejected the thin bundle; sending a full bundle", err=True)
        info = _bundle(snap, [], scratch / "full.bundle", grant)
        reply = gate.upload(info.path, sha256=info.sha256, commit=snap.commit)
    if (reply.commit, reply.bundle_sha256, reply.bytes) != (
        snap.commit,
        info.sha256,
        info.bytes,
    ):
        raise ClientError(
            "the node stored something other than what was sent; nothing was started",
        )
    return info


def _bundle(
    snap: Snapshot,
    bases: Sequence[str],
    out: Path,
    grant: Grant,
) -> BundleInfo:
    """Cut a bundle on the bases and refuse one over the node's limit."""
    info = job_source.bundle(snap, bases, out)
    how = "full" if info.base is None else f"on base {info.base[:12]}"
    echo(f"bundle: {info.bytes} bytes, {how}", err=True)
    if info.bytes > grant.max_bundle_bytes:
        raise ClientError(
            f"the bundle is {info.bytes} bytes; this node takes at most "
            f"{grant.max_bundle_bytes}",
        )
    return info


def _await_queued(rig: Rig, job: int, snap: Snapshot, info: BundleInfo) -> None:
    """Tell the server the code is up, then wait until the job is queued or later."""
    view = rig.client.job_start(
        job,
        wire_job.StartBody(
            commit=snap.commit,
            bundle_sha256=info.sha256,
            bundle_bytes=info.bytes,
        ),
    )
    deadline = rig.clock() + 5 * 60
    while view.state in {"requested", "placed", "granted", "starting"}:
        if rig.clock() >= deadline:
            raise ClientError(f"job {job} was not queued within 5 minutes")
        rig.sleep(2)
        view = rig.client.job_view(job)
    if view.state in wire_job.TERMINAL_STATES and view.state != "completed":
        echo(f"job {job} {view.state}: {view.reason or 'no reason given'}", err=True)
        raise SystemExit(1)


def _results_dir(url: str, job: int, to: str | None) -> Path:
    """Return where a job's results go, refusing a ``--to`` inside the repository."""
    if to is None:
        server = machine_host.server_dir(url)
        return data_dir() / "rekursiv-ai" / "trax" / "job" / server / str(job)
    target = (cwd() / Path(to).expanduser()).resolve()
    top = job_source.worktree_top(cwd())
    if top is not None and target.is_relative_to(top):
        raise ClientError(
            f"--to {to} is inside the repository at {top}; "
            "results are not kept in a worktree",
        )
    return target
