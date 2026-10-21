"""Ask in Chat whether to trust the folder a CLI starts in, instead of hanging.

Claude and codex each ask before working in a folder they have not been trusted in.
Claude asks before any hook runs, so the run never hears it is ready, and the first
line Chat types lands on the dialog: its Enter picks claude's highlighted "No, exit",
or codex's "Trust and continue", which then saves trust nobody agreed to.

So the run reads the CLI's own trust state before it starts (read-only: trust is the
person's decision, and the CLI saves it when they accept), and when the folder is not
trusted it opens a card in Chat through :class:`TrustGate`. Lines wait while the card is
open. The first answer wins: Chat's, typed as the dialog's own keys, or the terminal's,
seen when the CLI says it is ready.

The rules mirror claude 2.1.304 and codex 0.163, read from their code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol

import logging
import os
import re
import threading
import time
import tomllib
import unicodedata
import uuid

from treekle import PlainTree, from_plain, json_loads

from trackinizer.lib.agent.types.sessions import SystemMessage


if TYPE_CHECKING:
    from collections.abc import Mapping


__all__ = [
    "Keys",
    "TrustGate",
    "TrustScreen",
    "claude_remembers",
    "claude_trusted",
    "codex_trusted",
]


_logger = logging.getLogger(__name__)

type _Decision = Literal["allow", "deny", "released"]
type _Resolver = Literal["chat", "terminal", "run"]

# How long claude's highlight takes to move onto "Yes, I trust this folder" before
# Enter confirms it. Enter sent with the Down in one write would confirm "No, exit".
_KEY_GAP_SEC: Final = 0.3


# Neither CLI writes its dialog's words side by side: each is placed by a cursor move
# (codex ``\x1b[5;9H``), so the screen is matched with every CSI sequence made a space.
_CSI: Final = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_CODEX_ASKS: Final = b"Trust this folder?"

# Enough of the output's end to hold a sequence split across two reads.
_SCREEN_TAIL: Final = 256

_RESOLVED_WORDS: Final[Mapping[tuple[_Decision, _Resolver], str]] = {
    ("allow", "chat"): "Trusted from Chat",
    ("allow", "terminal"): "Trusted in the terminal",
    ("deny", "chat"): "Not trusted from Chat: the run stopped",
    ("deny", "terminal"): "Not trusted in the terminal: the run stopped",
    ("released", "run"): "No longer waiting for trust",
}


def claude_trusted(cwd: Path, *, environ: Mapping[str, str], home: Path) -> bool:
    """Whether claude starts in ``cwd`` without asking to trust it.

    A sandboxed claude trusts every folder. Otherwise the folder's project entry in
    claude's global config says, the project being the canonical git root (a worktree's
    main repository) or the folder itself outside a repository. Failing that, a trusted
    folder between ``cwd`` and the root of its repository (or ``/`` outside one) covers
    it.

    Args:
      cwd: The folder claude starts in.
      environ: The environment claude starts with.
      home: The account's home folder.

    Returns:
      trusted: Whether claude records trust there; a config that cannot be read
        records none.

    """
    if environ.get("CLAUDE_CODE_SANDBOXED", ""):
        return True
    projects = _claude_projects(environ=environ, home=home)
    folder = _normal(cwd)
    root = _git_root(folder)
    key = folder if root is None else _canonical_root(root)
    if _claude_accepted(projects, key):
        return True
    for each in (folder, *folder.parents):
        if _claude_accepted(projects, each):
            return True
        if each == root:
            return False
    return False


def claude_remembers(cwd: Path, *, home: Path) -> bool:
    """Whether claude saves trust given in ``cwd``: it keeps the home folder's per run."""
    return _normal(cwd) != _normal(home)


