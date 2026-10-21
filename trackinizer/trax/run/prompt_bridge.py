"""Bring claude's permission and question prompts to Chat, and answer them from it.

Claude asks through hooks, which ``trax run`` gives it (:mod:`.box_hook.hook`). Each
hook event runs the hook entry point, which hands the event to this bridge over a Unix
socket in the run's own 0700 directory (:func:`ask`) and prints what the bridge replies.

- ``PreToolUse`` notes the call: its tool, its input, and claude's id for it.
- ``PermissionRequest`` opens a prompt. Claude waits on the hook while its TUI shows
  its own dialog, and the first answer wins. One from Chat (:meth:`PromptBridge.answer`)
  is the hook's reply. One given in the terminal lets the tool run, and claude leaves
  the losing hook running, so the bridge itself lets it go.
- ``PostToolUse`` lets go of its call's prompt. ``PermissionDenied`` ends its call's
  prompt as denied. ``UserPromptSubmit``, ``Stop`` and ``SessionEnd`` let go of every
  prompt still open: a line the person submits in the terminal means no dialog is up.
  A hook let go of prints nothing, except a ``PostToolUse`` that carries a note.
- A message sent with an allow rides on that call's ``PostToolUse``, as context for the
  model: claude reads a message on a denial only, and ignores it on an allow.

A ``PermissionRequest`` carries no tool-use id, so it is matched to the ``PreToolUse``
before it by its tool and input within one of claude's prompts. That call's id is what
``PostToolUse`` names, and the only way to find the prompt again: for a question, its
input then holds the answers too. ``AskUserQuestion`` is a ``PermissionRequest`` on that
tool, whose answers go back in ``updatedInput.answers``.

Each prompt, and each end of one, is a :class:`SystemMessage` handed to ``emit`` for the
run to write, built by :mod:`.asks`: ``extra["$interaction"]`` opens a card in Chat, and
``extra["$interaction_resolved"]`` closes it. ``pending`` is set while any prompt is
open, since a line typed into the TUI then would answer its dialog.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import contextlib
import json
import select
import socket
import threading
import uuid

from treekle import PlainTree, from_plain, json_loads

from trackinizer.trax.run.asks import (
    Answer,
    Ask,
    AskKind,
    Option,
    Resolved,
    answer_from_payload,
    opened_record,
    resolved_record,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from trackinizer.lib.agent.types.sessions import SystemMessage
    from trackinizer.trax.run.asks import Resolver


__all__ = ["PromptBridge", "ask"]


_HOOK_BUDGET_SEC: Final = 2.0
"""How long a hook that asks nothing waits on the bridge: each runs before or after a
tool call, which waits on it."""

_EVENT_MAX_BYTES: Final = 16 << 20
"""The largest hook event read; a tool's input holds a whole file it writes."""

_CALLS_REMEMBERED: Final = 256
"""How many calls wait for a prompt. One that fails gets no ``PostToolUse``, so the
oldest are dropped rather than kept for the life of the run."""


