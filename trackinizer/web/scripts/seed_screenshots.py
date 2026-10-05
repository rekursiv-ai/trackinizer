#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Seed an empty local server with the synthetic records the README's screenshots show.

First a large graph: the shape of a real research graph, read from
graph_structure.json, with invented contents (seed_graph.py). Then three
research efforts, each an Issue tree with the Beliefs, Experiments, Papers and
agent sessions it produced, so the graph's newest islands are theirs. Among
them: a Belief with evidence for and against, a Paper that cites and is cited,
an Experiment with metrics, an HTML Artifact, and three agents talking in two
rooms for the console. Every person is an example.com address and every text is
fixed, so every seed writes the same.

It prints one JSON line, the ids of what the screenshots and the demo video
open: the Belief, Paper and Experiment, the Issue and HTML Artifact, the large
graph's showcase root and Belief, and the session the demo messages. Run it
once, against an empty database, as scripts/screenshots.ts does: a second run
adds a second copy. Local servers only: it writes with whatever identity the
server grants.

Examples:
  ./seed_screenshots.py --url http://127.0.0.1:8812

'''
# fmt: on

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast

import argparse
import itertools
import json
import math
import uuid

from trackinizer.client.client import Client
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    SessionRecord,
    ShellCommandResult,
    ToolCall,
    UserMessage,
)
from trackinizer.lib.custom_json import convert
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.web.scripts.graph_structure import GRAPH_STRUCTURE, load
from trackinizer.web.scripts.seed_graph import seed_graph
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody
from trackinizer.wire.wire_sessions import SessionStart


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from trackinizer.lib.custom_json import JSONValue
    from trackinizer.types.inquiries import Inquiry
    from trackinizer.web.scripts.graph_structure import Structure
    from trackinizer.wire.wire_sessions import SessionEnd, SessionStartResponse

    type _Node = tuple[str, Inquiry.InquiryKind, dict[str, object]]


def main() -> int:
    """Seed the server at ``--url`` and print what the screenshots open.

    Returns:
      result: Process exit code (0 on success).

    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args())
    structure = load(GRAPH_STRUCTURE.read_text())
    # The large graph's heaviest batch walks some 100,000 steps of the server's change
    # cascade (seed_graph.cascade_order): two minutes on a PGlite server, and a retry
    # after a timeout only starts it again.
    with Client(flags.url, author="ada@example.com", timeout_sec=600.0) as client:
        shots = seed(client, now=datetime.now(UTC), structure=structure)
    print(
        json.dumps(
            {
                "belief": str(shots.belief),
                "paper": str(shots.paper),
                "experiment": str(shots.experiment),
                "issue": str(shots.issue),
                "artifact": str(shots.artifact),
                "root": str(shots.root),
                "cited": str(shots.cited),
                "session": str(shots.session),
                "agent": shots.agent,
            },
        ),
    )
    return 0


class SeedClient(Protocol):
    """The writes ``seed`` makes, as :class:`Client` makes them."""

    def submit_batch(
        self,
        items: Sequence[tuple[Inquiry.InquiryKind, Mapping[str, object]]],
        *,
        edges: Sequence[Mapping[str, object]] = (),
        actor: str | None = None,
    ) -> list[uuid.UUID]:
        """Create ``items`` and ``edges`` in one batch; return their ids."""
        ...

    def log_metrics(
        self,
        experiment_id: uuid.UUID,
        points: Sequence[MetricPoint],
    ) -> object:
        """Log ``points`` to the Experiment ``experiment_id``."""
        ...

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        """Open an agent session."""
        ...

    def append_records(
        self,
        session_id: uuid.UUID,
        *,
        name: str = "",
        manifest: ManifestBody | None = None,
        records: Sequence[RecordBody] = (),
    ) -> object:
        """Append records to one file of a session."""
        ...

    def add_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: str,
        valence: float | None = None,
    ) -> object:
        """Add one edge."""
        ...

    def session_end(
        self,
        session_id: uuid.UUID,
        body: SessionEnd | None = None,
    ) -> object:
        """End the session ``session_id``."""
        ...

    def post(self, path: str, *, body: object = None) -> JSONValue:
        """Send one POST request."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Shots:
    """What the screenshots and the demo video open."""

    belief: uuid.UUID
    paper: uuid.UUID
    experiment: uuid.UUID
    issue: uuid.UUID
    """The Issue the demo opens: the tokenizer effort's root."""

    artifact: uuid.UUID
    """The HTML Artifact the demo opens."""

    root: uuid.UUID
    """The large graph's showcase island, which the demo frames."""

    cited: uuid.UUID
    """The Belief in it that the demo selects and focuses on."""

    session: uuid.UUID
    """The live session of ``agent``, which the demo messages."""

    agent: str


def seed(client: SeedClient, *, now: datetime, structure: Structure) -> Shots:
    """Write the screenshots' records through ``client``.

    Args:
      client: A client of an empty server.
      now: When the last turn happened. The console's timeline counts records by
        when the server stored them, and each line shows the record's own stamp, so
        the turns end where the server stores them, as live capture's do.
      structure: The large graph's shape, written first, so it is the oldest.

    Returns:
      shots: The ids of what the screenshots and the demo video open.

    """
    graph = seed_graph(client, structure, actor="ada@example.com")
    nodes = _nodes()
    keys = [key for key, _, _ in nodes]
    created = client.submit_batch(
        [(kind, body) for _, kind, body in nodes],
        edges=[
            {
                "from_index": keys.index(child),
                "edge_kind": kind,
                "to_index": keys.index(parent),
                **({} if valence is None else {"valence": valence}),
            }
            for child, kind, parent, valence in _links()
        ],
        actor="ada@example.com",
    )
    ids = dict(zip(keys, created, strict=True))
    for key, points in _metrics().items():
        client.log_metrics(ids[key], points)
    artifact = _publish(client, issue=ids["tok-cache"], cites=ids["cache-bench"])
    sessions = _converse(client, _ending_at(_sessions(), now), ids)
    return Shots(
        belief=ids["sparse-belief"],
        paper=ids["block-sparse"],
        experiment=ids["cache-bench"],
        issue=ids["tok"],
        artifact=artifact,
        root=graph.root,
        cited=graph.belief,
        # Its last turn waits on reruns, which the demo asks about.
        session=sessions["evals-seeds"],
        agent="evals-seeds",
    )


class _Flags(Protocol):
    url: str


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument("--url", default="http://127.0.0.1:8812")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Session:
    """One agent's session: who, where, the Issue it works on, and its turns."""

    actor: str
    room: str
    title: str
    produced_by: str
    turns: tuple[SessionRecord, ...]


# The console orders turns by when the server stored them, so turns appended session by
# session would show each conversation whole, one after another. Each run of one
# session's turns is one append, and the next run's request stores it a few milliseconds
# later: turns of two sessions never share a store time, which the console would break
# by session id, a random UUID.
def _converse(
    client: SeedClient,
    sessions: Sequence[_Session],
    ids: Mapping[str, uuid.UUID],
) -> dict[str, uuid.UUID]:
    """Start ``sessions``, append every turn in the order the turns happened; ids by actor."""
    started = [client.session_start(_start(session)).id for session in sessions]
    for session_id, session in zip(started, sessions, strict=True):
        client.add_edge(
            session_id,
            ids[session.produced_by],
            "produced_by",
            actor="ada@example.com",
        )
    turns = sorted(
        (
            (_stamp(turn), n, idx)
            for n, session in enumerate(sessions)
            for idx, turn in enumerate(session.turns)
        ),
    )
    for n, run in itertools.groupby(turns, key=lambda turn: turn[1]):
        indexes = [idx for _, _, idx in run]
        client.append_records(
            started[n],
            name="main.jsonl",
            manifest=ManifestBody(
                name="main.jsonl",
                ir_id=uuid.uuid5(uuid.NAMESPACE_URL, sessions[n].actor),
                format="claude",
                records=indexes[-1] + 1,
            ),
            records=[
                RecordBody.of(
                    SessionRecordRow.of(
                        session_id=started[n],
                        part=0,
                        idx=idx,
                        record=sessions[n].turns[idx],
                    ),
                )
                for idx in indexes
            ],
        )
    return {
        session.actor: session_id
        for session, session_id in zip(sessions, started, strict=True)
    }


def _publish(client: SeedClient, *, issue: uuid.UUID, cites: uuid.UUID) -> uuid.UUID:
    """Publish the cache benchmark's HTML report, produced by ``issue``; its Artifact's id."""
    published = convert(
        client.post(
            "/api/artifacts/content",
            body={
                "issue_id": str(issue),
                "title": "Merge-rank cache: benchmark report",
                "summary": (
                    "The cache doubles tokenizer throughput on shard-01 with identical "
                    "token ids; on the small shard it gives 1.7x."
                ),
                "format": "html",
                # The page renders in a sandboxed frame with no network, so it carries
                # its own styles and draws its chart in SVG.
                "html": """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Merge-rank cache</title>
<style>
body{margin:0;padding:24px 28px;font:14px/1.5 system-ui,sans-serif;
background:#14171c;color:#e6e8eb}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 16px;color:#a9b0ba}
.cards{display:flex;gap:12px;margin:0 0 20px}
.card{flex:1;background:#1d2128;border:1px solid #2b313a;border-radius:8px;
padding:12px 14px}.card b{display:block;font-size:22px;color:#7ee2a8}
table{border-collapse:collapse;width:100%;margin-top:16px}
td,th{padding:6px 8px;border-bottom:1px solid #2b313a;text-align:left}
th{color:#a9b0ba;font-weight:500}
</style></head><body>
<h1>Merge-rank cache benchmark</h1>
<p>Three passes per build on shard-01 (1 GB) and shard-07 (100 MB); token ids
compared byte for byte.</p>
<div class="cards">
<div class="card"><b>1.9x</b>throughput on shard-01</div>
<div class="card"><b>1.7x</b>throughput on shard-07</div>
<div class="card"><b>0</b>token ids changed</div>
</div>
<svg viewBox="0 0 520 150" width="100%" role="img"
aria-label="Throughput in million tokens a second, main against the cache">
<g font-size="12" fill="#a9b0ba">
<text x="0" y="34">main, shard-01</text><text x="0" y="64">cache, shard-01</text>
<text x="0" y="104">main, shard-07</text><text x="0" y="134">cache, shard-07</text>
</g>
<rect x="120" y="22" width="150" height="16" rx="3" fill="#5b6472"/>
<rect x="120" y="52" width="286" height="16" rx="3" fill="#7ee2a8"/>
<rect x="120" y="92" width="142" height="16" rx="3" fill="#5b6472"/>
<rect x="120" y="122" width="241" height="16" rx="3" fill="#7ee2a8"/>
<g font-size="12" fill="#e6e8eb">
<text x="276" y="35">1.04 M/s</text><text x="412" y="65">1.98 M/s</text>
<text x="268" y="105">0.98 M/s</text><text x="367" y="135">1.67 M/s</text>
</g></svg>
<table><tr><th>Shard</th><th>main</th><th>cache</th><th>Speedup</th></tr>
<tr><td>shard-01 (1 GB)</td><td>1.04 M/s</td><td>1.98 M/s</td><td>1.9x</td></tr>
<tr><td>shard-07 (100 MB)</td><td>0.98 M/s</td><td>1.67 M/s</td><td>1.7x</td></tr>
</table></body></html>
""",
                "citations": [{"record_id": str(cites)}],
            },
        ),
        dict[str, object],
    )
    return uuid.UUID(convert(published.get("artifact_id"), str))


def _ending_at(sessions: Sequence[_Session], end: datetime) -> list[_Session]:
    """``sessions`` with every turn moved alike, so the last of them happened at ``end``."""
    shift = end - max(_stamp(turn) for session in sessions for turn in session.turns)
    return [
        replace(
            session,
            turns=tuple(
                replace(turn, timestamp=(_stamp(turn) + shift).isoformat())
                for turn in session.turns
            ),
        )
        for session in sessions
    ]


def _start(session: _Session) -> SessionStart:
    return SessionStart(
        cli="claude",
        title=session.title,
        actor=session.actor,
        rooms=[session.room],
        started=_stamp(session.turns[0]),
    )


def _stamp(turn: SessionRecord) -> datetime:
    stamp = getattr(turn, "timestamp", None)
    assert isinstance(stamp, str), f"every seeded turn is stamped: {turn}"
    return datetime.fromisoformat(stamp)


def _nodes() -> list[_Node]:
    """Every inquiry: the key ``_links`` names it by, its kind, and its body."""
    return [
        # The tokenizer effort: the Experiment screenshot's.
        _issue(
            "tok",
            "Make the tokenizer twice as fast",
            owner="ada",
            kind="feature",
            priority=10,
            labels=["tokenizer", "perf"],
            subscribers=["ben@example.com"],
            description=(
                "Tokenizing the training shards takes 40 minutes a run. "
                "**Target:** twice the throughput, with identical token ids.\n\n"
                "- Profile first, then cache or vectorize what dominates.\n"
                "- Check every change against `main` on shard-01 (1 GB).\n"
            ),
        ),
        _issue(
            "tok-profile",
            "Profile the merge loop",
            owner="ada",
            kind="task",
            priority=10,
            status="complete",
        ),
        _issue(
            "tok-cache",
            "Cache merge ranks between calls",
            owner="ben",
            kind="feature",
            priority=10,
            labels=["tokenizer", "perf"],
        ),
        _issue(
            "tok-vocab",
            "Clear the rank cache when the vocabulary changes",
            owner="ben",
            kind="bug",
            status="complete",
        ),
        _issue(
            "tok-vector",
            "Vectorize pre-tokenization",
            owner="ada",
            kind="feature",
            priority=30,
            labels=["tokenizer", "perf"],
        ),
        _artifact(
            "tok-belief",
            "Belief",
            "Merge-rank lookups dominate tokenizer time",
            owner="ada",
            labels=["tokenizer"],
            judgement="proven",
            confidence=0.8,
            description=(
                "Most of each call goes to looking up merge ranks; "
                "pre-tokenization and I/O share the rest."
            ),
        ),
        _artifact(
            "tok-profile-run",
            "Experiment",
            "Profile of a 1 GB shard",
            owner="ada",
            labels=["tokenizer", "profile"],
            status="complete",
            outcome="62% of the time is in merge-rank lookups, 21% in pre-tokenization",
            config={"shard": "shard-01", "profiler": "sampling", "seconds": 300},
        ),
        _artifact(
            "cache-bench",
            "Experiment",
            "Merge-rank cache benchmark",
            owner="ben",
            labels=["tokenizer", "perf", "benchmark"],
            status="complete",
            outcome=(
                "1.9x throughput: 1.04 M to 1.98 M tokens/s on shard-01, "
                "with identical token ids"
            ),
            config={
                "shard": "shard-01 (1 GB)",
                "baseline": "main",
                "passes": 3,
                "cache_entries": 65_536,
            },
            description=(
                "Three passes of each build over the same shard. The cache warms "
                "during the first pass, so throughput climbs before it levels off."
            ),
        ),
        _artifact(
            "rank-tables",
            "Paper",
            "Rank tables for linear-time byte-pair merges",
            owner="ben",
            labels=["tokenizer"],
            authors=["Ben Sample", "Dee Placeholder"],
            publication_type="inproceedings",
            venue="Workshop on Text Processing Systems",
            publish_date="2024-06-10T00:00:00Z",
            source="https://example.com/papers/rank-tables",
            abstract=(
                "Byte-pair encoders spend most of their time finding the "
                "lowest-ranked pair to merge. We keep ranks in a flat table "
                "indexed by pair and reuse it across calls, which makes each "
                "merge step constant time."
            ),
        ),
        # The eval-drift effort.
        _issue(
            "drift",
            "Find why eval scores drift between runs",
            owner="cleo",
            kind="bug",
            priority=0,
            labels=["evals", "reproducibility"],
            subscribers=["ada@example.com"],
            description=(
                "The same checkpoint scores anywhere from 69.8 to 71.6 on the "
                "held-out set. Until the spread is under half a point, no "
                "difference under two points means anything."
            ),
        ),
        _issue(
            "drift-seed",
            "Seed every sampler",
            owner="cleo",
            kind="task",
            status="complete",
        ),
        _issue(
            "drift-order",
            "Pin the eval data order",
            owner="cleo",
            kind="task",
            status="complete",
        ),
        _issue("drift-gpu", "Rerun on one GPU type", owner="ada", kind="task"),
        _artifact(
            "drift-belief",
            "Belief",
            "Unseeded sampling causes the eval drift",
            owner="cleo",
            labels=["evals"],
            judgement="proven",
            confidence=0.85,
        ),
        _artifact(
            "drift-gpu-belief",
            "Belief",
            "GPU type moves scores by under 0.1 points",
            owner="cleo",
            labels=["evals"],
            judgement="unproven",
            confidence=0.5,
        ),
        _artifact(
            "drift-reruns",
            "Experiment",
            "Ten reruns with fixed seeds",
            owner="cleo",
            labels=["evals", "reproducibility"],
            status="complete",
            outcome="Spread fell from 1.8 to 0.2 points (71.3 to 71.5)",
            config={"checkpoint": "step-40000", "runs": 10, "seed": 7},
        ),
        # The long-context survey: the Belief and Paper screenshots'.
        _issue(
            "survey",
            "Survey attention past 64k tokens",
            owner="ben",
            kind="task",
            labels=["attention", "literature"],
            description=(
                "Which sparse-attention schemes keep quality on long inputs, and "
                "where does each one break?"
            ),
        ),
        _issue(
            "survey-read",
            "Read the block-sparse papers",
            owner="ben",
            kind="task",
            status="complete",
        ),
        _issue(
            "survey-summary",
            "Summarize what holds at 64k tokens and beyond",
            owner="ben",
            kind="task",
        ),
        _artifact(
            "sparse-belief",
            "Belief",
            "Block-sparse attention keeps quality up to 64k tokens",
            owner="ben",
            labels=["attention"],
            judgement="unproven",
            confidence=0.6,
            description=(
                "It holds for retrieval and summarization at 64k tokens. Past "
                "96k, recall of single facts falls off faster than with dense "
                "attention.\n\n**Open:** whether a wider local window closes "
                "that gap."
            ),
        ),
        _artifact(
            "needle",
            "Experiment",
            "Needle recall at 64k: block-sparse against dense",
            owner="ben",
            labels=["attention"],
            status="complete",
            outcome="Block-sparse recalls 94% of needles at 64k tokens, dense 97%",
            config={"lengths": "8k-64k", "needles": 200},
        ),
        _artifact(
            "block-sparse",
            "Paper",
            "Block-sparse attention at long context",
            owner="ben",
            labels=["attention"],
            authors=["Ada Example", "Ben Sample", "Cleo Demo"],
            publication_type="inproceedings",
            venue="Workshop on Efficient Sequence Models",
            publish_date="2025-11-03T00:00:00Z",
            source="https://example.com/papers/block-sparse",
            abstract=(
                "We study block-sparse attention on inputs of 16k to 128k tokens. "
                "Each query attends to its local window and to a few blocks chosen "
                "by a learned score. At 64k tokens the model keeps 97% of dense "
                "quality on retrieval and summarization while using a fifth of the "
                "attention compute. Past 96k tokens, recall of single facts drops "
                "faster than with dense attention, which we trace to blocks the "
                "score never picks."
            ),
        ),
        _paper(
            "sliding-windows",
            "Sliding windows with global tokens",
            authors=["Dee Placeholder"],
            publication_type="article",
            venue="Journal of Sequence Modeling",
            published="2024-02-20",
        ),
        _paper(
            "routing",
            "Learned routing for sparse attention",
            authors=["Eli Sample", "Ada Example"],
            publication_type="misc",
            venue="Preprint",
            published="2025-04-08",
        ),
        _paper(
            "dense-baselines",
            "Dense attention baselines at 32k tokens",
            authors=["Fay Example"],
            publication_type="techreport",
            venue="Example Lab technical report",
            published="2023-09-01",
        ),
        _paper(
            "recall-loss",
            "Where sparse attention loses recall",
            authors=["Gus Demo", "Cleo Demo"],
            publication_type="misc",
            venue="Preprint",
            published="2026-01-15",
        ),
    ]


def _issue(
    key: str,
    title: str,
    *,
    owner: str,
    kind: str,
    priority: int = 20,
    status: str = "active",
    labels: Sequence[str] = (),
    subscribers: Sequence[str] = (),
    description: str = "",
) -> _Node:
    """Make an Issue of one ``kind``, owned by ``owner`` at example.com."""
    _, _, body = _artifact(
        key,
        "Issue",
        title,
        owner=owner,
        status=status,
        labels=labels,
        description=description,
    )
    body |= {"priority": priority, "issue_kind": [kind]}
    if subscribers:
        body["subscribers"] = list(subscribers)
    return key, "Issue", body


def _paper(
    key: str,
    title: str,
    *,
    authors: list[str],
    publication_type: str,
    venue: str,
    published: str,
) -> _Node:
    """Make a Paper the survey read, at its example.com source."""
    return _artifact(
        key,
        "Paper",
        title,
        owner="ben",
        authors=authors,
        publication_type=publication_type,
        venue=venue,
        publish_date=f"{published}T00:00:00Z",
        source=f"https://example.com/papers/{key}",
    )


def _artifact(
    key: str,
    kind: Inquiry.InquiryKind,
    title: str,
    *,
    owner: str,
    status: str = "active",
    labels: Sequence[str] = (),
    description: str = "",
    **fields: object,
) -> _Node:
    """Make an inquiry created and owned by ``owner`` at example.com, with ``fields``."""
    email = f"{owner}@example.com"
    body: dict[str, object] = {
        "title": title,
        "status": status,
        "owner": email,
        "actor": email,
        **fields,
    }
    if labels:
        body["labels"] = list(labels)
    if description:
        body["description"] = description
    return key, kind, body


def _links() -> list[tuple[str, str, str, float | None]]:
    """Every edge, child to parent by key, with its valence where it has one."""
    tree = [
        ("tok-profile", "narrows", "tok"),
        ("tok-cache", "narrows", "tok"),
        ("tok-vector", "narrows", "tok"),
        ("tok-vocab", "narrows", "tok-cache"),
        ("tok-vector", "requires", "tok-profile"),
        ("tok-belief", "produced_by", "tok-profile"),
        ("tok-profile-run", "produced_by", "tok-profile"),
        ("cache-bench", "produced_by", "tok-cache"),
        ("rank-tables", "produced_by", "tok-cache"),
        ("drift-seed", "narrows", "drift"),
        ("drift-order", "narrows", "drift"),
        ("drift-gpu", "narrows", "drift"),
        ("drift-gpu", "requires", "drift-seed"),
        ("drift-belief", "produced_by", "drift-seed"),
        ("drift-reruns", "produced_by", "drift-seed"),
        ("drift-gpu-belief", "produced_by", "drift-gpu"),
        ("survey-read", "narrows", "survey"),
        ("survey-summary", "narrows", "survey"),
        ("survey-summary", "requires", "survey-read"),
        ("sparse-belief", "produced_by", "survey-summary"),
        ("needle", "produced_by", "survey-summary"),
        *(
            (paper, "produced_by", "survey-read")
            for paper in (
                "block-sparse",
                "sliding-windows",
                "routing",
                "dense-baselines",
                "recall-loss",
            )
        ),
        ("block-sparse", "cites_paper", "sliding-windows"),
        ("block-sparse", "cites_paper", "routing"),
        ("block-sparse", "cites_paper", "dense-baselines"),
        ("sliding-windows", "cites_paper", "dense-baselines"),
        ("routing", "cites_paper", "dense-baselines"),
        ("recall-loss", "cites_paper", "block-sparse"),
        ("recall-loss", "cites_paper", "routing"),
    ]
    evidence = [
        ("tok-profile-run", "proves", "tok-belief", 0.7),
        ("cache-bench", "proves", "tok-belief", 0.6),
        ("rank-tables", "favors", "tok-belief", 0.4),
        ("drift-reruns", "proves", "drift-belief", 0.8),
        ("needle", "proves", "sparse-belief", 0.4),
        ("block-sparse", "favors", "sparse-belief", 0.6),
        ("routing", "favors", "sparse-belief", 0.3),
        ("recall-loss", "favors", "sparse-belief", -0.5),
    ]
    return [*((child, kind, parent, None) for child, kind, parent in tree), *evidence]


def _metrics() -> dict[str, list[MetricPoint]]:
    """Each Experiment's metrics, from closed forms and fixed values."""
    return {
        "cache-bench": [
            point
            for step in range(24)
            for point in (
                MetricPoint(
                    key="throughput_mtok_s",
                    step=step,
                    value=round(1.04 + 0.94 * (1 - math.exp(-step / 4)), 3),
                ),
                MetricPoint(
                    key="cache_hit_rate",
                    step=step,
                    value=round(0.97 * (1 - math.exp(-step / 3)), 3),
                ),
            )
        ],
        "drift-reruns": [
            MetricPoint(key="score", step=run, value=value)
            for run, value in enumerate(
                (71.4, 71.3, 71.5, 71.4, 71.4, 71.5, 71.3, 71.4, 71.5, 71.4),
            )
        ],
        # Steps are context lengths, in thousands of tokens.
        "needle": [
            point
            for length, sparse, dense in (
                (8, 0.99, 0.99),
                (16, 0.98, 0.99),
                (32, 0.96, 0.98),
                (64, 0.94, 0.97),
            )
            for point in (
                MetricPoint(key="recall_block_sparse", step=length, value=sparse),
                MetricPoint(key="recall_dense", step=length, value=dense),
            )
        ],
    }


def _sessions() -> list[_Session]:
    """Three agents in two rooms; ``_converse`` interleaves their turns by time."""
    return [
        _Session(
            actor="tok-bench",
            room="tokenizer",
            title="Benchmark the merge-rank cache",
            produced_by="tok-cache",
            turns=(
                _user(
                    "14:31:05",
                    "ada@example.com: Benchmark the merge-rank cache against main "
                    "on shard-01. The token ids must match exactly.",
                ),
                _assistant(
                    "14:31:12",
                    "Running both builds over shard-01, three passes each, and "
                    "comparing the token ids.",
                ),
                *_shell(
                    "14:31:14",
                    "14:38:40",
                    "bench-1",
                    "python bench.py --shard shard-01 --passes 3 --builds main,cache",
                    "build  pass  tokens/s\nmain   3     1.04 M\ncache  3     1.98 M\n"
                    "token ids identical: yes\n",
                ),
                _assistant(
                    "14:38:52",
                    "The cache gives **1.9x**: 1.98 M tokens/s against 1.04 M on "
                    "main, with identical token ids. The passes are logged to the "
                    "benchmark experiment.",
                ),
                _user("14:47:30", "ada@example.com: Does it hold on the small shards?"),
                _assistant(
                    "14:47:41",
                    "Mostly: 1.7x on shard-07 (100 MB), where the cache has less "
                    "time to warm.",
                ),
            ),
        ),
        _Session(
            actor="tok-review",
            room="tokenizer",
            title="Review the merge-rank cache",
            produced_by="tok-cache",
            turns=(
                _user(
                    "14:35:02",
                    "ben@example.com: Review the cache change before it merges.",
                ),
                *_shell(
                    "14:35:20",
                    "14:35:21",
                    "diff-1",
                    "git diff main -- tokenizer/merge.py",
                    "+    rank = self._ranks.get(pair)\n"
                    "+    if rank is None:\n"
                    "+        rank = self._ranks[pair] = self._lookup(pair)\n",
                ),
                _assistant(
                    "14:36:44",
                    "One problem: the cache is keyed by byte pair alone, so a "
                    "vocabulary swap would serve stale ranks.",
                ),
                _user(
                    "14:41:10",
                    "ben@example.com: Fix it here, with a test that swaps "
                    "vocabularies mid-stream.",
                ),
                _assistant(
                    "14:44:58",
                    "Done: the cache now clears when the vocabulary changes, and "
                    "the new test passes.",
                ),
            ),
        ),
        _Session(
            actor="evals-seeds",
            room="evals",
            title="Rerun the eval with fixed seeds",
            produced_by="drift-seed",
            turns=(
                _user(
                    "14:33:15",
                    "cleo@example.com: Rerun the eval ten times with every sampler "
                    "seeded.",
                ),
                _assistant(
                    "14:33:24",
                    "Starting ten reruns of step-40000 with data order, dropout and "
                    "sampling all seeded.",
                ),
                *_shell(
                    "14:33:25",
                    "14:52:03",
                    "eval-1",
                    "python eval.py --checkpoint step-40000 --seed 7 --runs 10",
                    "runs  mean  min   max\n10    71.4  71.3  71.5\n",
                ),
                _assistant(
                    "14:52:16",
                    "The spread fell from 1.8 to 0.2 points (71.3 to 71.5), so "
                    "unseeded sampling explains the drift.",
                ),
                _user(
                    "14:55:40",
                    "cleo@example.com: Good. Next, the same ten runs on one GPU type.",
                ),
                _assistant(
                    "14:55:48",
                    "Queued. I will post the spread when they finish.",
                ),
            ),
        ),
    ]


def _user(clock: str, content: str) -> UserMessage:
    return UserMessage(timestamp=_when(clock), content=content)


def _assistant(clock: str, content: str) -> AssistantMessage:
    return AssistantMessage(timestamp=_when(clock), content=content)


def _shell(
    called: str,
    answered: str,
    call_id: str,
    command: str,
    stdout: str,
) -> tuple[ToolCall, ShellCommandResult]:
    """Make a Bash call at ``called`` and its output at ``answered``."""
    return (
        ToolCall(
            timestamp=_when(called),
            call_id=call_id,
            name="Bash",
            arguments={"command": command},
        ),
        ShellCommandResult(
            timestamp=_when(answered),
            call_id=call_id,
            stdout=stdout,
            exit_code=0,
        ),
    )


def _when(clock: str) -> str:
    """``clock``, ``HH:MM:SS`` in UTC, on the seed's one day."""
    return f"2026-03-12T{clock}+00:00"


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
