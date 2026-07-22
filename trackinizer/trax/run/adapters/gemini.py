"""Gemini CLI adapter.

Sessions live at
``~/.gemini/tmp/<project-sha256>/chats/session-<timestamp>-<uuid>.json``.
Unlike the others, the session is one JSON object that gemini rewrites in
place on every update, so there are no appended lines to follow.

The runner hands the whole file body to ``parse`` on each change (the adapter
declares ``whole_file = True``); this adapter parses it and normalizes the
most recent message into a typed :data:`Message`.

See ``docs/cli-scraping-investigation.md`` for the empirical layout.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast

import json

from trackinizer.lib.custom_json import JSON, json_freeze
from trackinizer.trax.run.adapters.base import Event
from trackinizer.types.agent_session_events import (
    AssistantMessage,
    Message,
    ToolCall,
    UnknownMessage,
    UserMessage,
)


class GeminiAdapter:
    """Reads the ``gemini`` CLI's whole-file session JSON.

    Stateful within one run: gemini rewrites its whole session JSON in place,
    so the runner re-reads the entire body on each change. The adapter tracks
    how many messages it has already emitted, per session file, and emits only
    the newly-appended slice on the next parse, so a burst of N messages
    between two polls surfaces all N rather than only the last (REV-004).

    The cursor is keyed by the body's ``sessionId`` (every gemini session file
    stamps one), not a single counter: the runner reuses ONE adapter across
    every matching session file, so a per-adapter counter would carry one
    file's count into the next and drop the second file's turns (#498). Each
    run builds a fresh adapter, so the cursors never leak across runs.
    """

    name: str = "gemini"
    cli_binary: str = "gemini"
    whole_file: bool = True

    def __init__(self) -> None:
        # Per-session-file count of leading messages already emitted, keyed by
        # the body's ``sessionId``. The next parse of that file emits
        # ``messages[_emitted[session_id]:]`` and advances its entry.
        self._emitted: dict[str, int] = {}

    @property
    def _tmp_dir(self) -> Path:
        # Resolve ``$HOME`` per call, not at import (see ClaudeAdapter).
        return Path.home() / ".gemini" / "tmp"

    def session_dirs(self) -> Iterable[Path]:
        tmp = self._tmp_dir
        if not tmp.is_dir():
            return ()
        return tuple(d / "chats" for d in tmp.iterdir() if (d / "chats").is_dir())

    def matches_session_file(self, path: Path) -> bool:
        return (
            path.suffix == ".json"
            and path.parent.name == "chats"
            and path.name.startswith("session-")
        )

    def session_id_from_path(self, path: Path) -> str | None:
        # Gemini's ``session-<id>.json`` stem carries an id, but resume
        # correlation isn't wired for it yet; treat as non-resumable for now.
        del path
        return None

    def parse(self, raw: bytes, *, whole_file: bool) -> Iterable[Event]:
        # The runner passes the whole file body here (whole_file=True); we emit
        # the messages appended to this session file since the last parse.
        del whole_file
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        if not isinstance(parsed, Mapping):
            return ()
        obj = json_freeze(cast(Mapping[str, object], parsed))
        messages = obj.get("messages")
        if (
            not isinstance(messages, Sequence)
            or isinstance(messages, str)
            or not messages
        ):
            # An empty file is session lifecycle, not a turn; lifecycle lives
            # on the AgentSession row, so there is no event to emit here.
            return ()
        # Emit only messages appended to THIS session file since its last parse.
        # The cursor is keyed by ``sessionId`` so one adapter draining several
        # files keeps their counts apart (#498). A shorter list than already
        # emitted means the file rotated or was rewritten from scratch; restart
        # from its start so nothing is silently skipped (REV-004).
        #
        # A body with no ``sessionId`` (malformed / pre-id file) carries no
        # stable per-file identity, so it cannot share the keyed cursor map: two
        # keyless files would both key ``""`` and the second's turns would be
        # dropped against the first's count (K6-002). Such a body emits every
        # message it carries -- the runner only re-parses a whole-file on a
        # ``(size, mtime)`` change, so a keyless file is re-emitted only when it
        # actually changes, never per idle poll.
        session_id = _str(obj.get("sessionId"))
        if not session_id:
            appended = messages
        else:
            emitted = self._emitted.get(session_id, 0)
            if len(messages) < emitted:
                emitted = 0
            appended = messages[emitted:]
            self._emitted[session_id] = len(messages)
        events: list[Event] = []
        for raw_msg in appended:
            if not isinstance(raw_msg, Mapping):
                continue
            # Even after ``isinstance(_, Mapping)``, ty won't narrow the indexed
            # element into ``JSON`` (generic invariance); cast as the project
            # pattern does (see ``switchboard/hub.py:989``).
            message = _to_message(cast(JSON, raw_msg))
            if message is not None:
                events.append(Event(message=message))
        return tuple(events)


def _to_message(msg: JSON) -> Message | None:
    """Normalize one gemini message to a typed message, or ``None`` to skip."""
    msg_type = msg.get("type")
    if msg_type == "user":
        return UserMessage(text=_str(msg.get("content")))
    if msg_type == "gemini":
        return _assistant_message(msg)
    return UnknownMessage(raw=msg)


def _assistant_message(msg: JSON) -> Message:
    """A ``gemini`` message: response text plus any ``toolCalls`` entries."""
    # Keyed by id (last-wins) so a duplicate ``toolCalls`` id cannot trip
    # ``AssistantMessage``'s duplicate-id invariant, which would raise and let
    # the runner silently drop the whole turn (mirrors claude's R-41 guard).
    tool_calls: dict[str, ToolCall] = {}
    for call in _tool_calls(msg):
        tc = ToolCall(
            id=_str(call.get("id")),
            name=_str(call.get("name")),
            args=_mapping(call.get("args")),
        )
        tool_calls[tc.id] = tc
    return AssistantMessage(
        text=_str(msg.get("content")), tool_calls=tuple(tool_calls.values())
    )


def _tool_calls(msg: JSON) -> tuple[JSON, ...]:
    """The ``toolCalls`` array of a gemini message, or empty."""
    calls = msg.get("toolCalls")
    if not isinstance(calls, Sequence) or isinstance(calls, str):
        return ()
    return tuple(
        cast(JSON, c) for c in cast("Sequence[object]", calls) if isinstance(c, Mapping)
    )


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _mapping(value: object) -> dict[str, object]:
    return (
        dict(cast("Mapping[str, object]", value)) if isinstance(value, Mapping) else {}
    )
