"""``trax helper``: answer the canvas Chat as its assistant, through a model CLI.

Gives any server a Chat assistant. The server's assistant (``--assistant
ACTOR=EMAIL``) is whichever live session the configured account opens under the
configured actor, so ``trax helper claude --as ACTOR``, run with that account's
key, becomes it; run with your own key, it is the helper a canvas of yours can
choose. It serves science chat as a hosted assistant does: each conversation is a
session of its own (``cli_session_id`` ``chat:<id>``, label ``science-chat``,
account the first poster), and the helper records the lines posted there, and its
answers, as that session's records. Each conversation is also one CLI conversation:
its first line starts one and every later one resumes it (``claude --resume``,
``codex exec resume``), so a chat reopened from History goes on where it left off.
The first line of a fork names the line of another chat it starts from: the helper
opens the fork's session with that chat's lines up to it, links the two, and tells the
CLI those lines with the first turn, since a new CLI conversation has not heard them.

A line left unanswered by a restart is answered when the helper starts. The session
holds the line but not the canvas it was sent from, so that answer is made without
the canvas commands or context, as is a restarted assistant's. The helper's own
session names itself by its actor and resumes on a restart, so lines posted while it
was down are still queued for it; run one helper per actor and key, and name another
with ``--as``.

The service session takes lines only through Chat: the server refuses a Console or
direct send to it, since the helper answers into conversations. Arguments after
``--`` go to the CLI on every turn, e.g. a model or the tools it may run
(``--allowedTools 'Bash(trax:*)'`` lets Claude read the graph and move the canvas
with ``trax``).

Examples:
  trax helper claude -- --model haiku --allowedTools 'Bash(trax:*)'
  trax helper codex --as guide -- -m gpt-6-luna

"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar, Final, Protocol, cast

import argparse
import json
import logging
import os
import subprocess
import threading

from trackinizer.client.errors import ClientError
from trackinizer.lib.codec import ReadError, from_plain, loads
from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.run.chat_sessions import ChatSessions
from trackinizer.trax.run.inbound import render_fork_lines, render_inbound
from trackinizer.trax.run.redact import redactor_from_environ
from trackinizer.wire.wire_science_chat import CHAT_HELPER_CLI
from trackinizer.wire.wire_sessions import SessionEnd, SessionStart


if TYPE_CHECKING:
    from pathlib import Path

    import uuid

    from trackinizer.client.chat_forks import ForkLine
    from trackinizer.client.client import Client
    from trackinizer.trax.run.redact import Redactor


_logger = logging.getLogger(__name__)

GUIDE: Final = (
    "You are the Chat helper of a Trackinizer canvas. The person is looking at a "
    "page of their research graph and asks about it. Read what you need with the "
    "trax CLI, the record named in the message's context first; answer briefly in "
    "Markdown, citing records as Kind#seq; and show what you talk about with the "
    "canvas commands the message lists."
)
"""What the CLI is told it is, on every Claude turn and at the start of a Codex one."""


class HelperError(Exception):
    """A turn gave no answer: the CLI failed, timed out, or printed none."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Turn:
    """One answer, and the CLI's id for the conversation it continues."""

    text: str
    cli_session_id: str | None


class HelperCli(Protocol):
    """A model CLI run one turn at a time, resuming a conversation by its id."""

    name: ClassVar[str]

    def argv(
        self,
        prompt: str,
        *,
        resume: str | None,
        extra: Sequence[str],
    ) -> list[str]:
        """Return the command that runs one turn."""
        ...

    def read(self, stdout: str) -> Turn:
        """Return the turn's answer from what the command printed."""
        ...


class Claude:
    """``claude -p``: one JSON result per turn, resumed by its session id."""

    name: ClassVar[str] = "claude"

    def argv(
        self,
        prompt: str,
        *,
        resume: str | None,
        extra: Sequence[str],
    ) -> list[str]:
        """Return ``claude -p --output-format json``, resuming ``resume`` if given.

        Args:
          prompt: The turn's message.
          resume: The conversation to continue, or None to start one.
          extra: Arguments for the CLI, before the prompt.

        Returns:
          argv: The command.

        """
        resumed = ["--resume", resume] if resume is not None else []
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            *resumed,
            "--append-system-prompt",
            GUIDE,
            *extra,
            prompt,
        ]

    def read(self, stdout: str) -> Turn:
        """Return the result and the session id.

        Args:
          stdout: What the command printed.

        Returns:
          turn: The answer.

        Raises:
          HelperError: The result is an error, or there is none.

        """
        try:
            result = _object(loads(stdout))
        except ValueError:
            result = {}
        text = _text(result.get("result"))
        if result.get("is_error") is True:
            raise HelperError(text or "claude reported an error")
        if not text.strip():
            raise HelperError("claude printed no answer")
        return Turn(
            text=text,
            cli_session_id=_text(result.get("session_id")) or None,
        )


