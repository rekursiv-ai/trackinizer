"""Shared Trackinizer clients for long-lived trax processes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import threading

from trackinizer.client.client import Client


@dataclass(frozen=True, kw_only=True, slots=True)
class Target:
    """The resolved connection identity a ``Client`` is keyed on."""

    url: str

    author: str

    api_key: str


_CLIENTS: Final[dict[Target, Client]] = {}
"""Live clients by connection identity, shared across invocations."""

_CLIENTS_LOCK: Final = threading.Lock()
"""Serializes cache access across daemon request threads."""


def close_clients() -> None:
    """Close every shared client and forget it."""
    with _CLIENTS_LOCK:
        for client in _CLIENTS.values():
            client.close()
        _CLIENTS.clear()


def shared_client(target: Target) -> Client:
    """Return the client for ``target``, building it once per process.

    Args:
      target: Connection identity (url, author, api key) the client is keyed on.

    Returns:
      client: The live shared ``Client`` for ``target``.

    """
    with _CLIENTS_LOCK:
        if (client := _CLIENTS.get(target)) is not None:
            return client
        client = Client(target.url, author=target.author, api_key=target.api_key)
        _CLIENTS[target] = client
        return client
