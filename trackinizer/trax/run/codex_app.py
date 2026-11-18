"""Drive ``codex app-server`` headless: lines in, asks out.

:class:`CodexAppDriver` is the codex side of the :class:`~.asks.Driver` contract. It
spawns ``codex app-server`` on stdio and speaks its JSON-RPC (one JSON object per line,
no ``jsonrpc`` header) as the only client, so a person's Chat line is a ``turn/start``
and not keystrokes in a TUI. The run keeps tailing codex's rollout file for the
transcript; this module carries only what the rollout never holds, the asks and the
state: codex writes neither to the rollout.

The wire, as measured on codex 0.163.0:

- ``initialize`` with ``experimentalApi`` (``availableDecisions`` and
  ``item/tool/requestUserInput`` are experimental), then ``initialized``.
- ``thread/start``, or ``thread/resume`` by thread id. Resume fails with ``no rollout
  found`` until the thread has had a turn, and then a new thread starts in its place.
- A server request is an :class:`~.asks.Ask`. Its answer is the JSON-RPC response to the
  request's id, in the shape of that request's method.
- ``serverRequest/resolved`` follows every answer, and an interrupt that clears an open
  ask. For an ask nobody here answered it ends the ask: ``by="run"`` after our own
  interrupt, ``by="terminal"`` otherwise.
- ``thread/status/changed`` carries ``waitingOnApproval`` and ``waitingOnUserInput``,
  which are the ``requires_action`` state.

On a computer the person declared a sandbox (``trax machine connect --sandbox``) no ask
is wanted: ``trax run`` builds the driver with ``approval_policy="never"`` and
``sandbox="danger-full-access"``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

import asyncio
import contextlib
import json
import logging
import uuid

from treekle import PlainTree, from_plain, json_loads, to_plain

from trackinizer.lib.posix.pipes import Piped
from trackinizer.trax.run.asks import (
    Answer,
    Ask,
    Option,
    OptionKind,
    Resolved,
    Resolver,
    RunState,
    StateChanged,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


__all__ = [
    "ApprovalPolicy",
    "CodexAppDriver",
    "CodexAppError",
    "Reviewer",
    "SandboxMode",
]


logger = logging.getLogger(__name__)

type ApprovalPolicy = Literal["untrusted", "on-request", "never"]
"""When codex asks before it runs something: ``never`` runs everything unasked."""

type SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
type Reviewer = Literal["user", "auto_review"]
"""Who decides an approval: the person, or codex's own reviewer agent."""

type _Event = Ask | Resolved | StateChanged
_Tree = dict[str, PlainTree]

_QUIET: Final = (
    "item/agentMessage/delta",
    "item/commandExecution/outputDelta",
    "item/fileChange/outputDelta",
    "item/plan/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
)
"""The notifications that fire per token or per output chunk. Nothing here reads them,
and the rollout holds the text, so the app-server is asked not to send them."""

_STDERR_KEPT: Final = 2_000
"""How much of the app-server's stderr is kept, to say why it ended."""

_DECISIONS: Final[Mapping[str, tuple[str, OptionKind]]] = {
    "accept": ("Yes", "allow_once"),
    "acceptForSession": ("Yes, for this session", "allow_session"),
    "decline": ("No", "deny"),
    "cancel": ("No, and stop the turn", "deny_stop"),
}
"""The plain decisions of a command or file-change approval, by codex's word for them."""

_NO_ROLLOUT: Final = "no rollout found"
"""The words of codex's refusal to resume a thread it has no rollout file for."""

_EXEC_AMENDMENT: Final = "acceptWithExecpolicyAmendment"
_NETWORK_AMENDMENT: Final = "applyNetworkPolicyAmendment"


