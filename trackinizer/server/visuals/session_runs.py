"""Who opened a session, and what the run that drains it is doing.

A run says its own state between its drains (``POST .../run-state``); a run that has
an ask open waits on its person whatever it said, unless it was taken over in a
terminal, where nothing waits on the person here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from trackinizer.server.asks import waiting_sessions


if TYPE_CHECKING:
    from collections.abc import Sequence

    import uuid

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.inbound import InboundQueue, PollerRun
    from trackinizer.wire.wire_sessions import RunState


__all__ = ["live_run_states", "opener_email", "settled"]


async def live_run_states(
    conn: Conn,
    *,
    inbound: InboundQueue,
    session_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, RunState]:
    """Say what each live run among some sessions is doing.

    Args:
      conn: A database connection.
      inbound: In-process poller leases, which hold what a run reported.
      session_ids: The sessions to ask about.

    Returns:
      states: By session, for those a run polls now; a session no run polls is left out.

    """
    runs = {
        session_id: run
        for session_id in session_ids
        if (run := inbound.poller_run(session_id)) is not None
    }
    waiting = await waiting_sessions(conn, session_ids=list(runs))
    return {
        session_id: settled(run, waiting=session_id in waiting)
        for session_id, run in runs.items()
    }


def settled(run: PollerRun, *, waiting: bool) -> RunState:
    """Say what a live run is doing: its own word, or waiting on an ask open to it.

    Args:
      run: The run's lease, which holds the state it last reported.
      waiting: Whether an ask is open in its session.

    Returns:
      state: ``taken_over`` as it said; else ``requires_action`` while an ask waits,
        or the state it reported.

    """
    if run.state == "taken_over":
        return run.state
    return "requires_action" if waiting else run.state


async def opener_email(conn: Conn, *, api_key_id: uuid.UUID | None) -> str | None:
    """Return the email of the account that owns an API key.

    Args:
      conn: Database connection.
      api_key_id: The key that opened a session, if known.

    Returns:
      email: The owning account's email, or None for no or an unknown key.

    """
    if api_key_id is None:
        return None
    return cast(
        str | None,
        await conn.fetchval(
            "SELECT member.email FROM api_keys AS credential "
            "JOIN users AS member ON member.id = credential.user_id "
            "WHERE credential.id = $1",
            api_key_id,
        ),
    )
