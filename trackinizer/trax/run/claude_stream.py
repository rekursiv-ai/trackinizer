"""Drive claude headless over its stream-json control protocol, for Chat.

``claude -p --input-format stream-json --output-format stream-json
--permission-prompt-tool stdio`` is the protocol the Agent SDK and the VS Code panel
speak. The person's lines go in as ``user`` frames. Each time claude needs a person it
writes a ``control_request`` with ``can_use_tool`` and waits for our ``control_response``,
so an ask has an id, the tool call it belongs to, and a withdrawal frame
(``control_cancel_request``), none of which a hook can give. The transcript still comes
from tailing the session file, as it does for a TUI run; this module carries asks,
answers, the turn state and the interrupt, and nothing of the transcript.

Two halves:

- :class:`ClaudeWire` turns frames into :class:`~.asks.Ask`, :class:`~.asks.Resolved`
  and :class:`~.asks.StateChanged`, and an :class:`~.asks.Answer` into the frame that
  answers it. It does no I/O.
- :class:`ClaudeStreamDriver` is the :class:`~.asks.Driver`: it runs the process and
  moves lines between it and the wire.

What was measured on claude 2.1.306 (haiku, ``--permission-mode default``):

- A new session is named with ``--session-id``; ``--resume <id>`` keeps its id. Only
  ``/clear`` mints another, announced in the next ``init`` frame.
- ``init`` is written with the first user line, but the ``initialize`` control request
  is answered at once, so the driver sends it first and knows the CLI is alive.
- ``session_state_changed`` is written only when ``CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS``
  is set. It reports ``running`` and ``requires_action`` and, per turn, ``idle``.
- A deny with ``interrupt: true`` ends the turn at once (``terminal_reason``
  ``aborted_tools``) and puts claude's own words in the tool result in place of ours.
- A line sent while an ask is open is queued and joins the turn after the answer.
- ``/compact``, ``/goal``, ``/context``, ``/effort``, ``/model``, ``/rename``, ``/usage``
  and ``/clear`` work as lines. ``/help``, ``/status`` and ``/permissions`` answer
  "isn't available in this environment".
- On 2.1.308, an ``AskUserQuestion`` allow reads ``updatedInput.answers`` keyed by each
  question's text. Keyed by its ``header``, the model's tool result is "The user did not
  answer the questions."; keyed by its text, "The user answered: ...".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

import asyncio
import contextlib
import difflib
import json
import re
import uuid

from treekle import PlainTree, ReadError, from_plain, json_loads

from trackinizer.lib.posix.pipes import Piped
from trackinizer.trax.run.asks import Answer, Ask, Option, Resolved, StateChanged


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path


__all__ = ["ClaudeStreamDriver", "ClaudeWire", "Reading", "Reply"]


type _Frame = dict[str, PlainTree]
type _Event = Ask | Resolved | StateChanged


_STATE_ENV: Final = "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS"
"""Without it claude writes no ``session_state_changed``, and a run could not tell a
turn in progress from one waiting on the person."""

_STDERR_KEPT: Final = 500
"""How much of claude's stderr an error carries."""

_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
"""Claude's ``decision_reason`` may carry terminal escapes, which Chat must not render."""

_SESSION_MODES: Final = frozenset({"acceptEdits"})
"""The modes a ``setMode`` suggestion may switch the session to. Only what claude's own
dialog offers; a suggestion for any other mode is never presented as a plain allow."""

_REQUEST_TIMEOUT_SEC: Final = 30.0
"""How long claude has to answer a control request of ours."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Reading:
    """What one frame from claude means."""

    events: tuple[_Event, ...] = ()
    replies: tuple[_Frame, ...] = ()
    """Frames to write back at once: answers to requests nobody here can serve."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Reply:
    """The frame that answers an ask, and the end of the ask it records."""

    ask: Ask
    frame: _Frame
    resolved: Resolved


@dataclass(frozen=True, slots=True, kw_only=True)
class _Open:
    """An ask claude waits on, with the permission updates each session option grants."""

    ask: Ask
    grants: Mapping[str, PlainTree] = field(default_factory=dict[str, PlainTree])


