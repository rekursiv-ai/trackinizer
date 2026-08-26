"""Claude Code adapter.

Sessions live at ``$CLAUDE_CONFIG_DIR/projects/<path-hash>/<session-id>.jsonl``
(``~/.claude`` when ``$CLAUDE_CONFIG_DIR`` is unset), append-only, one JSON
object per line. A top-level ``type`` discriminates;
for ``user`` / ``assistant`` lines the real category lives in
``message.content``: a bare string is a user prompt, a ``content[]`` block
list carries ``thinking`` / ``text`` / ``tool_use`` (assistant) or
``tool_result`` (a user line echoing tool output).

``parse`` normalizes one line into typed :data:`Message`s -- an ``assistant``
line aggregates its text + thinking + every ``tool_use`` block into one
:class:`AssistantMessage` (tool calls nested), matching the model, while a
``user`` line emits one :class:`ToolResult` per ``tool_result`` block (claude
batches parallel results into one line). Verified against claude 2.1.158
logs. See ``docs/cli-scraping-investigation.md`` for the empirical layout.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast

import json
import os

from trackinizer.lib.custom_json import JSON, json_freeze
from trackinizer.trax.run.adapters.base import Event
from trackinizer.types.agent_session_events import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResult,
    UnknownMessage,
    UserMessage,
)


# Types that must be skipped DESPITE carrying a ``message`` field, so
# ``_to_messages``'s structural test cannot reach them. ``system`` is claude's
# meta record (``isMeta`` / ``durationMs`` / ``gitBranch``); it sometimes
# carries content but is never a model turn.
#
# Everything else is handled structurally -- a record without ``message`` is
# session state and is dropped without being named here. Do NOT re-add
# name-by-name entries for state records: that list is vendor-owned and
# open-ended, and enumerating it is what let ``atis-latch``,
# ``bridge-session``, and ``cost-state`` each reach a captured transcript.
_SKIP_TYPES = frozenset({"system"})


class ClaudeAdapter:
    """Reads the ``claude`` CLI's per-project session JSONL files."""

    name: str = "claude"
    cli_binary: str = "claude"
    whole_file: bool = False

    @property
    def _projects_dir(self) -> Path:
        # Resolve per call, not at import: a test (or a run under a switched
        # env) must see the current value, not one frozen when the module first
        # loaded. ``$CLAUDE_CONFIG_DIR`` is where claude itself keeps its
        # config root -- hermetic launchers point it at a throwaway dir -- so
        # honor it, else ``~/.claude``.
        root = os.environ.get("CLAUDE_CONFIG_DIR")
        return (Path(root) if root else Path.home() / ".claude") / "projects"

    def session_dirs(self) -> Iterable[Path]:
        projects = self._projects_dir
        if not projects.is_dir():
            return ()
        # The projects ROOT, not the per-project subdirectories under it.
        # Claude shards sessions by hashed cwd and mints that directory when
        # it first runs in a workspace -- which, for the run being captured,
        # is after the watch is already armed. A watch on the subdirectories
        # existing at arming time cannot adopt a new sibling (recursion only
        # descends INTO a watched tree), so the run would capture nothing and
        # say nothing. Watching the root covers every project, present and
        # future; ``matches_session_file`` still scopes capture to a
        # ``<project>/<session>.jsonl``, so nothing extra is swept in.
        return (projects,)

    def matches_session_file(self, path: Path) -> bool:
        return path.suffix == ".jsonl" and path.parent.parent == self._projects_dir

    def session_id_from_path(self, path: Path) -> str | None:
        """Claude's own session id is the ``<session-id>.jsonl`` filename stem.

        Used to correlate a resumed run to its prior AgentSession (the same id
        names the same claude session across ``--resume``). Returns ``None`` for
        a path that is not one of this adapter's session files.
        """
        if path.suffix != ".jsonl":
            return None
        return path.stem or None

    def parse(self, raw: bytes, *, whole_file: bool) -> Iterable[Event]:
        del whole_file  # claude is line-oriented; one line in.
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        if not isinstance(parsed, Mapping):
            return ()
        obj = json_freeze(cast(Mapping[str, object], parsed))
        return tuple(Event(message=m) for m in _to_messages(obj))