class CodexAppError(RuntimeError):
    """The app-server refused a request, or ended before it answered one."""

    def __init__(self, message: str, *, code: int = 0) -> None:
        """Keep the JSON-RPC error ``code``; it is 0 when the app-server ended."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True, kw_only=True)
class _Open:
    """An ask the app-server waits on, with what its answer is built from."""

    request_id: PlainTree
    """The id the response goes back under, in the type the app-server sent it."""

    ask: Ask
    params: _Tree
    decisions: Mapping[str, PlainTree]
    """For an approval, the decision each option id sends, as codex worded it."""


class CodexAppDriver:
    """One ``codex app-server`` run, driven as a :class:`~.asks.Driver`.

    Args:
      argv: The command that starts the app-server on stdio. Settings that must not
        touch the person's codex config go here as ``-c key=value``.
      approval_policy: When codex asks before it runs something.
      sandbox: What a command may touch without asking.
      reviewer: Who decides an approval that is asked.

    """

    def __init__(
        self,
        *,
        argv: Sequence[str] = ("codex", "app-server"),
        approval_policy: ApprovalPolicy = "on-request",
        sandbox: SandboxMode = "workspace-write",
        reviewer: Reviewer = "user",
    ) -> None:
        self._argv = list(argv)
        self._policy: _Tree = {
            "approvalPolicy": approval_policy,
            "sandbox": sandbox,
            "approvalsReviewer": reviewer,
        }
        self._piped: Piped | None = None
        self._stack = contextlib.AsyncExitStack()
        self._events = asyncio.Queue[_Event | None]()
        self._waiters: dict[int, _Pending] = {}
        self._open: dict[str, _Open] = {}
        self._diffs: dict[str, str] = {}
        self._run = _Run()

    async def start(self, *, cwd: Path, resume: str, model: str, effort: str) -> str:
        """Start the app-server in ``cwd`` and open or resume the thread.

        Args:
          cwd: The folder codex works in.
          resume: The thread id to resume; empty starts a new thread.
          model: The model to run; empty keeps codex's own.
          effort: The reasoning effort; empty keeps codex's own.

        Returns:
          thread_id: Codex's id for the thread, which resumes it later. It is a new
            id when ``resume`` names a thread with no rollout to resume.

        Raises:
          FileNotFoundError: The app-server's command is not on PATH.
          CodexAppError: The app-server refused a step.
          RuntimeError: The driver was started before; its events have ended.

        """
        if self._run.reader is not None:
            raise RuntimeError("a CodexAppDriver starts once; build a new one")
        piped = await self._stack.enter_async_context(Piped(self._argv, cwd=cwd))
        self._piped = piped
        self._run.reader = asyncio.create_task(self._read(piped))
        params: _Tree = {"cwd": str(cwd), **self._policy}
        if model:
            params["model"] = model
        try:
            _ = await self._call("initialize", _initialize())
            await self._send({"method": "initialized"})
            reply = await self._thread(params, resume)
            self._run.thread_id = _text(_tree(reply, "thread"), "id")
            if effort:
                await self.settings(model=model, effort=effort)
        except BaseException:
            await self.stop()
            raise
        return self._run.thread_id

    async def send_line(self, text: str) -> None:
        """Send the person's line: steer the turn in progress, else start a turn.

        Args:
          text: What the person wrote.

        """
        words: PlainTree = [{"type": "text", "text": text}]
        if self._run.turns:
            # A steer the app-server refuses (the turn ended in the meantime) is no
            # error: the line then starts a turn of its own.
            with contextlib.suppress(CodexAppError):
                _ = await self._call(
                    "turn/steer",
                    {
                        "threadId": self._run.thread_id,
                        "expectedTurnId": self._run.turns[-1],
                        "input": words,
                    },
                )
                return
        _ = await self._call(
            "turn/start",
            {"threadId": self._run.thread_id, "input": words},
        )

    async def command(self, verb: str, args: str) -> None:
        """Run ``/compact`` or ``/goal`` as the app-server's own methods.

        Args:
          verb: The slash command, with or without its slash.
          args: For ``/goal``: an objective to set, ``pause``, ``resume`` or
            ``clear``; empty reads the goal.

        Raises:
          ValueError: The app-server has no method for ``verb``.

        """
        thread: _Tree = {"threadId": self._run.thread_id}
        match verb.removeprefix("/"):
            case "compact":
                _ = await self._call("thread/compact/start", thread)
            case "goal":
                await self._goal(thread, args.strip())
            case _:
                raise ValueError(f"the codex app-server has no command {verb!r}")

    async def answer(self, answer: Answer) -> None:
        """Hand codex the person's choice; an ask already ended takes none.

        Args:
          answer: The choice. A question's ``updated_input`` holds ``answers`` keyed by
            question header or id; an accepted elicitation's holds its content.

        Raises:
          ValueError: The ask offers no such option, or the answer names a question it
            does not ask. The ask stays open.

        """
        opened = self._open.get(answer.ask_id)
        if opened is None:
            return
        taken = opened.ask.chosen(answer.option)
        if taken is None:
            raise ValueError(
                f"ask {answer.ask_id!r} offers no option {answer.option!r}",
            )
        result = _result(opened, taken, answer)
        # The ask closes before the first await, so a second answer finds it gone.
        self._close(answer.ask_id, by="chat", option=taken.id, who=answer.who)
        await self._send({"id": opened.request_id, "result": result})

    async def interrupt(self) -> None:
        """Stop the turn in progress; between turns there is nothing to stop."""
        if not self._run.turns:
            return
        turn = self._run.turns[-1]
        self._run.interrupted = turn
        _ = await self._call(
            "turn/interrupt",
            {"threadId": self._run.thread_id, "turnId": turn},
        )

    async def settings(self, *, model: str, effort: str) -> None:
        """Change the model and effort of the turns that follow.

        Args:
          model: The model to run; empty keeps the one in use.
          effort: The reasoning effort; empty keeps the one in use.

        """
        params: _Tree = {"threadId": self._run.thread_id}
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        _ = await self._call("thread/settings/update", params)

    async def stop(self) -> None:
        """End the app-server; the events end once its output is read. Safe to repeat."""
        await self._stack.aclose()

    async def events(self) -> AsyncIterator[_Event]:
        """Stream the asks, their ends and the state until the app-server ends.

        Yields:
          event: What the app-server reported, in its order.

        """
        while (event := await self._events.get()) is not None:
            yield event
        self._events.put_nowait(None)

    async def _thread(self, params: _Tree, resume: str) -> _Tree:
        """Resume the thread ``resume`` names, else start one; return the reply."""
        if not resume:
            return await self._call("thread/start", params)
        try:
            return await self._call("thread/resume", {**params, "threadId": resume})
        except CodexAppError as error:
            # Measured on 0.163.0: a thread that never had a turn has no rollout, and
            # resume refuses it as it refuses an id codex never made. Such a thread
            # holds nothing to lose; raising here would leave a chat that was opened
            # and never used unable to reattach.
            if _NO_ROLLOUT not in str(error):
                raise
        logger.warning(
            "thread %s has no rollout to resume; starting a new thread",
            resume,
        )
        return await self._call("thread/start", params)

    async def _goal(self, thread: _Tree, args: str) -> None:
        """Map ``/goal``'s argument onto the goal methods."""
        user: _Tree = {**thread, "origin": "user"}
        match args:
            case "":
                _ = await self._call("thread/goal/get", thread)
            case "clear":
                _ = await self._call("thread/goal/clear", user)
            case "pause" | "resume":
                status = "paused" if args == "pause" else "active"
                _ = await self._call("thread/goal/set", {**user, "status": status})
            case _:
                _ = await self._call(
                    "thread/goal/set",
                    {**user, "objective": args, "status": "active"},
                )

    async def _call(self, method: str, params: _Tree) -> _Tree:
        """Send a request and wait for its result."""
        self._run.calls += 1
        call = self._run.calls
        future = asyncio.Future[_Tree]()
        self._waiters[call] = _Pending(method=method, future=future)
        await self._send({"method": method, "id": call, "params": params})
        return await future

    async def _send(self, frame: _Tree) -> None:
        piped = self._piped
        data = json.dumps(to_plain(frame)).encode() + b"\n"
        if piped is None or not await piped.write(data):
            raise CodexAppError("the codex app-server is not running")

    async def _read(self, piped: Piped) -> None:
        """Read the app-server's frames until it ends, then end the events."""
        pending = b""
        try:
            async for stream, chunk in piped.output():
                if stream == "stderr":
                    text = self._run.stderr + chunk.decode(errors="replace")
                    self._run.stderr = text[-_STDERR_KEPT:]
                    continue
                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    await self._dispatch(line)
        finally:
            self._finish()

    async def _dispatch(self, line: bytes) -> None:
        """Route one frame; a frame that does not read is skipped, not fatal."""
        try:
            frame = from_plain(json_loads(line), _Tree)
        except ValueError as error:
            logger.warning("skipping a line that is not a frame: %s", error)
            return
        method = _text(frame, "method")
        params = _tree(frame, "params")
        if not method:
            self._on_response(frame)
        elif "id" in frame:
            await self._on_request(frame["id"], method, params)
        else:
            self._on_notification(method, params)

    def _on_response(self, frame: _Tree) -> None:
        call = frame.get("id")
        # A response that names no call of ours is the app-server's own complaint, such
        # as the error it sends with a null id for a line it could not read.
        pending = self._waiters.pop(call, None) if isinstance(call, int) else None
        if pending is None or pending.future.done():
            return
        error = _tree(frame, "error")
        if error:
            code = from_plain(error.get("code"), int, default=0)
            pending.future.set_exception(
                CodexAppError(_text(error, "message"), code=code),
            )
            return
        result = _tree(frame, "result")
        if pending.method == "turn/start":
            # Set here, in the order the app-server wrote its frames: a turn/completed
            # that follows the reply at once must find the turn it ends already set.
            self._run.begin(_text(_tree(result, "turn"), "id"))
        pending.future.set_result(result)

    async def _on_request(
        self,
        request_id: PlainTree,
        method: str,
        params: _Tree,
    ) -> None:
        """Open an ask for a server request, or refuse one nothing here can answer."""
        ask_id = self._ask_id(request_id)
        try:
            built = self._ask(ask_id, method, params)
        except ValueError as error:
            logger.warning("refusing %s whose params do not read: %s", method, error)
            await self._refuse(request_id, -32_602, f"invalid params for {method}")
            return
        if built is None:
            await self._refuse(request_id, -32_601, f"unsupported request {method}")
            return
        ask, decisions = built
        self._open[ask_id] = _Open(
            request_id=request_id,
            ask=ask,
            params=params,
            decisions=decisions,
        )
        self._events.put_nowait(ask)
        self._refresh()

    def _ask(
        self,
        ask_id: str,
        method: str,
        params: _Tree,
    ) -> tuple[Ask, dict[str, PlainTree]] | None:
        """Build the ask a server request opens; ``None`` for one not known."""
        if method == "item/fileChange/requestApproval":
            diff = self._diffs.get(_text(params, "itemId"), "")
            return _file_ask(ask_id, params, diff)
        build = _ASKS.get(method)
        return None if build is None else build(ask_id, params)

    async def _refuse(self, request_id: PlainTree, code: int, message: str) -> None:
        await self._send(
            {"id": request_id, "error": {"code": code, "message": message}},
        )

    def _on_notification(self, method: str, params: _Tree) -> None:
        match method:
            case "thread/status/changed":
                status = _tree(params, "status")
                self._run.active = _text(status, "type") == "active"
                flags = from_plain(status.get("activeFlags"), list[str], default=[])
                self._run.waiting = len(flags) > 0
                self._refresh()
            case "turn/started":
                self._run.begin(_text(_tree(params, "turn"), "id"))
            case "turn/completed":
                turn = _text(_tree(params, "turn"), "id")
                if turn in self._run.turns:
                    self._run.turns.remove(turn)
            case "item/started":
                item = _tree(params, "item")
                if _text(item, "type") == "fileChange":
                    self._diffs[_text(item, "id")] = _diff(item)
            case "item/completed":
                _ = self._diffs.pop(_text(_tree(params, "item"), "id"), None)
            case "serverRequest/resolved":
                self._on_resolved(self._ask_id(params.get("requestId")))

    def _on_resolved(self, ask_id: str) -> None:
        """End an ask nobody here answered: our interrupt cleared it, or a client did."""
        opened = self._open.get(ask_id)
        if opened is None:
            return
        turn = _text(opened.params, "turnId")
        interrupted = bool(turn) and turn == self._run.interrupted
        self._close(ask_id, by="run" if interrupted else "terminal")

    def _close(
        self,
        ask_id: str,
        *,
        by: Resolver,
        option: str = "",
        who: str = "",
    ) -> None:
        """End an ask that is open."""
        opened = self._open.pop(ask_id)
        self._events.put_nowait(opened.ask.resolve(option, by=by, who=who))
        self._refresh()

    def _refresh(self) -> None:
        """Report the run's state: waiting while any ask is open."""
        if self._open or self._run.waiting:
            self._set_state("requires_action")
        else:
            self._set_state("running" if self._run.active else "idle")

    def _set_state(self, state: RunState) -> None:
        if state != self._run.state:
            self._run.state = state
            self._events.put_nowait(StateChanged(state=state))

    def _finish(self) -> None:
        """Release every open ask, fail every call in flight and end the events."""
        for ask_id in list(self._open):
            self._close(ask_id, by="released")
        self._set_state("idle")
        tail = self._run.stderr.strip()
        ended = (
            f"the codex app-server ended ({tail})"
            if tail
            else "the codex app-server ended"
        )
        for pending in self._waiters.values():
            if not pending.future.done():
                pending.future.set_exception(
                    CodexAppError(f"{ended} before it answered {pending.method}"),
                )
        self._waiters.clear()
        self._events.put_nowait(None)

    def _ask_id(self, request_id: PlainTree) -> str:
        """Return the ask's id: request ids restart at 0 with each app-server."""
        return f"{self._run.token}-{request_id}"


