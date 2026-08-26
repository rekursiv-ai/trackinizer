"""The ``Adapter`` protocol and the ``Event`` record it produces.

Each supported CLI ships one ``Adapter`` saying where its session log lives
and how to turn one raw chunk into zero or more ``Event``s. The session
runner stays CLI-agnostic and drives every adapter through this protocol.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from trackinizer.types.agent_session_events import (
    Kind,
    Message,
)


__all__ = ["Adapter", "Event", "StreamAdapter"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """One parsed turn, in memory, before it becomes an ``EventBody``.

    ``message`` is the typed turn content (a :data:`Message` member) the
    adapter normalized the CLI's native record into; :attr:`kind` is derived
    from it. ``model`` / ``timestamp`` are the per-turn envelope the adapter
    could read, or ``None``.
    """

    message: Message
    """The normalized turn content; its class name is the row ``kind``."""

    model: str | None = None
    """Per-turn model, when the CLI surfaced it."""

    timestamp: datetime | None = None
    """When the turn happened, on the CLI clock, when available."""

    @property
    def kind(self) -> Kind:
        """The row discriminator: the message member's class name."""
        return cast(Kind, type(self.message).__name__)


class Adapter(Protocol):
    """One CLI's session-log dialect."""

    name: str
    """Adapter identifier (``"claude"`` / ``"gemini"`` / ``"codex"``)."""

    cli_binary: str
    """The executable we exec (``"claude"`` / ``"gemini"`` / ``"codex"``)."""

    whole_file: bool
    """How the runner drains this adapter's session files.

    ``False`` (claude / codex): the log is append-only JSONL; the runner
    follows a byte offset and hands each new newline-terminated line to
    :meth:`parse`. ``True`` (gemini): the CLI rewrites one JSON object in
    place, so the runner re-reads the whole file on each change and hands
    the entire body to :meth:`parse`.
    """

    def session_dirs(self) -> Iterable[Path]:
        """Directories the wrapped CLI writes session files to.

        An iterable, so a CLI that shards sessions across per-project
        subdirs (claude's ``~/.claude/projects/<hash>/``) can yield each
        one. Paths may not exist before a first hermetic run; the live runner
        creates them before arming its filesystem watches. An empty iterable
        means the adapter has no filesystem capture source.
        """
        ...

    def matches_session_file(self, path: Path) -> bool:
        """Whether ``path`` under ``session_dirs()`` is a log this adapter parses.

        Usually a check on the suffix (``*.jsonl``) and maybe the parent dir.
        """
        ...

    def session_id_from_path(self, path: Path) -> str | None:
        """The CLI's OWN session id for this file, used to correlate a resume.

        Claude names each session file ``<session-id>.jsonl``, so the stem is
        the id that stays stable across ``--resume``; an adapter whose CLI has
        no stable per-session id returns ``None`` (such a CLI's runs are simply
        not resumable via correlation).
        """
        ...

    def parse(self, raw: bytes, *, whole_file: bool) -> Iterable[Event]:
        """Translate one raw chunk into zero or more ``Event``s.

        One transport chunk is not one turn: a single line can carry several
        results (claude's batched parallel ``tool_result`` blocks), and a
        whole-file body carries the latest turn. Yielding an iterable lets the
        adapter map a chunk to however many turns it really contains.

        Args:
          raw: One newline-terminated log line (``whole_file=False``) or the
            entire file body (``whole_file=True``), matching :attr:`whole_file`.
          whole_file: Whether ``raw`` is the full file (vs. one appended line);
            the runner passes the adapter's own :attr:`whole_file` value.

        Yields:
          event: One captured turn. Non-event chunks (blank lines, CLI
            bookkeeping) yield nothing. On malformed JSON the adapter chooses:
            raise to surface a parser bug, or yield nothing for known partial
            writes.

        """
        ...


@runtime_checkable
class StreamAdapter(Adapter, Protocol):
    """An adapter whose session source is the child's own PTY stream.

    The wrapped command writes no session log, so the runner takes the
    binary from the ``--`` args, tails nothing, and instead frames the
    pump's output bytes into lines (``LineCapture``), feeding each
    completed line to :meth:`Adapter.parse` -- here ``raw`` is one output
    line, never a log line, and ``whole_file`` is always ``False``.

    Structurally an :class:`Adapter` whose :attr:`cli_binary` is empty;
    the marker :attr:`stream_source` is what the runner dispatches on
    (``runtime_checkable`` isinstance only sees attribute presence, so a
    plain file adapter never matches). A new stream dialect (say
    JSON-lines to richer typed events) is one subclass overriding
    ``parse`` plus a registry entry.
    """

    stream_source: bool
    """Marker: True on every stream adapter; absent on file adapters."""
