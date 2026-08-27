"""Codex ``app-server`` JSON-RPC notification parser (optional fidelity).

The Phase-0 codex adapter tails the rollout JSONL on disk
(``codex.py``). ``codex app-server --stdio`` is an **optional fidelity
upgrade**: it streams the *same* reasoning summary and tool activity as
live JSON-RPC 2.0 notifications (finer granularity, identical content;
raw chain-of-thought stays encrypted and unrecoverable either way -- see
``docs/cli-scraping-investigation.md``).

This module is the parser half: it maps one ``ServerNotification`` line
(``{"method": "item/...", "params": {...}}``) to a typed :data:`Message`
on an :class:`Event`. The method names and param shapes are pinned to the
schema emitted by ``codex app-server generate-json-schema`` (verified
against codex-cli 0.136.0, ``ThreadItem`` variants in the v2 protocol);
re-verify on upgrade. Driving the ``app-server`` subprocess and pumping its
stdout is deferred -- the rollout tailer remains the default capture path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import json

from trackinizer.lib.custom_json import JSON, json_freeze
from trackinizer.trax.run.custom_types import Event
from trackinizer.types.agent_session_events import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResult,
    UnknownMessage,
    UserMessage,
)


# Streaming/lifecycle methods that carry a turn-grained fragment. The
# delta methods fold to the assistant turn (text or reasoning); command
# output folds to a tool result. ``thread/started`` and ``turn/completed``
# are session lifecycle -- they live on the AgentSession row, not as events.
_ASSISTANT_TEXT_METHODS = frozenset({"item/agentMessage/delta", "item/plan/delta"})
_ASSISTANT_THINKING_METHODS = frozenset(
    {
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/textDelta",
    }
)

# ThreadItem.type values that map to a tool call, for item/started and
# item/completed. Each carries its real name/args under type-specific keys
# (see ``_tool_name`` / ``_tool_args``), not the bare category.
_TOOL_ITEM_TYPES = frozenset({"commandExecution", "fileChange", "mcpToolCall"})


def parse_notification(raw: bytes) -> Event | None:
    """Map one app-server JSON-RPC notification line to an ``Event``.

    Returns ``None`` for malformed lines, JSON-RPC requests/responses (only
    notifications carry session content), session lifecycle, and methods we
    do not surface. The typed :data:`Message` preserves the turn content;
    unrecognized item types fall back to :class:`UnknownMessage`.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    obj = json_freeze(cast(Mapping[str, object], parsed))
    method = obj.get("method")
    if not isinstance(method, str):
        return None  # a request/response, not a notification
    message = _to_message(method, _params(obj))
    if message is None:
        return None
    return Event(message=message)


def _to_message(method: str, params: JSON) -> Message | None:
    """Normalize one notification to a typed message, or ``None`` to skip."""
    if method in _ASSISTANT_TEXT_METHODS:
        return AssistantMessage(text=_str(params.get("delta")))
    if method in _ASSISTANT_THINKING_METHODS:
        return AssistantMessage(thinking=_str(params.get("delta")))
    if method == "item/commandExecution/outputDelta":
        # Schema: the delta text is ``delta`` and ``itemId`` names the
        # command item this output belongs to, so the result can pair to it.
        return ToolResult(
            call_id=_str(params.get("itemId")),
            content=_str(params.get("delta")),
        )
    if method in ("item/started", "item/completed"):
        return _item_message(params)
    return None


def _item_message(params: JSON) -> Message:
    """Classify an ``item/started`` | ``item/completed`` by its item type."""
    item = params.get("item")
    if not isinstance(item, Mapping):
        return UnknownMessage(raw=params)
    item_obj = cast(JSON, item)
    item_type = _str(item_obj.get("type"))
    if item_type == "userMessage":
        return UserMessage(text=_str(item_obj.get("text")))
    if item_type == "agentMessage":
        return AssistantMessage(text=_str(item_obj.get("text")))
    if item_type == "reasoning":
        return AssistantMessage(thinking=_str(item_obj.get("text")))
    if item_type in _TOOL_ITEM_TYPES:
        return AssistantMessage(
            tool_calls=(
                ToolCall(
                    id=_str(item_obj.get("id")),
                    name=_tool_name(item_type, item_obj),
                    args=_tool_args(item_type, item_obj),
                ),
            )
        )
    return UnknownMessage(raw=item_obj)


def _tool_name(item_type: str, item: JSON) -> str:
    """The real tool name for a tool ThreadItem, by its schema shape.

    ``commandExecution`` names the shell ``command``; ``mcpToolCall`` names
    the invoked ``tool``; ``fileChange`` has no tool name, so it falls back to
    its category. Never the bare item category for the first two.
    """
    if item_type == "commandExecution":
        return _str(item.get("command"))
    if item_type == "mcpToolCall":
        return _str(item.get("tool"))
    return item_type


def _tool_args(item_type: str, item: JSON) -> dict[str, object]:
    """The parsed call arguments for a tool ThreadItem, by its schema shape.

    ``commandExecution`` carries no ``arguments`` field; its inputs are the
    ``command`` plus its working directory. ``mcpToolCall`` carries the real
    ``arguments`` (arbitrary JSON). ``fileChange`` carries its ``changes``
    list. None pass the whole ThreadItem envelope.
    """
    if item_type == "commandExecution":
        return {"command": _str(item.get("command")), "cwd": _str(item.get("cwd"))}
    if item_type == "mcpToolCall":
        return _mapping(item.get("arguments"))
    return {"changes": _list(item.get("changes"))}


def _params(obj: JSON) -> JSON:
    params = obj.get("params")
    return cast(JSON, params) if isinstance(params, Mapping) else cast(JSON, {})


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _mapping(value: object) -> dict[str, object]:
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return list(value)
    return []
