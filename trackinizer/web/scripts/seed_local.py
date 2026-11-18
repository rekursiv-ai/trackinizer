#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Seed the local web app preview with a small, representative graph.

The graph covers what the UI must render: an Issue tree with a prerequisite and
edge priorities, a ``question`` Issue for Needs you, a Belief with supporting and
opposing evidence, Papers with and without authors, an Experiment with metrics,
an AgentSession with a transcript, a status and a judgement change for Activity,
and 60 backlog Issues so lists page. The graph is one batch, skipped when the
root Issue exists; every later step is idempotent and runs each time.
``--writer`` then keeps editing as a second client, for the live checks.
Local servers only: it writes with whatever identity the server grants.

Examples:
  ./seed_local.py                      # against scripts/preview.sh on :8765
  ./seed_local.py --writer --interval 1

'''
# fmt: on

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol, cast

import argparse
import itertools
import time
import uuid

from trackinizer.client.client import Client
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    ShellCommandResult,
    Thinking,
    ToolCall,
    TurnContext,
    UserMessage,
)
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.filters import Filter
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody
from trackinizer.wire.wire_sessions import SessionStart


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.types.inquiries import Inquiry


_ROOT_TITLE: Final = "Web app preview: seeded root"
_EXPERIMENT_TITLE: Final = "Membership check latency"
_BELIEF_TITLE: Final = "A membership check holds at most 13 ids"
# A fixed key, so a rerun replays the session start instead of opening a second.
_SESSION_KEY: Final = uuid.uuid5(uuid.NAMESPACE_URL, "trackinizer-web-app-seed-session")
_ACTOR: Final = "seed"
_OWNERS: Final = ("dan", "Agent", "josh", None)

_DESCRIPTION: Final = """\
The seeded root for the **local web app preview**.

- Lists, detail and relations render from this tree.
- `inline code`, a [link](https://example.com), and a fenced block:

```python
print("hello")
```
"""


def main() -> int:
    """Seed the preview server, then optionally keep writing.

    Returns:
      result: Process exit code (0 on success).

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args())
    with Client(flags.url, author=_ACTOR) as client:
        root = _find(client, "Issue", _ROOT_TITLE) or _seed(client)
        _log_metrics(client)
        _settle(client)
        _seed_transcript(client)
        print(f"root: {root}")
        if flags.writer:
            _write_forever(client, root, interval=flags.interval)
    return 0


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--writer",
        action="store_true",
        help="After seeding, keep making changes every --interval seconds.",
    )
    parser.add_argument("--interval", type=float, default=2.0)


def _find(client: Client, kind: Inquiry.InquiryKind, title: str) -> uuid.UUID | None:
    rows = client.list_kind(
        kind,
        limit=1,
        filters=(Filter(field="title", op="is", value=title),),
    )
    return uuid.UUID(str(rows[0]["id"])) if rows else None


def _seed(client: Client) -> uuid.UUID:
    items: list[tuple[Inquiry.InquiryKind, dict[str, object]]] = [
        ("Issue", _issue(_ROOT_TITLE, priority=10, kinds=["feature"], owner="dan")),
        ("Issue", _issue("Lists: filter, group and sort", priority=0, kinds=["task"])),
        (
            "Issue",
            _issue("Detail: fields and relations", priority=20, status="complete"),
        ),
        ("Issue", _issue("Live updates across views", priority=20, kinds=["feature"])),
        (
            "Issue",
            _issue(
                "Which default sort should lists use?",
                kinds=["question"],
                owner="dan",
                subscribers=["Agent"],
            ),
        ),
        (
            "Belief",
            {
                "title": _BELIEF_TITLE,
                "judgement": "unproven",
                "confidence": 0.9,
            },
        ),
        (
            "Paper",
            {"title": "A paper with no authors", "source": "https://example.com/a"},
        ),
        (
            "Paper",
            {
                "title": "Incremental layout for provenance graphs",
                "source": "https://example.com/b",
                "authors": ["Ada Lovelace", "Alan Turing"],
            },
        ),
        (
            "Experiment",
            {
                "title": _EXPERIMENT_TITLE,
                "outcome": "0.19 to 0.23 s per check",
                "config": {"ids": 13, "runs": 20},
            },
        ),
    ]
    edges: list[dict[str, object]] = [
        {"from_index": 1, "to_index": 0, "edge_kind": "narrows", "priority": 0},
        {"from_index": 2, "to_index": 0, "edge_kind": "narrows", "priority": 20},
        {"from_index": 3, "to_index": 0, "edge_kind": "narrows"},
        {"from_index": 4, "to_index": 0, "edge_kind": "narrows"},
        {"from_index": 3, "to_index": 1, "edge_kind": "requires"},
        {"from_index": 5, "to_index": 3, "edge_kind": "produced_by"},
        {"from_index": 8, "to_index": 3, "edge_kind": "produced_by"},
        {"from_index": 7, "to_index": 5, "edge_kind": "proves", "valence": 0.8},
        {"from_index": 6, "to_index": 5, "edge_kind": "favors", "valence": -0.4},
        {"from_index": 8, "to_index": 5, "edge_kind": "proves", "valence": 0.6},
    ]
    # One batch, so seeding either lands whole or not at all, and a rerun after
    # a failure starts clean: the root's existence means the graph is complete.
    backlog = _backlog()
    edges.extend(
        {"from_index": index, "to_index": 0, "edge_kind": "narrows"}
        for index in range(len(items), len(items) + len(backlog))
    )
    return client.submit_batch(items + backlog, edges=edges, actor=_ACTOR)[0]


def _log_metrics(client: Client) -> None:
    # Idempotent on (key, step), so every run re-logs them: a run that died
    # between the batch and this call is completed by the next.
    experiment = _find(client, "Experiment", _EXPERIMENT_TITLE)
    if experiment is None:
        raise SystemExit(f"seeded experiment {_EXPERIMENT_TITLE!r} is missing")
    client.log_metrics(
        experiment,
        [
            MetricPoint(key="latency_s", step=s, value=0.19 + 0.004 * s)
            for s in range(10)
        ],
    )


def _settle(client: Client) -> None:
    # Activity needs status and judgement changes. Setting a value a row already
    # holds writes nothing, so a rerun adds no rows.
    belief = _find(client, "Belief", _BELIEF_TITLE)
    item = _find(client, "Issue", "Backlog item 3")
    if belief is None or item is None:
        raise SystemExit("seeded rows are missing; reset the preview database")
    client.edit(belief, "judgement", "proven", actor=_ACTOR, reason="measured")
    client.edit(item, "status", "abandoned", actor=_ACTOR, reason="superseded")


def _seed_transcript(client: Client) -> None:
    # Appends are idempotent on (session, part, idx), so a rerun rewrites nothing.
    session = client.session_start(
        SessionStart(
            cli="claude",
            title="Seeded agent session",
            actor=_ACTOR,
            idempotency_key=_SESSION_KEY,
        ),
    )
    turns = (
        TurnContext(model="claude-opus-5-5", extra={"cwd": "/work"}),
        UserMessage(content="What does **Issue#1** ask for?"),
        Thinking(content="Reading the seeded root first."),
        ToolCall(call_id="c1", name="Bash", arguments={"command": "trax issue 1"}),
        ShellCommandResult(call_id="c1", stdout="Issue#1 [active]\n", exit_code=0),
        AssistantMessage(content="It is the seeded root; see `Issue#1`."),
    )
    records = [
        RecordBody.of(
            SessionRecordRow.of(
                session_id=session.id,
                part=0,
                idx=idx,
                record=_nth(turns, idx),
            ),
        )
        for idx in range(40)
    ]
    client.append_records(
        session.id,
        name="main.jsonl",
        manifest=ManifestBody(
            name="main.jsonl",
            metadata={},
            ir_id=_SESSION_KEY,
            format="claude",
            records=len(records),
        ),
        records=records,
    )


def _backlog() -> list[tuple[Inquiry.InquiryKind, dict[str, object]]]:
    return [
        (
            "Issue",
            _issue(
                f"Backlog item {n}",
                priority=_nth((0, 10, 20, 30, 40, None), n),
                status=_nth(("active", "active", "active", "complete"), n * 3),
                kinds=[_nth(("feature", "bug", "task"), n * 5)],
                owner=_nth(_OWNERS, n * 7),
                labels=["seed", _nth(("ui", "server", "docs"), n * 11)],
            ),
        )
        for n in range(60)
    ]


def _issue(
    title: str,
    *,
    priority: int | None = 20,
    status: str = "active",
    kinds: list[str] | None = None,
    owner: str | None = None,
    subscribers: list[str] | None = None,
    labels: list[str] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "title": title,
        "description": _DESCRIPTION,
        "status": status,
        "labels": labels or ["seed"],
    }
    optional = {
        "priority": priority,
        "issue_kind": kinds,
        "owner": owner,
        "subscribers": subscribers,
    }
    body.update({key: value for key, value in optional.items() if value is not None})
    return body


def _write_forever(client: Client, root: uuid.UUID, *, interval: float) -> None:
    for n in itertools.count():
        time.sleep(interval)
        rows = client.list_kind(
            "Issue",
            limit=50,
            filters=(Filter(field="labels", op="re", value="seed"),),
        )
        target = uuid.UUID(str(_nth(rows, n * 7)["id"]))
        match n % 3:
            case 0:
                priority = _nth((0, 10, 20, 30), n)
                client.edit(target, "priority", priority, actor="writer")
                print(f"priority -> {priority}")
            case 1:
                client.add_label(target, f"live-{n}", actor="writer")
                print(f"label live-{n}")
            case _:
                ids = client.submit_batch(
                    [("Issue", _issue(f"Live item {n}", labels=["seed", "live"]))],
                    edges=[
                        {"from_index": 0, "to_id": str(root), "edge_kind": "narrows"},
                    ],
                    actor="writer",
                )
                print(f"created {ids[0]}")


def _nth[T](options: Sequence[T], n: int) -> T:
    """Pick deterministically, so every seeded database is the same."""
    return options[n % len(options)]


class _Flags(Protocol):
    url: str
    writer: bool
    interval: float


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
