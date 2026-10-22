"""Wire contract for direct GPU jobs: the routes a dev box calls on the server.

A person submits a job with a one-time SSH public key. The server records it, a
broker places it on a node, and a certificate authority signs the key for that one
job on that one node. The dev box then talks to the node over SSH: it uploads the
code as a git bundle and downloads the results. No code or result bytes pass
through the server; these bodies carry control fields only.

The node's gate answers the dev box over SSH with the JSON shapes at the end of
this module.

This package is part of the publishable client distribution, so it must not import
``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

import base64
import binascii
import hashlib

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)


__all__ = [
    "GATE_RO_VERBS",
    "GATE_RW_VERBS",
    "JOBS_PATH",
    "JOB_CANCEL_PATH",
    "JOB_GRANTS_PATH",
    "JOB_GRANT_PATH",
    "JOB_GRANT_SERIAL_PATH",
    "JOB_PATH",
    "JOB_PREFIX",
    "JOB_SECRETS_PATH",
    "JOB_SECRET_PATH",
    "JOB_START_PATH",
    "MAX_ARGV_CHARS",
    "NODE_PATTERN",
    "SECRET_NAMES",
    "TERMINAL_STATES",
    "GateStatus",
    "Grant",
    "GrantStatus",
    "GrantTicket",
    "JobList",
    "JobState",
    "JobView",
    "ManifestEntry",
    "NodeAddress",
    "ReadGrantBody",
    "SecretNames",
    "SecretPut",
    "StartBody",
    "SubmitBody",
    "UploadReply",
    "ed25519_public_key",
    "key_sha256",
]

JOB_PREFIX: Final = "/api/addons/jobs"
JOBS_PATH: Final = f"{JOB_PREFIX}/jobs"
JOB_PATH: Final = f"{JOB_PREFIX}/jobs/{{job_id}}"
JOB_GRANT_PATH: Final = f"{JOB_PREFIX}/jobs/{{job_id}}/grant"
JOB_GRANTS_PATH: Final = f"{JOB_PREFIX}/jobs/{{job_id}}/grants"
JOB_GRANT_SERIAL_PATH: Final = f"{JOB_PREFIX}/jobs/{{job_id}}/grants/{{serial}}"
JOB_START_PATH: Final = f"{JOB_PREFIX}/jobs/{{job_id}}/start"
JOB_CANCEL_PATH: Final = f"{JOB_PREFIX}/jobs/{{job_id}}/cancel"
JOB_SECRETS_PATH: Final = f"{JOB_PREFIX}/secrets"
JOB_SECRET_PATH: Final = f"{JOB_PREFIX}/secrets/{{name}}"

NODE_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,62}$"
"""A node name, as Slurm and the principal spell it."""

SECRET_NAMES: Final = frozenset({"HF_TOKEN", "WANDB_API_KEY"})
"""The per-person secrets a job's proxy may use."""

GATE_RW_VERBS: Final = ("bases", "upload", "status", "logs", "manifest", "download")
"""What an upload certificate may ask the node's gate."""

GATE_RO_VERBS: Final = ("status", "logs", "manifest", "download")
"""What a read certificate may ask the node's gate."""

MAX_ARGV_CHARS: Final = 4_096
"""Longest job command line, all arguments together."""

type JobState = Literal[
    "requested",
    "placed",
    "granted",
    "starting",
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timeout",
    "expired",
]

TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {"completed", "failed", "cancelled", "timeout", "expired"},
)
"""States a job never leaves."""

_ED25519: Final = "ssh-ed25519"


