"""Gemini CLI adapter.

Sessions live at
``~/.gemini/tmp/<project-sha256>/chats/session-<timestamp>-<uuid>.json``.
Unlike the others the session is ONE JSON object that gemini rewrites in place
on every update, so there are no appended lines to follow: the runner re-reads
the whole body on each change and feeds that.

Reading it is ``trackinizer.lib.agent.sessions.gemini``'s job, not this module's --
one IR reader serves capture here AND the conversion paths.

See ``docs/cli-scraping-investigation.md`` for the empirical layout.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import hashlib

from trackinizer.lib.agent.sessions import gemini as gemini_ir
from trackinizer.trax.run.adapters.tail import Tail


class GeminiAdapter:
    """Locates the ``gemini`` CLI's whole-file session JSON.

    Stateless: the reading state lives on the reader, which is built per FILE.
    That is what keeps two concurrent session documents from sharing one, which
    a per-adapter counter did not (#498).
    """

    name: str = "gemini"
    cli_binary: str = "gemini"
    whole_file: bool = True

    @property
    def _tmp_dir(self) -> Path:
        # Resolve ``$HOME`` per call, not at import (see ClaudeAdapter).
        return Path.home() / ".gemini" / "tmp"

    def session_dirs(self) -> Iterable[Path]:
        # Returned whether or not it exists yet: the runner MINTS these before
        # arming its watch (see ClaudeAdapter for why withholding an absent
        # root silently disables capture on a first-ever run).
        #
        # The tmp ROOT, not each project's ``chats`` leaf. Gemini shards by
        # project sha and mints ``<sha>/chats`` when it first runs in a
        # workspace -- after the watch is armed for the run being captured. A
        # watch on the leaves existing at arming time cannot adopt a new
        # sibling, so the run would capture nothing silently.
        # ``matches_session_file`` still requires a ``chats/session-*.json``,
        # so widening the watch does not widen what is captured.
        return (self._tmp_dir,)

    def matches_session_file(self, path: Path) -> bool:
        return (
            path.suffix == ".json"
            and path.parent.name == "chats"
            and path.name.startswith("session-")
        )

    def session_scope(self) -> Path | None:
        """The one project directory this run's cwd hashes to.

        Gemini shards ``tmp/`` by the sha256 of the working directory, so the
        run's own subtree is derivable and a concurrent run in another
        workspace lands under a different hash. The path is RESOLVED first:
        the CLI hashes what it resolved at startup, so a symlinked cwd would
        name a directory that never receives a write.
        """
        digest = hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()
        return self._tmp_dir / digest

    def session_id_from_path(self, path: Path) -> str | None:
        # Gemini's ``session-<id>.json`` stem carries an id, but resume
        # correlation isn't wired for it yet; treat as non-resumable for now.
        del path
        return None

    def reader(self) -> Tail:
        """A fresh IR reader for one gemini session document.

        Whole-file: gemini rewrites in place, so each chunk is the entire
        session again rather than a continuation. The runner marks such a
        chunk a restart, so every record lands back on the position it already
        held instead of being appended a second time.
        """
        return Tail(gemini_ir.normalize, whole_file=True)
