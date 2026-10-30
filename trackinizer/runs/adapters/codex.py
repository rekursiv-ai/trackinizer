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

from pathlib import Path
from typing import TYPE_CHECKING, Final

import os
import re

from treekle import PlainTree, from_plain, json_loads

from trackinizer.lib.agent.sessions import codex
from trackinizer.lib.agent.sessions.tail import Tail
from trackinizer.runs.adapters.custom_types import Subagent


if TYPE_CHECKING:
    from collections.abc import Iterable

    from trackinizer.types.streams import TraxRecord


_ROLLOUT_NAME: Final = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"(?P<session_id>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
)
"""A rollout's name: ``rollout-<ISO>-<uuid>``, the uuid being codex's own id."""

_SESSION_ID: Final = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
"""A session id as codex prints and names rollouts after: lowercase and dashed."""

_VALUED_OPTIONS: Final = frozenset(
    {
        *("-c", "--config", "-m", "--model", "-p", "--profile", "-s", "--sandbox"),
        *("-a", "--ask-for-approval", "-C", "--cd", "-i", "--image", "--add-dir"),
        *("--enable", "--disable", "--remote", "--remote-auth-token-env"),
        "--local-provider",
    },
)
"""Codex 0.163.0's options that take a separate value (``codex --help``)."""


class CodexAdapter:
    """Locates the ``codex`` CLI's rollout JSONL files.

    Stateless: the per-turn reading state (codex names its model once per
    turn, on a separate ``turn_context`` line, and later items inherit it)
    belongs to the reader, which is built per FILE rather than per run.
    """

    name: str = "codex"
    cli_binary: str = "codex"
    whole_file: bool = False
    parent_session_env: frozenset[str] = frozenset[str]()
    # Measured on 0.163.0: ``/new`` (and ``/clear``) start a new rollout,
    # ``/resume`` switches to another, and ``/fork`` continues in a new one.
    session_switch_commands: frozenset[str] = frozenset(
        {"new", "clear", "resume", "fork"},
    )

    @property
    def _sessions_dir(self) -> Path:
        # Resolve per call, not at import (see ClaudeAdapter). ``$CODEX_HOME``
        # is where codex itself keeps its config root -- hermetic launchers
        # point it at a throwaway dir -- so honor it, else ``~/.codex``.
        home = os.environ.get("CODEX_HOME", "")
        if home:
            return Path(home) / "sessions"
        return Path.home() / ".codex" / "sessions"  # noqa: TID251 -- vendor fixed path, not ours (AGENTS.md rule 3)  # house-ignore[xdg-literal] -- Vendor CLI's fixed home path, not ours (AGENTS.md rule 3).

    def session_dirs(self) -> Iterable[Path]:
        """Return the directories this CLI writes sessions under."""
        # Codex shards by Y/M/D; returning the root lets the runner glob
        # recursively, so older days still get captured if they keep growing.
        return (self._sessions_dir,)

    def matches_session_file(self, path: Path) -> bool:
        """Return whether ``path`` is one of this CLI's session files."""
        return (
            path.suffix == ".jsonl"
            and path.name.startswith("rollout-")
            and self._sessions_dir in path.parents
        )

    def session_scope(self) -> Path | None:
        """Return the scope key a session file is filed under.

        Returns:
          result: The Path | None.

        """
        # Codex shards by DATE, not canvas, so concurrent runs share this
        # directory. The runner claims the child-held rollout's session ID.
        return None

    def session_id_from_path(self, path: Path) -> str | None:
        """Return the uuid ``codex resume`` takes, read off the rollout's name.

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
        return from_plain(found["session_id"], str)

    def resumed_session(self, cli_args: tuple[str, ...]) -> str | None:
        """Return the session ``cli_args`` continue; see ``Adapter.resumed_session``.

        ``codex [exec] resume <SESSION_ID>`` names it; ``--last``, the picker
        and a session NAME leave codex to choose. ``fork`` starts a new rollout.

        Args:
          cli_args: The arguments codex is spawned with.

        Returns:
          session_id: The id resumed, ``""`` when codex picks the session, or
            ``None`` for a new one.

        """
        words = _positionals(cli_args)
        if words[:1] == ("exec",):
            words = words[1:]
        if words[:1] != ("resume",):
            return None
        named = words[1] if len(words) > 1 and "--last" not in cli_args else ""
        return named if _SESSION_ID.fullmatch(named) else ""

    # Measured on codex 0.163.0: a spawned agent is a thread with a rollout of its own,
    # whose opening ``session_meta`` names the thread that spawned it under
    # ``source.subagent.thread_spawn``. Only that first line declares it.
    def subagent_of(self, path: Path) -> Subagent | None:
        """Return the spawned thread whose rollout ``path`` is; ``None`` for any other.

        Args:
          path: A rollout file.

        Returns:
          result: The thread and the one that spawned it, by codex's ids, or ``None``.

        """
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                opening = json_loads(handle.readline(), dict[str, PlainTree])
        except (OSError, ValueError):
            return None
        payload = _object(opening.get("payload"))
        spawn = _object(
            _object(_object(payload.get("source")).get("subagent")).get("thread_spawn"),
        )
        parent = _text(spawn.get("parent_thread_id"))
        if opening.get("type") != "session_meta" or not parent:
            return None
        name, where = _text(spawn.get("agent_nickname")), _text(spawn.get("agent_path"))
        return Subagent(
            child=self.session_id_from_path(path)
            or _text(payload.get("id"))
            or path.stem,
            parent=parent,
            note=f"{name} ({where})" if name and where else name or where,
        )

    def reader(self) -> Tail[TraxRecord]:
        """Return a fresh IR reader for one codex rollout file."""
        return Tail(codex.normalize)


# A subcommand is the first word that is neither a flag nor a flag's value, so a value
# that happens to read ``resume`` (``-m resume``) is not mistaken for one.
def _positionals(cli_args: tuple[str, ...]) -> tuple[str, ...]:
    """Return the words of ``cli_args`` that are not options or option values."""
    words: list[str] = []
    consumed = False
    for arg in cli_args:
        if consumed:
            consumed = False
        elif arg.startswith("-"):
            consumed = arg in _VALUED_OPTIONS
        else:
            words.append(arg)
    return tuple(words)


def _object(value: PlainTree) -> dict[str, PlainTree]:
    """Return ``value`` when it is a JSON object, else an empty one."""
    return from_plain(value, dict[str, PlainTree]) if isinstance(value, dict) else {}


def _text(value: PlainTree) -> str:
    """Return ``value`` when it is text, else ``""``."""
    return value if isinstance(value, str) else ""