def codex_trusted(cwd: Path, *, environ: Mapping[str, str], home: Path) -> bool:
    """Whether codex has a saved trust decision for ``cwd``, so it asks nothing.

    Args:
      cwd: The folder codex starts in.
      environ: The environment codex starts with.
      home: The account's home folder.

    Returns:
      decided: Whether the folder, its git root or its worktree's main root is saved
        as trusted or untrusted; a config that cannot be read saves neither.

    """
    config = Path(environ.get("CODEX_HOME") or home / ".codex") / "config.toml"
    try:
        projects = from_plain(
            tomllib.loads(config.read_text(encoding="utf-8")).get("projects"),
            dict[str, dict[str, PlainTree]],
            default={},
        )
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
        return False
    folder = _normal(cwd)
    root = _git_root(folder)
    keys = {folder} if root is None else {folder, root, _canonical_root(root)}
    return any(
        projects.get(str(key), {}).get("trust_level") in {"trusted", "untrusted"}
        for key in keys
    )


class Keys(Protocol):
    """What the gate needs of the CLI's terminal."""

    def write(self, data: bytes) -> None:
        """Send raw keys, with no paste and no Enter."""
        ...

    def terminate(self) -> None:
        """Stop the CLI."""
        ...


@dataclass(slots=True, kw_only=True)
class TrustGate:
    """One trust card in Chat: opened while the CLI asks, closed by the first answer.

    Args:
      emit: Takes each record the gate writes, for the run's own part.
      pending: Set while the card is open; the run holds its lines meanwhile.
      ready: Whether the CLI has said it is ready, which it says only once trusted.
      rearm: Restarts the run's readiness fallback, once trust is given from Chat.
      relay: The CLI's terminal, bound once it is built (before any answer arrives).
      sleep: Waits between claude's two keys.
      clock: The time each record is stamped with.

    """

    emit: Callable[[SystemMessage], None]
    pending: threading.Event
    ready: Callable[[], bool]
    rearm: Callable[[], None]
    relay: Keys | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = field(default_factory=lambda: _utc_now)
    _cli: str = field(default="", init=False)
    _id: str | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def open(
        self,
        cli: str,
        folder: str,
        *,
        machine: str | None,
        remembered: bool,
    ) -> None:
        """Open the card, once; a card already open or answered stays as it is.

        Args:
          cli: ``claude`` or ``codex``.
          folder: The folder as Chat shows it.
          machine: The machine the run is on, if it has a name.
          remembered: Whether the CLI saves the trust it is given there.

        """
        with self._lock:
            if self._cli:
                return
            self._cli = cli
            self._id = uuid.uuid4().hex
            self.pending.set()
            self.emit(
                SystemMessage(
                    timestamp=self.clock().isoformat(),
                    subtype="interaction",
                    content=f"{cli} waits for trust in {folder}",
                    extra={
                        "$interaction": {
                            "id": self._id,
                            "type": "trust",
                            "tool": cli,
                            "input": {"folder": folder},
                            "questions": None,
                            "plan": None,
                            "state": "pending",
                            "cli": cli,
                            "folder": folder,
                            "machine": machine,
                            "remembered": remembered,
                        },
                    },
                ),
            )

    def holds(self, prompt_id: object) -> bool:
        """Whether ``prompt_id``, as an answer carries it, names this gate's open card."""
        with self._lock:
            return self._id is not None and self._id == prompt_id

    # Keys, never a paste: a pasted line's Enter picks whatever is highlighted. A key
    # within 150 ms of the dialog opening is refused, which human latency covers. Typed
    # after the terminal answered, the keys land in an empty composer, where a bare
    # Enter does nothing.
    def answer(self, payload: Mapping[str, object]) -> bool:
        """Answer the card with Chat's decision: the dialog's keys, or stop the run.

        Args:
          payload: An ``answer`` message's payload: ``prompt_id`` and ``decision``.

        Returns:
          answered: Whether it named the open card and won it; a second answer, one
            for another card, or one the terminal beat is not.

        """
        decision = payload.get("decision")
        relay = self.relay
        if (
            not self.holds(payload.get("prompt_id"))
            or relay is None
            or decision not in {"allow", "deny"}
        ):
            _logger.info("trax run: a trust answer names no open card: %s", payload)
            return False
        if decision == "deny":
            relay.terminate()
            return self._resolve("deny", by="chat")
        if self._cli == "claude":
            relay.write(b"\x1b[B")
            self.sleep(_KEY_GAP_SEC)
            if self.ready():
                _ = self._resolve("allow", by="terminal")
                return False
        relay.write(b"\r")
        if not self._resolve("allow", by="chat"):
            return False
        self.rearm()
        return True

    def check(self, *, ready: bool) -> None:
        """Close the card as trusted in the terminal once the CLI says it is ready."""
        if ready:
            _ = self._resolve("allow", by="terminal")

    def close(self, *, cli_exited: bool) -> None:
        """Close a card still open as the run ends: refused, if the CLI quit at it."""
        if cli_exited:
            _ = self._resolve("deny", by="terminal")
        else:
            _ = self._resolve("released", by="run")

    def _resolve(self, decision: _Decision, *, by: _Resolver) -> bool:
        """Close the open card and write its end; False when none is open."""
        with self._lock:
            if self._id is None:
                return False
            card, self._id = self._id, None
            self.pending.clear()
            self.emit(
                SystemMessage(
                    timestamp=self.clock().isoformat(),
                    subtype="interaction_resolved",
                    content=_RESOLVED_WORDS[decision, by],
                    extra={
                        "$interaction_resolved": {
                            "id": card,
                            "decision": decision,
                            "by": by,
                        },
                    },
                ),
            )
        return True