class PromptBridge:
    """Serve one run's claude hooks on a Unix socket, and hold its open prompts.

    Args:
      socket_path: Where to listen. Whoever can connect can answer a prompt, so its
        directory must be one only the run's owner can enter.
      emit: Takes each record the bridge writes. Called with the bridge's lock held,
        so records arrive in order; it must not block.
      pending: Set while any prompt is open, cleared when none is.
      clock: The time each record is stamped with.

    """

    def __init__(
        self,
        socket_path: Path,
        *,
        emit: Callable[[SystemMessage], None],
        pending: threading.Event,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._path = socket_path
        self._emit = emit
        self._pending = pending
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: OrderedDict[str, _Call] = OrderedDict()
        self._prompts: dict[str, _Prompt] = {}
        self._notes: OrderedDict[str, _Note] = OrderedDict()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Written once by ``close`` to end the accept loop, which waits on both.
        self._wake, self._woken = socket.socketpair()
        self._accepting = threading.Thread(
            target=self._accept,
            name="prompt bridge",
            daemon=True,
        )
        self._closed = False

    def start(self) -> None:
        """Listen, and serve each hook that connects on a thread of its own."""
        self._listener.bind(str(self._path))
        self._listener.listen()
        self._accepting.start()

    def answer(self, payload: Mapping[str, object]) -> bool:
        """Answer an open prompt with Chat's choice; its hook replies with it.

        Args:
          payload: An ``answer`` message's payload, in either form
            :func:`~trackinizer.trax.run.asks.answer_from_payload` reads.

        Returns:
          answered: Whether it named a prompt still open and one of its options; a
            malformed payload, or one answered first in the terminal, is not.

        """
        try:
            answer = answer_from_payload(
                from_plain(dict(payload), dict[str, PlainTree]),
            )
        except ValueError:
            return False
        with self._lock:
            prompt = self._prompts.get(answer.ask_id)
            chosen = None if prompt is None else prompt.ask.chosen(answer.option)
            if prompt is None or chosen is None:
                return False
            self._resolve(prompt, prompt.ask.resolve(chosen.id, by="chat"))
            if answer.message and not chosen.kind.startswith("deny"):
                self._remember(prompt, answer.message)
        _let_go(prompt.conn, reply=_reply(prompt, chosen, answer))
        return True

    def close(self) -> None:
        """Let go of every open prompt and stop listening; idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            released = self._release(list(self._prompts.values()), by="run")
        for prompt in released:
            _let_go(prompt.conn)
        # Not ``shutdown`` on the listener, which wakes no ``accept`` on macOS, nor a
        # connection to it, which finds no socket once the run's directory is gone.
        _ = self._wake.send(b"\0")
        if self._accepting.is_alive():
            self._accepting.join()
        for each in (self._listener, self._wake, self._woken):
            each.close()

    def _accept(self) -> None:
        """Hand each hook that connects to a thread of its own until closed."""
        while True:
            ready = select.select([self._listener, self._woken], [], [])[0]
            if self._woken in ready:
                return
            try:
                conn = self._listener.accept()[0]
            except OSError:
                continue
            threading.Thread(
                target=self._serve,
                args=(conn,),
                name="prompt bridge hook",
                daemon=True,
            ).start()

    # A held hook waits on its own thread for the connection to close, which is how the
    # bridge learns that claude ended it: past its timeout, or on Esc in the TUI.
    def _serve(self, conn: socket.socket) -> None:
        """Take one hook's event; hold its connection while it is an open prompt."""
        with conn:
            try:
                event = json_loads(_read_line(conn), dict[str, PlainTree])
            except (OSError, ValueError):
                return
            if event.get("hook_event_name") != "PermissionRequest":
                released, reply = self._note(event)
                for prompt in released:
                    _let_go(prompt.conn)
                _let_go(conn, reply=reply)
                return
            prompt = self._open(event, conn)
            if prompt is None:
                return
            _wait_closed(conn)
            with self._lock:
                if prompt.id in self._prompts:
                    self._resolve(prompt, _released(prompt, by="terminal"))

    def _open(
        self,
        event: Mapping[str, PlainTree],
        conn: socket.socket,
    ) -> _Prompt | None:
        """Open a prompt for a ``PermissionRequest``; ``None`` once closed."""
        turn = from_plain(event.get("prompt_id"), str, default="")
        tool = from_plain(event.get("tool_name"), str, default="")
        tool_input = from_plain(
            event.get("tool_input"),
            dict[str, PlainTree],
            default={},
        )
        grants = _grants(event.get("permission_suggestions"))
        with self._lock:
            if self._closed:
                return None
            call = next(
                (
                    key
                    for key, each in reversed(self._calls.items())
                    if (each.turn, each.tool, each.input) == (turn, tool, tool_input)
                ),
                None,
            )
            matched = None if call is None else self._calls.pop(call)
            prompt = _Prompt(
                turn=turn,
                ask=_ask(
                    uuid.uuid4().hex if matched is None else matched.id,
                    tool=tool,
                    tool_input=tool_input,
                    tool_call_id=call or "",
                    cwd=from_plain(event.get("cwd"), str, default=""),
                    grants=grants,
                ),
                grants=grants,
                tool_use_id=call,
                conn=conn,
            )
            self._prompts[prompt.id] = prompt
            self._pending.set()
            self._emit(opened_record(prompt.ask, at=self._clock()))
        return prompt

    # Return the prompts it lets go of, and what its hook prints.
    def _note(self, event: Mapping[str, PlainTree]) -> tuple[list[_Prompt], bytes]:
        """Take an event that asks nothing."""
        name = event.get("hook_event_name")
        tool_use_id = from_plain(event.get("tool_use_id"), str, default="")
        with self._lock:
            if name == "PreToolUse" and tool_use_id:
                self._calls[tool_use_id] = _Call(
                    id=uuid.uuid4().hex,
                    turn=from_plain(event.get("prompt_id"), str, default=""),
                    tool=from_plain(event.get("tool_name"), str, default=""),
                    input=from_plain(
                        event.get("tool_input"),
                        dict[str, PlainTree],
                        default={},
                    ),
                )
                while len(self._calls) > _CALLS_REMEMBERED:
                    _ = self._calls.popitem(last=False)
                return [], b""
            if name == "PostToolUse":
                _ = self._calls.pop(tool_use_id, None)
                released = self._release(
                    self._finished(event, tool_use_id=tool_use_id),
                    by="terminal",
                )
                return released, self._context(event, tool_use_id=tool_use_id)
            if name == "PermissionDenied":
                denied = self._finished(event, tool_use_id=tool_use_id)
                for prompt in denied:
                    self._resolve(prompt, prompt.ask.resolve("deny", by="terminal"))
                return denied, b""
            if name in {"Stop", "SessionEnd", "UserPromptSubmit"}:
                self._calls.clear()
                self._notes.clear()
                # A line submitted in the terminal means no dialog is up, whatever
                # ended the prompt: claude fires nothing for a "No" there.
                return (
                    self._release(
                        list(self._prompts.values()),
                        by="terminal" if name == "UserPromptSubmit" else "run",
                    ),
                    b"",
                )
        return [], b""

    # A prompt matched to its call is found by the call's id. One that had no call
    # before it is found by its tool within the same turn, the first such one: its
    # input cannot tell, since a question's input after the tool ran holds the answers.
    def _finished(
        self,
        event: Mapping[str, PlainTree],
        *,
        tool_use_id: str,
    ) -> list[_Prompt]:
        """Return the open prompt of the call ``event`` says finished, if any."""
        open_prompts = list(self._prompts.values())
        by_id = [
            p for p in open_prompts if tool_use_id and p.tool_use_id == tool_use_id
        ]
        if by_id:
            return by_id
        turn = from_plain(event.get("prompt_id"), str, default="")
        tool = from_plain(event.get("tool_name"), str, default="")
        return [
            p
            for p in open_prompts
            if p.tool_use_id is None and (p.turn, p.ask.tool) == (turn, tool)
        ][:1]

    def _remember(self, prompt: _Prompt, text: str) -> None:
        """Keep a message sent with an allow, for its call's ``PostToolUse``."""
        self._notes[prompt.id] = _Note(
            turn=prompt.turn,
            tool=prompt.ask.tool,
            tool_use_id=prompt.tool_use_id,
            text=text,
        )
        while len(self._notes) > _CALLS_REMEMBERED:
            _ = self._notes.popitem(last=False)

    def _context(self, event: Mapping[str, PlainTree], *, tool_use_id: str) -> bytes:
        """Return the hook output that adds the message sent with a finished call."""
        turn = from_plain(event.get("prompt_id"), str, default="")
        tool = from_plain(event.get("tool_name"), str, default="")
        key = next(
            (
                key
                for key, note in self._notes.items()
                if (tool_use_id and note.tool_use_id == tool_use_id)
                or (note.tool_use_id is None and (note.turn, note.tool) == (turn, tool))
            ),
            None,
        )
        if key is None:
            return b""
        note = self._notes.pop(key)
        context = f"The user allowed this {note.tool} call and said: {note.text}"
        output = {"hookEventName": "PostToolUse", "additionalContext": context}
        return json.dumps({"hookSpecificOutput": output}).encode()

    def _release(self, prompts: list[_Prompt], *, by: Resolver) -> list[_Prompt]:
        """End ``prompts`` with no choice; return them, for their hooks to go."""
        for prompt in prompts:
            self._resolve(prompt, _released(prompt, by=by))
        return prompts

    def _resolve(self, prompt: _Prompt, resolved: Resolved) -> None:
        """Close ``prompt`` and write its end; the caller holds the lock."""
        del self._prompts[prompt.id]
        if not self._prompts:
            self._pending.clear()
        self._emit(resolved_record(resolved, at=self._clock()))


def ask(socket_path: Path, request: str) -> str:
    """Hand one hook event to the run's bridge; return what the hook prints.

    A ``PermissionRequest`` waits for the bridge for as long as claude lets the hook
    run; any other event waits at most :data:`_HOOK_BUDGET_SEC`. Whatever fails returns
    nothing, and claude then goes on as if there were no hook: it asks in its TUI.

    Args:
      socket_path: The bridge's socket, as the run's settings name it.
      request: The hook's stdin: the event claude wrote.

    Returns:
      reply: The JSON claude reads as the hook's decision, or empty for none.

    """
    try:
        event = json_loads(request, dict[str, PlainTree])
    except ValueError:
        return ""
    held = event.get("hook_event_name") == "PermissionRequest"
    reply = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(None if held else _HOOK_BUDGET_SEC)
            conn.connect(str(socket_path))
            # One line: ``json.dumps`` escapes every newline inside a value.
            conn.sendall(json.dumps(event).encode() + b"\n")
            while chunk := conn.recv(65_536):
                reply += chunk
    except OSError:
        return ""
    return reply.decode(errors="replace")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Call:
    """A tool call ``PreToolUse`` named, which a prompt may follow."""

    id: str
    """The id its prompt takes, minted here."""

    turn: str
    """Claude's id for the person's prompt the call answers."""

    tool: str
    input: dict[str, PlainTree]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Note:
    """A message sent with an allow, waiting for its call to finish."""

    turn: str
    tool: str
    tool_use_id: str | None
    """The call it was sent for; ``None`` when no ``PreToolUse`` came before it."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _Prompt:
    """One open prompt, and the connection its hook waits on."""

    turn: str
    ask: Ask
    grants: tuple[dict[str, PlainTree], ...]
    """The ``permission_suggestions`` an allow-always or allow-session echoes back."""

    tool_use_id: str | None
    """The call it was matched to; ``None`` when no ``PreToolUse`` came before it."""

    conn: socket.socket

    @property
    def id(self) -> str:
        """The id its card and its end carry."""
        return self.ask.id


def _ask(
    ask_id: str,
    *,
    tool: str,
    tool_input: dict[str, PlainTree],
    tool_call_id: str,
    cwd: str,
    grants: tuple[dict[str, PlainTree], ...],
) -> Ask:
    """Return what a ``PermissionRequest`` for ``tool`` asks the person."""
    kind: AskKind = (
        "question"
        if tool == "AskUserQuestion"
        else "plan"
        if tool == "ExitPlanMode"
        else "permission"
    )
    questions = (
        from_plain(tool_input.get("questions"), list[PlainTree], default=[])
        if kind == "question"
        else None
    )
    shell = tool == "Bash"
    return Ask(
        id=ask_id,
        provider="claude",
        kind=kind,
        title=(
            "Claude's plan is ready for approval"
            if kind == "plan"
            else f"Claude wants to use {tool}"
            if kind == "permission"
            else "Claude has a question"
            if len(questions or []) == 1
            else "Claude has questions"
        ),
        tool=tool,
        tool_call_id=tool_call_id,
        command=from_plain(tool_input.get("command"), str, default="") if shell else "",
        cwd=cwd,
        reason=(
            from_plain(tool_input.get("description"), str, default="") if shell else ""
        ),
        questions=questions,
        plan=(
            from_plain(tool_input.get("plan"), str, default="")
            if kind == "plan"
            else None
        ),
        input=tool_input,
        options=_options(kind, grants),
    )


def _options(
    kind: AskKind,
    grants: tuple[dict[str, PlainTree], ...],
) -> tuple[Option, ...]:
    """Return the choices a card offers: yes, no, and a grant when the hook gave one."""
    yes, no = {
        "question": ("Answer", "Skip"),
        "plan": ("Approve", "Ask for changes"),
    }.get(kind, ("Yes", "No"))
    options = [Option(id="allow_once", label=yes, kind="allow_once")]
    if kind == "permission" and grants:
        # A grant claude keeps on disk outlasts the run; one it keeps in memory ends
        # with the session.
        if any(each.get("destination") != "session" for each in grants):
            options.append(
                Option(
                    id="allow_always",
                    label="Yes, and always allow",
                    kind="allow_always",
                ),
            )
        else:
            options.append(
                Option(
                    id="allow_session",
                    label="Yes, for this session",
                    kind="allow_session",
                ),
            )
    return (*options, Option(id="deny", label=no, kind="deny"))


# A grant adds an allow rule or a folder. A ``setMode`` is left out: the person's "yes"
# in Chat must not also change the mode claude runs in.
def _grants(suggested: PlainTree) -> tuple[dict[str, PlainTree], ...]:
    """Return the suggestions in ``suggested`` that are safe to echo back as a grant."""
    try:
        entries = from_plain(suggested, list[dict[str, PlainTree]], default=[])
    except ValueError:
        return ()
    return tuple(
        each
        for each in entries
        if each.get("type") == "addDirectories"
        or (each.get("type") == "addRules" and each.get("behavior") == "allow")
    )


def _released(prompt: _Prompt, *, by: Resolver) -> Resolved:
    """Return the end of ``prompt`` that took no choice of its own."""
    return Resolved(ask_id=prompt.id, option="", by=by, decision="released")


def _reply(prompt: _Prompt, chosen: Option, answer: Answer) -> bytes:
    """Return the hook output that hands claude ``chosen``."""
    denied = chosen.kind.startswith("deny")
    decision: dict[str, PlainTree] = {"behavior": "deny" if denied else "allow"}
    if denied:
        if answer.message:
            decision["message"] = answer.message
    else:
        if answer.updated_input is not None:
            # Claude takes the whole input back: a question's answers ride beside it.
            decision["updatedInput"] = {**prompt.ask.input, **answer.updated_input}
        if chosen.kind in {"allow_session", "allow_always"}:
            decision["updatedPermissions"] = list(prompt.grants)
    output = {"hookEventName": "PermissionRequest", "decision": decision}
    return json.dumps({"hookSpecificOutput": output}).encode()


def _read_line(conn: socket.socket) -> bytes:
    """Return the one line a hook sends, without its newline."""
    received = bytearray()
    while b"\n" not in received:
        chunk = conn.recv(65_536)
        if not chunk:
            break
        received += chunk
        if len(received) > _EVENT_MAX_BYTES:
            raise ValueError(f"a hook event over {_EVENT_MAX_BYTES} bytes")
    return bytes(received.partition(b"\n")[0])


def _wait_closed(conn: socket.socket) -> None:
    """Block until the connection closes, from either end."""
    with contextlib.suppress(OSError):
        while conn.recv(65_536):
            pass


def _let_go(conn: socket.socket, *, reply: bytes = b"") -> None:
    """Send a held hook its reply, if any, and close; a hook already gone is fine."""
    with contextlib.suppress(OSError):
        if reply:
            conn.sendall(reply)
        conn.shutdown(socket.SHUT_RDWR)
