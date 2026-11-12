"""A computer's hostname that stays when its network changes.

``socket.gethostname`` reads the kernel's hostname. macOS sets that, unless
someone named the computer with ``scutil --set HostName``, from what the
network offers -- a DHCP server's name for it, or its address's reverse DNS --
so one Mac reads ``Ada-MacBook.local`` on one network and
``unknown9626d97ae742.attlocal.net`` behind another router. Its Bonjour name,
the LocalHostName, is the one it keeps: the name System Settings shows under
Sharing. Linux sets its hostname once at boot, so the kernel's is stable there.

Typical usage example:

  record = {"host": hostname.stable_hostname()}
"""

from __future__ import annotations

from pathlib import Path

import socket
import subprocess


__all__ = ["stable_hostname"]


def stable_hostname(*, scutil: Path = Path("/usr/sbin/scutil")) -> str:
    """Return this computer's hostname, the same on every network it joins.

    Args:
      scutil: macOS's configuration tool; where there is none, as on Linux,
        the kernel's hostname is stable and is returned.

    Returns:
      hostname: On macOS, the HostName someone set, else the LocalHostName
        (``Ada-MacBook``); elsewhere, or on a Mac with neither, the kernel's.

    """
    # HostName first: a Mac someone named keeps that name as its kernel hostname
    # on every network, and was known by it before LocalHostName was read here.
    if scutil.exists():
        for key in ("HostName", "LocalHostName"):
            name = _configured(scutil, key=key)
            if name:
                return name
    return socket.gethostname()


def _configured(scutil: Path, *, key: str) -> str:
    """Return the name macOS has configured as ``key``; empty when none is set."""
    try:
        return subprocess.check_output(  # noqa: S603 -- No shell; scutil and a key this module spells.
            [scutil, "--get", key],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,  # pragma: no mutate -- Only a hung scutil reads it; none is.
        ).strip()
    # An unset name exits 1, saying so on stderr; a hung scutil times out.
    except (OSError, subprocess.SubprocessError):
        return ""