class TrustScreen:
    """Watches the CLI's output for codex's trust dialog, and calls ``seen`` once.

    Args:
      seen: Called the first time the screen asks ``Trust this folder?``.

    """

    def __init__(self, seen: Callable[[], None]) -> None:
        self._seen: Callable[[], None] | None = seen
        self._tail = b""

    def output(self, raw: bytes) -> None:
        """Read one chunk of the CLI's output.

        Args:
          raw: The bytes the CLI wrote to its terminal.

        """
        if self._seen is None:
            return
        joined = self._tail + raw
        # Matched as whole words, so ``XTrust this folder?`` is not the dialog.
        words = b" ".join([b"", *_CSI.sub(b" ", joined).split(), b""])
        if b" ".join([b"", _CODEX_ASKS, b""]) in words:
            seen, self._seen = self._seen, None
            seen()
            return
        self._tail = joined[-_SCREEN_TAIL:]


def _utc_now() -> datetime:
    """Return the time now, in UTC."""
    return datetime.now(UTC)


# NFC, absolute and unresolved, as claude keys its projects: a symlinked folder is
# its own project, and ``Path.cwd()`` is already physical.
def _normal(path: Path) -> Path:
    """Return ``path`` absolute and NFC-normalized, with no symlink resolved."""
    return Path(unicodedata.normalize("NFC", os.path.normpath(path.absolute())))


def _claude_projects(
    *,
    environ: Mapping[str, str],
    home: Path,
) -> dict[str, dict[str, PlainTree]]:
    """Return the ``projects`` of claude's global config; empty when unreadable."""
    legacy = home / ".claude" / ".config.json"
    suffix = "-custom-oauth" if environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL", "") else ""
    config = (
        legacy
        if legacy.is_file() and legacy.stat().st_size
        else Path(environ.get("CLAUDE_CONFIG_DIR") or home) / f".claude{suffix}.json"
    )
    try:
        return from_plain(
            json_loads(config.read_text(encoding="utf-8"), dict[str, PlainTree]).get(
                "projects",
            ),
            dict[str, dict[str, PlainTree]],
            default={},
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


def _claude_accepted(
    projects: Mapping[str, Mapping[str, PlainTree]],
    key: Path,
) -> bool:
    """Whether claude's entry for ``key`` says its trust dialog was accepted."""
    return projects.get(str(key), {}).get("hasTrustDialogAccepted") is True


def _git_root(folder: Path) -> Path | None:
    """Return the nearest folder at or above ``folder`` holding ``.git``, if any."""
    return next(
        (each for each in (folder, *folder.parents) if (each / ".git").exists()),
        None,
    )


# A worktree's ``.git`` is a file naming its git directory, whose ``commondir`` names
# the main repository's: the worktree then shares its main checkout's trust.
def _canonical_root(root: Path) -> Path:
    """Return the main repository's root for a worktree root, else ``root`` itself."""
    dot_git = root / ".git"
    if not dot_git.is_file():
        return root
    try:
        gitdir = dot_git.read_text(encoding="utf-8").strip().removeprefix("gitdir:")
        linked = root / gitdir.strip()
        common = linked / (linked / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return root
    return _normal(common).parent