class ClaudeWire:
    """Turn claude's frames into asks and a person's answers into frames.

    Args:
      cwd: The folder claude works in, which every ask names.
      session_id: The session claude was started on; ``init`` frames update it.

    """

    def __init__(self, *, cwd: Path, session_id: str = "") -> None:
        self.session_id = session_id
        self._cwd = str(cwd)
        self._open: dict[str, _Open] = {}

    def read(self, frame: Mapping[str, PlainTree]) -> Reading:
        """Read one frame claude wrote.

        Args:
          frame: The frame, parsed.

        Returns:
          reading: The asks it opens or closes and the turn state it reports, and any
            frames that must go back at once. A frame that means nothing here reads as
            nothing.

        """
        match _text(frame, "type"):
            case "system":
                return self._system(frame)
            case "control_request":
                return self._request(frame)
            case "control_cancel_request":
                return self._withdrawn(_text(frame, "request_id"))
            case _:
                return Reading()

    def answer(self, answer: Answer) -> Reply | None:
        """Answer an open ask with the person's choice.

        Args:
          answer: The choice, naming an option by its id or its kind.

        Returns:
          reply: The frame to write and how the ask ended; ``None`` when the ask is not
            open, which is a late answer, so the first answer wins.

        Raises:
          ValueError: The ask offers no such option, or a question is submitted
            without ``answers``. The ask stays open.

        """
        held = self._open.get(answer.ask_id)
        if held is None:
            return None
        chosen = held.ask.chosen(answer.option)
        if chosen is None:
            raise ValueError(
                f"ask {answer.ask_id!r} offers no option {answer.option!r}",
            )
        frame = _success(answer.ask_id, **_body(held, chosen, answer))
        del self._open[answer.ask_id]
        return Reply(
            ask=held.ask,
            frame=frame,
            resolved=held.ask.resolve(chosen.id, by="chat", who=answer.who),
        )

    def release(self) -> tuple[Resolved, ...]:
        """Close every open ask, for a CLI that has ended.

        Returns:
          resolved: One release per ask that was open.

        """
        ended = tuple(
            each.ask.resolve("", by="released") for each in self._open.values()
        )
        self._open.clear()
        return ended

    def _system(self, frame: Mapping[str, PlainTree]) -> Reading:
        match _text(frame, "subtype"):
            case "init":
                self.session_id = _text(frame, "session_id") or self.session_id
            case "session_state_changed":
                match _text(frame, "state"):
                    case "idle" | "running" | "requires_action" as state:
                        return Reading(events=(StateChanged(state=state),))
        return Reading()

    def _request(self, frame: Mapping[str, PlainTree]) -> Reading:
        request_id = _text(frame, "request_id")
        request = from_plain(frame.get("request"), dict[str, PlainTree], default={})
        if not request_id or request_id in self._open:
            return Reading()
        subtype = _text(request, "subtype")
        try:
            match subtype:
                case "can_use_tool":
                    held = self._tool(request_id, request)
                case "elicitation":
                    held = self._elicitation(request_id, request)
                case "request_user_dialog":
                    # No dialog kind is declared in ``initialize``, so claude should
                    # never send one; the protocol says to cancel what is not rendered.
                    return Reading(
                        replies=(_success(request_id, behavior="cancelled"),),
                    )
                case _:
                    return Reading(
                        replies=(
                            _failure(
                                request_id,
                                f"unsupported control request: {subtype}",
                            ),
                        ),
                    )
        except ReadError as error:
            # Left unanswered, claude would wait on this request for the whole run.
            return Reading(
                replies=(
                    _failure(request_id, f"unreadable {subtype} request: {error}"),
                ),
            )
        self._open[request_id] = held
        return Reading(events=(held.ask,))

    def _withdrawn(self, request_id: str) -> Reading:
        held = self._open.pop(request_id, None)
        if held is None:
            return Reading()
        return Reading(events=(held.ask.resolve("", by="released"),))

    def _tool(self, request_id: str, request: Mapping[str, PlainTree]) -> _Open:
        tool = _text(request, "tool_name")
        tool_input = from_plain(request.get("input"), dict[str, PlainTree], default={})
        base = Ask(
            id=request_id,
            provider="claude",
            kind="permission",
            title=_text(request, "title") or _text(request, "description"),
            tool=tool,
            tool_call_id=_text(request, "tool_use_id"),
            cwd=self._cwd,
            reason=_ANSI.sub("", _text(request, "decision_reason")),
            input=tool_input,
        )
        match tool:
            case "AskUserQuestion":
                questions = from_plain(
                    tool_input.get("questions"),
                    list[PlainTree],
                    default=[],
                )
                return _Open(
                    ask=replace(
                        base,
                        kind="question",
                        questions=questions,
                        options=_choices("Submit answers", "Skip"),
                    ),
                )
            case "ExitPlanMode":
                return _Open(
                    ask=replace(
                        base,
                        kind="plan",
                        plan=_text(tool_input, "plan"),
                        options=_choices("Approve plan", "Keep planning"),
                    ),
                )
        grants = _grants(request)
        options = (
            Option(id="allow_once", label="Allow", kind="allow_once"),
            *(
                Option(id=key, label=label, kind="allow_session")
                for key, label, _ in grants
            ),
            Option(id="deny", label="Deny", kind="deny"),
            Option(id="deny_stop", label="Deny and stop", kind="deny_stop"),
        )
        ask = replace(
            base,
            command=_text(tool_input, "command"),
            diff=_edit_diff(tool, tool_input),
            options=options,
        )
        return _Open(ask=ask, grants={key: update for key, _, update in grants})

    def _elicitation(self, request_id: str, request: Mapping[str, PlainTree]) -> _Open:
        ask = Ask(
            id=request_id,
            provider="claude",
            kind="elicitation",
            title=_text(request, "title"),
            tool=_text(request, "mcp_server_name"),
            reason=_text(request, "message"),
            cwd=self._cwd,
            input={
                key: request[key]
                for key in ("mode", "url", "requested_schema")
                if key in request
            },
            options=_choices("Accept", "Decline"),
        )
        return _Open(ask=ask)


