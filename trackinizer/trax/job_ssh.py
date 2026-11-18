"""Throwaway ssh keys, and the node's gate verbs over ssh.

``trax job`` holds no long-lived key. Each command makes an ed25519 key in a private
temporary directory, has the server sign it for one job on one node, talks to the
node's gate with the certificate, and deletes the directory when it ends.

Every ssh call is an argv list with the gate verb after ``--``; no shell string is
built. A ``Runner`` runs argv lists, so tests drive the whole path without ssh.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Final, Protocol, Self

import io
import re
import shutil
import subprocess
import tempfile
import threading

from trackinizer.client.errors import ClientError
from trackinizer.wire.wire_job import (
    GateStatus,
    ManifestEntry,
    UploadReply,
    ed25519_public_key,
)


if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from pydantic import BaseModel

    from trackinizer.wire.wire_job import Grant, NodeAddress


__all__ = [
    "Gate",
    "GateError",
    "OneTimeKey",
    "Runner",
    "SubprocessRunner",
    "Tools",
    "one_time_key",
    "require_tool",
]


class Runner(Protocol):
    """Runs one argv list; a test stands in for ssh by implementing this."""

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
    ) -> int:
        """Run ``argv`` to the end and return its exit code.

        Args:
          argv: The program and its arguments, never a shell string.
          stdin: Bytes or a stream to feed the program; ``None`` gives it none.
          stdout: Where the program's output is copied as it arrives; ``None``
            discards it. The program's stderr is left to reach the terminal.

        Returns:
          code: The program's exit code.

        """
        ...


class SubprocessRunner:
    """The real runner: a child process with streamed stdin and stdout."""

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
    ) -> int:
        """Run ``argv`` to the end and return its exit code.

        Args:
          argv: The program and its arguments.
          stdin: Bytes or a stream to feed the program.
          stdout: Where output is copied as it arrives.

        Returns:
          code: The program's exit code.

        """
        with subprocess.Popen(  # noqa: S603 -- argv is built from fixed words and checked values.
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL if stdout is None else subprocess.PIPE,
        ) as child:
            feeder = threading.Thread(target=_feed, args=(child, stdin))
            feeder.start()
            try:
                if stdout is not None:
                    pipe = child.stdout
                    assert isinstance(pipe, io.BufferedReader)
                    for chunk in iter(pipe.read1, b""):
                        _ = stdout.write(chunk)
                return child.wait()
            except BaseException:
                # A sink that cannot take more, or Ctrl-C: nothing is left to wait for.
                child.kill()
                raise
            finally:
                feeder.join()


@dataclass(frozen=True, kw_only=True, slots=True)
class Tools:
    """The ssh programs this host runs."""

    ssh: str
    ssh_keygen: str

    @classmethod
    def find(cls) -> Self:
        """Find ``ssh`` and ``ssh-keygen`` on ``PATH``.

        Returns:
          tools: Their full paths.

        Raises:
          ClientError: One of them is missing.

        """
        return cls(ssh=require_tool("ssh"), ssh_keygen=require_tool("ssh-keygen"))


def require_tool(name: str) -> str:
    """Return the full path of ``name`` on ``PATH``, or raise one plain error."""
    if (found := shutil.which(name)) is None:
        raise ClientError(f"{name} was not found on PATH")
    return found


@dataclass(frozen=True, kw_only=True, slots=True)
class OneTimeKey:
    """An ed25519 key in a private directory that is deleted when the command ends."""

    directory: Path
    public_key: str

    @property
    def private(self) -> Path:
        """The private key file."""
        return self.directory / "key"

    @property
    def certificate(self) -> Path:
        """Where ssh looks for the certificate of the key."""
        return self.directory / "key-cert.pub"

    @property
    def known_hosts(self) -> Path:
        """The one-line file that pins the node's host key."""
        return self.directory / "known_hosts"


@contextmanager
def one_time_key(runner: Runner, *, tools: Tools) -> Generator[OneTimeKey]:
    """Make a key in a 0700 temporary directory, and delete it on exit.

    Args:
      runner: Runs ``ssh-keygen``.
      tools: The ssh programs.

    Yields:
      key: The key and its directory.

    Raises:
      ClientError: ``ssh-keygen`` failed.

    """
    directory = Path(tempfile.mkdtemp(prefix="trax-job-"))
    try:
        private = directory / "key"
        code = runner.run(
            [
                tools.ssh_keygen,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "trax-job",
                "-f",
                str(private),
            ],
        )
        if code:
            raise ClientError(f"ssh-keygen failed (exit {code})")
        public = ed25519_public_key(private.with_suffix(".pub").read_text())
        yield OneTimeKey(directory=directory, public_key=public)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class GateError(ClientError):
    """The node's gate, or ssh, ended with a nonzero exit code."""

    def __init__(self, verb: str, exit_code: int) -> None:
        super().__init__(
            _EXIT_TEXT.get(exit_code, "ssh exited {1} running {0}").format(
                verb,
                exit_code,
            ),
        )
        self.exit_code = exit_code


_EXIT_TEXT: Final = {
    2: "the node refused {0} (exit {1})",
    3: "the node failed an integrity check on {0} (exit {1})",
    4: "the node reports not found for {0} (exit {1})",
    5: "the node hit an internal error on {0} (exit {1})",
    255: "ssh could not connect to the node for {0} (exit {1})",
}


