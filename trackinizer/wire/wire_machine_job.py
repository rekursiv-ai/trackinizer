"""Wire contract for jobs on a person's own machine, run by the machine host.

A person's own API key asks for a job on a machine they own whose host says it runs
jobs (:data:`JOBS_FACT`). The host holds a wait on :data:`MACHINE_WORK_PATH` and is
handed one piece of work at a time: a job to start, a job to cancel, or a read of a
job's log or results. It reports each job's state on :data:`MACHINE_JOB_REPORT_PATH`
and answers a read on :data:`MACHINE_READ_REPLY_PATH` with at most
:data:`MAX_CHUNK_BYTES`, which the server hands to the reader waiting for it and keeps
nowhere.

No code passes through the server: a job names a git remote, a branch and a commit, and
the host fetches them itself. A job's id comes from the same sequence as a GPU job's,
so one number names one job of either kind.

This package is part of the publishable client distribution, so it must not import
``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit

import re
import uuid

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from trackinizer.wire.wire_exec import MAX_STREAM_BYTES, Base64
from trackinizer.wire.wire_job import (
    JOB_PREFIX,
    MAX_ARGV_CHARS,
    CommitSha,
    ManifestEntry,
)
from trackinizer.wire.wire_machines import NAME_PATTERN


__all__ = [
    "GPUS_FACT",
    "JOBS_FACT",
    "JOBS_MEMORY_FACT",
    "MACHINE_JOBS_PATH",
    "MACHINE_JOB_CANCEL_PATH",
    "MACHINE_JOB_FILE_PATH",
    "MACHINE_JOB_LOG_PATH",
    "MACHINE_JOB_MANIFEST_PATH",
    "MACHINE_JOB_PATH",
    "MACHINE_JOB_REPORT_PATH",
    "MACHINE_READ_REPLY_PATH",
    "MACHINE_TERMINAL_STATES",
    "MACHINE_WORK_PATH",
    "MAX_CHUNK_BYTES",
    "MAX_GPUS",
    "MAX_MANIFEST_BYTES",
    "MAX_MANIFEST_ENTRIES",
    "MAX_MEMORY_MB",
    "MAX_MINUTES",
    "MAX_RESULT_PATH_BYTES",
    "MIN_MEMORY_MB",
    "READ_HOLD_MAX_SEC",
    "WAIT_MAX_SEC",
    "CancelWork",
    "Chunk",
    "JobRecovery",
    "JobReport",
    "MachineJobList",
    "MachineJobSpec",
    "MachineJobState",
    "MachineJobSubmit",
    "MachineJobView",
    "ReadRefused",
    "ReadReply",
    "ReadWhat",
    "ReadWork",
    "ResultManifest",
    "StartWork",
    "WorkOffer",
    "branch_ref",
    "remote_protocol",
    "remote_url",
]

MACHINE_JOBS_PATH: Final = f"{JOB_PREFIX}/machine-jobs"
"""``POST``: queue a job on one of the caller's machines; ``GET``: list them."""

MACHINE_JOB_PATH: Final = f"{JOB_PREFIX}/machine-jobs/{{job_id}}"
MACHINE_JOB_CANCEL_PATH: Final = f"{JOB_PREFIX}/machine-jobs/{{job_id}}/cancel"
MACHINE_JOB_LOG_PATH: Final = f"{JOB_PREFIX}/machine-jobs/{{job_id}}/log"
"""``GET ?offset=N&wait=S``: the log from byte ``N``, relayed from the host."""

MACHINE_JOB_MANIFEST_PATH: Final = f"{JOB_PREFIX}/machine-jobs/{{job_id}}/manifest"
MACHINE_JOB_FILE_PATH: Final = f"{JOB_PREFIX}/machine-jobs/{{job_id}}/file"
"""``GET ?path=P&offset=N``: one result file from byte ``N``, relayed from the host."""

MACHINE_WORK_PATH: Final = f"{JOB_PREFIX}/machines/{{machine_id}}/work"
"""``GET ?instance=ID&wait_sec=S``, held up to :data:`WAIT_MAX_SEC`: the host's next
piece of work, as a :class:`WorkOffer`."""

MACHINE_JOB_REPORT_PATH: Final = f"{JOB_PREFIX}/machines/{{machine_id}}/jobs/{{job_id}}"
"""``POST``: the host's :class:`JobReport` on one of its jobs."""

MACHINE_READ_REPLY_PATH: Final = (
    f"{JOB_PREFIX}/machines/{{machine_id}}/reads/{{read_id}}"
)
"""``POST``: the host's :class:`ReadReply` to a read it was handed."""

JOBS_FACT: Final = "jobs"
"""The fact a host that serves jobs reports as ``true``."""

GPUS_FACT: Final = "gpus"
"""The fact naming how many GPUs a host offers jobs."""