def _initialize() -> _Tree:
    return {
        "clientInfo": {"name": "trax-run", "title": "trax run", "version": "1"},
        "capabilities": {
            "experimentalApi": True,
            "optOutNotificationMethods": list(_QUIET),
        },
    }


def _command_ask(ask_id: str, params: _Tree) -> tuple[Ask, dict[str, PlainTree]]:
    network = _tree(params, "networkApprovalContext")
    host = _text(network, "host")
    command = _text(params, "command")
    cwd = _text(params, "cwd")
    shown = {
        "command": command,
        "host": host,
        "protocol": _text(network, "protocol"),
        "cwd": cwd,
    }
    options, decisions = _approval_options(
        from_plain(params.get("availableDecisions"), list[PlainTree], default=[]),
    )
    ask = Ask(
        id=ask_id,
        provider="codex",
        kind="command",
        title=f"Allow network access to {host}" if host else "Run a command",
        tool="shell",
        tool_call_id=_text(params, "itemId"),
        command=command,
        cwd=cwd,
        reason=_text(params, "reason"),
        input={name: value for name, value in shown.items() if value},
        options=options,
    )
    return ask, decisions


def _file_ask(
    ask_id: str,
    params: _Tree,
    diff: str,
) -> tuple[Ask, dict[str, PlainTree]]:
    options, decisions = _approval_options([])
    root = _text(params, "grantRoot")
    ask = Ask(
        id=ask_id,
        provider="codex",
        kind="file_change",
        title="Edit files",
        tool="apply_patch",
        tool_call_id=_text(params, "itemId"),
        reason=_text(params, "reason"),
        diff=diff,
        input={"grant_root": root} if root else {},
        options=options,
    )
    return ask, decisions


