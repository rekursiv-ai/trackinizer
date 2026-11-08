"""Each machine owner's version of their machine list, for a long-poll on its change.

The listing (``TERMINAL_MACHINES_PATH``) says which machines a person may use. A route
that writes a machine bumps its owner's version as soon as the write has committed,
before anything else that may fail, and a follower holds :meth:`MachineChanges.wait`
until the version moves, then lists again. A follower whose wait runs out lists nothing,
so a write made outside these routes, by hand or by another process, bumps nothing and
is not seen until the next bump of that owner, or this process restarts.

The versions live in this process's memory, as ``PipeHub``'s pipes and
``TerminalHub``'s terminals do, so it assumes one server process. A restart forgets
them: each process starts every owner at a random number of its own, so a version a
client kept from the last one does not match by chance, and its next wait answers at
once.
"""

from __future__ import annotations

from typing import Final

import asyncio
import contextlib
import secrets


__all__ = ["MachineChanges"]

_START_BELOW: Final = 1 << 52
"""Versions start below this, so one stays exact as a JavaScript number."""


class MachineChanges:
    """The version of each owner's machine list in this process, and its waits."""

    def __init__(self) -> None:
        self._start = secrets.randbelow(_START_BELOW)
        self._versions: dict[str, int] = {}
        self._changed: dict[str, asyncio.Event] = {}

    def version(self, owner: str) -> int:
        """Return the version of ``owner``'s machine list."""
        return self._versions.get(owner, self._start)

    def bump(self, owner: str) -> None:
        """Move ``owner``'s version on, waking every wait on it."""
        self._versions[owner] = self.version(owner) + 1
        if (changed := self._changed.pop(owner, None)) is not None:
            changed.set()

    async def wait(self, owner: str, *, since: int | None, wait_sec: float) -> int:
        """Return ``owner``'s version once it is not ``since``, or after ``wait_sec``.

        Args:
          owner: Whose machine list.
          since: The version the caller holds; ``None`` for none, answered at once.
          wait_sec: How long to hold while the version is still ``since``.

        Returns:
          version: The version now, which is ``since`` only when the wait ran out.

        """
        if since is None or since != self.version(owner):
            return self.version(owner)
        changed = self._changed.setdefault(owner, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(wait_sec):
                _ = await changed.wait()
        return self.version(owner)