class ClaudeStreamDriver:
    """Run claude headless and report its asks, as :class:`~.asks.Driver`.

    A driver runs one claude process, once.

    Args:
      argv: The command that starts claude; the flags follow it.
      env: Added to claude's environment.
      permission_mode: Passed as ``--permission-mode``. Empty leaves the person's own
        configured mode, since this is a layer over their claude, not a replacement.
      request_timeout_sec: How long claude has to answer a control request of ours.

    """

    def __init__(
        self,
        *,
        argv: Sequence[str] = ("claude",),
        env: Mapping[str, str] | None = None,
        permission_mode: str = "",
        request_timeout_sec: float = _REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._argv = tuple(argv)
        self._env = dict(env or {})
        self._permission_mode = permission_mode
        self._request_timeout_sec = request_timeout_sec
        self._events: asyncio.Queue[_Event | None] = asyncio.Queue()
        self._process: _Process | None = None
        self._waiting: dict[str, asyncio.Future[None]] = {}
        self._stderr = ""

    @property
    def session_id(self) -> str:
        """The session claude is on; ``/clear`` changes it."""
        return "" if self._process is None else self._process.wire.session_id

    async def start(self, *, cwd: Path, resume: str, model: str, effort: str) -> str:
        """Start claude in ``cwd`` and wait until it answers ``initialize``.

        Args:
          cwd: The folder claude works in.
          resume: The session to resume; empty starts a new one.
          model: The model to run; empty keeps the CLI's own.
          effort: The reasoning effort; empty keeps the CLI's own.

        Returns:
          session_id: The id of the session, new or resumed.

        Raises:
          RuntimeError: The driver was started before, or claude refused ``initialize``.
          ConnectionError: Claude ended before it answered.
          TimeoutError: Claude did not answer in time.
          FileNotFoundError: The claude binary is not on the path.

        """
        if self._process is not None:
            raise RuntimeError("claude driver already started")
        session = resume or str(uuid.uuid4())
        flags = [
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-prompt-tool",
            "stdio",
            *(
                ["--permission-mode", self._permission_mode]
                if self._permission_mode
                else []
            ),
            *(["--model", model] if model else []),
            *(["--effort", effort] if effort else []),
            *(["--resume", resume] if resume else ["--session-id", session]),
        ]
        piped = Piped(
            [*self._argv, *flags],
            cwd=cwd,
            env={_STATE_ENV: "1", **self._env},
        )
        await piped.start()
        wire = ClaudeWire(cwd=cwd, session_id=session)
        self._process = _Process(
            piped=piped,
            wire=wire,
            reader=asyncio.create_task(self._read(piped, wire)),
        )
        try:
            await self._request("initialize")
        except BaseException:
            await self.stop()
            raise
        return session

    async def send_line(self, text: str) -> None:
        """Send the person's line to claude, as a ``user`` frame."""
        await self._write(
            {"type": "user", "message": {"role": "user", "content": text}},
        )

    async def command(self, verb: str, args: str) -> None:
        """Send ``/verb args`` as a line, which is how claude takes a slash command."""
        await self.send_line(f"/{verb} {args}" if args else f"/{verb}")

    async def answer(self, answer: Answer) -> None:
        """Hand claude the person's choice on an open ask.

        A late answer, for an ask no longer open, does nothing.

        Args:
          answer: The choice, naming one of the ask's options.

        Raises:
          ValueError: The ask offers no such option. It stays open.
          ConnectionError: Claude has ended; the ask is released.

        """
        if self._process is None:
            raise RuntimeError("claude driver not started")
        reply = self._process.wire.answer(answer)
        if reply is None:
            return
        try:
            await self._write(reply.frame)
        except ConnectionError:
            self._events.put_nowait(reply.ask.resolve("", by="released"))
            raise
        self._events.put_nowait(reply.resolved)

    async def interrupt(self) -> None:
        """Stop the turn in progress; claude withdraws the asks it had open."""
        await self._request("interrupt")

    async def settings(self, *, model: str, effort: str) -> None:
        """Change the model and effort for the turns that follow.

        The effort goes in the flag-settings layer, which lasts for this process and
        writes none of the person's settings files.

        Args:
          model: The model to switch to; empty leaves it.
          effort: The reasoning effort to switch to; empty leaves it.

        """
        if model:
            await self._request("set_model", model=model)
        if effort:
            await self._request("apply_flag_settings", settings={"effortLevel": effort})

    async def stop(self) -> None:
        """End claude; the events end with it, and open asks are released."""
        if self._process is None:
            return
        await self._process.piped.terminate()
        await self._process.reader

    async def events(self) -> AsyncIterator[_Event]:
        """Stream what claude reports until it ends."""
        while (event := await self._events.get()) is not None:
            yield event
        self._events.put_nowait(None)

    async def _request(self, subtype: str, **fields: PlainTree) -> None:
        request_id = uuid.uuid4().hex
        waiting = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = waiting
        try:
            await self._write(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": subtype, **fields},
                },
            )
            async with asyncio.timeout(self._request_timeout_sec):
                await waiting
        finally:
            del self._waiting[request_id]

    async def _write(self, frame: _Frame) -> None:
        if self._process is None:
            raise RuntimeError("claude driver not started")
        # The reader is done only once it has seen the end and released every ask, so a
        # pipe that still takes bytes after that is no one's.
        piped = self._process.piped
        if self._process.reader.done() or not await piped.write(
            (json.dumps(frame) + "\n").encode(),
        ):
            raise self._gone()

    def _gone(self) -> ConnectionError:
        tail = self._stderr.strip()
        return ConnectionError(
            f"claude has ended: {tail}" if tail else "claude has ended",
        )

    async def _read(self, piped: Piped, wire: ClaudeWire) -> None:
        buffer = bytearray()
        try:
            async for stream, chunk in piped.output():
                if stream == "stderr":
                    self._stderr = (self._stderr + chunk.decode(errors="replace"))[
                        -_STDERR_KEPT:
                    ]
                    continue
                buffer += chunk
                while (end := buffer.find(b"\n")) >= 0:
                    line = bytes(buffer[:end])
                    del buffer[: end + 1]
                    await self._handle(wire, line)
            await self._handle(wire, bytes(buffer))
        finally:
            for resolved in wire.release():
                self._events.put_nowait(resolved)
            for waiting in self._waiting.values():
                if not waiting.done():
                    waiting.set_exception(self._gone())
            self._events.put_nowait(None)

    async def _handle(self, wire: ClaudeWire, line: bytes) -> None:
        if not line.strip():
            return
        try:
            frame = json_loads(line, dict[str, PlainTree])
            if _text(frame, "type") == "control_response":
                self._settle(from_plain(frame.get("response"), dict[str, PlainTree]))
                return
            reading = wire.read(frame)
        except ValueError:
            # A line that is not a frame must not end the reader, which carries every
            # ask: claude would wait on an answer nobody could hear.
            return
        for event in reading.events:
            self._events.put_nowait(event)
        for reply in reading.replies:
            # A claude that has gone needs no answer; the reader sees its end next.
            with contextlib.suppress(ConnectionError):
                await self._write(reply)

    def _settle(self, response: Mapping[str, PlainTree]) -> None:
        waiting = self._waiting.get(_text(response, "request_id"))
        if waiting is None or waiting.done():
            return
        if _text(response, "subtype") == "error":
            waiting.set_exception(
                RuntimeError(_text(response, "error") or "request refused"),
            )
        else:
            waiting.set_result(None)