class Codex:
    """``codex exec --json``: events, one line each; the thread id names the conversation."""

    name: ClassVar[str] = "codex"

    def argv(
        self,
        prompt: str,
        *,
        resume: str | None,
        extra: Sequence[str],
    ) -> list[str]:
        """Return ``codex exec --json``, or ``codex exec resume --json`` for ``resume``.

        Codex takes no system prompt here, so a new conversation opens with the
        guide; a resumed one has it already.

        Args:
          prompt: The turn's message.
          resume: The thread to continue, or None to start one.
          extra: Arguments for the CLI, before the prompt.

        Returns:
          argv: The command.

        """
        if resume is not None:
            return ["codex", "exec", "resume", "--json", *extra, resume, prompt]
        return ["codex", "exec", "--json", *extra, f"{GUIDE}\n\n{prompt}"]

    def read(self, stdout: str) -> Turn:
        """Return the turn's last agent message and its thread.

        Args:
          stdout: What the command printed.

        Returns:
          turn: The answer.

        Raises:
          HelperError: No agent message came.

        """
        thread: str | None = None
        text = ""
        for line in stdout.splitlines():
            try:
                event = _object(loads(line))
            except ValueError:
                continue
            item = _object(event.get("item"))
            if event.get("type") == "thread.started":
                thread = _text(event.get("thread_id")) or None
            elif (
                event.get("type") == "item.completed"
                and item.get("type") == "agent_message"
            ):
                text = _text(item.get("text"))
        if not text.strip():
            raise HelperError("codex printed no answer")
        return Turn(text=text, cli_session_id=thread)


class Memory:
    """Which CLI conversation each Chat conversation is, kept in a JSON file.

    A file that is missing or unreadable is an empty memory: the next message of
    each conversation then starts a CLI conversation afresh.
    """

    def __init__(self, path: Path) -> None:
        """Keep the file's path; it is read on every lookup."""
        self.path = path

    def get(self, conversation_id: uuid.UUID) -> str | None:
        """Return the CLI conversation a Chat conversation continues, if any."""
        return _text(self._read().get(str(conversation_id))) or None

    def put(self, conversation_id: uuid.UUID, cli_session_id: str) -> None:
        """Remember the CLI conversation of a Chat conversation.

        Args:
          conversation_id: The Chat conversation.
          cli_session_id: The CLI's id for the conversation that answers it.

        """
        kept = self._read()
        kept[str(conversation_id)] = cli_session_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        written = self.path.with_suffix(".tmp")
        written.write_text(json.dumps(kept, sort_keys=True), encoding="utf-8")
        written.replace(self.path)

    def _read(self) -> dict[str, object]:
        try:
            return _object(loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}


type Runner = Callable[[Sequence[str]], str]
"""Run one turn's command and return what it printed; raise HelperError on failure."""


def serve(
    client: Client,
    cli: HelperCli,
    *,
    actor: str,
    extra: Sequence[str],
    memory: Memory,
    run: Runner,
    stop: threading.Event,
    redactor: Redactor | None = None,
    wait_sec: float = 25.0,
    retry_sec: float = 5.0,
) -> None:
    """Open the helper's session and serve science chat until ``stop`` is set.

    It first answers each conversation whose session ends in a line nobody
    answered, as after a restart, then each line as it comes. A line from outside
    Chat names no conversation, and is left. Every session it opened ends however
    the loop does.

    Args:
      client: The Trackinizer client, as the assistant's account.
      cli: The model CLI that answers.
      actor: The routing name to open the session under.
      extra: Arguments for the CLI on every turn.
      memory: Which CLI conversation each Chat conversation is.
      run: Runs one turn's command.
      stop: Ends the loop once set.
      redactor: Masks secret values in every record uploaded to a chat's session.
      wait_sec: How long each drain waits for a message.
      retry_sec: How long to wait before draining again after the server fails.

    """
    # The service session names itself, so a restart resumes it: a line posted while
    # the helper was down is still queued for it, and a new session would not have it.
    started = client.session_start(
        SessionStart(
            cli=CHAT_HELPER_CLI,
            cli_session_id=f"{CHAT_HELPER_CLI}:{actor}",
            actor=actor,
            title=f"Chat helper ({cli.name})",
            started=datetime.now(UTC),
        ),
    )
    chats = ChatSessions(client, actor=actor, redactor=redactor)
    try:
        for owed in chats.owed():
            _answer(
                chats,
                cli,
                prompt=render_inbound(owed.text, owed.author, None),
                conversation_id=owed.conversation_id,
                extra=extra,
                memory=memory,
                run=run,
            )
        while not stop.is_set():
            try:
                drained = client.drain_inbound(started.id, wait_sec=wait_sec)
            except ClientError:
                _logger.warning("trax helper: could not drain; retrying", exc_info=True)
                _ = stop.wait(retry_sec)
                continue
            for text, source, room, context in drained:
                if context is None or context.conversation_id is None:
                    continue
                forked: list[ForkLine] = []
                try:
                    forked = chats.fork(
                        context.conversation_id,
                        fork=context.fork,
                        poster=source,
                        title=text,
                    )
                    chats.hear(context.conversation_id, poster=source, text=text)
                except ClientError:
                    _logger.warning(
                        "trax helper: could not record a line of %s",
                        context.conversation_id,
                        exc_info=True,
                    )
                    continue
                _answer(
                    chats,
                    cli,
                    prompt=render_fork_lines(
                        render_inbound(text, source, room, context=context),
                        lines=forked,
                    ),
                    conversation_id=context.conversation_id,
                    extra=extra,
                    memory=memory,
                    run=run,
                )
    finally:
        chats.close()
        try:
            _ = client.session_end(started.id, SessionEnd(ended=datetime.now(UTC)))
        except ClientError:
            _logger.warning("trax helper: could not end its session", exc_info=True)


