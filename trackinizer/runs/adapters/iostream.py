"""The IO-stream contract: wrap any binary, capture its lines verbatim.

Registry key ``sh``: the runner's ``--as alice sh -- CMD [ARGS...]``. The wrapped
command has no session log to tail, so the process's own IO is the source of
truth: each completed line becomes one record. Semantic parsing belongs to the
wrapped script, not trax -- the contract is line-delimited UTF-8 text in both
directions (stdin lines arrive via the inbound poller's injection; output lines
are captured verbatim).

The child runs on a pty, like every other adapter, so every captured line is a
``Stdout``: the kernel merges stderr into the one terminal before trax sees a
byte. Pipes would keep the streams apart, and were measured against it: on a
child printing three lines then one to stderr, the pty delivered each line as
printed (0.01s, 0.31s, 0.61s) while the pipes delivered nothing until the
child exited at 0.91s and flushed its block buffer -- and a child killed first
loses what it held. Liveness wins, and the pty is also what lets a
``--detach`` host serve the child to a viewer. A command that needs stderr
apart redirects it itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.lib.agent.sessions.tail import Tail
from trackinizer.types.line_capture import normalize


if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from trackinizer.runs.adapters.custom_types import Subagent
    from trackinizer.types.streams import TraxRecord


__all__ = ["IOStreamAdapter"]


class IOStreamAdapter:
    """Wrap an arbitrary binary; its own IO streams are the session source.

    Every file-tailing method is vacuous -- there is no log. The runner
    detects the empty :attr:`cli_binary`, takes the command from the ``--``
    args, and frames the pty's output with a :class:`LineCapture` that feeds
    each completed line to this adapter's normalizer.

    The READER is the configurable seam: a new stream dialect (say JSON-lines
    into richer records) is this class returning a different one, with its own
    ``name`` in the runner's adapter table. The framing (chunk buffering, the
    line-length clamp) stays in :class:`LineCapture`, shared by every stream
    adapter.
    """

    name: str = "sh"
    cli_binary: str = ""
    whole_file: bool = False
    parent_session_env: frozenset[str] = frozenset[str]()
    session_switch_commands: frozenset[str] = frozenset[str]()
    stream_source: bool = True

    def session_dirs(self) -> Iterable[Path]:
        """Return the directories this CLI writes sessions under."""
        return ()

    def matches_session_file(self, path: Path) -> bool:
        """Return whether ``path`` is one of this CLI's session files."""
        del path
        return False

    def session_scope(self) -> Path | None:
        """Return the scope key a session file is filed under.

        Returns:
          result: The Path | None.

        """
        # The capture source is the child's own IO; no session file to scope.
        return None

    def session_id_from_path(self, path: Path) -> str | None:
        """Return the session id encoded in ``path``.

        Args:
          path: File path to extract session ID from.

        Returns:
          result: Session ID string, or None if not present in this adapter.

        """
        del path
        return None

    def resumed_session(self, cli_args: tuple[str, ...]) -> str | None:
        """Return ``None``: a wrapped command keeps no session to continue."""
        del cli_args
        return None

    def subagent_of(self, path: Path) -> Subagent | None:
        """Return ``None``: a stream has no sub-agents."""
        del path
        return None

    def reader(self) -> Tail[TraxRecord]:
        """Return a fresh IR reader for one captured stream.

        Every line becomes a stream record: the contract is verbatim
        line-delimited text, so nothing is parsed and nothing can be
        misparsed. Semantic structure belongs to the wrapped script; WHICH
        stream carried the line is the one thing capture knows and the file
        does not, which is why the runner tags it rather than this reader.

        Returns:
          result: Tail reader consuming line-delimited plaintext.

        """
        return Tail(normalize)