def _to_messages(obj: JSON) -> tuple[Message, ...]:
    """Normalize one claude line into zero or more typed messages.

    Dispatch is STRUCTURAL, not a list of known names: a record that carries
    no ``message`` is session state, never a turn. Measured across 58,842
    records of on-disk claude 2.1.240 logs, the split is exact -- every
    ``user`` / ``assistant`` line has ``message``, and no other type ever
    does.

    That matters because the set of state types is open and vendor-owned:
    claude adds them without notice (``atis-latch``, ``bridge-session``,
    ``cost-state`` all appeared this way), and an allowlist misses each new
    one until someone notices an empty ``UnknownMessage`` sitting in the
    middle of a captured transcript. It also fails the other way -- a future
    turn-bearing type would be silently dropped. Keying on the field the
    content actually lives in handles both without an edit.

    ``_SKIP_TYPES`` remains for the exceptions only: types that DO carry a
    ``message`` but still are not model turns.
    """
    line_type = obj.get("type")
    if line_type in _SKIP_TYPES:
        return ()
    if line_type == "user":
        return _user_messages(obj)
    if line_type == "assistant":
        return (_assistant_message(obj),)
    if obj.get("message") is None:
        return ()
    return (UnknownMessage(raw=obj),)


def _user_messages(obj: JSON) -> tuple[Message, ...]:
    """A ``user`` line: one ``ToolResult`` per echoed block, or a typed prompt.

    Claude batches parallel tool results into one user line's ``content[]``,
    so every ``tool_result`` block becomes its own :class:`ToolResult`; a line
    with none is a human prompt.
    """
    results = tuple(
        ToolResult(
            call_id=_str(block.get("tool_use_id")),
            content=_text_of(block.get("content")),
            is_error=bool(block.get("is_error")),
        )
        for block in _content_blocks(obj)
        if block.get("type") == "tool_result"
    )
    return results or (UserMessage(text=_message_text(obj)),)


def _assistant_message(obj: JSON) -> Message:
    """An ``assistant`` line: text + thinking + every tool_use, in one turn."""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    thinking_signature = ""
    # Keyed by id so a duplicate ``tool_use`` id (last-wins) cannot trip
    # ``AssistantMessage``'s duplicate-id invariant -- which raises in
    # ``__post_init__``, is swallowed by the runner's ``_process_chunk``, and
    # silently drops the whole turn (R-41). A dict preserves first-seen order.
    tool_calls: dict[str, ToolCall] = {}
    for block in _content_blocks(obj):
        btype = block.get("type")
        if btype == "text":
            text_parts.append(_str(block.get("text")))
        elif btype == "thinking":
            # Join multiple thinking blocks like text; the last *non-empty*
            # signature wins (R2R-036). Assigning would drop earlier blocks, and
            # only signed blocks (Anthropic signs the final one) overwrite, so a
            # later unsigned block does not clear an earlier signature.
            thinking_parts.append(_str(block.get("thinking")))
            signature = _str(block.get("signature"))
            if signature:
                thinking_signature = signature
        elif btype == "tool_use":
            call = ToolCall(
                id=_str(block.get("id")),
                name=_str(block.get("name")),
                args=_mapping(block.get("input")),
            )
            tool_calls[call.id] = call
    return AssistantMessage(
        text="".join(text_parts),
        thinking="".join(thinking_parts),
        thinking_signature=thinking_signature,
        tool_calls=tuple(tool_calls.values()),
    )


def _content_blocks(obj: JSON) -> tuple[JSON, ...]:
    """The ``message.content`` block list, or empty when it's a bare string."""
    message = obj.get("message")
    if not isinstance(message, Mapping):
        return ()
    content = cast(JSON, message).get("content")
    if not isinstance(content, Sequence) or isinstance(content, str):
        return ()
    return tuple(
        cast(JSON, b) for b in cast(Sequence[object], content) if isinstance(b, Mapping)
    )


def _message_text(obj: JSON) -> str:
    """The user prompt text: a bare-string ``content`` or joined text blocks."""
    message = obj.get("message")
    if isinstance(message, Mapping):
        content = cast(JSON, message).get("content")
        if isinstance(content, str):
            return content
    return "".join(
        _str(b.get("text")) for b in _content_blocks(obj) if b.get("type") == "text"
    )


def _text_of(value: object) -> str:
    """Coerce a ``tool_result.content`` (str or block list) to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return "".join(
            _str(cast(JSON, b).get("text")) for b in value if isinstance(b, Mapping)
        )
    return ""


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _mapping(value: object) -> dict[str, object]:
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}