def main(argv: Sequence[str], *, client_factory: Callable[[], Client]) -> int:
    """Run ``trax helper``, as ``trax/cli.py`` calls it.

    Args:
      argv: ``trax helper`` arguments; everything after ``--`` goes to the CLI.
      client_factory: Builds the Trackinizer client from the active profile.

    Returns:
      exit_code: 0 once the helper stops.

    """
    own, extra = list(argv), list[str]()
    if "--" in argv:
        split = argv.index("--")
        own, extra = list(argv[:split]), list(argv[split + 1 :])
    parser = argparse.ArgumentParser(
        prog="trax helper",
        description=(__doc__ or "").strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args(own))
    cli: HelperCli = Claude() if flags.cli == "claude" else Codex()
    stop = threading.Event()
    try:
        serve(
            client_factory(),
            cli,
            actor=flags.actor,
            extra=extra,
            memory=Memory(
                state_dir() / "rekursiv-ai" / "trax" / "helper" / f"{flags.actor}.json",
            ),
            run=lambda command: _run(command, timeout_sec=flags.turn_timeout),
            stop=stop,
            redactor=redactor_from_environ(os.environ),
        )
    except KeyboardInterrupt:
        stop.set()
    return 0


class _Flags(Protocol):
    cli: str
    actor: str
    turn_timeout: float


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``trax helper``'s own flags on ``parser``."""
    parser.add_argument(
        "cli",
        choices=("claude", "codex"),
        help="The model CLI that answers.",
    )
    parser.add_argument(
        "--as",
        dest="actor",
        default="helper",
        help="The assistant's actor, as the server's --assistant names it.",
    )
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=900.0,
        help="Seconds one answer may take.",
    )


def _answer(
    chats: ChatSessions,
    cli: HelperCli,
    *,
    prompt: str,
    conversation_id: uuid.UUID,
    extra: Sequence[str],
    memory: Memory,
    run: Runner,
) -> None:
    """Answer one line by a CLI turn, resuming its conversation; a failure is said in it."""
    resume = memory.get(conversation_id)
    try:
        turn = _turn(cli, prompt=prompt, resume=resume, extra=extra, run=run)
    except HelperError as error:
        _logger.warning("trax helper: no answer for %s: %s", conversation_id, error)
        _record(
            chats,
            conversation_id=conversation_id,
            text=f"The helper could not answer: {error}",
        )
        return
    if turn.cli_session_id is not None:
        memory.put(conversation_id, turn.cli_session_id)
    _record(chats, conversation_id=conversation_id, text=turn.text)


# An answer the server would not take leaves the line last in its session, so the next
# start finds it owed; the helper has other conversations to serve meanwhile.
def _record(chats: ChatSessions, *, conversation_id: uuid.UUID, text: str) -> None:
    """Record an answer in its conversation's session; a server failure is logged."""
    try:
        chats.answer(conversation_id, text=text)
    except ClientError:
        _logger.warning(
            "trax helper: could not record the answer to %s",
            conversation_id,
            exc_info=True,
        )


# A conversation whose CLI session is gone (deleted, or from another machine) is
# better answered afresh than not at all: the Chat itself still shows its history.
def _turn(
    cli: HelperCli,
    *,
    prompt: str,
    resume: str | None,
    extra: Sequence[str],
    run: Runner,
) -> Turn:
    """Run one turn, starting afresh when the conversation it resumes cannot be resumed."""
    try:
        return cli.read(run(cli.argv(prompt, resume=resume, extra=extra)))
    except HelperError:
        if resume is None:
            raise
        _logger.warning("trax helper: could not resume %s; starting afresh", resume)
        return cli.read(run(cli.argv(prompt, resume=None, extra=extra)))


def _run(command: Sequence[str], *, timeout_sec: float) -> str:
    """Run one turn's command and return its output."""
    try:
        done = subprocess.run(  # noqa: S603 -- the user's chosen CLI and flags; no shell.
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HelperError(f"{command[0]} did not answer: {error}") from error
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()[-1:] or [""]
        raise HelperError(f"{command[0]} exited {done.returncode}: {detail[0]}")
    return done.stdout


def _object(value: object) -> dict[str, object]:
    """Return a JSON object, or an empty one for anything else a CLI printed."""
    try:
        return from_plain(value, dict[str, object], default={})
    except ReadError:
        return {}


def _text(value: object) -> str:
    """Return a JSON string, or "" for anything else a CLI printed."""
    try:
        return from_plain(value, str, default="")
    except ReadError:
        return ""