def _permissions_ask(ask_id: str, params: _Tree) -> tuple[Ask, dict[str, PlainTree]]:
    ask = Ask(
        id=ask_id,
        provider="codex",
        kind="permissions",
        title="Grant more access",
        tool="request_permissions",
        tool_call_id=_text(params, "itemId"),
        cwd=_text(params, "cwd"),
        reason=_text(params, "reason"),
        input={"permissions": params.get("permissions")},
        options=(
            Option(id="turn", label="Yes", kind="allow_once"),
            Option(id="session", label="Yes, for this session", kind="allow_session"),
            Option(id="decline", label="No", kind="deny"),
        ),
    )
    return ask, {}


def _question_ask(ask_id: str, params: _Tree) -> tuple[Ask, dict[str, PlainTree]]:
    ask = Ask(
        id=ask_id,
        provider="codex",
        kind="question",
        tool="request_user_input",
        tool_call_id=_text(params, "itemId"),
        questions=from_plain(params.get("questions"), list[PlainTree], default=[]),
        options=(Option(id="answer", label="Send answers", kind="allow_once"),),
    )
    return ask, {}


def _elicitation_ask(ask_id: str, params: _Tree) -> tuple[Ask, dict[str, PlainTree]]:
    shown = {
        "server": params.get("serverName"),
        "mode": params.get("mode"),
        "requestedSchema": params.get("requestedSchema"),
        "url": params.get("url"),
    }
    ask = Ask(
        id=ask_id,
        provider="codex",
        kind="elicitation",
        title=_text(params, "message"),
        input={name: value for name, value in shown.items() if value is not None},
        options=(
            Option(id="accept", label="Accept", kind="allow_once"),
            Option(id="decline", label="Decline", kind="deny"),
            Option(id="cancel", label="Cancel", kind="deny_stop"),
        ),
    )
    return ask, {}


