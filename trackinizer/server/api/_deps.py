"""Shared dependencies for API route modules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

import dataclasses
import datetime as dt
import functools
import logging
import uuid

from trackinizer.server.api.replays import Replays
from trackinizer.server.api.tool_access import tool_access
from trackinizer.server.capture_relay import CaptureRelay
from trackinizer.server.config import Config
from trackinizer.server.host_changes import HostChanges
from trackinizer.server.terminals import Terminal, TerminalRelay
from trackinizer.server.tool_relay import ToolConnection, ToolRelay
from trackinizer.wire.json_types import MutableJSON, MutableJSONValue


if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from starlette.requests import HTTPConnection

    from trackinizer.server.config import Assistant, ChatOrgs
    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.secrets import SecretBackend
    from trackinizer.server.store.core import Store
    from trackinizer.types.inquiries import Inquiry


_logger = logging.getLogger(__name__)


class _State(Protocol):
    store: Store
    inbound: InboundQueue
    secrets: SecretBackend | None


def get_store(request: HTTPConnection) -> Store:
    """Return the Store held on the FastAPI app state.

    Args:
      request: Request.

    Returns:
      result: The Store.

    """
    app = cast(_App, request.app)
    return app.state.store


def get_inbound(request: Request) -> InboundQueue:
    """Return the inbound-message queue held on the FastAPI app state.

    Args:
      request: Request.

    Returns:
      result: The InboundQueue.

    """
    app = cast(_App, request.app)
    return app.state.inbound


def nudge_streams(request: Request) -> None:
    """Wake the event streams the app holds, if any, to read what changed.

    Args:
      request: The request that changed a session, a run or a host.

    """
    streams: object = getattr(cast("FastAPI", request.app).state, "hub", None)
    if isinstance(streams, _Streams):
        streams.nudge()


def get_replays(request: Request) -> Replays:
    """Return the app's consuming-request receipts, made on first use.

    Args:
      request: The authorized consuming request.

    Returns:
      replays: Receipts kept by this application process.

    """
    app = cast("FastAPI", request.app)
    replays: object = getattr(app.state, "replays", None)
    if not isinstance(replays, Replays):
        replays = app.state.replays = Replays()
    return replays


def terminal_relay(app: FastAPI) -> TerminalRelay:
    """Return the app's terminal relay, made on first use.

    Args:
      app: The application, whose state holds the store.

    Returns:
      result: The relay.

    """
    held: object = getattr(app.state, "terminals", None)
    if isinstance(held, TerminalRelay):
        return held
    relay = TerminalRelay(
        alive=functools.partial(_alive, store=cast("_State", app.state).store),
        ice_servers=_config(app).terminal_stun,
    )
    app.state.terminals = relay
    return relay


def get_terminal_relay(request: HTTPConnection) -> TerminalRelay:
    """Return the terminal relay held on the app state.

    Args:
      request: Request.

    Returns:
      result: The relay.

    """
    return terminal_relay(cast("FastAPI", request.app))


def tool_relay(app: FastAPI) -> ToolRelay:
    """Return the app's tool connection relay, made on first use.

    Args:
      app: The application, whose state holds the store and the configuration.

    Returns:
      result: The relay.

    """
    held: object = getattr(app.state, "tool_relay", None)
    if isinstance(held, ToolRelay):
        return held
    config = _config(app)
    relay = ToolRelay(
        alive=functools.partial(_connection_alive, app=app),
        connections_per_host=config.tool_connections_per_host,
        dial_back_sec=config.tool_dial_back_sec,
        line_bytes=config.tool_line_bytes,
    )
    app.state.tool_relay = relay
    return relay


def get_tool_relay(request: HTTPConnection) -> ToolRelay:
    """Return the tool connection relay held on the app state.

    Args:
      request: Request.

    Returns:
      result: The relay.

    """
    return tool_relay(cast("FastAPI", request.app))


def host_idle_sec(app: FastAPI, host_id: uuid.UUID) -> float:
    """Return how long the host has had no tool call or result, nor terminal use.

    The tool connection relay sees each call and result; the terminal relay each terminal's input
    and output, relayed or reported by a direct link's keepalive. Both keep it in memory
    alone, so after a restart a host is counted from the restart.

    Args:
      app: The application, whose state holds both relays.
      host_id: The host.

    Returns:
      idle_sec: Seconds since the later of the two relays' last sight of use.

    """
    return min(
        tool_relay(app).idle_sec(host_id),
        terminal_relay(app).idle_sec(host_id),
    )


async def host_closed(app: FastAPI, host_id: uuid.UUID) -> None:
    """End the tool connections and terminals of a host taken out of service.

    Args:
      app: The application, whose state holds both relays.
      host_id: The host.

    """
    tool_relay(app).close_host(host_id)
    await terminal_relay(app).close_host(host_id)


def host_changes(app: FastAPI) -> HostChanges:
    """Return the app's host-list versions, made on first use.

    Args:
      app: The application.

    Returns:
      changes: The version of each owner's host list in this process.

    """
    held: object = getattr(app.state, "host_changes", None)
    if not isinstance(held, HostChanges):
        held = app.state.host_changes = HostChanges()
    return held


def get_host_changes(request: HTTPConnection) -> HostChanges:
    """Return the host-list versions held on the app state.

    Args:
      request: Request.

    Returns:
      changes: The version of each owner's host list in this process.

    """
    return host_changes(cast("FastAPI", request.app))


# Called once the write has committed: a follower woken before it would list the
# hosts as they were, and wait again with the change unseen.
def hosts_changed(request: HTTPConnection, *, owner: str | None) -> None:
    """Bump ``owner``'s host-list version after a write; a host none owns has none.

    Args:
      request: The request whose committed write changed a host.
      owner: The host's owner, whose listing it is in.

    """
    if owner is not None:
        get_host_changes(request).bump(owner)


def get_relay(request: Request) -> CaptureRelay:
    """Return the canvas-capture relay held on the app state, made on first use.

    Args:
      request: Request.

    Returns:
      result: The CaptureRelay.

    """
    app = cast("FastAPI", request.app)
    relay: object = getattr(app.state, "relay", None)
    if not isinstance(relay, CaptureRelay):
        relay = app.state.relay = CaptureRelay()
    return relay


def get_assistant(request: HTTPConnection) -> Assistant | None:
    """Return the configured assistant; none without a Config.

    Args:
      request: Request.

    Returns:
      result: The configured assistant, if any.

    """
    return _assistant(cast("FastAPI", request.app))


def auth_is_disabled(request: HTTPConnection) -> bool:
    """Return whether the server runs with authentication off (``--no-auth``).

    Args:
      request: The request or WebSocket.

    Returns:
      disabled: True when every caller resolves to a synthetic admin.

    """
    return _config(cast("FastAPI", request.app)).auth_disabled


def get_tool_line_bytes(request: HTTPConnection) -> int:
    """Return the longest line the server reads from an agent's tool connection.

    Args:
      request: Request.

    Returns:
      result: The Config's cap, or its default without a Config.

    """
    return _config(cast("FastAPI", request.app)).tool_line_bytes


def get_staff(request: HTTPConnection) -> tuple[str, ...]:
    """Return the configured staff patterns; none without a Config.

    Args:
      request: Request.

    Returns:
      result: The staff patterns, empty when nobody is staff.

    """
    return _config(cast("FastAPI", request.app)).staff


def get_chat_orgs(request: Request) -> ChatOrgs:
    """Return how science chats group their users; ``individual`` without a Config.

    Args:
      request: Request.

    Returns:
      result: ``single``, ``domain`` or ``individual``.

    """
    return _config(cast("FastAPI", request.app)).chat_orgs


def get_secrets(request: Request) -> SecretBackend | None:
    """Return the secret backend held on the app state; ``None`` when disabled.

    Args:
      request: Request.

    Returns:
      result: The backend, or ``None`` when secret storage is turned off.

    """
    app = cast(_App, request.app)
    return app.state.secrets


def tag_row(inquiry: Inquiry, names: frozenset[str] | None = None) -> MutableJSON:
    """Serialize one Inquiry that is known to exist, whole or only the ``names`` keys.

    The total variant. Callers holding a ``list[Inquiry]`` used to route
    through :func:`tag_kind` and then filter its ``None`` back out -- a guard
    for a case the type already excluded, kept alive only because the
    signature was wider than any caller needed.

    A key not in ``names`` is never converted, so a list naming a few keys
    skips the walk of the large ones (an Experiment's ``config`` and
    ``outcome``). The keys keep the whole row's order.

    Args:
      inquiry: Inquiry to serialize; raises TypeError if None.
      names: The keys to keep, ``kind`` among them; None keeps every key.

    Returns:
      payload: JSON with "kind" field set to the Inquiry subclass name.

    """
    payload: MutableJSON = {
        # Dataclass reflection returns Any; this cast establishes the recursive boundary.
        field.name: cast(
            MutableJSONValue,
            _jsonable(cast(object, getattr(inquiry, field.name))),
        )
        for field in dataclasses.fields(inquiry)
        if names is None or field.name in names
    }
    if names is None or "kind" in names:
        payload["kind"] = type(inquiry).__name__
    return payload


def tag_kind(inquiry: Inquiry | None) -> MutableJSON | None:
    """Serialize one Inquiry, adding an explicit ``kind`` discriminator field.

    Walks the dataclass tree directly instead of calling ``jsonable_encoder``,
    which measured 5.3ms of a 9ms 50-row listing -- more than the SQL, the
    edge fetch, and model construction combined. Every Inquiry is a frozen
    dataclass of JSON-shaped fields, so the encoder's general type dispatch is
    cost without benefit; :func:`_jsonable` does the same job in 2.6ms.

    ``dataclasses.asdict`` is NOT the shortcut it appears to be: it cannot
    convert the leaves, so the scalars come back as ``UUID`` and ``datetime``
    and need a ``dict_factory`` -- which restores the per-field Python call
    that made the encoder slow (measured 3.9ms, only 1.3x). One pass that both
    flattens and converts is the whole win.

    ``_deps_test`` pins this output against ``jsonable_encoder`` for every
    kind, building each fixture from ``dataclasses.fields``. A newly added
    field of an already-sampled type is compared with no edit there; one of a
    NEW type fails the fixture loudly, naming the type, rather than being
    skipped. An earlier version passed only the base kwargs, comparing nine
    all-``None`` payloads and proving nothing about the fields it named.

    Args:
      inquiry: Inquiry to serialize; None returns None.

    Returns:
      result: The MutableJSON | None.

    """
    if inquiry is None:
        return None
    return tag_row(inquiry)


def _jsonable(value: object) -> object:
    """Convert one value -- and everything under it -- to JSON-shaped data."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result: dict[str, object] = {}
        for field in dataclasses.fields(value):
            # Dataclass reflection returns Any; this cast establishes the recursive boundary.
            field_value = cast(object, getattr(value, field.name))
            result[field.name] = _jsonable(field_value)
        return result
    if isinstance(value, (tuple, list)):
        # Tuples become lists: the projection fields (edges, ``labels``,
        # ``issue_kind``) are tuples on the model, and a caller comparing
        # structures in-process would see the type differ even though both
        # dump to the same JSON array.
        return [_jsonable(item) for item in cast(tuple[object, ...], value)]
    if isinstance(value, dict):
        return {
            key: _jsonable(item) for key, item in cast(dict[str, object], value).items()
        }
    if isinstance(value, dt.datetime | dt.date):
        # ISO-8601, never ``str(datetime)``: that yields a space separator
        # which Python re-parses happily and other languages reject.
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str | int | float | bool | None):
        return value
    # Everything reachable is handled above, so an unrecognized type means a
    # new field type arrived without a branch here. Raising names it at the
    # seam; returning it unchanged would put a non-JSON object in the payload
    # and blow up inside ``json.dumps`` as FastAPI writes the response -- a
    # 500 whose traceback points at the encoder instead of the field.
    #
    # Only ``Experiment.config`` can carry a caller-chosen type: it is the
    # one JSONB column, and jsonb round-trips through ``json.loads``, so a
    # read yields nothing exotic. The guard is for the next field, not this
    # one.
    raise TypeError(f"cannot serialize {type(value).__name__} to JSON: {value!r}")


