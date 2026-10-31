"""Read a sagent ``session.jsonl`` into the session IR, and write one for a resume.

Today's sagent journals the IR itself: each line is one record, tagged with its
class, read back as sagent wrote it and written the same way, so a stored session
resumes as a journal of its own. Read-only and deliberately lossy for what older
sagents wrote, this module is also the one place that knows their three record
families:

- ``kind: history`` records (``user``, ``assistant``, ``tool_result``,
  ``agent_send``, ``system_message``, ``compact_*``), plus ``meta`` / ``tool_state``
  side records;
- the older ``kind: message`` records tagged by ``descriptor``;
- the oldest ``kind: message`` records tagged by ``role``.

A ``context_splice`` (or legacy ``context_override``) is a compaction; one that
summarized also opens a window, as the IR's :class:`ContextClear`.

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
from typing import TYPE_CHECKING

import json

from treekle import ReadError, from_plain, immutable, json_loads

from trackinizer.sagent.session.jsonl import read_record, record_line
from trackinizer.sagent.session.types import (
    AgentStatusResult,
    AgentToAgentMessage,
    AnyToolResult,
    AssistantMessage,
    ContextClear,
    ContextCompaction,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    IncompleteRecord,
    SessionRecord,
    ShellCommandResult,
    Splice,
    SystemMessage,
    Thinking,
    TokenUsage,
    ToolCall,
    TurnContext,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    record_classes,
)


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from typing import TextIO


__all__ = ["denormalize", "is_sagent", "normalize"]


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


def denormalize(records: Iterable[SessionRecord], stream: TextIO) -> None:
    """Write ``records`` as a journal today's sagent resumes.

    A line read torn stays the bytes it was, so the journal reads back the same.

    Args:
      records: The records, in order.
      stream: Where the journal's text goes.

    """
    for record in records:
        if isinstance(record, IncompleteRecord):
            _ = stream.write(record.text)
        else:
            _ = stream.write(record_line(record).decode())


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
            record = json_loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(record, dict):
            return False
        if record.get("py/object") in record_classes():
            return True
        kind = record.get("kind")
        return isinstance(kind, str) and (
            kind in only_sagent
            or (kind == "message" and ("descriptor" in record or "role" in record))
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
            record = json_loads(line, dict[str, object])
        except (json.JSONDecodeError, ValueError, TypeError):
            return [IncompleteRecord(text=line)]
        if record.get("py/object") in record_classes():
            return [read_record(line.encode())]
        # The WHOLE read is the guarded region, not only the parse: every field
        # below is narrowed as it is read, and one malformed field raised out of
        # ``normalize`` and lost the rest of the file. A record this reader
        # cannot type keeps its bytes, as an unrecognized family does.
        try:
            return self._dispatch(_str(record.get("kind")), record)
        except ReadError:
            return [UncategorizedRecord(kind="unknown", payload=immutable(record))]

    def _dispatch(self, kind: str, record: dict[str, object]) -> list[SessionRecord]:
        """Return the records one parsed line states, by its family."""
        if kind == "history":
            return self._history(record)
        if kind == "meta":
            return self._meta(record)
        if kind == "persistent_agent":
            label = _str(record.get("label"))
            self.children[label] = PurePath(
                _str(record.get("session_dir")),
            ).name
        if kind == "message" and "descriptor" in record:
            return self._descriptor(record)
        if kind == "message" and "role" in record:
            return self._role(record)
        if kind in {"context_override", "context_splice"}:
            return self._compaction(record)
        return [
            UncategorizedRecord(kind=kind or "unknown", payload=immutable(record)),
        ]

    def _history(self, record: dict[str, object]) -> list[SessionRecord]:
        """Normalize one ``kind: history`` record."""
        kind = _str(record.get("type"))
        stamp = _stamp(record.get("timestamp"))
        text = _str(record.get("text"))
        if kind == "user":
            return [UserMessage(timestamp=stamp, content=text)]
        # The agent's own reminders to its model: the harness, never the user.
        if kind == "system_message":
            return [SystemMessage(timestamp=stamp, content=text, subtype="reminder")]
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
                    from_plain(c.get("args"), dict[str, object], default={}),
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
                    sender=_str(record.get("source")),
                ),
            ]
        if kind == "compact_complete":
            return [
                ContextCompaction(
                    timestamp=stamp,
                    extra=immutable(_scalars(record)),
                ),
            ]
        return [
            UncategorizedRecord(
                kind=f"history/{kind}",
                payload=immutable(record),
            ),
        ]

    # A summary compaction's payload is what the agent's next context begins with: the
    # continuation that holds the summary, then the turns it kept. That is the IR's
    # opening of a window, so it reads as the event followed by the clear, as claude's
    # and codex's compactions do; a window read as the event alone named the whole
    # session as what the model was looking at. A splice that carries no summary, such
    # as a fallback that dropped history, states no window and reads as the event.
    def _compaction(self, record: dict[str, object]) -> list[SessionRecord]:
        """Read a compaction: the event, and the window it opened when it summarized."""
        event = ContextCompaction(extra=immutable(_scalars(record)))
        # Only a splice states a strategy, and it writes its payload as a list; the
        # legacy override's payload is a mapping this never reads.
        if _str(record.get("strategy")) != "summary":
            return [event]
        payload = _mappings(record.get("payload"))
        if not payload or _str(payload[0].get("type")) not in {"user", "agent_send"}:
            return [event]
        first, *kept = payload
        return [
            event,
            ContextClear(
                summary=_str(first.get("text")),
                history=tuple(each for entry in kept for each in self._history(entry)),
            ),
        ]

    def _meta(self, record: dict[str, object]) -> list[SessionRecord]:
        """Split a meta record: a model change is a setting, spend is accounting."""
        out: list[SessionRecord] = []
        model = _str(record.get("model_id")) or None
        if model is not None and model != self.model:
            self.model = model
            out.append(
                TurnContext(
                    model=model,
                    extra=immutable(
                        {
                            k: _str(record.get(k))
                            for k in ("provider", "session_id", "bash_cwd", "name")
                            if k in record
                        },
                    ),
                ),
            )
        tokens = from_plain(record.get("tokens"), dict[str, object], default={})
        spend = from_plain(record.get("spend"), dict[str, object], default={})
        total_cost = from_plain(record.get("total_cost_usd"), float, default=0.0)
        cost = total_cost or sum(
            from_plain(v, float, default=0.0) for v in spend.values()
        )
        out.append(
            TokenUsage(
                info=immutable(
                    {
                        "cost_usd": cost,
                        "input_tokens": _int(tokens.get("input_tokens")),
                        "output_tokens": _int(tokens.get("output_tokens")),
                        "cache_read_tokens": _int(tokens.get("cache_read_tokens")),
                        "rounds": _int(
                            record.get("num_tool_call_rounds")
                            or record.get("turn_count"),
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
                    arguments=immutable(arguments),
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
                    block = from_plain(body, dict[str, object], default={})
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
                payload=immutable(record),
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
                    from_plain(
                        (c.get("input") or c.get("args")),
                        dict[str, object],
                        default={},
                    ),
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
                payload=immutable(record),
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
            arguments = from_plain(part.get("content"), dict[str, object], default={})
    return call_id, name, arguments


def _plain(content: object, parts: list[dict[str, object]]) -> str:
    """Return a legacy message's readable text: its string, or its text parts."""
    if isinstance(content, str):
        return content
    return "".join(
        _str(p.get("content"))
        for p in parts
        if _str(p.get("descriptor")) in {"text/plain", "text/markdown", "text/x-error"}
    )


# History timestamps are epoch seconds; legacy ones are epoch nanoseconds. Anything
# past year 5000 in seconds is nanoseconds.
def _stamp(value: object) -> str | None:
    """Return an epoch timestamp as ISO-8601 UTC, or ``None``."""
    seconds = from_plain(value, float, default=0.0)
    if seconds <= 0:
        return None
    if seconds > 1e11:
        seconds /= 1e9
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


# A missing or null field reads as empty; a mistyped one raises, and the per-record guard in
# ``_Reader.read`` keeps that record whole.
def _str(value: object) -> str:
    return from_plain(value, str, default="")


def _bool(value: object) -> bool:
    return from_plain(value, bool, default=False)


def _int(value: object) -> int:
    return from_plain(value, int, default=0)


def _mappings(value: object) -> list[dict[str, object]]:
    # A part's ``content`` is a string or a list of parts; only the list has any.
    if isinstance(value, str):
        return []
    return from_plain(value, list[dict[str, object]], default=[])


def _scalars(record: Mapping[str, object]) -> dict[str, object]:
    """Return a record's scalar fields: the numbers a compaction reports."""
    return {
        k: v
        for k, v in record.items()
        if isinstance(v, (int, float, str, bool)) and k != "kind"
    }
