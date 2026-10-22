"""In-process limits on the device login's public and guessable routes.

Both counts live in this process, like the terminal hub's open rate: they only have to
outlast a code, ten minutes, and a restart forgets them. Each check and its count are
one synchronous step, with no ``await`` between, so a burst of concurrent requests is
counted one by one rather than all passing the check before any is recorded.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

import time

from trackinizer.wire.wire_device import DEVICE_CODE_TTL_SEC, WRONG_ENTRIES_LOCK


if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import HTTPConnection


__all__ = [
    "CODES_PER_ADDRESS",
    "CodeRequests",
    "WrongEntries",
    "device_code_requests",
    "device_wrong_entries",
]


CODES_PER_ADDRESS: Final = 20
"""Codes one address may ask for within a code's lifetime; the global cap is a backstop."""

_SWEEP_ABOVE: Final = 1_024
"""Addresses tracked before the ones that went quiet are dropped."""


@dataclass(slots=True, kw_only=True)
class WrongEntries:
    """Wrong codes each person entered lately; five lock their entries out.

    A wrong code names no row, so the count belongs to the person who typed it, not to
    a code. An entry is counted when it arrives and forgiven once it names an open
    code, so concurrent guesses cannot all slip past the check before one is recorded.

    Attributes:
      clock: Monotonic seconds; a test moves it by hand.
      failures: Seconds of each person's entries still inside the window.

    """

    clock: Callable[[], float] = time.monotonic
    failures: dict[UUID, list[float]] = field(default_factory=dict, init=False)

    def reserve(self, user_id: UUID) -> bool:
        """Count one entry by ``user_id``, unless five are already counted.

        Args:
          user_id: The signed-in person entering a code.

        Returns:
          allowed: False when the person is locked out; the entry is not counted then.

        """
        recent = _recent(self.failures.get(user_id, []), self.clock())
        if len(recent) >= WRONG_ENTRIES_LOCK:
            self.failures[user_id] = recent
            return False
        self.failures[user_id] = [*recent, self.clock()]
        return True

    def forgive(self, user_id: UUID) -> None:
        """Take back the latest counted entry of ``user_id``: it named an open code."""
        kept = _recent(self.failures.get(user_id, []), self.clock())[:-1]
        if kept:
            self.failures[user_id] = kept
        else:
            self.failures.pop(user_id, None)


@dataclass(slots=True, kw_only=True)
class CodeRequests:
    """Codes each address asked for lately.

    Anyone may ask for a code, so without a ceiling per address one caller could fill
    the table of open codes and turn every real login away. Behind a proxy the address
    is the caller's only when the server trusts the proxy's forwarded headers.

    Attributes:
      clock: Monotonic seconds; a test moves it by hand.
      limit: Codes one address may ask for within a code's lifetime.
      asks: Seconds of each address's requests still inside the window.

    """

    clock: Callable[[], float] = time.monotonic
    limit: int = CODES_PER_ADDRESS
    asks: dict[str, list[float]] = field(default_factory=dict, init=False)

    def allow(self, address: str) -> bool:
        """Count one request from ``address``, unless it asked ``limit`` times already.

        Args:
          address: The caller's network address.

        Returns:
          allowed: False when the address is over its ceiling; the request is not
            counted then.

        """
        now = self.clock()
        if len(self.asks) > _SWEEP_ABOVE:
            self.asks = {
                each: recent
                for each, times in self.asks.items()
                if (recent := _recent(times, now))
            }
        recent = _recent(self.asks.get(address, []), now)
        if len(recent) >= self.limit:
            self.asks[address] = recent
            return False
        self.asks[address] = [*recent, now]
        return True


def device_wrong_entries(request: HTTPConnection) -> WrongEntries:
    """Return the app's wrong-entry counts, made on first use."""
    app = cast("FastAPI", request.app)
    entries: object = getattr(app.state, "device_wrong_entries", None)
    if not isinstance(entries, WrongEntries):
        entries = app.state.device_wrong_entries = WrongEntries()
    return entries


def device_code_requests(request: HTTPConnection) -> CodeRequests:
    """Return the app's per-address code request counts, made on first use."""
    app = cast("FastAPI", request.app)
    asks: object = getattr(app.state, "device_code_requests", None)
    if not isinstance(asks, CodeRequests):
        asks = app.state.device_code_requests = CodeRequests()
    return asks


def _recent(times: list[float], now: float) -> list[float]:
    """Return the entries of ``times`` still inside a code's lifetime at ``now``."""
    horizon = now - DEVICE_CODE_TTL_SEC
    return [at for at in times if at > horizon]
