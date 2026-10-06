#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Write to a local trackinizer server as a second client, for the live suite.

It reads one JSON command per line on stdin and answers each with one JSON
line on stdout. Every write goes through the Python client, and each answer
carries ``at``, the time in epoch milliseconds when the write returned, so the
page's clock can time the change from there. A command that fails answers
``{"error": ...}`` and the writer carries on.

Commands:
  {"op": "create", "title": "...", "labels": ["..."]}  -> {"id", "at"}
  {"op": "edit", "id": "...", "field": "title", "value": "..."}  -> {"at"}
  {"op": "edge", "from": "...", "to": "...", "kind": "requires"}  -> {"at"}
  {"op": "edge", ..., "remove": true}  -> {"at"}
  {"op": "burst", "rate": 20, "seconds": 60, "title": "...", "labels": [...]}
      -> {"ids", "ats"}: creates, evenly spaced, titled "<title> <n>"
  {"op": "steady", "ids": [...], "rate": 3.1, "seconds": 30}
      -> {"ats"}: title edits, taking the ids in turn

Local servers only: it writes with whatever identity the server grants.

Examples:
  echo '{"op": "create", "title": "Hello", "labels": ["x"]}' | ./writer.py
  ./writer.py --url http://127.0.0.1:8871 < commands.jsonl

'''
# fmt: on

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, cast

import argparse
import functools
import json
import math
import sys
import time
import uuid

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.lib.custom_json import ReadError, convert, parse


if TYPE_CHECKING:
    from trackinizer.types.inquiries import Inquiry


_ACTOR: Final = "e2e-writer"


def main() -> int:
    """Answer the commands on stdin until it closes.

    Returns:
      result: Process exit code (0 on success).

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args())
    clock = Clock(now=time.time, sleep=time.sleep)
    with Client(flags.url, author=_ACTOR) as client:
        for line in sys.stdin:
            if line.strip():
                print(json.dumps(answer(client, line, clock=clock)), flush=True)
    return 0


class Writes(Protocol):
    """The part of the Python client the writer uses."""

    def submit(
        self,
        kind: Inquiry.InquiryKind,
        body: Mapping[str, object],
    ) -> uuid.UUID:
        """Create an inquiry and return its id."""
        ...

    def edit(
        self,
        target_id: uuid.UUID,
        field: str,
        value: object,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        """Overwrite one field."""
        ...

    def add_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: Inquiry.Actor,
    ) -> object:
        """Add an edge."""
        ...

    def remove_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        """Remove an edge."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Clock:
    """Wall-clock seconds and a sleep; tests pass fakes."""

    now: Callable[[], float]
    sleep: Callable[[float], None]

    def ms(self) -> int:
        """Return the time in epoch milliseconds, as the page's ``Date.now()`` reads it."""
        return round(self.now() * 1000)


def answer(client: Writes, line: str, *, clock: Clock) -> dict[str, object]:
    """Run the command on ``line`` and return its answer.

    Args:
      client: Where the writes go.
      line: One JSON command.
      clock: Times the writes and paces the bursts.

    Returns:
      answer: The command's result, or ``{"error": ...}`` when it failed.

    """
    # ReadError is a TypeError, so a field of the wrong type answers with the field's
    # own message; a separate arm for it would misreport every field as the line.
    try:
        return _run(client, parse(line, dict[str, object]), clock=clock)
    except (ClientError, KeyError, TypeError, ValueError) as error:
        return {"error": f"{type(error).__name__}: {error}"}


def _run(
    client: Writes,
    command: Mapping[str, object],
    *,
    clock: Clock,
) -> dict[str, object]:
    """Dispatch one command by its ``op``."""
    match _field(command, "op", str):
        case "create":
            created = _create(client, command, title=_field(command, "title", str))
            return {"id": str(created), "at": clock.ms()}
        case "edit":
            client.edit(
                _field(command, "id", uuid.UUID),
                _field(command, "field", str),
                command["value"],
                actor=_ACTOR,
            )
            return {"at": clock.ms()}
        case "edge":
            _edge(client, command)
            return {"at": clock.ms()}
        case "burst":
            ids: list[str] = []
            title = _field(command, "title", str)

            def create(n: int) -> None:
                ids.append(str(_create(client, command, title=f"{title} {n}")))

            return {"ats": _paced(command, clock, create), "ids": ids}
        case "steady":
            targets = _field(command, "ids", list[uuid.UUID])
            if not targets:
                raise ValueError("steady needs at least one id.")
            edit = functools.partial(_steady_edit, client, targets)
            return {"ats": _paced(command, clock, edit)}
        case op:
            raise ValueError(f"Unknown op {op!r}.")


def _field[T](command: Mapping[str, object], key: str, target: type[T]) -> T:
    """Return ``command[key]`` as ``target``; a ReadError names ``key``."""
    try:
        return convert(command[key], target)
    except ReadError as error:
        raise ReadError(f"{key!r}: {error}") from error


def _create(client: Writes, command: Mapping[str, object], *, title: str) -> uuid.UUID:
    """Create one Issue titled ``title`` with ``command``'s labels."""
    return client.submit(
        "Issue",
        {"title": title, "labels": _field(command, "labels", list[str])},
    )


def _steady_edit(client: Writes, targets: Sequence[uuid.UUID], n: int) -> None:
    """Retitle the ``n``-th edit's target, taking ``targets`` in turn."""
    client.edit(targets[n % len(targets)], "title", f"Steady edit {n}", actor=_ACTOR)


def _edge(client: Writes, command: Mapping[str, object]) -> None:
    ends = (
        _field(command, "from", uuid.UUID),
        _field(command, "to", uuid.UUID),
        _field(command, "kind", str),
    )
    if command.get("remove") is True:
        client.remove_edge(*ends, actor=_ACTOR)
    else:
        client.add_edge(*ends, actor=_ACTOR)


# Each write has its own slot counted from the start, so one slow write is caught up by
# those after it instead of pushing every later one back.
def _paced(
    command: Mapping[str, object],
    clock: Clock,
    write: Callable[[int], None],
) -> list[int]:
    """Call ``write`` ``rate`` times a second for ``seconds``, evenly."""
    rate = _field(command, "rate", float)
    total = rate * _field(command, "seconds", float)
    if math.isinf(total) or math.isnan(total):
        raise ValueError(f"rate {rate} for {command['seconds']} s is no count.")
    start = clock.now()
    ats: list[int] = []
    for n in range(round(total)):
        clock.sleep(max(0.0, start + n / rate - clock.now()))
        write(n)
        ats.append(clock.ms())
    return ats


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8765",
        help="The server to write to.",
    )


class _Flags(Protocol):
    url: str


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