def _approval_options(
    offered: Sequence[PlainTree],
) -> tuple[tuple[Option, ...], dict[str, PlainTree]]:
    """Return a command or file change's options and what each one sends."""
    decisions = list(offered) or list(_DECISIONS)
    # Codex 0.163.0 leaves ``decline`` out of a command's list yet takes it (measured:
    # the command ends ``declined`` and the turn goes on). Without it the card's only
    # denial would be "stop the turn".
    if "decline" not in decisions:
        decisions.insert(
            decisions.index("cancel") if "cancel" in decisions else len(decisions),
            "decline",
        )
    options: list[Option] = []
    sends: dict[str, PlainTree] = {}
    for decision in decisions:
        option = _option(decision)
        if option is not None:
            options.append(option)
            sends[option.id] = decision
    return tuple(options), sends


def _option(decision: PlainTree) -> Option | None:
    """Return the option for one of codex's decisions; ``None`` for one not known."""
    if isinstance(decision, str):
        known = _DECISIONS.get(decision)
        return (
            None
            if known is None
            else Option(id=decision, label=known[0], kind=known[1])
        )
    if not isinstance(decision, Mapping):
        return None
    if _EXEC_AMENDMENT in decision:
        return Option(
            id=_EXEC_AMENDMENT,
            label="Yes, and do not ask again for commands like this",
            kind="allow_always",
        )
    if _NETWORK_AMENDMENT in decision:
        rule = _tree(_tree(decision, _NETWORK_AMENDMENT), "network_policy_amendment")
        action, host = _text(rule, "action"), _text(rule, "host")
        allowed = action == "allow"
        return Option(
            id=f"{_NETWORK_AMENDMENT}:{action}:{host}",
            label=f"Yes, and always allow {host}"
            if allowed
            else f"No, and always block {host}",
            kind="allow_always" if allowed else "deny",
        )
    return None


