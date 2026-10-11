"""Host a machine: join it and keep it connected.

``trax machine NAME connect`` joins once with the one-use ``enr_`` token read from
stdin (never argv, which any user on the host can read in a process listing) and
saves the machine credential it receives, readable by this user only. It then
heartbeats every 15 s until the server answers 410, which means the machine was
revoked. Lost contact never stops it: a refused connection, a server
error, or another host still holding the machine is retried, sooner at first.

It uses the machine's own credential, never the profile's key, so it runs in the
calling process rather than in traxd.
"""

from __future__ import annotations

from contextlib import closing
from importlib import metadata
from typing import TYPE_CHECKING, Final, Protocol

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

    from trackinizer.wire.wire_machine_host import HeartbeatResponse
    from trackinizer.wire.wire_machines import Facts
else:
    from wrapt import lazy_import

    # Only reading a corrupt credential file needs pydantic's error type.
    pydantic = lazy_import("pydantic")


__all__ = [
    "HOST_CLIS",
    "HostClient",
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
    ) -> HeartbeatResponse:
        """Report that the host is alive."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...


def connect(
    name: str,
    *,
    client_for: Callable[[str], HostClient],
    read_token: Callable[[], str],
    sleep: Callable[[float], None],
) -> None:
    """Join ``name`` when no credential is saved, then heartbeat until it is revoked.

    Args:
      name: The machine's name, already checked against the name rules.
      client_for: Returns a client that sends the given key; ``""`` sends none.
      read_token: Returns the enrollment token, read from stdin.
      sleep: Waits the given seconds between heartbeats.

    Raises:
      ClientError: The token was refused, the saved credential is unknown to the
        server, or any other answer a retry cannot change.

    """
    instance = uuid.uuid4()
    version = _host_version()
    facts = host_facts()
    # Sent with the first heartbeat only, and not after a join, which stored them.
    unsent: Facts | None = facts
    saved = _saved_credential(name)
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
        _save_credential(name, saved)
        echo(f"joined: machine {name}")
        unsent = None
    with closing(client_for(saved.credential)) as client:
        failures = 0
        while True:
            try:
                client.heartbeat_machine(
                    saved.machine_id,
                    instance=instance,
                    host_version=version,
                    facts=unsent,
                )
            except ClientError as err:
                if err.status_code == 410:
                    echo(f"revoked: machine {name}")
                    return
                failures += 1
                sleep(_retry_delay(err, failures=failures))
                continue
            unsent = None
            failures = 0
            sleep(HEARTBEAT_SEC)


def host_facts() -> Facts:
    """Return what this host reports about itself.

    Returns:
      facts: Its OS and architecture, CPU count, memory in MB, the user it runs
        as, and which of :data:`HOST_CLIS` are on ``PATH``.

    """
    return {
        "os": platform.system().lower(),
        "arch": platform.machine(),
        "cpus": os.cpu_count() or 0,
        "memory_mb": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2**20,
        "user": getpass.getuser(),
        "clis": [cli for cli in HOST_CLIS if shutil.which(cli) is not None],
    }


# A transport failure carries no status. A 409 here is another host instance still
# holding the machine, which lets go after 180 s of silence. Anything else in the
# 4xx range is an answer a retry cannot change. The exponent is capped: a host offline
# for hours counts past a thousand failures, and 2.0 ** 1024 raises OverflowError.
def _retry_delay(err: ClientError, *, failures: int) -> float:
    """Return how long to wait before the next try, or re-raise ``err``."""
    if (
        err.status_code is not None
        and 400 <= err.status_code < 500
        and err.code != "another_host_connected"
    ):
        raise err
    echo(f"trax: retrying after failure {failures}: {err}", err=True)
    return min(2.0 ** min(failures, 4), float(HEARTBEAT_SEC))


def _host_version() -> str:
    """Return the installed ``trackinizer`` version; empty from a source checkout."""
    try:
        return metadata.version("trackinizer")
    except metadata.PackageNotFoundError:
        return ""


def _saved_credential(name: str) -> JoinResponse | None:
    """Return the credential saved for ``name``, or ``None`` when none is."""
    path = state_dir() / "rekursiv-ai" / "trax" / "machines" / f"{name}.credential"
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
def _save_credential(name: str, joined: JoinResponse) -> None:
    """Save the credential the join returned, readable by this user only."""
    path = state_dir() / "rekursiv-ai" / "trax" / "machines" / f"{name}.credential"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(joined.model_dump_json())
        file.flush()
        os.fsync(file.fileno())
