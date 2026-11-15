"""What the key a start mints for an agent process may call: the one list, by route.

The server refuses such a key 403 on every route not listed here, and on a listed
route that names a session, for every session but the one its process was started
for and those its agents spawned (``server.auth.process_reaches``). A test walks
every route the server serves with one, and the agent process's own test records
each request its client sends against this list.

Paths are written out, as ``audience.SESSION_CONTENT_ROUTES`` writes them, so this
module imports nothing: ``server.auth`` reads it, and every service that imports
``auth`` would otherwise import the session wire's record types, and sagent's with
them.
"""

from __future__ import annotations

from typing import Final


__all__ = ["PROCESS_KEY_ROUTES", "WEBSOCKET"]


WEBSOCKET: Final = "WEBSOCKET"
"""How :data:`PROCESS_KEY_ROUTES` names a socket's route, which has no HTTP method."""

PROCESS_KEY_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        # Its own session: the parts and records it restores its journal from, the
        # records it saves, and its socket.
        ("GET", "/api/sessions/{session_id}/parts"),
        ("GET", "/api/sessions/{session_id}/records"),
        ("POST", "/api/sessions/{session_id}/records"),
        (WEBSOCKET, "/api/sessions/{session_id}/process/ws"),
        # The session of each agent its agents spawn, which the start route opens
        # only ``spawned_by`` a session the key acts for.
        ("POST", "/api/sessions/start"),
        # Its account's hosts, which its agents' tools run on.
        ("GET", "/api/terminals/hosts"),
        ("GET", "/api/terminals/hosts/changes"),
        (WEBSOCKET, "/api/hosts/{name}/tools/ws"),
        # Its agents' messages to another session's agents, sent as its own session's.
        ("POST", "/api/messages"),
        # Its agents' canvas tools (``know/op/client/tools/canvas.py``): the canvas
        # whose Chat shows its session, and no other (``canvas_store``), and a row
        # named ``Kind#seq``, as the account reads it.
        ("GET", "/api/canvases/{canvas_id}"),
        ("POST", "/api/canvases/{canvas_id}/operations"),
        ("POST", "/api/canvases/{canvas_id}/captures"),
        ("GET", "/api/inquiries/{kind}/{seq}"),
        # Its agents' Canvas tool's ``set``: the settings it offers. It changes them
        # through the canvas's ``preferences`` operation, and only those
        # (``canvas_routes.refuse_process_settings``).
        ("GET", "/api/me/preferences/agent"),
    },
)
"""Every route an agent process calls with the key a start minted for it, by method
and path: what its client calls as it restores and saves its session and follows its
account's hosts, its socket, its agents' tool connections and messages, and what its
agents' own tools call. A route nobody lists is refused to that key until someone
does: an agent gets an ability by a tool, and the tool's routes are listed with it."""
