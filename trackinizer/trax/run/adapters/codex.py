"""Codex CLI adapter.

Rollouts live at
``$CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-<ISO>-<uuid-v7>.jsonl``
(``~/.codex`` when ``$CODEX_HOME`` is unset), append-only JSONL.

Reading those lines is ``trackinizer.lib.agent.sessions.codex``'s job, not this
module's: one IR reader serves capture here AND the conversion/resume paths,
so a dialect change is fixed once. What remains here is where the rollouts
live and which of them belong to THIS run.

See ``docs/cli-scraping-investigation.md`` for the empirical layout.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Final

import os
import re

from trackinizer.lib.agent.sessions import codex as codex_ir
from trackinizer.lib.custom_json import StrCodec
from trackinizer.trax.run.adapters.tail import Tail


_ROLLOUT_NAME: Final = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"(?P<session_id>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)
"""A rollout's name: ``rollout-<ISO>-<uuid>``, the uuid being codex's own id."""


class CodexAdapter:
    """Locates the ``codex`` CLI's rollout JSONL files.

    Stateless: the per-turn reading state (codex names its model once per
    turn, on a separate ``turn_context`` line, and later items inherit it)
    belongs to the reader, which is built per FILE rather than per run.
    """

    name: str = "codex"
    cli_binary: str = "codex"
    whole_file: bool = False

    @property
    def _sessions_dir(self) -> Path:
        # Resolve per call, not at import (see ClaudeAdapter). ``$CODEX_HOME``
        # is where codex itself keeps its config root -- hermetic launchers
        # point it at a throwaway dir -- so honor it, else ``~/.codex``.
        home = os.environ.get("CODEX_HOME")
        return (Path(home) if home else Path.home() / ".codex") / "sessions"

    def session_dirs(self) -> Iterable[Path]:
        # Codex shards by Y/M/D; returning the root lets the runner glob
        # recursively, so older days still get captured if they keep growing.
        return (self._sessions_dir,)

    def matches_session_file(self, path: Path) -> bool:
        return (
            path.suffix == ".jsonl"
            and path.name.startswith("rollout-")
            and self._sessions_dir in path.parents
        )

    def session_scope(self) -> Path | None:
        # Codex shards by DATE, not by workspace, so every concurrent run
        # writes into the same ``<Y>/<M>/<D>/`` directory. There is nothing in
        # the layout that distinguishes this run's rollout from a sibling's,
        # so the runner keeps its capture-every-new-match fallback.
        return None

    def session_id_from_path(self, path: Path) -> str | None:
        """The uuid ``codex resume`` takes, read off the rollout's name.

        ``rollout-<ISO>-<uuid>.jsonl``, and the launch line repeats that uuid
        as ``payload.id`` -- so the name is authoritative, not a guess. The
        pattern is ANCHORED on the whole stem because the ISO timestamp is
        itself digits and dashes: an unanchored search for a uuid-shaped run
        would match inside it.

        Args:
          path: A rollout file.

        Returns:
          session_id: The uuid, or ``None`` when the name carries none.

        """
        found = _ROLLOUT_NAME.fullmatch(path.stem)
        if found is None:
            return None
        # Typeshed types a match group ``str | Any`` -- a group may be optional
        # in general. This one is not: it is unconditional in the pattern, so
        # the match cannot succeed without it.
        return StrCodec.coerce(found["session_id"])

    def reader(self) -> Tail:
        """A fresh IR reader for one codex rollout file."""
        return Tail(codex_ir.normalize)