def _text(frame: Mapping[str, PlainTree], key: str) -> str:
    """Return ``frame[key]`` when it is text, else ``""``."""
    return from_plain(frame.get(key), str, default="")


def _success(request_id: str, **body: PlainTree) -> _Frame:
    return {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": request_id, "response": body},
    }


def _failure(request_id: str, error: str) -> _Frame:
    return {
        "type": "control_response",
        "response": {"subtype": "error", "request_id": request_id, "error": error},
    }


def _choices(allow: str, deny: str) -> tuple[Option, ...]:
    """Return the two options of an ask that is answered, not permitted."""
    return (
        Option(id="allow_once", label=allow, kind="allow_once"),
        Option(id="deny", label=deny, kind="deny"),
    )


def _body(held: _Open, chosen: Option, answer: Answer) -> dict[str, PlainTree]:
    """Return the body of the ``control_response`` for ``chosen``."""
    allowed = chosen.kind.startswith("allow")
    if held.ask.kind == "elicitation":
        if allowed:
            return {"action": "accept", "content": answer.updated_input or {}}
        return {"action": "decline"}
    if not allowed:
        stop = chosen.kind == "deny_stop"
        message = answer.message or (
            "The person declined this request and asked you to stop."
            if stop
            else "The person declined this request."
        )
        return {
            "behavior": "deny",
            "message": message,
            **({"interrupt": True} if stop else {}),
        }
    merged = {**held.ask.input, **(answer.updated_input or {})}
    if held.ask.kind == "question" and "answers" not in merged:
        raise ValueError("a question is answered with updated_input.answers")
    if held.ask.kind == "question":
        merged["answers"] = _by_question(held.ask.questions or [], merged["answers"])
    body: dict[str, PlainTree] = {"behavior": "allow", "updatedInput": merged}
    if chosen.kind == "allow_session":
        body["updatedPermissions"] = [held.grants[chosen.id]]
    return body


