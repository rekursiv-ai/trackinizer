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
    from collections.abc import Collection, Mapping, Sequence


__all__ = [
    "PROCESS_KEY_ROUTES",
    "RECORDS_ROUTES",
    "RECORD_WRITE_ROUTES",
    "WEBSOCKET",
    "listed_route",
]


WEBSOCKET: Final = "WEBSOCKET"
"""How :data:`PROCESS_KEY_ROUTES` names a socket's route, which has no HTTP method."""

type Scope = Literal["account", "tree", "descendant"]
"""How far a listed route reaches; see the module docstring."""

RECORDS_ROUTES: Final[Mapping[tuple[str, str], Scope]] = MappingProxyType(
    {
        # Its agents' record reads, which run trax's read commands: every route those
        # commands call, each a GET, and no write. A read trax sends as a POST (metrics
        # query and rank) is not listed. Each applies the caller's read filter. Routes
        # listed for other tools keep their scope here: a session's parts and records
        # stay its own tree's.
        ("GET", "/api/inquiries"): "account",
        ("GET", "/api/inquiries/{kind}/{seq}"): "account",
        ("GET", "/api/inquiries/next_issue"): "account",
        ("GET", "/api/inquiries/{target_id}/cost"): "account",
        ("GET", "/api/inquiries/{target_id}/confidence"): "account",
        ("GET", "/api/inquiries/{target_id}/authority"): "account",
        ("GET", "/api/web/lookup/{target_id}"): "account",
        ("GET", "/api/web/get/{target_id}"): "account",
        ("GET", "/api/web/recent_changes"): "account",
        ("GET", "/api/web/search_sessions"): "account",
        ("GET", "/api/web/search_inquiries"): "account",
        ("GET", "/api/version"): "account",
        ("GET", "/api/sessions/{session_id}/parts"): "tree",
        ("GET", "/api/sessions/{session_id}/records"): "tree",
    },
)
"""The routes an agent's record reads call, by method and path, with their scopes."""

RECORD_WRITE_ROUTES: Final[Mapping[tuple[str, str], Scope]] = MappingProxyType(
    {
        # Every route trax's write verbs call to file, edit, label, cost, link, unlink,
        # claim and delete a row; each route holds a writer to its own access, and a
        # delete to an unclaimed row of its account's. Left off, so a leaked key cannot
        # reach them: a row's ``account`` and ``cli_session_id``, the org's variables,
        # the export, metric writes and every admin route.
        ("POST", "/api/inquiries/batch"): "account",
        ("POST", "/api/inquiries/lookup"): "account",
        ("POST", "/api/inquiries/next_issue"): "account",
        ("DELETE", "/api/inquiries/{target_id}"): "account",
        ("PUT", "/api/inquiries/{target_id}/title"): "account",
        ("PUT", "/api/inquiries/{target_id}/status"): "account",
        ("PUT", "/api/inquiries/{target_id}/owner"): "account",
        ("PUT", "/api/inquiries/{target_id}/description"): "account",
        ("PUT", "/api/inquiries/{target_id}/app"): "account",
        ("PUT", "/api/issue/{target_id}/priority"): "account",
        ("PUT", "/api/issue/{target_id}/validation"): "account",
        ("PUT", "/api/belief/{target_id}/judgement"): "account",
        ("PATCH", "/api/inquiries/{target_id}/labels"): "account",
        ("PATCH", "/api/inquiries/{target_id}/marginal_cost_agent_usd"): "account",
        ("POST", "/api/edges/{from_id}/{edge_kind}/{to_id}"): "account",
        ("DELETE", "/api/edges/{from_id}/{edge_kind}/{to_id}"): "account",
        # The one annotation trax sends after an edge's upsert: a clear of its labels.
        ("PUT", "/api/edges/{from_id}/{edge_kind}/{to_id}/labels"): "account",
    },
)
"""The routes an agent's record writes call, by method and path, with their scopes."""


def _route_union(
    *tables: Mapping[tuple[str, str], Scope],
) -> Mapping[tuple[str, str], Scope]:
    """Return every table's routes as one; a route two tables scope apart raises."""
    union: dict[tuple[str, str], Scope] = {}
    for table in tables:
        for route, scope in table.items():
            if union.setdefault(route, scope) != scope:
                method, template = route
                raise ValueError(
                    f"{method} {template} is listed as {union[route]} and as {scope}",
                )
    return MappingProxyType(union)


PROCESS_KEY_ROUTES: Final[Mapping[tuple[str, str], Scope]] = _route_union(
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
        # Its account's spend on one campaign, and its account's GPU jobs, to say
        # whether that spend is final; the routes read the caller's own alone.
        ("GET", "/api/addons/knowspend/campaigns"): "account",
        ("GET", "/api/addons/jobs/jobs"): "account",
        # Its account's spend today and what of its allowance and credit is left, which
        # it tells the person before it starts work that spends them.
        ("GET", "/api/addons/knowspend/today"): "account",
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
    RECORDS_ROUTES,
    RECORD_WRITE_ROUTES,
)
"""Every route an agent process calls with the key a start minted for it, by method
and path, with its scope. A route nobody lists is refused to that key until someone
does: an agent gets an ability by a tool, and the tool's routes are listed with it.
:data:`RECORDS_ROUTES` and :data:`RECORD_WRITE_ROUTES` join their own entries; a route
two lists name keeps one scope, so joining a tool's list never widens what the key
reaches."""


def listed_route(
    method: str,
    path: str,
    routes: Collection[tuple[str, str]],
) -> tuple[str, str] | None:
    """Return the listed route a concrete request matches, or ``None``.

    A template matches segment by segment, each ``{param}`` one non-empty segment. Of
    several matches the most specific wins, the one with the most literal segments and
    so the fewest parameters, so ``/api/inquiries/next_issue`` is its own route, not
    ``/api/inquiries/{x}``.

    Args:
      method: The request's method, or :data:`WEBSOCKET`.
      path: The request's path from the server's root, without its query.
      routes: The ``(method, template)`` pairs to match against.

    Returns:
      route: The matching ``(method, template)``, or ``None`` when none matches.

    """
    segments = path.split("/")
    return max(
        (
            (listed, template)
            for listed, template in routes
            if listed == method and _template_matches(template, segments)
        ),
        # Every match has as many segments as the path, so fewest parameters is most
        # literal segments.
        key=lambda route: -route[1].count("{"),
        default=None,
    )


def _template_matches(template: str, segments: Sequence[str]) -> bool:
    """Return whether ``template`` names the path split into ``segments``."""
    parts = template.split("/")
    return len(parts) == len(segments) and all(map(_part_matches, parts, segments))


def _part_matches(part: str, segment: str) -> bool:
    """Return whether one template segment names one path segment."""
    return part == segment or (part.startswith("{") and segment != "")
