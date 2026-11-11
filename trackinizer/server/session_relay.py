"""The live session relay: an agent process's lines out to its viewers, their inputs in.

The relay's contract lands ahead of the relay itself, so its routes and the canvas
console are written against one signature: this module declares what they call, and
each call raises until the relay is built. ``wire_session_relay`` names the frames.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.wire.wire_session_relay import NewInput


__all__ = ["NotLiveError", "SessionRelay"]


class NotLiveError(Exception):
    """An input was sent to a session that no agent process serves."""


class SessionRelay:
    """Passes a live session's lines to its viewers and their inputs to its process."""

    def send_input(self, session_id: UUID, new: NewInput, *, author: str) -> UUID:
        """Send ``new`` to the agent process serving ``session_id``.

        Args:
          session_id: The session whose root agent the input is for.
          new: A line, a stop or a slash line.
          author: The sender's email, which the caller attested.

        Returns:
          input_id: The id the process tells the input's effect back under.

        Raises:
          NotLiveError: No agent process serves the session.

        """
        del session_id, new, author
        raise NotImplementedError("The session relay is not built yet.")
