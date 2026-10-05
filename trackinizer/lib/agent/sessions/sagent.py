"""Read a sagent ``session.jsonl`` into the session IR.

A bridge, read-only and deliberately lossy: sagent will write the IR itself,
and this module is deleted when it does. Until then it is the one place that
knows sagent's three record families:

- ``kind: history`` records (``user``, ``assistant``, ``tool_result``,
  ``agent_send``, ``compact_*``), plus ``meta`` / ``tool_state`` side records;
- the older ``kind: message`` records tagged by ``descriptor``;
- the oldest ``kind: message`` records tagged by ``role``.

A tool result carries no tool name in sagent -- only the ``call_id`` of the
call it answers -- so the reader remembers each call and types the result by
what that call was.

The IR has no field for a failed tool result, so the reader states sagent's
``is_error`` in ``extra["is_error"]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePath
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

import json

from trackinizer.lib.agent.types.sessions import (
    AgentStatusResult,
    AgentToAgentMessage,
    AnyToolResult,
    AssistantMessage,
    ContextCompaction,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    IncompleteRecord,
    SessionRecord,
    ShellCommandResult,
    Splice,
    Thinking,
    TokenUsage,
    ToolCall,
    TurnContext,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
)
from trackinizer.lib.custom_json import convert, json_freeze, parse


if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from typing import TextIO


__all__ = ["is_sagent", "normalize"]


def normalize(stream: TextIO) -> Iterator[SessionRecord]:
    """Normalize a sagent session stream into IR records.

    Yields as it reads (axiom 11), so a live session reads as far as it has
    been written.

    Args:
      stream: A sagent ``session.jsonl`` text stream.

    Yields:
      record: Each record the stream states, in order. A line that is not
        JSON becomes an ``IncompleteRecord``; a record family this reader
        does not model becomes an ``UncategorizedRecord``.

    """
    reader = _Reader()
    for line in stream:
        if line.strip():
            yield from reader.read(line)


def is_sagent(head: str) -> bool:
    """Return whether the first lines of a file were written by sagent.

    Args:
      head: The file's opening lines.

    Returns:
      found: Whether the first non-blank line is a record kind only sagent
        writes.

    """
    only_sagent = {
        "meta",
        "history",
        "tool_state",
        "context_override",
        "context_splice",
        "persistent_agent",
        "runtime_event",
        "event",
        "update",
        "clear",
    }
    for line in head.splitlines():
        if not line.strip():
            continue
        try:
            record = parse(line, dict[str, object])
        except json.JSONDecodeError:
            return False
        kind = record.get("kind")
        return kind in only_sagent or (
            kind == "message" and ("descriptor" in record or "role" in record)
        )
    return False


@dataclass(frozen=True, slots=True, kw_only=True)
class _Call:
    """What a result needs to know about the call it answers."""

    name: str
    arguments: dict[str, object]


@dataclass(slots=True, kw_only=True)
class _Reader:
    """Line-at-a-time state: open calls, spawned children, the model in force."""

    calls: dict[str, _Call] = field(default_factory=dict[str, _Call])
    children: dict[str, str] = field(default_factory=dict[str, str])
    """Spawned child directory name, by the label the spawn gave it."""

    model: str | None = None

    def read(self, line: str) -> list[SessionRecord]:
        """Return the records one line states.

        Args:
          line: One line of the session file.

        Returns:
          records: The IR records it states, possibly none.

        """
        try:
            record = parse(line, dict[str, object])
        except (json.JSONDecodeError, ValueError, TypeError):
            return [IncompleteRecord(text=line)]
        kind = convert(record.get("kind"), str, default="")
        if kind == "history":
            return self._history(record)
        if kind == "meta":
            return self._meta(record)
        if kind == "persistent_agent":
            label = convert(record.get("label"), str, default="")
            self.children[label] = PurePath(
                convert(record.get("session_dir"), str),
            ).name
        if kind == "message" and "descriptor" in record:
            return self._descriptor(record)
        if kind == "message" and "role" in record:
            return self._role(record)
        if kind in {"context_override", "context_splice"}:
            return [ContextCompaction(extra=json_freeze(_scalars(record)))]
        return [
            UncategorizedRecord(kind=kind or "unknown", payload=json_freeze(record)),
        ]

    def _history(self, record: dict[str, object]) -> list[SessionRecord]:
        """Normalize one ``kind: history`` record."""
        kind = convert(record.get("type"), str, default="")
        stamp = _stamp(record.get("timestamp"))
        text = convert(record.get("text"), str, default="")
        if kind == "user":
            return [UserMessage(timestamp=stamp, content=text)]
        if kind == "assistant":
            thoughts = [
                (
                    _str(b.get("thinking")),
                    _str(b.get("signature")),
                )
                for b in _mappings(record.get("thinking_blocks"))
            ]
            calls = [
                (
                    _str(c.get("id")),
                    _str(c.get("name")),
                    convert(c.get("args"), dict[str, object], default={}),
                )
                for c in _mappings(record.get("tool_calls"))
            ]
            return self._turn(stamp, thoughts=thoughts, text=text, calls=calls)
        if kind == "tool_result":
            return [
                self._result(
                    _str(record.get("call_id")),
                    _str(record.get("content")),
                    failed=_bool(record.get("is_error")),
                    stamp=stamp,
                ),
            ]
        if kind == "agent_send":
            return [
                AgentToAgentMessage(
                    timestamp=stamp,
                    content=text,
                    sender=convert(record.get("source"), str, default=""),
                ),
            ]
        if kind == "compact_complete":
            return [
                ContextCompaction(
                    timestamp=stamp,
                    extra=json_freeze(_scalars(record)),
                ),
            ]
        return [
            UncategorizedRecord(
                kind=f"history/{kind}",
                payload=json_freeze(record),
            ),
        ]

    def _meta(self, record: dict[str, object]) -> list[SessionRecord]:
        """Split a meta record: a model change is a setting, spend is accounting."""
        out: list[SessionRecord] = []
        model = convert(record.get("model_id"), str, default="") or None
        if model is not None and model != self.model:
            self.model = model
            out.append(
                TurnContext(
                    model=model,
                    extra=json_freeze(
                        {
                            k: _str(record.get(k))
                            for k in ("provider", "session_id", "bash_cwd", "name")
                            if k in record
                        },
                    ),
                ),
            )
        tokens = convert(record.get("tokens"), dict[str, object], default={})
        spend = convert(record.get("spend"), dict[str, object], default={})
        total_cost = convert(record.get("total_cost_usd"), float, default=0.0)
        cost = total_cost or sum(convert(v, float) for v in spend.values())
        out.append(
            TokenUsage(
                info=json_freeze(
                    {
                        "cost_usd": cost,
                        "input_tokens": convert(
                            tokens.get("input_tokens"),
                            int,
                            default=0,
                        ),
                        "output_tokens": convert(
                            tokens.get("output_tokens"),
                            int,
                            default=0,
                        ),
                        "cache_read_tokens": convert(
                            tokens.get("cache_read_tokens"),
                            int,
                            default=0,
                        ),
                        "rounds": convert(
                            record.get("num_tool_call_rounds")
                            or record.get("turn_count")
                            or 0,
                            int,
                        ),
                    },
                ),
            ),
        )
        return out

    def _turn(
        self,
        stamp: str | None,
        *,
        thoughts: list[tuple[str, str]],
        text: str,
        calls: list[tuple[str, str, dict[str, object]]],
    ) -> list[SessionRecord]:
        """Split one assistant turn into its acts, remembering each call."""
        out: list[SessionRecord] = [
            Thinking(
                timestamp=stamp,
                content=thought or None,
                encrypted=signature or None,
            )
            for thought, signature in thoughts
        ]
        if text.strip():
            out.append(AssistantMessage(timestamp=stamp, content=text))
        for call_id, name, arguments in calls:
            self.calls[call_id] = _Call(name=name, arguments=arguments)
            out.append(
                ToolCall(
                    timestamp=stamp,
                    call_id=call_id,
                    name=name,
                    arguments=json_freeze(arguments),
                ),
            )
        return out

    def _result(
        self,
        call_id: str,
        content: str,
        *,
        failed: bool,
        stamp: str | None,
    ) -> AnyToolResult:
        """Type a result by the call it answers."""
        call = self.calls.pop(call_id, None)
        extra = {"is_error": failed}
        name = call.name.lower() if call is not None else ""
        args = call.arguments if call is not None else {}
        path = _str(args.get("file_path") or args.get("path")) or None
        if name == "bash":
            command = _str(args.get("command"))
            return ShellCommandResult(
                timestamp=stamp,
                call_id=call_id,
                command=(command,) if command else None,
                stdout=content,
                extra=extra,
            )
        if name == "read":
            return FileReadResult(
                timestamp=stamp,
                call_id=call_id,
                path=path,
                content=content,
                extra=extra,
            )
        # A refused file op changed nothing; keep its error text instead.
        if failed and name in {"write", "edit"}:
            name = ""
        if name == "write":
            return FileWriteResult(
                timestamp=stamp,
                call_id=call_id,
                path=path,
                content=_str(args.get("content")) or None,
                extra=extra,
            )
        if name == "edit":
            return FileEditResult(
                timestamp=stamp,
                call_id=call_id,
                path=path,
                edits=(
                    Splice(
                        before=_str(args.get("old_string")),
                        after=_str(args.get("new_string")),
                    ),
                ),
                extra=extra,
            )
        if name == "webfetch":
            return WebFetchResult(
                timestamp=stamp,
                call_id=call_id,
                url=_str(args.get("url")) or None,
                content=content,
                extra=extra,
            )
        if name == "agentspawn":
            label = _str(args.get("label"))
            return AgentStatusResult(
                timestamp=stamp,
                call_id=call_id,
                agent_id=self.children.get(label) if label else None,
                agent_kind=label or None,
                prompt=_str(args.get("prompt")) or None,
                model=_str(args.get("model_id")) or None,
                content=content,
                extra=extra,
            )
        return UncategorizedToolResult(
            timestamp=stamp,
            call_id=call_id,
            content=content,
            extra=extra,
        )

    def _descriptor(self, record: dict[str, object]) -> list[SessionRecord]:
        """Normalize the older descriptor-tagged message family."""
        descriptor = _str(record.get("descriptor"))
        stamp = _stamp(record.get("_timestamp"))
        content = record.get("content")
        parts = _mappings(content)
        if descriptor in {
            "text/x-user-message",
            "multipart/x-user-message",
            "multipart/x-user-turn",
        }:
            return [UserMessage(timestamp=stamp, content=_plain(content, parts))]
        if descriptor in {"multipart/x-model-message", "multipart/x-assistant-turn"}:
            thoughts: list[tuple[str, str]] = []
            calls: list[tuple[str, str, dict[str, object]]] = []
            text: list[str] = []
            for part in parts:
                kind = _str(part.get("descriptor"))
                body = part.get("content")
                if kind.startswith("application/x-thinking"):
                    block = _dict(body)
                    thoughts.append(
                        (
                            _str(block.get("thinking")),
                            _str(block.get("signature")),
                        ),
                    )
                elif kind in {"text/plain", "text/markdown"}:
                    text.append(_str(body))
                elif kind == "multipart/x-tool-call":
                    calls.append(_legacy_call(_mappings(body)))
            return self._turn(stamp, thoughts=thoughts, text="".join(text), calls=calls)
        if descriptor == "multipart/x-tool-result":
            call_id = next(
                (
                    _str(p.get("content"))
                    for p in parts
                    if p.get("descriptor") == "text/x-queue-id"
                ),
                "",
            )
            failed = any(p.get("descriptor") == "text/x-error" for p in parts)
            return [
                self._result(
                    call_id,
                    _plain(content, parts),
                    failed=failed,
                    stamp=stamp,
                ),
            ]
        return [
            UncategorizedRecord(
                kind=f"message/{descriptor}",
                payload=json_freeze(record),
            ),
        ]

    def _role(self, record: dict[str, object]) -> list[SessionRecord]:
        """Normalize the oldest role-tagged message family."""
        role = _str(record.get("role"))
        text = _str(record.get("content"))
        if role == "user":
            return [UserMessage(content=text)]
        if role == "assistant":
            thoughts = [
                (
                    _str(b.get("thinking")),
                    _str(b.get("signature")),
                )
                for b in _mappings(record.get("thinking_blocks"))
            ]
            calls = [
                (
                    _str(c.get("id")),
                    _str(c.get("name")),
                    _dict(c.get("input") or c.get("args")),
                )
                for c in _mappings(record.get("tool_calls"))
            ]
            return self._turn(None, thoughts=thoughts, text=text, calls=calls)
        if role == "tool":
            return [
                self._result(
                    _str(record.get("tool_call_id")),
                    text,
                    failed=_bool(record.get("is_error")),
                    stamp=None,
                ),
            ]
        return [
            UncategorizedRecord(
                kind=f"message/{role}",
                payload=json_freeze(record),
            ),
        ]


# Legacy tool names are the lowercased tail of a descriptor (``application/x-tool-
# read``); the history family spells them ``Read``. A single-word name capitalizes;
# ``compound`` spells the rest.
def _legacy_call(
    parts: list[dict[str, object]],
    *,
    compound: Mapping[str, str] = MappingProxyType(
        {
            "webfetch": "WebFetch",
            "websearch": "WebSearch",
            "agentspawn": "AgentSpawn",
            "agentsend": "AgentSend",
            "agentself": "AgentSelf",
        },
    ),
) -> tuple[str, str, dict[str, object]]:
    """Return ``(call_id, name, arguments)`` from a legacy tool-call part list."""
    call_id, name, arguments = "", "", dict[str, object]()
    for part in parts:
        kind = _str(part.get("descriptor"))
        if kind == "text/x-queue-id":
            call_id = _str(part.get("content"))
        elif kind.startswith("application/x-tool-"):
            tail = kind.removeprefix("application/x-tool-")
            name = compound.get(tail, tail.capitalize())
            arguments = _dict(part.get("content"))
    return call_id, name, arguments


def _plain(content: object, parts: list[dict[str, object]]) -> str:
    """Return a legacy message's readable text: its string, or its text parts."""
    if isinstance(content, str):
        return content
    return "".join(
        _str(p.get("content"))
        for p in parts
        if p.get("descriptor") in {"text/plain", "text/markdown", "text/x-error"}
    )


# History timestamps are epoch seconds; legacy ones are epoch nanoseconds. Anything
# past year 5000 in seconds is nanoseconds.
def _stamp(value: object) -> str | None:
    """Return an epoch timestamp as ISO-8601 UTC, or ``None``."""
    seconds = _float(value)
    if seconds <= 0:
        return None
    if seconds > 1e11:
        seconds /= 1e9
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def _str(value: object) -> str:
    return "" if value is None else convert(value, str)


def _bool(value: object) -> bool:
    return False if value is None else convert(value, bool)


def _float(value: object) -> float:
    return 0.0 if value is None else convert(value, float)


def _dict(value: object) -> dict[str, object]:
    return {} if value is None else convert(value, dict[str, object])


def _mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    values = cast(list[object] | tuple[object, ...], value)
    return convert(values, list[dict[str, object]])


def _scalars(record: Mapping[str, object]) -> dict[str, object]:
    """Return a record's scalar fields: the numbers a compaction reports."""
    return {
        k: v
        for k, v in record.items()
        if isinstance(v, (int, float, str, bool)) and k != "kind"
    }
