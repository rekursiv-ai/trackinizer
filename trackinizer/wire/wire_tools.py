"""Wire contract for tool pipes: an assistant's tool calls on a connected computer.

The configured assistant reaches the tools of a computer its asker connected over a
pipe the server pairs from two legs:

- The agent opens a websocket on :data:`TOOLS_WS_PATH`.
- The machine's host holds a ``GET`` on :data:`MACHINE_TOOLS_WAIT_PATH`; when an agent
  arrives it returns a :class:`ToolOffer`, and the host dials back on
  :data:`MACHINE_TOOLS_WS_PATH` under the offered pipe id, within the dial-back the
  server's configuration sets.
- The server reads each line, logs each call, then relays the line between the legs.

A leg is refused after its handshake, and a pipe ends, with a close code of
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
    "MACHINE_TOOLS_WAIT_PATH",
    "MACHINE_TOOLS_WS_PATH",
    "TOOLS_WS_PATH",
    "WAIT_MAX_SEC",
    "ToolOffer",
]

TOOLS_WS_PATH: Final = "/api/machines/{name}/tools/ws"
"""``?asker=EMAIL``: the agent's leg of a tool pipe."""

MACHINE_TOOLS_WAIT_PATH: Final = "/api/machines/{machine_id}/tools/wait"
"""``GET ?instance=ID``, held up to :data:`WAIT_MAX_SEC`: the host's next
:class:`ToolOffer`, or ``null`` when none came."""

MACHINE_TOOLS_WS_PATH: Final = "/api/machines/{machine_id}/tools/{pipe_id}/ws"
"""``?instance=ID``: the host's leg of a tool pipe, dialed back for an offer."""

WAIT_MAX_SEC: Final = 25
"""The longest the server holds a host's wait."""

FRAME_BYTES: Final = 65_536
"""The largest websocket frame a leg sends."""


class ToolOffer(BaseModel):
    """A waiting agent, as the host receives it."""

    # A field a newer server adds must not stop an older host, as the pipe's own wire
    # reads past one too.
    model_config = ConfigDict(extra="ignore", frozen=True)

    pipe_id: uuid.UUID
    """The id the host dials back under."""