def _result(opened: _Open, taken: Option, answer: Answer) -> _Tree:
    """Return the JSON-RPC result that answers ``opened`` with ``taken``."""
    match opened.ask.kind:
        case "permissions":
            denied = taken.kind.startswith("deny")
            granted = {} if denied else _tree(opened.params, "permissions")
            scope = "session" if taken.kind == "allow_session" else "turn"
            return {"permissions": granted, "scope": scope}
        case "question":
            made = _answers(opened.ask.questions or (), answer.updated_input)
            return {"answers": made}
        case "elicitation":
            content: PlainTree = answer.updated_input if taken.id == "accept" else None
            return {"action": taken.id, "content": content}
        case _:
            return {"decision": opened.decisions[taken.id]}


# The card keys its answers by question header, as it does for claude; an id is taken as
# well.
def _answers(
    questions: Sequence[PlainTree],
    updated: Mapping[str, PlainTree] | None,
) -> dict[str, PlainTree]:
    """Return a question ask's answers keyed by question id, as codex reads them."""
    supplied = from_plain(
        None if updated is None else updated.get("answers"),
        dict[str, str | list[str]],
        default={},
    )
    ids: dict[str, str] = {}
    for each in questions:
        question = from_plain(each, _Tree)
        ids[_text(question, "id")] = _text(question, "id")
    for each in questions:
        question = from_plain(each, _Tree)
        ids.setdefault(_text(question, "header"), _text(question, "id"))
    answers: dict[str, PlainTree] = {}
    for name, value in supplied.items():
        if name not in ids:
            raise ValueError(f"the ask has no question named {name!r}")
        answers[ids[name]] = {"answers": [value] if isinstance(value, str) else value}
    return answers


