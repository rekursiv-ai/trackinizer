"""How a server-delivered message reads to the agent that receives it.

Shared by ``trax run``, which types it into the CLI's terminal, and ``trax
helper``, which hands it to one CLI turn. Kept apart from the PTY machinery so
the helper does not import it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import json

from trackinizer.lib.codec import from_plain, loads


if TYPE_CHECKING:
    from trackinizer.wire.wire_sessions import WorkspaceMessageContext


# A single PTY interleaves every room's messages into one input stream, so the agent
# needs the room and sender to know who is steering it. Renders ``[room] sender: text``
# (dropping whichever of room/sender is absent), so a direct session-id enqueue with no
# attested sender injects the bare text.
#
# Change envelopes are shaped per consumer HERE, at the client -- the server pushes one
# uniform JSON envelope to every session. A model-CLI session (``stream=False``)
# receives only the envelope's ``agent_message`` line: the remaining fields would spend
# the model's context on metadata it can fetch on demand (the line itself names the
# ``trax`` command). An IO-stream session (``stream=True``) receives the whole envelope
# to parse itself -- behind the same room/sender prefix as any other message, since a
# line-reading child needs to know who sent it just as much. Only the route-attested
# ``trackinizer`` sender unwraps -- ``source`` is stamped server-side from the
# principal, so another sender's JSON-looking text renders as a plain message.
def render_inbound(
    text: str,
    source: str | None,
    room: str | None,
    *,
    context: WorkspaceMessageContext | None = None,
    stream: bool = False,
) -> str:
    """Decorate an inbound message with its routing context for injection.

    Args:
      text: The message as sent.
      source: The route-attested sender, if any.
      room: The room it was sent to, if any.
      context: The canvas it was sent from, for a Chat message.
      stream: Whether the receiver is an IO-stream child rather than a model CLI.

    Returns:
      rendered: The text the agent receives.

    """
    if context is None and source == "trackinizer" and not stream:
        agent_message = _envelope_agent_message(text)
        if agent_message is not None:
            return agent_message
    prefix = ""
    if room:
        prefix += f"[{room}] "
    if source:
        prefix += f"{source}: "
    rendered = f"{prefix}{text}"
    if context is not None:
        rendered += (
            f"\nTrackinizer context (verify with trax): {context.model_dump_json()}"
        )
        workspace = f"trax workspace {context.workspace_id}"
        record = context.record_id or "RECORD_UUID"
        rendered += (
            "\nCanvas: run these commands; describing them does nothing."
            f"\n  Show a record: {workspace} navigate '#/lookup/{record}'"
            f"\n  Show its graph: {workspace} show trax.subgraph --record {record}"
            " --placement side"
            f"\n  Show its timeline: {workspace} show trax.timeline --record {record}"
            f"\n  Show an artifact: {workspace} show trax.artifact --record {record}"
            " --placement main"
            f"\n  Point at records: {workspace} highlight UUID[,UUID...]"
            " (highlight '' clears)"
            f"\n  Go to a view: {workspace} navigate '#/list/Issue' (or '#/graph',"
            " '#/activity', '#/console', '#/search/TEXT' with TEXT percent-encoded)"
            f"\n  Hide a visual: {workspace} hide INSTANCE_UUID (never the page)"
        )
        if context.artifact_content is not None:
            artifact = context.artifact_content.artifact_id
            rendered += (
                f"\nThe open artifact: trax artifact {artifact}; full content: "
                f"GET /api/artifacts/{artifact}/content"
            )
    return rendered


def _envelope_agent_message(text: str) -> str | None:
    """Return the ``agent_message`` line of a change envelope, or None if not one."""
    try:
        payload = loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    message = from_plain(payload, dict[str, object]).get("agent_message")
    return message if isinstance(message, str) else None
