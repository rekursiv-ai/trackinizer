"""Host a machine: join it and keep it connected.

``trax machine NAME connect`` joins once with the one-use ``enr_`` token read from
stdin (never argv, which any user on the host can read in a process listing) and
saves the machine credential it receives, readable by this user only, under the
server it joined: one computer may be a machine of several servers. It then
heartbeats every 15 s until the server answers 410, which means the machine was
revoked. Lost contact never stops it: a refused connection or a server error is
retried, sooner at first. Its first heartbeat takes the machine from any host still
holding it, so a restart starts serving at once; the host it replaced stops at its
next heartbeat. Ctrl-C releases the machine, which then reads offline at once.

It uses the machine's own credential, never the profile's key, so it runs in the
calling process rather than in traxd.

A host that opted in to terminals also serves them (``trax/terminal_host.py``) for as
long as it stays connected, and kills every shell when the machine is revoked.
"""

from __future__ import annotations

from contextlib import closing, suppress
from importlib import metadata
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import urlsplit

import getpass
import os
import platform
import shutil
import uuid

from trackinizer.client.errors import ClientError
from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.render import echo
from trackinizer.wire.wire_machine_host import HEARTBEAT_SEC, JoinResponse


if TYPE_CHECKING:
    from collections.abc import Callable

    import pydantic

    from trackinizer.wire.wire_machine_host import Claim, HeartbeatResponse
    from trackinizer.wire.wire_machines import Facts
else:
    from wrapt import lazy_import

    # Only reading a corrupt credential file needs pydantic's error type.
    pydantic = lazy_import("pydantic")


__all__ = [
    "HOST_CLIS",
    "HostClient",
    "TerminalServing",
    "connect",
    "host_facts",
]

HOST_CLIS: Final = ("claude", "codex", "gemini", "git", "uv")
"""The commands a host reports when it finds them on ``PATH``."""


