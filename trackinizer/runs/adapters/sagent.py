"""sagent CLI adapter.

Journals live at ``data_dir()/rekursiv-ai/sagent/projects/<project>/<id>/session.jsonl``,
append-only, one IR record a line. An agent a session spawns journals in a folder of
the spawner's own, ``<id>/<label>/session.jsonl``, at any depth, beside the session's
``session.log``.

Reading those lines is ``trackinizer.lib.agent.sessions.sagent``'s job, as for every CLI: one
reader serves capture and conversion. What remains here is where the journals live
and which of them belong to this run.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from trackinizer.lib.agent.sessions import sagent
from trackinizer.lib.agent.sessions.tail import Tail
from trackinizer.lib.userdirs import data_dir
from trackinizer.runs.adapters.custom_types import Subagent
from trackinizer.sagent.session.projects import find_sessions


if TYPE_CHECKING:
    from collections.abc import Iterable

    from trackinizer.types.streams import TraxRecord


__all__ = ["SagentAdapter"]


class SagentAdapter:
    """Locates the ``sagent`` CLI's journals."""

    name: str = "sagent"
    cli_binary: str = "sagent"
    whole_file: bool = False
    # `sagent` exports nothing that names its session to the commands it runs.
    parent_session_env: frozenset[str] = frozenset[str]()
    # Measured on sagent 0.2: no command moves a session to another journal; ``/clear``
    # starts over inside the same one (``agent/types.py``, ``Agent.clear``).
    session_switch_commands: frozenset[str] = frozenset[str]()

    def session_dirs(self) -> Iterable[Path]:
        """Return the directories this CLI writes journals under."""
        return (data_dir() / "rekursiv-ai" / "sagent" / "projects",)

    def matches_session_file(self, path: Path) -> bool:
        """Return whether ``path`` is one of this CLI's journals."""
        return (
            path.name == "session.jsonl"
            and data_dir() / "rekursiv-ai" / "sagent" / "projects" in path.parents
        )

    def session_scope(self) -> Path | None:
        """Return ``None``: the run's own journal is named before sagent starts."""
        # The project folder is sagent's slug of the working directory, which this
        # adapter does not re-derive; the runner names the journal instead.
        return None

    def session_id_from_path(self, path: Path) -> str | None:
        """Return the session's id: its folder below its project's.

        A spawned agent's id carries the folders above it, ``<id>/<label>``, since a
        label repeats across sessions.

        Args:
          path: A journal.

        Returns:
          session_id: The id, or ``None`` for a file that is no journal.

        """
        if not self.matches_session_file(path):
            return None
        root = data_dir() / "rekursiv-ai" / "sagent" / "projects"
        folders = path.parent.relative_to(root).parts[1:]
        return "/".join(folders) or None

    def resumed_session(self, cli_args: tuple[str, ...]) -> str | None:
        """Return the session ``cli_args`` continue; see ``Adapter.resumed_session``.

        ``--resume ID`` takes a prefix of an id, which sagent matches across projects;
        ``--session PATH`` names a journal, continued once it holds records.
        ``--continue``, a bare ``--resume`` and the ``-all`` forms leave sagent to pick.

        Args:
          cli_args: The arguments sagent is spawned with.

        Returns:
          session_id: The id resumed, ``""`` when it cannot be named, or ``None`` for
            a new session.

        """
        for index, arg in enumerate(cli_args):
            flag, joined, value = arg.partition("=")
            following = cli_args[index + 1] if index + 1 < len(cli_args) else ""
            if flag in {"--continue", "--resume-all", "--continue-all"}:
                return ""
            if flag == "--resume":
                prefix = value if joined else following
                return (
                    ""
                    if not prefix or prefix.startswith("-")
                    else self._by_prefix(prefix)
                )
            if flag == "--session":
                journal = Path(value if joined else following).expanduser().absolute()
                if not journal.is_file() or journal.stat().st_size == 0:
                    return None
                return self.session_id_from_path(journal) or ""
        return None

    # sagent's spawned agents journal in a folder of their spawner's, so the folder
    # above names the spawner.
    def subagent(self, path: Path) -> Subagent | None:
        """Return the spawned agent whose journal ``path`` is; ``None`` for a root one.

        Args:
          path: A journal.

        Returns:
          result: The agent and the session that spawned it, by their ids, or ``None``.

        """
        child = self.session_id_from_path(path)
        if child is None or "/" not in child:
            return None
        parent, _, label = child.rpartition("/")
        return Subagent(child=child, parent=parent, note=label)

    def reader(self) -> Tail[TraxRecord]:
        """Return a fresh IR reader for one sagent journal."""
        return Tail(sagent.normalize)

    def _by_prefix(self, prefix: str) -> str:
        """Return the id of the one session whose id starts with ``prefix``, else ``""``."""
        found = find_sessions(prefix, limit=2)
        if len(found) != 1:
            return ""
        return self.session_id_from_path(found[0].path) or ""