class Gate:
    """One signed grant and its key: the node's gate verbs over ssh."""

    def __init__(
        self,
        key: OneTimeKey,
        grant: Grant,
        *,
        runner: Runner,
        tools: Tools,
    ) -> None:
        """Write the certificate and the host key beside the key.

        Args:
          key: The one-time key the grant was signed for.
          grant: The signed certificate and where the node is.
          runner: Runs ssh.
          tools: The ssh programs.

        Raises:
          ClientError: The node's host is not a plain name or address.

        """
        # `ssh` reads a host that starts with '-' as an option (ProxyCommand runs a
        # program here), so the grant's host is held to a name or an address.
        if not re.fullmatch(r"[A-Za-z0-9:][A-Za-z0-9.:-]*", grant.node.host):
            raise ClientError(
                f"the grant names no usable node host: {grant.node.host!r}",
            )
        self.node: NodeAddress = grant.node
        self._key = key
        self._runner = runner
        self._tools = tools
        key.certificate.write_text(f"{grant.certificate}\n")
        key.known_hosts.write_text(f"{grant.node.name} {grant.node.host_key}\n")

    def bases(self) -> list[str]:
        """Return the commits the node already holds for this owner, newest first."""
        out = io.BytesIO()
        self._call("bases", stdout=out)
        lines = out.getvalue().decode(errors="replace").split()
        return [line for line in lines if re.fullmatch(r"[0-9a-f]{40}", line)]

    def upload(self, bundle: Path, *, sha256: str, commit: str) -> UploadReply:
        """Send the bundle to the node, which stores and checks it.

        Args:
          bundle: The bundle file.
          sha256: Its hex digest.
          commit: The snapshot commit it carries.

        Returns:
          reply: What the node stored.

        """
        out = io.BytesIO()
        with bundle.open("rb") as file:
            self._call(
                "upload",
                str(bundle.stat().st_size),
                sha256,
                commit,
                stdin=file,
                stdout=out,
            )
        return _reply(UploadReply, out, "reply to upload")

    def status(self) -> GateStatus:
        """Return the node's view of the job."""
        out = io.BytesIO()
        self._call("status", stdout=out)
        return _reply(GateStatus, out, "reply to status")

    def logs(self, out: IO[bytes], *, follow: bool) -> None:
        """Copy the job's log to ``out``; with ``follow``, until the job ends."""
        self._call("logs", *(["--follow"] if follow else []), stdout=out)

    def manifest(self) -> list[ManifestEntry]:
        """Return every result file with its size and digest.

        Returns:
          entries: One per regular file under the job's ``out/``.

        """
        out = io.BytesIO()
        self._call("manifest", stdout=out)
        lines = out.getvalue().decode(errors="replace").splitlines()
        return [
            _parse(ManifestEntry, line, "manifest") for line in lines if line.strip()
        ]

    def download(self, paths: Sequence[str], out: IO[bytes]) -> None:
        """Copy a tar stream of the named result files to ``out``.

        Args:
          paths: Paths under the job's ``out/``; none means every file.
          out: Receives the tar stream as it arrives.

        """
        wanted = "".join(f"{path}\n" for path in paths).encode()
        self._call("download", stdin=wanted, stdout=out)

    def _call(
        self,
        *words: str,
        stdin: bytes | IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
    ) -> None:
        """Run one gate verb and raise ``GateError`` on a nonzero exit."""
        code = self._runner.run(self._argv(words), stdin=stdin, stdout=stdout)
        if code:
            raise GateError(words[0], code)

    def _argv(self, words: Sequence[str]) -> list[str]:
        """Build the ssh argv for one gate verb."""
        key, node = self._key, self.node
        options = {
            "BatchMode": "yes",
            "IdentitiesOnly": "yes",
            "IdentityFile": str(key.private),
            "CertificateFile": str(key.certificate),
            "UserKnownHostsFile": str(key.known_hosts),
            "GlobalKnownHostsFile": "none",
            "StrictHostKeyChecking": "yes",
            "UpdateHostKeys": "no",
            "HostKeyAlias": node.name,
            "ControlPath": "none",
            "ClearAllForwardings": "yes",
            "ForwardAgent": "no",
            "ConnectTimeout": "10",
            "ServerAliveInterval": "15",
        }
        return [
            self._tools.ssh,
            "-F",
            "none",
            *(
                arg
                for name, value in options.items()
                for arg in ("-o", f"{name}={value}")
            ),
            "-p",
            str(node.port),
            "-l",
            node.user,
            node.host,
            "--",
            *words,
        ]


def _reply[M: BaseModel](model: type[M], out: io.BytesIO, what: str) -> M:
    """Parse the last non-empty line the node printed."""
    lines = [
        line for line in out.getvalue().decode(errors="replace").splitlines() if line
    ]
    if not lines:
        raise _malformed(what)
    return _parse(model, lines[-1], what)


def _parse[M: BaseModel](model: type[M], line: str, what: str) -> M:
    """Parse one JSON line, or raise an error that never quotes it."""
    try:
        return model.model_validate_json(line)
    except ValueError:
        # A pydantic error is a ValueError, and its message would quote the line.
        raise _malformed(what) from None


def _malformed(what: str) -> ClientError:
    """Return the error for a node reply that does not parse."""
    return ClientError(f"the node's {what} was malformed")


# A child that exits before it has read everything closes the pipe. That is its
# answer, not ours: the exit code says why, so the write error is dropped.
def _feed(child: subprocess.Popen[bytes], source: bytes | IO[bytes] | None) -> None:
    """Copy ``source`` to the child's stdin, then close it."""
    pipe = child.stdin
    if pipe is None:
        return
    try:
        if isinstance(source, bytes):
            _ = pipe.write(source)
        elif source is not None:
            shutil.copyfileobj(source, pipe)
        pipe.close()
    except OSError:
        pass