def ed25519_public_key(value: str) -> str:
    """Return ``ssh-ed25519 <base64>`` with any comment dropped.

    Args:
      value: An OpenSSH public key line.

    Returns:
      key: The key type and blob only.

    Raises:
      ValueError: It is not one ed25519 public key.

    """
    parts = value.split()
    if len(parts) < 2 or parts[0] != _ED25519:
        raise ValueError("the public key must be one ssh-ed25519 key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except binascii.Error as err:
        raise ValueError("the public key is not base64") from err
    prefix = len(_ED25519).to_bytes(4) + _ED25519.encode()
    if len(blob) != 51 or not blob.startswith(prefix):
        raise ValueError("the public key must be one ssh-ed25519 key")
    return f"{_ED25519} {parts[1]}"


def key_sha256(public_key: str) -> str:
    """Return the hex sha256 of an ed25519 public key's blob.

    Args:
      public_key: An OpenSSH public key line.

    Returns:
      digest: 64 lowercase hex digits.

    """
    blob = ed25519_public_key(public_key).split()[1]
    return hashlib.sha256(base64.b64decode(blob)).hexdigest()


type PublicKey = Annotated[str, AfterValidator(ed25519_public_key)]
type CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
type Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type NodeName = Annotated[str, StringConstraints(pattern=NODE_PATTERN)]


class SubmitBody(BaseModel):
    """What a dev box asks for: a job on one node, and a key to reach it."""

    model_config = ConfigDict(extra="forbid")

    public_key: PublicKey
    """The one-time key the certificate will be signed for."""

    gpus: int = Field(ge=1, le=8)
    minutes: int = Field(ge=1, le=360)
    """Wall-clock limit; the job is stopped when it runs out."""

    label: str = Field(default="", max_length=80, pattern=r"^[A-Za-z0-9._ -]*$")
    argv: list[Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")]] = Field(
        min_length=1,
    )
    """The command, run without a shell in the checked-out tree."""

    commit: CommitSha
    """The snapshot commit the dev box will upload."""

    @field_validator("argv")
    @classmethod
    def _short(cls, value: list[str]) -> list[str]:
        if sum(len(each) + 1 for each in value) > MAX_ARGV_CHARS:
            raise ValueError("the command is too long")
        return value


class JobView(BaseModel):
    """One job as its owner sees it."""

    id: int
    state: JobState
    node: str | None
    gpus: int
    minutes: int
    label: str
    argv: list[str]
    commit: str
    bundle_sha256: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    gpu_minutes: int | None
    usd: float | None
    reason: str
    """One plain sentence when the job failed or was refused; empty otherwise."""

    upload_serial: int | None
    """The serial of the upload certificate, once the job is placed."""


class JobList(BaseModel):
    """The caller's jobs, newest first."""

    jobs: list[JobView]


class NodeAddress(BaseModel):
    """Where and as whom a dev box reaches the node."""

    name: NodeName
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65_535)
    user: Annotated[str, StringConstraints(pattern=r"^gj[0-9]{1,12}$")]
    host_key: str = Field(pattern=r"^ssh-ed25519 [A-Za-z0-9+/=]+$")
    """The node's host key; the client pins it under the node's name."""


class Grant(BaseModel):
    """A signed one-time certificate for one job on one node."""

    job_id: int
    serial: int
    role: Literal["rw", "ro"]
    node: NodeAddress
    certificate: str = Field(pattern=r"^ssh-ed25519-cert-v01@openssh\.com \S+")
    valid_before: datetime
    max_bundle_bytes: int


class GrantStatus(BaseModel):
    """Whether a grant is signed yet."""

    state: Literal["pending", "ready", "refused"]
    grant: Grant | None = None
    reason: str = ""


class ReadGrantBody(BaseModel):
    """Ask for a read certificate for logs or results."""

    model_config = ConfigDict(extra="forbid")

    public_key: PublicKey


class GrantTicket(BaseModel):
    """The serial to poll for a requested certificate."""

    serial: int


class StartBody(BaseModel):
    """Tell the server the code is on the node, so it may queue the job."""

    model_config = ConfigDict(extra="forbid")

    commit: CommitSha
    bundle_sha256: Sha256Hex
    bundle_bytes: int = Field(ge=1)


class SecretPut(BaseModel):
    """A per-person secret's new value."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=4_096)


class SecretNames(BaseModel):
    """The secret names a person has set; values are never returned."""

    names: list[str]


class UploadReply(BaseModel):
    """The gate's answer to ``upload``."""

    commit: CommitSha
    bundle_sha256: Sha256Hex
    bytes: int
    files: int


class ManifestEntry(BaseModel):
    """One result file; ``sha256`` is null until the job is sealed."""

    path: str
    size: int = Field(ge=0)
    sha256: Sha256Hex | None


class GateStatus(BaseModel):
    """The gate's answer to ``status``."""

    job: int
    state: str
    exit_code: int | None
