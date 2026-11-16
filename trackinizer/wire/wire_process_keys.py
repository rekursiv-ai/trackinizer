"""What the key a start mints for an agent process may call: the one list, by route.

The server refuses such a key 403 on every route not listed here. Each entry names
its scope: ``account`` leaves ownership to the route; ``tree`` reaches the process's
own session and those its agents spawned; ``descendant`` excludes its own session,
even through a cycle, and requires the same account. Authentication checks the tree
before dispatch; a UUID inquiry read checks its row's kind and account too.

Paths are written out, as ``audience.SESSION_CONTENT_ROUTES`` writes them, so this
module imports no session wire: ``server.auth`` reads it, and every service that
imports ``auth`` would otherwise import the session wire's record types, and
sagent's with them.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal


if TYPE_CHECKING:
    from collections.abc import Mapping


__all__ = ["PROCESS_KEY_ROUTES", "WEBSOCKET"]


WEBSOCKET: Final = "WEBSOCKET"
"""How :data:`PROCESS_KEY_ROUTES` names a socket's route, which has no HTTP method."""

PROCESS_KEY_ROUTES: Final[
    Mapping[tuple[str, str], Literal["account", "tree", "descendant"]]
] = MappingProxyType(
    {
        # Its own session: the parts and records it restores its journal from, the
        # records it saves, and its socket.
        ("GET", "/api/sessions/{session_id}/parts"): "tree",
        ("GET", "/api/sessions/{session_id}/records"): "tree",
        ("POST", "/api/sessions/{session_id}/records"): "tree",
        (WEBSOCKET, "/api/sessions/{session_id}/process/ws"): "tree",
        # The session of each agent its agents spawn, which the start route opens
        # only ``spawned_by`` a session the key acts for.
        ("POST", "/api/sessions/start"): "account",
        # Its account's KnowHosts, with role and billing checks; never deletion.
        ("GET", "/api/addons/knowhost/hosts"): "account",
        ("POST", "/api/addons/knowhost/hosts"): "account",
        ("POST", "/api/addons/knowhost/hosts/{host_id}/pause"): "account",
        ("POST", "/api/addons/knowhost/hosts/{host_id}/resume"): "account",
        # Processes below its own session, never its own process (``auth``);
        # a start names no host, login or folder.
        ("GET", "/api/sessions/{session_id}/process"): "descendant",
        ("PUT", "/api/sessions/{session_id}/process"): "descendant",
        ("DELETE", "/api/sessions/{session_id}/process"): "descendant",
        ("GET", "/api/sessions/{session_id}/lines"): "descendant",
        ("GET", "/api/inquiries/{target_id}"): "descendant",
        # Its account's limits; it changes only the one on each campaign.
        ("GET", "/api/addons/knowspend/limits"): "account",
        ("PUT", "/api/addons/knowspend/limits"): "account",
        # Its account's hosts, which its agents' tools run on.
        ("GET", "/api/terminals/hosts"): "account",
        ("GET", "/api/terminals/hosts/changes"): "account",
        (WEBSOCKET, "/api/hosts/{name}/tools/ws"): "account",
        # Its agents' messages to another session's agents, sent as its own session's.
        ("POST", "/api/messages"): "account",
        # Its agents' canvas tools (``know/op/client/tools/canvas.py``): the canvas
        # whose Chat shows its session, and no other (``canvas_store``), and a row
        # named ``Kind#seq``, as the account reads it.
        ("GET", "/api/canvases/{canvas_id}"): "account",
        ("POST", "/api/canvases/{canvas_id}/operations"): "account",
        ("POST", "/api/canvases/{canvas_id}/captures"): "account",
        ("GET", "/api/inquiries/{kind}/{seq}"): "account",
        # Its agents' Canvas tool's ``chats`` and ``rename``: its account's workspaces
        # and chats, by name, and their names alone (``canvas_routes``,
        # ``workspace_routes``).
        ("GET", "/api/workspaces"): "account",
        ("PATCH", "/api/workspaces/{workspace_id}"): "account",
        ("GET", "/api/canvases"): "account",
        ("PUT", "/api/canvases/{canvas_id}/name"): "account",
        # Its agents' Canvas tool's ``set``: the settings it offers. It changes them
        # through the canvas's ``preferences`` operation, and only those
        # (``canvas_routes.refuse_process_settings``).
        ("GET", "/api/me/preferences/agent"): "account",
    },
)
"""Every route an agent process calls with the key a start minted for it, by method
and path, with its scope. A route nobody lists is refused to that key until someone
does: an agent gets an ability by a tool, and the tool's routes are listed with it."""
