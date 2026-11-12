"""Wire contract for tool connections: an assistant's tool calls on a connected computer.

The configured assistant reaches the tools of a computer its asker connected over a
tool connection the server pairs from two sockets:

- The agent opens a websocket on :data:`TOOLS_WS_PATH`.
- The daemon holds a ``GET`` on :data:`HOST_TOOLS_WAIT_PATH`; when an agent
  arrives it returns a :class:`ToolOffer`, and the daemon dials back on
  :data:`HOST_TOOLS_WS_PATH` under the offered tool connection id, within the dial-back the
  server's configuration sets.
- The server reads each line, logs each call, then relays the line between the sockets.

A socket is refused after its handshake, and a tool connection ends, with a close code of
``wire_terminals`` and a reason saying what happened: nothing acts on which refusal it
was, so they share one code. Frames are binary.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Final

import uuid

from pydantic import BaseModel, ConfigDict


__all__ = [
    "FRAME_BYTES",
    "HOST_TOOLS_WAIT_PATH",
    "HOST_TOOLS_WS_PATH",
    "TOOLS_WS_PATH",
    "WAIT_MAX_SEC",
    "ToolOffer",
]

TOOLS_WS_PATH: Final = "/api/hosts/{name}/tools/ws"
"""``?asker=EMAIL``: the agent's socket of a tool connection."""

HOST_TOOLS_WAIT_PATH: Final = "/api/hosts/{host_id}/tools/wait"
"""``GET ?instance=ID``, held up to :data:`WAIT_MAX_SEC`: the daemon's next
:class:`ToolOffer`, or ``null`` when none came."""

HOST_TOOLS_WS_PATH: Final = "/api/hosts/{host_id}/tools/{connection_id}/ws"
"""``?instance=ID``: the daemon's socket of a tool connection, dialed back for an offer."""

WAIT_MAX_SEC: Final = 25
"""The longest the server holds a daemon's wait."""

FRAME_BYTES: Final = 65_536
"""The largest websocket frame a socket sends."""


class ToolOffer(BaseModel):
    """A waiting agent, as the daemon receives it."""

    # A field a newer server adds must not stop an older daemon, as the tool connection's own wire
    # reads past one too.
    model_config = ConfigDict(extra="ignore", frozen=True)

    connection_id: uuid.UUID
    """The id the daemon dials back under."""