# Claude reads a question's answer under the question's text; under any other key it
# tells the model "The user did not answer the questions." The card keys by header.
def _by_question(questions: Sequence[PlainTree], answers: PlainTree) -> PlainTree:
    """Return ``answers`` keyed by question text, from text, header or id keys."""
    names: dict[str, str] = {}
    for each in questions:
        question = from_plain(each, dict[str, PlainTree])
        text = _text(question, "question")
        for name in (text, _text(question, "id"), _text(question, "header")):
            if name:
                names.setdefault(name, text)
    supplied = from_plain(answers, dict[str, PlainTree])
    unknown = next((name for name in supplied if name not in names), None)
    if unknown is not None:
        raise ValueError(f"the ask has no question named {unknown!r}")
    return {names[name]: value for name, value in supplied.items()}


# Only a ``session`` destination is offered: the others write the person's settings
# files, which an answer in Chat does not ask for.
def _grants(request: Mapping[str, PlainTree]) -> list[tuple[str, str, PlainTree]]:
    """Return the session-scoped suggestions claude offers, as (id, label, update)."""
    broad = request.get("suppress_always_allow_rule") is True
    offered: list[tuple[str, str, PlainTree]] = []
    for each in from_plain(
        request.get("permission_suggestions"),
        list[PlainTree],
        default=[],
    ):
        update = from_plain(each, dict[str, PlainTree])
        label = (
            _label(update, broad=broad)
            if _text(update, "destination") == "session"
            else ""
        )
        if label:
            key = f"allow_session_{len(offered) + 1}" if offered else "allow_session"
            offered.append((key, label, each))
    return offered


def _label(update: Mapping[str, PlainTree], *, broad: bool) -> str:
    """Return the words for a suggestion, or ``""`` when it is not an allow to offer."""
    kind = _text(update, "type")
    if kind == "addRules" and not broad and _text(update, "behavior") == "allow":
        rules = from_plain(update.get("rules"), list[dict[str, PlainTree]], default=[])
        named = [
            f"{_text(rule, 'toolName')}({content})"
            if (content := _text(rule, "ruleContent"))
            else _text(rule, "toolName")
            for rule in rules
        ]
        return f"Allow {', '.join(named)} for this session" if named else ""
    if kind == "addDirectories":
        folders = ", ".join(
            from_plain(update.get("directories"), list[str], default=[]),
        )
        return f"Allow access to {folders} for this session" if folders else ""
    if kind == "setMode" and _text(update, "mode") in _SESSION_MODES:
        return f"Switch to {_text(update, 'mode')} mode for this session"
    return ""


def _edit_diff(tool: str, tool_input: Mapping[str, PlainTree]) -> str:
    """Return the unified diff an ``Edit`` ask would make, else ``""``."""
    old = from_plain(tool_input.get("old_string"), str, default=None)
    new = from_plain(tool_input.get("new_string"), str, default=None)
    if tool != "Edit" or old is None or new is None:
        return ""
    path = _text(tool_input, "file_path")
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Process:
    """A running claude: its pipes, its wire, and the task that reads it."""

    piped: Piped
    wire: ClaudeWire
    reader: asyncio.Task[None]