class HostClient(Protocol):
    """The calls a host makes: the ``Client`` methods, and closing it."""

    def join_machine(
        self,
        name: str,
        *,
        token: str,
        instance: uuid.UUID,
        host_version: str,
        facts: Facts,
    ) -> JoinResponse:
        """Exchange the enrollment token for the machine credential."""
        ...

    def heartbeat_machine(
        self,
        machine_id: uuid.UUID,
        *,
        instance: uuid.UUID,
        host_version: str,
        facts: Facts | None = None,
        claim: Claim = "keep",
    ) -> HeartbeatResponse:
        """Report that the host is alive, or that it lets go of the machine."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...


class TerminalServing(Protocol):
    """Serves terminals for a joined machine: started once, stopped on any exit."""

    def start(self, machine_id: uuid.UUID, instance: uuid.UUID) -> None:
        """Start listening for terminals to open."""
        ...

    def stop(self) -> None:
        """Stop listening and kill every shell."""
        ...


def connect(
    name: str,
    *,
    server: str,
    client_for: Callable[[str], HostClient],
    read_token: Callable[[], str],
    sleep: Callable[[float], None],
    terminals_for: Callable[[JoinResponse], TerminalServing] | None = None,
) -> None:
    """Join ``name`` when no credential is saved, then heartbeat until it is revoked.

    It also returns when a newer host took the machine over, and releases the machine
    on Ctrl-C before the interrupt goes on.

    Args:
      name: The machine's name, already checked against the name rules.
      server: The server's URL, which keeps its own credential for ``name``.
      client_for: Returns a client that sends the given key; ``""`` sends none.
      read_token: Returns the enrollment token, read from stdin.
      sleep: Waits the given seconds between heartbeats.
      terminals_for: Makes the terminal server for the joined machine's credential;
        ``None`` serves none, whatever the server asks.

    Raises:
      ClientError: The token was refused, the saved credential is unknown to the
        server, or any other answer a retry cannot change.

    """
    instance = uuid.uuid4()
    version = _host_version()
    facts = host_facts(terminal=terminals_for is not None)
    # Sent with the first heartbeat only, and not after a join, which stored them.
    unsent: Facts | None = facts
    saved = _saved_credential(name, server=server)
    if saved is None:
        token = read_token().strip()
        if not token:
            raise ClientError(
                "connect reads the enrollment token from stdin; none came",
            )
        with closing(client_for("")) as joining:
            saved = joining.join_machine(
                name,
                token=token,
                instance=instance,
                host_version=version,
                facts=facts,
            )
        _save_credential(name, joined=saved, server=server)
        echo(f"joined: machine {name}")
        unsent = None
    terminals = None if terminals_for is None else terminals_for(saved)
    if terminals is not None:
        terminals.start(saved.machine_id, instance)
    try:
        with closing(client_for(saved.credential)) as client:
            try:
                _heartbeat(
                    client,
                    name=name,
                    machine_id=saved.machine_id,
                    instance=instance,
                    version=version,
                    facts=unsent,
                    sleep=sleep,
                )
            except KeyboardInterrupt:
                # Once and best effort: a server out of reach leaves the machine to
                # read offline after 180 s of silence, as a crash does.
                with suppress(ClientError):
                    client.heartbeat_machine(
                        saved.machine_id,
                        instance=instance,
                        host_version=version,
                        claim="release",
                    )
                raise
    finally:
        if terminals is not None:
            terminals.stop()


def host_facts(*, terminal: bool = False) -> Facts:
    """Return what this host reports about itself.

    Args:
      terminal: Whether this host serves terminals, which the facts then say.

    Returns:
      facts: Its OS and architecture, CPU count, memory in MB, the user it runs
        as, which of :data:`HOST_CLIS` are on ``PATH``, and ``terminal: true`` when
        it serves terminals.

    """
    return {
        **({"terminal": True} if terminal else {}),
        "os": platform.system().lower(),
        "arch": platform.machine(),
        "cpus": os.cpu_count() or 0,
        "memory_mb": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2**20,
        "user": getpass.getuser(),
        "clis": [cli for cli in HOST_CLIS if shutil.which(cli) is not None],
    }


# The first heartbeat that lands takes the machine, so a restart never waits out the
# host it replaced; every later one keeps it. A keep refused with 409 means a newer
# host took the machine, and this one stops rather than fight it for the machine.
def _heartbeat(
    client: HostClient,
    *,
    name: str,
    machine_id: uuid.UUID,
    instance: uuid.UUID,
    version: str,
    facts: Facts | None,
    sleep: Callable[[float], None],
) -> None:
    """Heartbeat until the machine is revoked or another host takes it over."""
    claim: Claim = "take"
    unsent = facts
    failures = 0
    while True:
        try:
            client.heartbeat_machine(
                machine_id,
                instance=instance,
                host_version=version,
                facts=unsent,
                claim=claim,
            )
        except ClientError as err:
            if err.status_code == 410:
                echo(f"revoked: machine {name}")
                return
            if err.code == "another_host_connected":
                echo(f"taken over: machine {name} is served by a newer connect")
                return
            failures += 1
            sleep(_retry_delay(err, failures=failures))
            continue
        unsent = None
        claim = "keep"
        failures = 0
        sleep(HEARTBEAT_SEC)


# A transport failure carries no status. Anything in the 4xx range is an answer a
# retry cannot change. The exponent is capped: a host offline for hours counts past a
# thousand failures, and 2.0 ** 1024 raises OverflowError.
def _retry_delay(err: ClientError, *, failures: int) -> float:
    """Return how long to wait before the next try, or re-raise ``err``."""
    if err.status_code is not None and 400 <= err.status_code < 500:
        raise err
    echo(f"trax: retrying after failure {failures}: {err}", err=True)
    return min(2.0 ** min(failures, 4), float(HEARTBEAT_SEC))


def _host_version() -> str:
    """Return the installed ``trackinizer`` version; empty from a source checkout."""
    try:
        return metadata.version("trackinizer")
    except metadata.PackageNotFoundError:
        return ""


def _saved_credential(name: str, *, server: str) -> JoinResponse | None:
    """Return ``name``'s credential from ``server``, or ``None`` when none is saved."""
    path = (
        state_dir()
        / "rekursiv-ai"
        / "trax"
        / "machines"
        / _server_dir(server)
        / f"{name}.credential"
    )
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        return JoinResponse.model_validate_json(text)
    except pydantic.ValidationError:
        # Not chained: pydantic's message would quote the secret.
        raise ClientError(
            f"{path} does not hold a machine credential; remove it to join again",
        ) from None


# Created with mode 0600 in a 0700 directory before any byte lands, so the secret is
# never readable by another user; ``O_EXCL`` refuses to overwrite one saved meanwhile.
def _save_credential(name: str, *, joined: JoinResponse, server: str) -> None:
    """Save the credential the join returned, readable by this user only."""
    path = (
        state_dir()
        / "rekursiv-ai"
        / "trax"
        / "machines"
        / _server_dir(server)
        / f"{name}.credential"
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(joined.model_dump_json())
        file.flush()
        os.fsync(file.fileno())


# A credential belongs to the server that issued it: the same name on another server is
# another machine, which would refuse it. The host and port name the server's directory.
def _server_dir(url: str) -> str:
    """Return the directory of the server at ``url``: its host, and its port if set."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        raise ClientError(f"{url!r} names no server host")
    return host if parts.port is None else f"{host}_{parts.port}"
