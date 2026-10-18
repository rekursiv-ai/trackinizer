"""Claude Code adapter.

Sessions live at ``$CLAUDE_CONFIG_DIR/projects/<path-hash>/<session-id>.jsonl``
(``~/.claude`` when ``$CLAUDE_CONFIG_DIR`` is unset), append-only, one JSON
object per line. A top-level ``type`` discriminates;
for ``user`` / ``assistant`` lines the real category lives in
``message.content``: a bare string is a user prompt, a ``content[]`` block
list carries ``thinking`` / ``text`` / ``tool_use`` (assistant) or
``tool_result`` (a user line echoing tool output).

Reading those lines is ``trackinizer.lib.agent.sessions.claude``'s job, not this
module's: one IR reader serves capture here AND the conversion/resume paths,
so a dialect change is fixed once. What remains here is where the files live,
which of them belong to THIS run, and how to recognise the CLI's own id.

See ``docs/cli-scraping-investigation.md`` for the empirical layout.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Final

import os
import re

from trackinizer.lib.agent.sessions import claude
from trackinizer.lib.agent.sessions.tail import Tail


if TYPE_CHECKING:
    from collections.abc import Iterable

    from trackinizer.types.streams import TraxRecord


_NOT_KEPT: Final = re.compile(r"[^A-Za-z0-9-]")
"""What claude replaces with a dash when naming a project directory after a
working directory. Everything outside ``[A-Za-z0-9-]``, so ``/home/x/repo``
becomes ``-home-x-repo``."""

_SESSION_ID: Final = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
"""A session id as claude looks one up: exactly, so lowercase and dashed. Any other
``--resume`` value -- uppercase, dashless, ``=<id>`` -- is a picker search term."""


class ClaudeAdapter:
    """Reads the ``claude`` CLI's per-project session JSONL files."""

    name: str = "claude"
    cli_binary: str = "claude"
    whole_file: bool = False
    # Measured: launched from inside a Claude Code session, the wrapped claude
    # inherited ``CLAUDE_CODE_CHILD_SESSION``, saved no transcript, and the run
    # captured nothing while the model answered. The other two name the
    # launching session itself, which a new top-level session is not.
    parent_session_env: frozenset[str] = frozenset(
        {"CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"},
    )
    # Measured on 2.1.302: ``/clear`` (aliases ``/reset``, ``/new``) and ``/branch``
    # mint a new session id, ``/resume`` switches to another session's file, and
    # ``/fork`` continues in a background session under its own id.
    session_switch_commands: frozenset[str] = frozenset(
        {"clear", "reset", "new", "resume", "branch", "fork"},
    )

    @property
    def _projects_dir(self) -> Path:
        # Resolve per call, not at import: a test (or a run under a switched
        # env) must see the current value, not one frozen when the module first
        # loaded. ``$CLAUDE_CONFIG_DIR`` is where claude itself keeps its
        # config root -- hermetic launchers point it at a throwaway dir -- so
        # honor it, else ``~/.claude``.
        root = os.environ.get("CLAUDE_CONFIG_DIR", "")
        if root:
            return Path(root) / "projects"
        return Path.home() / ".claude" / "projects"  # noqa: TID251 -- vendor fixed path, not ours (AGENTS.md rule 3)  # house-ignore[xdg-literal] -- Vendor CLI's fixed home path, not ours (AGENTS.md rule 3).

    def session_dirs(self) -> Iterable[Path]:
        """Return the directories this CLI writes sessions under."""
        # Returned whether or not it exists yet: the runner MINTS these before
        # arming its watch (``_prepare_session_dirs``), so an adapter that
        # withheld an absent root would leave the runner nothing to create --
        # and a first-ever run, whose root is absent by definition, would arm
        # no watch and capture nothing while logging nothing.
        #
        # The projects ROOT, not the per-project subdirectories under it.
        # Claude shards sessions by hashed cwd and mints that directory when
        # it first runs in a workspace -- which, for the run being captured,
        # is after the watch is already armed. A watch on the subdirectories
        # existing at arming time cannot adopt a new sibling (recursion only
        # descends INTO a watched tree), so the run would capture nothing and
        # say nothing. Watching the root covers every project, present and
        # future; ``matches_session_file`` still scopes capture to a
        # ``<project>/<session>.jsonl``, so nothing extra is swept in.
        return (self._projects_dir,)

    def matches_session_file(self, path: Path) -> bool:
        """Return whether ``path`` is one of this CLI's session files."""
        return path.suffix == ".jsonl" and path.parent.parent == self._projects_dir

    def session_scope(self) -> Path | None:
        """Return the one project directory this run's cwd maps to.

        Claude names it after the working directory, replacing every character
        outside ``[A-Za-z0-9-]`` with a dash. Encoding the RESOLVED path is
        load-bearing: the CLI encodes what it resolved at startup, so a
        symlinked or relative cwd would name a directory that never receives a
        write, and the run would capture nothing.

        Returns:
          result: The Path | None.

        """
        return self._projects_dir / _NOT_KEPT.sub("-", str(Path.cwd().resolve()))

    def session_id_from_path(self, path: Path) -> str | None:
        """Claude's own session id is the ``<session-id>.jsonl`` filename stem.

        Used to correlate a resumed run to its prior AgentSession (the same id
        names the same claude session across ``--resume``).

        Args:
          path: File path (typically *.jsonl).

        Returns:
          result: Session id stem from the filename, or None if not a .jsonl file.

        """
        if path.suffix != ".jsonl":
            return None
        return path.stem or None

    def resumed_session(self, cli_args: tuple[str, ...]) -> str | None:
        """Return the session ``cli_args`` continue; see ``Adapter.resumed_session``.

        Read as claude's own parser reads them: ``-r`` takes the rest of its
        short-flag cluster (``-r<id>``) or else the next token unless that is a
        flag, and ``--fork-session`` turns any resume into a new session.

        Args:
          cli_args: The arguments claude is spawned with.

        Returns:
          session_id: The id resumed, ``""`` when claude picks the session, or
            ``None`` for a new one.

        """
        options = cli_args[: cli_args.index("--")] if "--" in cli_args else cli_args
        if "--fork-session" in options:
            return None
        resumed: str | None = None
        consumed = False
        # Paired with the token after it; the last is paired with a lone dash,
        # which reads as a flag, so a trailing ``-r`` takes no value.
        for arg, following in pairwise((*options, "-")):
            if consumed:
                consumed = False
            elif arg == "--continue":
                resumed = ""
            elif arg == "--resume":
                consumed = following[:1] != "-"
                resumed = _session_named(following if consumed else "")
            elif arg.startswith("--resume="):
                resumed = _session_named(arg.partition("=")[2])
            elif arg[:1] == "-" and arg[1:2] not in {"", "-"}:
                resumed, consumed = _short_cluster(arg[1:], following, resumed=resumed)
        return resumed

    def reader(self) -> Tail[TraxRecord]:
        """Return a fresh IR reader for one claude session file."""
        return Tail(claude.normalize)


# Commander semantics: an option that takes a value takes the REST of its cluster
# (``-dr`` is ``--debug r``, not ``--debug --resume``), else the next token -- always
# for a required value (``-n``), and for an optional one (``-d``, ``-r``, ``-w``)
# only when that token is not itself a flag.
def _short_cluster(
    letters: str,
    following: str,
    *,
    resumed: str | None,
) -> tuple[str | None, bool]:
    """Fold one short-flag cluster into ``resumed``; say if it took ``following``."""
    for offset, letter in enumerate(letters, start=1):
        if letter == "c":
            resumed = ""
        elif letter in "dnrw":
            attached = letters[offset:]
            takes_next = not attached and (letter == "n" or following[:1] != "-")
            if letter == "r":
                resumed = _session_named(attached or (following if takes_next else ""))
            return resumed, takes_next
    return resumed, False


def _session_named(value: str) -> str:
    """Return ``value`` when it is a session id claude looks up, else ``""``."""
    return value if _SESSION_ID.fullmatch(value) else ""