class _App(Protocol):
    state: _State


def _assistant(app: FastAPI) -> Assistant | None:
    """Return the app's configured assistant; none without a Config."""
    return _config(app).assistant


# An app the lifespan has not started, as in a route test, has no Config of its own.
def _config(app: FastAPI) -> Config:
    """Return the app's Config, or the default one when it holds none."""
    config: object = getattr(app.state, "config", None)
    return config if isinstance(config, Config) else Config()


async def _alive(term: Terminal, *, store: Store) -> bool:
    """Whether the host's credential is live and its owner an active writer or admin."""
    async with store.engine.acquire() as conn:
        return bool(
            await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM hosts m "
                "JOIN host_credentials c ON c.host_id = m.id "
                "JOIN users u ON u.email = m.owner "
                "WHERE m.id = $1 AND c.revoked_at IS NULL "
                "AND u.status = 'active' AND u.role IN ('writer', 'admin'))",
                term.host_id,
            ),
        )


# The rule ``tool_routes`` opens a tool connection under, asked again of a tool connection that may outlive it.
# A tool connection no host has dialed back for has no instance to compare yet, and its timer is short.
# One that does not is logged, naming the check it failed, before the sweep ends it.
async def _connection_alive(connection: ToolConnection, *, app: FastAPI) -> bool:
    """Whether the assistant's key, its asker and the attached host still qualify."""
    async with cast("_State", app.state).store.engine.acquire() as conn:
        access = await tool_access(
            conn,
            assistant=_assistant(app),
            api_key_id=connection.api_key_id,
            asker=connection.asker,
            host=connection.host_id,
            instance=connection.instance,
        )
    if access.failed is not None:
        _logger.warning(
            "tool connection no longer allowed: connection=%s host_id=%s check=%s",
            connection.id,
            connection.host_id,
            access.failed,
        )
    return access.failed is None


@runtime_checkable
class _Streams(Protocol):
    """Event streams that read what they show again when nudged."""

    def nudge(self) -> None:
        """Have every open stream read its state again."""
        ...