JOBS_MEMORY_FACT: Final = "jobs_memory_mb"
"""The fact naming the most memory, in MiB, a host's jobs may hold together."""

MAX_CHUNK_BYTES: Final = MAX_STREAM_BYTES
"""The most one read returns."""

WAIT_MAX_SEC: Final = 25
"""The longest the server holds a host's wait for work."""

READ_HOLD_MAX_SEC: Final = 20
"""The longest a host holds a log read for new bytes."""

MAX_MINUTES: Final = 1_440
"""The longest wall-clock limit a job on a machine may ask for."""

MAX_GPUS: Final = 8
MIN_MEMORY_MB: Final = 64
MAX_MEMORY_MB: Final = 4_194_304

MAX_MANIFEST_ENTRIES: Final = 10_000
"""The most result files one manifest lists."""

MAX_MANIFEST_BYTES: Final = MAX_CHUNK_BYTES - 1_024
"""The serialized manifest budget, leaving room for its reply envelope."""

MAX_RESULT_PATH_BYTES: Final = 4_096
"""The most UTF-8 bytes in one result entry's path."""

type MachineJobState = Literal[
    "requested",
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timeout",
]
"""``requested`` until the host takes the job; ``queued`` while the host fetches its
code or waits for room; then ``running``, and one of the four ends."""

MACHINE_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {"completed", "failed", "cancelled", "timeout"},
)
"""States a job never leaves."""

type ReadWhat = Literal["log", "manifest", "file"]

_REMOTE_PROTOCOLS: Final = frozenset({"https", "ssh", "file"})

# ``[user@]host:path`` with no ``/`` before the colon and no second colon after it: git
# reads that as ssh. ``ext::cmd`` has the second colon, and a path has a ``/`` first.
_SCP: Final = re.compile(r"(?:[^@/:]+@)?[^@/:]+:(?!:).+")

_BRANCH: Final = re.compile(r"refs/heads/[A-Za-z0-9._/+@-]{1,200}")


def remote_protocol(url: str) -> str | None:
    """Return the protocol git would reach ``url`` by, or ``None`` for any other.

    Args:
      url: A git remote, as ``git remote get-url`` prints it.

    Returns:
      protocol: ``https``, ``ssh`` (an ``ssh://`` URL or the ``[user@]host:path``
        form) or ``file``; ``None`` for every other transport, a bare path, a URL
        with a blank or control character, and one that starts with ``-``, which git
        would read as an option.

    """
    if url.startswith("-") or re.search(r"[\x00-\x20\x7f]", url):
        return None
    if "://" in url:
        scheme = url.split("://", 1)[0].lower()
        return scheme if scheme in _REMOTE_PROTOCOLS else None
    return "ssh" if _SCP.fullmatch(url) else None


def remote_url(url: str) -> str:
    """Return ``url`` if a job may name it; raise ``ValueError`` saying why not.

    Only ``https`` and ``ssh`` remotes are accepted: ``file`` and a bare path would
    read the host's own repositories, and ``ext::`` runs a command. The server keeps
    the URL in a row, so one that carries a credential is refused.

    Args:
      url: A git remote.

    Returns:
      url: The same text.

    Raises:
      ValueError: It is too long, reaches git by another protocol, carries a
        credential, or names no host or repository path.

    """
    if len(url) > 2_048:
        raise ValueError("a remote URL has at most 2048 characters")
    protocol = remote_protocol(url)
    if protocol not in {"https", "ssh"}:
        raise ValueError("a job's remote must be an https or ssh git URL")
    if "://" not in url:
        return url
    split = urlsplit(url)
    if split.password is not None or (
        protocol == "https" and split.username is not None
    ):
        raise ValueError("a remote URL must not carry a credential")
    if split.hostname in {None, ""}:
        raise ValueError("a remote URL must name a host")
    if split.path.strip("/") == "":
        raise ValueError("a remote URL must name a repository path")
    return url


def branch_ref(ref: str) -> str:
    """Return ``ref`` if it is a branch git would accept; raise ``ValueError`` if not.

    Args:
      ref: A full ref name.

    Returns:
      ref: The same text.

    Raises:
      ValueError: It is not ``refs/heads/NAME`` with a name git takes, so it cannot
        read as an option either.

    """
    if (
        not _BRANCH.fullmatch(ref)
        or ".." in ref
        or any(
            not part or part.startswith((".", "-")) or part.endswith(".lock")
            for part in ref.split("/")
        )
    ):
        raise ValueError("the ref must be a branch, refs/heads/NAME")
    return ref


type RemoteUrl = Annotated[str, AfterValidator(remote_url)]
type BranchRef = Annotated[str, AfterValidator(branch_ref)]
type _Arg = Annotated[str, Field(pattern=r"^[^\x00]*$")]