def _diff(item: _Tree) -> str:
    """Return a file-change item's edits as text: one block per file."""
    blocks: list[str] = []
    for each in from_plain(item.get("changes"), list[PlainTree], default=[]):
        change = from_plain(each, _Tree)
        kind = _text(_tree(change, "kind"), "type")
        blocks.append(f"{_text(change, 'path')} ({kind})\n{_text(change, 'diff')}")
    return "\n".join(blocks)


def _text(tree: Mapping[str, PlainTree], key: str) -> str:
    return from_plain(tree.get(key), str, default="")


def _tree(tree: Mapping[str, PlainTree], key: str) -> _Tree:
    return from_plain(tree.get(key), _Tree, default={})


@dataclass(frozen=True, slots=True, kw_only=True)
class _Pending:
    """A request of ours the app-server has not answered."""

    method: str
    future: asyncio.Future[_Tree]


@dataclass(slots=True, kw_only=True)
class _Run:
    """What the driver knows of the run, as the app-server last said it."""

    thread_id: str = ""
    turns: list[str] = field(default_factory=list[str])
    """The turns in progress, the latest last; none between turns."""

    interrupted: str = ""
    """The turn our last interrupt stopped: an ask of that turn that ends was cleared."""

    calls: int = 0
    state: RunState = "idle"
    active: bool = False
    waiting: bool = False
    stderr: str = ""
    token: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    """Prefixes ask ids: request ids restart at 0 with each app-server."""

    reader: asyncio.Task[None] | None = None
    """Held so the task is not collected while it reads."""

    def begin(self, turn: str) -> None:
        """Note a turn in progress; the response and the notification both name it."""
        if turn and turn not in self.turns:
            self.turns.append(turn)


_ASKS: Final[Mapping[str, Callable[[str, _Tree], tuple[Ask, dict[str, PlainTree]]]]] = {
    "item/commandExecution/requestApproval": _command_ask,
    "item/permissions/requestApproval": _permissions_ask,
    "item/tool/requestUserInput": _question_ask,
    "mcpServer/elicitation/request": _elicitation_ask,
}
"""The server requests that open an ask from their params alone."""
