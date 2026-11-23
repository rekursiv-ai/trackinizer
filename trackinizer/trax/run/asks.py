"""The ask model every Chat agent shares, and the driver contract that produces it.

An agent asks the person to choose: allow a command, answer a question, approve a plan.
Each ask is a record in the chat's AgentSession, so AgentSession is the brain: a card in
Chat, a badge, and a reattach all read the same two records.

- :func:`opened_record` writes ``extra["$interaction"]``, which opens a card.
- :func:`resolved_record` writes ``extra["$interaction_resolved"]``, which closes it.
- :func:`answer_from_payload` reads what Chat sends back: ``{prompt_id, option, ...}``,
  or the older ``{prompt_id, decision}``.

Both records stay a superset of the ones :mod:`.prompt_bridge` wrote before this module:
the fields the web card reads (``type``, ``tool``, ``input``, ``questions``, ``plan``,
``decision``) keep their meaning, and the new fields ride beside them.

A :class:`Driver` is one agent CLI run headless: it starts the CLI, sends it the person's
lines, and streams back :class:`Ask`, :class:`Resolved` and :class:`StateChanged`. The
run turns those into the records above.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol

from treekle import PlainTree, from_plain

from trackinizer.lib.agent.types.sessions import SystemMessage


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime
    from pathlib import Path


__all__ = [
    "Answer",
    "Ask",
    "AskKind",
    "AskProvider",
    "Decision",
    "Driver",
    "Option",
    "OptionKind",
    "Resolved",
    "Resolver",
    "RunState",
    "StateChanged",
    "answer_from_payload",
    "opened_record",
    "resolved_record",
]


type AskProvider = str
"""``claude`` or ``codex`` for a CLI run. A native agent names itself: this package ships
to readers who have no such agent, so it cannot list one."""

type AskKind = Literal[
    "permission",
    "command",
    "file_change",
    "permissions",
    "question",
    "plan",
    "elicitation",
    "trust",
    "dialog",
]
type OptionKind = Literal[
    "allow_once",
    "allow_session",
    "allow_always",
    "deny",
    "deny_stop",
]
type Resolver = Literal["chat", "terminal", "run", "timeout", "released"]
"""Who ended an ask: ``released`` when the run ended or went offline with it open."""

type RunState = Literal["idle", "running", "requires_action"]

type Decision = Literal["allow", "deny", "answered", "released"]
"""What the web card says an ask ended in; it reads no other word."""

type _CardType = Literal["permission", "question", "plan", "trust"]


_CARD_TYPES: Final[Mapping[AskKind, _CardType]] = {
    "question": "question",
    "plan": "plan",
    "trust": "trust",
}
"""The ``type`` the web card knows, for the kinds it has a body for; every other kind is
asked as a permission."""

_WORDS: Final[Mapping[str, str]] = {
    "allow": "Allowed",
    "deny": "Denied",
    "answered": "Answered",
    "released": "Released",
    "chat": "from Chat",
    "terminal": "in the terminal",
    "run": "by the run",
    "timeout": "on timeout",
}
"""How a card's end reads to a person, by decision and by who made it."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Option:
    """One choice the agent offers on an ask, in the agent's own words."""

    id: str
    """The agent's id for the choice; an answer names it."""

    label: str
    kind: OptionKind
    """What the choice does, which is what a button's look and a decision follow."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Ask:
    """One thing an agent waits for the person to choose.

    Every field after ``kind`` is empty when the agent gives none; a field is never
    guessed, so ``tool_call_id`` is empty when the agent cannot name the call.
    """

    id: str
    provider: AskProvider
    kind: AskKind
    title: str = ""
    tool: str = ""
    tool_call_id: str = ""
    command: str = ""
    cwd: str = ""
    reason: str = ""
    diff: str = ""
    questions: Sequence[PlainTree] | None = None
    """A question's questions, as the agent wrote them."""

    plan: str | None = None
    input: Mapping[str, PlainTree] = field(default_factory=dict[str, PlainTree])
    options: tuple[Option, ...] = ()
    answerable_in_chat: bool = True
    """False for an ask only the terminal can answer."""

    def __post_init__(self) -> None:
        """Refuse two options under one id, which an answer could not tell apart."""
        ids = [option.id for option in self.options]
        for each in ids:
            if ids.count(each) > 1:
                raise ValueError(f"ask {self.id!r} offers option {each!r} twice")

    def chosen(self, option: str) -> Option | None:
        """Return the option an answer names: by its id, else the first of that kind.

        Args:
          option: An option's id, or a kind, which is what an answer in the older
            form names (``allow_once`` or ``deny``) in place of the agent's own id.

        Returns:
          option: The agent's option, or ``None`` when the ask offers none so named.

        """
        by_id = next((each for each in self.options if each.id == option), None)
        return by_id or next(
            (each for each in self.options if each.kind == option),
            None,
        )

    def resolve(self, option: str, *, by: Resolver, who: str = "") -> Resolved:
        """Return how this ask ended, with the decision its option stands for.

        Args:
          option: The option taken, as :meth:`chosen` finds it; empty when none was.
          by: Who ended the ask.
          who: The person, when one did.

        Returns:
          resolved: The end, naming the option's own id, and ``allow`` or ``deny``
            from its kind. A question is ``answered`` unless its option denies, which
            is a skip, and a run or timeout that took none is ``released``.

        """
        taken = self.chosen(option)
        denied = taken is not None and taken.kind.startswith("deny")
        decision: Decision
        if by in {"released", "timeout"} or not option:
            decision = _unchosen(by)
        elif taken is None or (self.kind == "question" and not denied):
            decision = "answered"
        else:
            decision = "deny" if denied else "allow"
        return Resolved(
            ask_id=self.id,
            option=option if taken is None else taken.id,
            by=by,
            who=who,
            decision=decision,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Answer:
    """A choice on an ask, as the person gave it."""

    ask_id: str
    option: str
    """The id of one of the ask's options."""

    updated_input: dict[str, PlainTree] | None = None
    """Fields laid over the tool's input: a question's ``answers``."""

    message: str = ""
    """Why a denial was given, which the agent reads."""

    who: str = ""
    """The person who answered; the server knows, the payload does not say."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Resolved:
    """The end of an ask."""

    ask_id: str
    option: str
    """The id of the option taken; empty when none was."""

    by: Resolver
    who: str = ""
    decision: Decision | None = None
    """Set by :meth:`Ask.resolve`. Left out, it reads as ``released`` for a run, a
    timeout or a release, and as ``answered`` for anyone else."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StateChanged:
    """A run moved to ``state``; ``requires_action`` while any ask is open."""

    state: RunState


class Driver(Protocol):
    """One agent CLI run headless; the run drives it and writes what it reports.

    Events come from :meth:`events` in the order the CLI reported them. An ``Ask`` stays
    open until a ``Resolved`` names its id, whoever answered, so a run that reads the
    stream sees an answer given in a terminal as well as one given here.
    """

    async def start(self, *, cwd: Path, resume: str, model: str, effort: str) -> str:
        """Start the CLI in ``cwd``; return its session or thread id.

        Args:
          cwd: The folder the CLI works in.
          resume: The id to resume; empty starts a new session.
          model: The model to run; empty keeps the CLI's own.
          effort: The reasoning effort; empty keeps the CLI's own.

        Returns:
          session_id: The CLI's own id for the session, which resumes it later.

        """
        ...

    async def send_line(self, text: str) -> None:
        """Send the person's line to the agent."""
        ...

    async def command(self, verb: str, args: str) -> None:
        """Run a slash command such as ``/compact`` or ``/goal`` with ``args``."""
        ...

    async def answer(self, answer: Answer) -> None:
        """Hand the agent the person's choice on an open ask."""
        ...

    async def interrupt(self) -> None:
        """Stop the turn in progress; the session stays."""
        ...

    async def settings(self, *, model: str, effort: str) -> None:
        """Change the model and effort for the turns that follow."""
        ...

    async def stop(self) -> None:
        """End the CLI, and the events with it."""
        ...

    def events(self) -> AsyncIterator[Ask | Resolved | StateChanged]:
        """Stream what the CLI reports until it ends."""
        ...


def opened_record(ask: Ask, *, at: datetime) -> SystemMessage:
    """Return the record that opens ``ask``'s card in Chat.

    Args:
      ask: What the agent waits for.
      at: When it asked.

    Returns:
      record: A ``SystemMessage`` whose ``extra["$interaction"]`` is the card.

    """
    questions = None if ask.questions is None else list(ask.questions)
    options: list[PlainTree] = [
        {"id": each.id, "label": each.label, "kind": each.kind} for each in ask.options
    ]
    return SystemMessage(
        timestamp=at.isoformat(),
        subtype="interaction",
        content=_asks(ask),
        extra={
            "$interaction": {
                "id": ask.id,
                "type": _CARD_TYPES.get(ask.kind, "permission"),
                "tool": ask.tool,
                "input": dict(ask.input),
                "questions": questions,
                "plan": ask.plan,
                "state": "pending",
                "provider": ask.provider,
                "kind": ask.kind,
                "title": ask.title,
                "tool_call_id": ask.tool_call_id,
                "command": ask.command,
                "cwd": ask.cwd,
                "reason": ask.reason,
                "diff": ask.diff,
                "options": options,
                "answerable_in_chat": ask.answerable_in_chat,
            },
        },
    )


def resolved_record(resolved: Resolved, *, at: datetime) -> SystemMessage:
    """Return the record that closes the card of the ask ``resolved`` names.

    Args:
      resolved: How the ask ended.
      at: When it ended.

    Returns:
      record: A ``SystemMessage`` whose ``extra["$interaction_resolved"]`` closes it.

    """
    decision = resolved.decision or _unchosen(resolved.by)
    ended = _WORDS[decision]
    if resolved.by != "released":
        ended = f"{ended} {_WORDS[resolved.by]}"
    return SystemMessage(
        timestamp=at.isoformat(),
        subtype="interaction_resolved",
        content=ended,
        extra={
            "$interaction_resolved": {
                "id": resolved.ask_id,
                "option": resolved.option,
                "by": resolved.by,
                "who": resolved.who,
                "decision": decision,
            },
        },
    )


def answer_from_payload(payload: Mapping[str, PlainTree]) -> Answer:
    """Read an ``answer`` message's payload, in either of its two forms.

    The form is ``{prompt_id, option, updated_input?, message?}``. The older form,
    ``{prompt_id, decision, ...}``, names an option kind: ``allow`` reads as
    ``allow_once`` and ``deny`` as ``deny``, which :meth:`Ask.chosen` finds among the
    agent's own options.

    Args:
      payload: The message's payload.

    Returns:
      answer: The person's choice. ``who`` is left for the server to fill.

    Raises:
      ValueError: The payload names no prompt, names neither or both of ``option`` and
        ``decision``, or holds a field of another type or one it has no place for.

    """
    read = from_plain(dict(payload), _Payload)
    if not read.prompt_id:
        raise ValueError("an answer must name a prompt_id")
    if read.option and read.decision:
        raise ValueError("an answer must name an option or a decision, not both")
    option = (
        {"allow": "allow_once", "deny": "deny"}[read.decision]
        if read.decision
        else read.option
    )
    if not option:
        raise ValueError("an answer must name an option")
    return Answer(
        ask_id=read.prompt_id,
        option=option,
        updated_input=read.updated_input,
        message=read.message,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Payload:
    """An ``answer`` payload before its two forms are told apart."""

    prompt_id: str
    option: str = ""
    decision: Literal["allow", "deny"] | None = None
    updated_input: dict[str, PlainTree] | None = None
    message: str = ""


def _unchosen(by: Resolver) -> Decision:
    """Return how an ask ended that took no option: released by the run, else answered."""
    return "released" if by in {"run", "timeout", "released"} else "answered"


def _asks(ask: Ask) -> str:
    """Return the words that say what ``ask`` asks, for a line of the transcript."""
    name = ask.provider.capitalize()
    if ask.kind == "question" and ask.questions is not None and len(ask.questions) > 0:
        asked = [
            from_plain(
                from_plain(each, dict[str, PlainTree], default={}).get("question"),
                str,
                default="",
            )
            for each in ask.questions
        ]
        return f"{name} asks: {' '.join(asked)}"
    if ask.kind == "plan":
        return f"{name} asks to approve its plan"
    if ask.tool:
        return f"{name} asks to use {ask.tool}"
    if ask.title:
        return f"{name} asks: {ask.title}"
    return f"{name} asks for an answer"
