"""Signal one process, or one child's process group, and never more.

``os.kill`` and ``os.killpg`` take any id, and a few reach far past the one
meant. A test once handed its stand-in pid of 1 to a real ``os.killpg``, and
glibc sends ``killpg(1)`` as ``kill(-1)``: every process of the user's on the
node got SIGTERM. ``kill`` and ``killpg`` refuse those ids before the kernel
sees them; every signal this repository sends goes through them, which
houselint's ``os-kill`` rule holds. Signal 0 delivers nothing and only asks
whether the target exists, so it passes for any id.

Typical usage example:

  killpg(child.pid, signal.SIGTERM)   # A child started in a session of its own.
  kill(server_pid, signal.SIGKILL)
"""

from __future__ import annotations

import os


__all__ = [
    "kill",
    "killpg",
]


# ``os.kill`` reaches past one process for a pid below 2: ``-1`` is every process
# the user may signal, ``0`` and ``-g`` a whole group, and ``1`` is init. This
# process's own pid ends the caller; ``signal.raise_signal`` signals it on purpose.
def kill(pid: int, sig: int) -> None:
    """Send ``sig`` to process ``pid``: one other process, never a group nor this one.

    Args:
      pid: The process.
      sig: The signal; 0 only probes, and may name any pid.

    Raises:
      ValueError: ``sig`` is a signal and ``pid`` is below 2 or this process.

    """
    if sig and (pid <= 1 or pid == os.getpid()):
        raise ValueError(
            f"Refusing to signal pid {pid}: it is no other single process.",
        )
    os.kill(pid, sig)  # house-ignore[os-kill] -- The one checked process signal.


# ``os.killpg`` reaches past a child's group for three ids: glibc sends
# ``killpg(1)`` as ``kill(-1)``, every process the user may signal, and ``0`` and
# this process's own group id end the caller with it. A group a child leads has
# none of them: its id is the child's pid, never 0 or 1 nor a running ancestor's.
def killpg(pgid: int, sig: int) -> None:
    """Send ``sig`` to process group ``pgid``, a child's own and no wider one.

    Args:
      pgid: The group, which a child of this process leads.
      sig: The signal; 0 only probes, and may name any group.

    Raises:
      ValueError: ``sig`` is a signal and ``pgid`` is below 2 or names this
        process's own group.

    """
    if sig and (pgid <= 1 or pgid == os.getpgrp()):
        raise ValueError(
            f"Refusing to signal process group {pgid}: it is no child's own group.",
        )
    os.killpg(pgid, sig)  # house-ignore[os-kill] -- The one checked group signal.