class MachineJobSubmit(BaseModel):
    """What a person asks for: a command on one of their machines, from a pushed commit."""

    model_config = ConfigDict(extra="forbid")

    machine: str = Field(pattern=NAME_PATTERN)
    """The machine, by name; it must be the caller's."""

    argv: list[_Arg] = Field(min_length=1)
    """The command, run without a shell in a fresh worktree of the commit."""

    gpus: int = Field(ge=0, le=MAX_GPUS)
    memory_mb: int = Field(ge=MIN_MEMORY_MB, le=MAX_MEMORY_MB)
    """The memory cap the host puts the job under, in MiB."""

    minutes: int = Field(ge=1, le=MAX_MINUTES)
    """Wall-clock limit; the job is stopped when it runs out."""

    label: str = Field(default="", max_length=80, pattern=r"^[A-Za-z0-9._ -]*$")
    remote: RemoteUrl
    """The git remote the host fetches from."""

    ref: BranchRef
    """The branch, ``refs/heads/NAME``, whose tip the commit was when it was pushed."""

    commit: CommitSha

    @field_validator("argv")
    @classmethod
    def _short(cls, value: list[str]) -> list[str]:
        if sum(len(each) + 1 for each in value) > MAX_ARGV_CHARS:
            raise ValueError("the command is too long")
        return value


class MachineJobView(BaseModel):
    """One machine job as its owner sees it."""

    id: int
    machine: str
    state: MachineJobState
    gpus: int
    memory_mb: int
    minutes: int
    label: str
    argv: list[str]
    remote: str
    ref: str
    commit: str
    exit_code: int | None
    reason: str
    """One plain sentence when the job failed or was refused; empty otherwise."""

    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None


class MachineJobList(BaseModel):
    """The caller's machine jobs, newest first."""

    jobs: list[MachineJobView]


class MachineJobSpec(BaseModel):
    """A job as the host is handed it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    owner: str
    """Whose job it is; the host runs only its own account's."""

    argv: list[str]
    remote: str
    ref: str
    commit: str
    gpus: int
    memory_mb: int
    minutes: int
    label: str


class StartWork(BaseModel):
    """Start a job: fetch its commit, then run it when there is room."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["start"] = "start"
    job: MachineJobSpec


class CancelWork(BaseModel):
    """Stop a job the host holds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["cancel"] = "cancel"
    job_id: int


class ReadWork(BaseModel):
    """Read a job's log, list its results, or read one result file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["read"] = "read"
    read_id: uuid.UUID
    """The id the host answers under."""

    job_id: int
    what: ReadWhat
    path: str = Field(default="", max_length=4_096)
    """For ``file``: the path under the job's results folder."""

    offset: int = Field(default=0, ge=0)
    wait_sec: float = Field(default=0.0, ge=0, le=READ_HOLD_MAX_SEC)
    """For ``log``: how long to hold for bytes past ``offset`` while the job runs."""


class WorkOffer(BaseModel):
    """The answer to a host's wait: one piece of work, or none."""

    work: (
        Annotated[
            StartWork | CancelWork | ReadWork,
            Field(discriminator="kind"),
        ]
        | None
    ) = None


class JobReport(BaseModel):
    """What the host says about one of its jobs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["queued", "running", "completed", "failed", "cancelled", "timeout"]
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    reason: str = Field(default="", max_length=1_000)


class JobRecovery(JobReport):
    """Authorize recovery of an owned job without changing its server state."""

    verify_only: Literal[True] = True


class Chunk(BaseModel):
    """Bytes of a job's log or of one result file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["chunk"] = "chunk"
    data_b64: Base64
    offset: int = Field(ge=0)
    """Where in the file the bytes start."""

    size: int = Field(ge=0)
    """How long the file was when it was read."""

    ended: bool
    """Whether the job has ended, so the file will not grow."""


class ResultManifest(BaseModel):
    """Every regular file under a job's results folder, with its size and digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["manifest"] = "manifest"
    entries: list[ManifestEntry] = Field(max_length=MAX_MANIFEST_ENTRIES)
    total_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if any(
            len(entry.path.encode()) > MAX_RESULT_PATH_BYTES for entry in self.entries
        ):
            raise ValueError("a result path exceeds the byte limit")
        if len(self.model_dump_json().encode()) > MAX_MANIFEST_BYTES:
            raise ValueError("the manifest exceeds the reply byte limit")
        return self


class ReadRefused(BaseModel):
    """Why the host will not answer a read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["refused"] = "refused"
    reason: str = Field(max_length=1_000)


class ReadReply(BaseModel):
    """The host's answer to one read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reply: Annotated[
        Chunk | ResultManifest | ReadRefused,
        Field(discriminator="kind"),
    ]
