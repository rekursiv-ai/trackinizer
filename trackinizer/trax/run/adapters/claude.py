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

from collections.abc import Iterable
from pathlib import Path
from typing import Final

import os
import re

from trackinizer.lib.agent.sessions import claude as claude_ir
from trackinizer.trax.run.adapters.tail import Tail


_NOT_KEPT: Final = re.compile(r"[^A-Za-z0-9-]")
"""What claude replaces with a dash when naming a project directory after a
working directory. Everything outside ``[A-Za-z0-9-]``, so ``/home/x/repo``
becomes ``-home-x-repo``."""


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
        return path.suffix == ".jsonl" and path.parent.parent == self._projects_dir

    def session_scope(self) -> Path | None:
        """The one project directory this run's cwd maps to.

        Claude names it after the working directory, replacing every character
        outside ``[A-Za-z0-9-]`` with a dash. Encoding the RESOLVED path is
        load-bearing: the CLI encodes what it resolved at startup, so a
        symlinked or relative cwd would name a directory that never receives a
        write, and the run would capture nothing.
        """
        return self._projects_dir / _NOT_KEPT.sub("-", str(Path.cwd().resolve()))

    def session_id_from_path(self, path: Path) -> str | None:
        """Claude's own session id is the ``<session-id>.jsonl`` filename stem.

        Used to correlate a resumed run to its prior AgentSession (the same id
        names the same claude session across ``--resume``). Returns ``None`` for
        a path that is not one of this adapter's session files.
        """
        if path.suffix != ".jsonl":
            return None
        return path.stem or None

    def reader(self) -> Tail:
        """A fresh IR reader for one claude session file."""
        return Tail(claude_ir.normalize)
