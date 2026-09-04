"""The ``Adapter`` protocol each supported CLI implements.

Each CLI ships one ``Adapter`` saying where its session log lives and which
reader turns that log into records. The session runner stays CLI-agnostic and
drives every adapter through this protocol.

The records themselves are the shared IR (``trackinizer.lib.agent.types.sessions``),
not a trackinizer-specific shape: one vocabulary reads a session, stores it,
and writes it back out as any CLI's native format.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from trackinizer.trax.run.adapters.tail import Tail


__all__ = ["Adapter", "Capture", "StreamAdapter"]


type Capture = Literal["pty", "pipe"]
"""How a wrapped child is spawned, and therefore what its capture can say.

Measured on a child printing three lines then one to stderr:

* ``"pty"`` -- one terminal for both output streams, so the kernel interleaves
  them before any reader sees a byte and ``Stderr`` is not recoverable. Lines
  arrive as they are printed (0.01s, 0.31s, 0.61s), because libc line-buffers
  on a tty. The child gets a real terminal, which is what a TUI needs.
* ``"pipe"`` -- three real descriptors, so ``Stdin``, ``Stdout``, and
  ``Stderr`` are all distinguishable. Nothing arrives until 0.91s, when the
  child exits and flushes its block buffer; flushing is the CHILD's business
  (``python -u``, ``stdbuf -oL``) and a child killed first loses what it held.

So the two are not better and worse: one buys separation, the other buys a
terminal and liveness.
"""


class Adapter(Protocol):
    """One CLI's session-log dialect."""

    name: str
    """Adapter identifier (``"claude"`` / ``"gemini"`` / ``"codex"``)."""

    cli_binary: str
    """The executable we exec (``"claude"`` / ``"gemini"`` / ``"codex"``)."""

    whole_file: bool
    """Whether the CLI rewrites its session file rather than appending.

    ``False`` (claude / codex): append-only JSONL, so the runner follows a
    byte offset and feeds each new line. ``True`` (gemini): one JSON object
    rewritten in place, so the runner re-reads the whole body on each change
    and feeds that -- which the reader takes as the whole session again, and
    the chunk is marked a restart so each record lands back on the position it
    already held.
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

    def session_scope(self) -> Path | None:
        """The subtree THIS run's session files land in, if the CLI has one.

        ``session_dirs()`` is deliberately wide -- a root, so a project
        directory minted mid-run is still covered. That width is what makes a
        concurrent run's brand-new file indistinguishable from this run's:
        both appear under the watched root after it was armed, and a
        first-seen-wins rule captures whichever writes first.

        A CLI that derives its directory from the working directory (claude
        encodes the cwd; gemini hashes it) can say so here, and the runner
        drops anything outside it. ``None`` means the CLI offers no such
        signal -- codex shards by DATE, which every concurrent run shares --
        and the run falls back to capturing every new match.
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

    def reader(self) -> Tail:
        """A fresh reader for one of this CLI's session files.

        One per FILE, not per run: a reader carries the position it has read
        to, and a session spans several files (claude splits on compaction,
        codex forks). Sharing one across files would number the second file's
        records after the first's.

        A :class:`Tail` rather than a reader of this module's own: the IR's
        ``normalize`` PULLS lines and the runner PUSHES them, and ``Tail`` is
        that turn -- so capture runs the same reader conversion does, and a
        dialect fix lands in both.
        """
        ...


@runtime_checkable
class StreamAdapter(Adapter, Protocol):
    """An adapter whose session source is the child's own IO streams.

    The wrapped command writes no session log, so the runner takes the
    binary from the ``--`` args, tails nothing, and instead frames the child's
    output bytes into lines (``LineCapture``), feeding each completed line to
    this adapter's reader -- there the "line" is one line of output, never a
    log line.

    Structurally an :class:`Adapter` whose :attr:`cli_binary` is empty; the
    marker :attr:`stream_source` is what the runner dispatches on
    (``runtime_checkable`` isinstance only sees attribute presence, so a plain
    file adapter never matches). A new stream dialect is one subclass with its
    own reader plus a registry entry.
    """

    stream_source: bool
    """Marker: True on every stream adapter; absent on file adapters."""

    capture: Capture
    """How the child is spawned, which decides what can be distinguished.

    Not a preference: a file adapter has no choice at all. Claude and codex
    are TUIs whose injected messages must be indistinguishable from typed
    ones, and owning the pty master is what makes that true -- so ``"pty"`` is
    their only mode and they do not carry this field. A stream adapter names
    one because both are usable and they trade against each other; see
    :data:`Capture`.
    """
