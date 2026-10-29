"""Tell a slash command typed in Chat from a line, by the provider that answers it.

A Chat post is typed into a ``claude`` or ``codex`` run's terminal. A line goes in
with its sender and the canvas, so the CLI reads ``/goal x`` there as a prompt. A
command goes in bare, so the CLI runs it as one of its own built-ins. The server
classifies each line with :func:`classify` before it queues it: a command is queued
as kind ``command``, a settings change as kind ``settings``, a refusal is
answered at once and nothing is queued, and any other text stays a line.

Only a known built-in of either CLI (:data:`KNOWN_VERBS`) at the very start of the
text is a command, so ``/etc/hosts is broken`` and ``/tmp is full`` stay lines. Of
those, Chat runs only the ones that need no terminal: ``/goal``, claude's
``/compact``, ``/rename``, claude's ``/rc`` (``/remote-control``), and ``/model
<name>`` or ``/effort <level>``, which are queued as a ``settings`` post rather than
typed. Every other one opens a menu, changes settings or switches the session, which
the person does at the terminal.

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import re

from trackinizer.sagent.providers import anthropic, openai
from trackinizer.wire.wire_science_chat import ChatModel, ChatModels


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.sagent.model.capability import ModelCapability
    from trackinizer.sagent.providers.table import ModelTable
    from trackinizer.wire.wire_science_chat import ChatProvider


__all__ = [
    "COMMAND_CHARS",
    "KNOWN_VERBS",
    "Classified",
    "Command",
    "Line",
    "Refused",
    "Settings",
    "chat_models",
    "classify",
]


COMMAND_CHARS: Final = 800
"""The longest command Chat types. claude turns a longer paste into a
``[Pasted text #N]`` pill and then does not run it as a command; codex does the same
over 1,000."""

KNOWN_VERBS: Final = frozenset(
    {
        "goal",
        "compact",
        "clear",
        "new",
        "model",
        "effort",
        "permissions",
        "resume",
        "status",
        "config",
        "exit",
        "quit",
        "init",
        "review",
        "plan",
        "fork",
        "rename",
        "rc",
        "remote-control",
        "diff",
        "mcp",
        "agents",
        "cost",
        "help",
        "hooks",
        "skills",
        "usage",
        "export",
        "copy",
        "theme",
        "vim",
        "ide",
        "side",
        "btw",
        "rewind",
        "undo",
        "cd",
        "pwd",
        "stop",
        "ps",
        "login",
        "logout",
        "memory",
        "context",
        "fast",
        "doctor",
        "feedback",
        "statusline",
    },
)
"""The built-in slash commands of claude and codex. A custom command or skill is sent
as a line."""


_COMMAND: Final = re.compile(
    r"/(?P<verb>[A-Za-z][A-Za-z0-9_-]*)(?:[ \t]+(?P<args>.*))?",
)
"""A command's line: a slash, its verb, then its arguments after a space or tab."""

_GOAL_CLEARS: Final = frozenset({"clear", "stop", "off", "reset", "none", "cancel"})
"""The words claude clears a goal with; codex knows ``clear`` alone."""

_MENU: Final = (
    "Chat cannot run /{verb}: it opens a menu, changes settings or switches the "
    "session. Use it in the run's own terminal."
)

_SETTING_CHARS: Final = {"model": 120, "effort": 20}
"""The longest argument of ``/model`` and ``/effort``: the limits of ``ChatSettings``."""

# The options are the catalog's roles, in the order best, default, everyday,
# utility. The levels are ``Effort`` in ``sagent/harness/types.py``, which both
# CLIs take.
_ROLES: Final = ("best", "default", "everyday", "utility")
_EFFORTS: Final = "low, medium, high, xhigh, max"
_LEVELS: Final = ("low", "medium", "high", "xhigh", "max")
"""The levels ``/effort`` takes, lowest first: ``Effort`` in ``lib/agent/harness/types.py``,
which this package's export does not ship."""
_BARE_MODEL: Final = (
    "Chat can't open the model menu. Type /model <name> to switch, for example "
    "/model {example}. Options: {options}."
)
_BARE_EFFORT: Final = (
    "Chat can't open the effort menu. Type /effort <level> to switch, for example "
    f"/effort high. Options: {_EFFORTS}."
)
_ONE_TOKEN: Final = {
    "model": "/model takes one model name of at most 120 characters, as in /model opus",
    "effort": "/effort takes one level of at most 20 characters, as in /effort high",
}

_GOAL_BARE: Final = "Chat shows the goal above the box; /goal clear stops it"
_GOAL_EDIT: Final = (
    "Chat cannot run /goal edit: it opens an editor. Use it in the run's own terminal."
)
_RENAME_BARE: Final = "codex asks for the name in a dialog; type /rename <name>"
# A chat's run drives its own ``codex app-server`` on stdio, which offers no request
# that turns remote control on.
_NO_RC: Final = (
    "codex has no remote control for a chat's run: codex remote-control serves "
    "codex's background daemon, not this run"
)

_TABLE: Final[Mapping[str, Mapping[ChatProvider, str | None]]] = {
    "goal": {"claude": None, "codex": None},
    # Claude opens a modal panel for a bare /goal.
    "goal bare": {"claude": _GOAL_BARE, "codex": _GOAL_BARE},
    "goal clear": {"claude": None, "codex": None},
    # Claude would take the word as the goal's condition.
    "goal pause": {
        "claude": "claude has no goal pause; /goal clear stops it",
        "codex": None,
    },
    "goal edit": {"claude": _GOAL_EDIT, "codex": _GOAL_EDIT},
    # Claude queues /compact until its turn ends; codex refuses it during a turn.
    "compact": {
        "claude": None,
        "codex": "codex runs /compact only between turns; attach to run it",
    },
    "rename": {"claude": None, "codex": None},
    # Claude names the session from the conversation; codex opens a dialog.
    "rename bare": {"claude": None, "codex": _RENAME_BARE},
    # Claude's run starts Remote Control by a control request, as a line cannot.
    "rc": {"claude": None, "codex": _NO_RC},
    "remote-control": {"claude": None, "codex": _NO_RC},
}
"""What each CLI does with each form of a command Chat may run: ``None`` types it, and
text refuses it. A known verb with no row is refused as :data:`_MENU`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Line:
    """Text that is a person's line, delivered with its sender and the canvas."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Command:
    """A slash command the CLI runs, typed bare."""

    text: str
    """What is typed: the verb lowercased, then its arguments, as ``/goal ship it``."""

    verb: str
    args: str
    """Everything after the verb, trimmed; empty for none."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Refused:
    """A known command Chat does not send to this provider, and why, for the person."""

    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """A ``/model`` or ``/effort`` with its argument, queued as a ``settings`` post."""

    text: str
    """What was typed, normalized: ``/model opus`` or ``/effort high``."""

    model: str | None
    effort: str | None


type Classified = Line | Command | Refused | Settings
"""What :func:`classify` makes of a typed text."""


def chat_models() -> ChatModels:
    """Return the models Chat offers each CLI, by the catalog's roles, best first.

    Returns:
      models: Claude's by family alias and codex's by wire id, as ``/model`` takes
        each, with the levels ``/effort`` takes with it.

    """
    return ChatModels(claude=_offered("claude"), codex=_offered("codex"))


def classify(text: str, provider: ChatProvider) -> Classified:
    """Say whether ``text`` typed in a chat answered by ``provider`` is a command.

    Args:
      text: What the person typed.
      provider: Who answers the chat: the chat's own provider, not the poster's.

    Returns:
      classified: ``Command`` for a command the provider runs from Chat,
        ``Settings`` for ``/model <name>`` or ``/effort <level>`` (one token; the
        level lowercased), ``Refused`` for a known command it does not run, a bare
        ``/model`` or ``/effort`` included, and ``Line`` for any other text.

    """
    lines = text.rstrip().splitlines()
    first = _COMMAND.fullmatch(lines[0]) if lines else None
    if first is None or first["verb"].lower() not in KNOWN_VERBS:
        return Line()
    if provider == "assistant":
        return Refused(
            reason="The assistant runs no slash commands. /goal works in a chat on "
            "claude or codex: pick one in the provider card.",
        )
    if len(lines) > 1 or len(lines[0]) > COMMAND_CHARS:
        return Refused(
            reason=f"A command is one line of at most {COMMAND_CHARS} characters; "
            "put a longer goal in a file and write /goal follow <path>",
        )
    verb = first["verb"].lower()
    if verb in _SETTING_CHARS:
        return _setting(verb, args=(first["args"] or "").strip(), provider=provider)
    row, args = _row(verb, args=(first["args"] or "").strip())
    reason = _TABLE.get(row, {}).get(provider, _MENU.format(verb=verb))
    if reason is not None:
        return Refused(reason=reason)
    return Command(text=f"/{verb} {args}" if args else f"/{verb}", verb=verb, args=args)


def _row(verb: str, *, args: str) -> tuple[str, str]:
    """Return a command's row of the table, and its arguments as they are typed."""
    if verb == "rename" and not args:
        return "rename bare", args
    if verb != "goal":
        return verb, args
    word = args.lower()
    if not args:
        return "goal bare", args
    if word in _GOAL_CLEARS:
        return "goal clear", "clear"
    if word in {"pause", "resume"}:
        return "goal pause", word
    if word == "edit":
        return "goal edit", word
    return "goal", args


def _setting(verb: str, *, args: str, provider: ChatProvider) -> Settings | Refused:
    """Return ``/model`` or ``/effort`` with its one argument, or why it is refused."""
    if not args:
        return Refused(
            reason=_BARE_EFFORT if verb == "effort" else _bare_model(provider),
        )
    if len(args.split()) > 1 or len(args) > _SETTING_CHARS[verb]:
        return Refused(reason=_ONE_TOKEN[verb])
    if verb == "model":
        return Settings(text=f"/model {args}", model=args, effort=None)
    return Settings(text=f"/effort {args.lower()}", model=None, effort=args.lower())


def _bare_model(provider: ChatProvider) -> str:
    """Return why a bare ``/model`` is refused, with the names it takes."""
    table = _table(provider)
    default = table["default"]
    # Two roles may name one model, which is listed once.
    options = ", ".join(dict.fromkeys(_name(provider, table[role]) for role in _ROLES))
    if provider == "claude":
        options += f", or a full id such as {default.wire_model_id or default.model_id}"
    return _BARE_MODEL.format(example=_name(provider, default), options=options)


def _offered(provider: ChatProvider) -> list[ChatModel]:
    """Return the models of the provider's roles, each once, as ``/model`` takes it."""
    table = _table(provider)
    default = _name(provider, table["default"])
    offered: dict[str, ChatModel] = {}
    for role in _ROLES:
        row = table[role]
        name = _name(provider, row)
        family, _, version = row.model_id.rpartition("-")
        offered.setdefault(
            name,
            ChatModel(
                id=name,
                label=f"{family.capitalize()} {version}",
                efforts=[level for level in _LEVELS if level in row.thinking_effort],
                default=name == default,
            ),
        )
    return list(offered.values())


def _table(provider: ChatProvider) -> ModelTable:
    """Return the models the provider's CLI serves: its own catalog section."""
    match provider:
        case "claude":
            return anthropic.models("anthropic-cli")
        case "codex":
            return openai.models("openai-sub")
        case "assistant":
            raise ValueError("The assistant takes no /model.")


def _name(provider: ChatProvider, row: ModelCapability) -> str:
    """Return what ``/model`` takes for ``row``: claude's family alias, codex's id."""
    if provider == "claude":
        return row.model_id.rpartition("-")[0]
    return row.wire_model_id or row.model_id
