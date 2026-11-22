"""Tests for ``seed_local.py``: what one run seeds, and what a rerun leaves alone."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self, cast

import sys
import time
import uuid

import pytest

from trackinizer.web.scripts import seed_local
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody
from trackinizer.wire.wire_sessions import SessionStart


if TYPE_CHECKING:
    from types import TracebackType

    from trackinizer.wire.filters import Filter


def test_a_first_run_seeds_the_graph_whole_and_a_rerun_seeds_nothing_twice(
    server: _Server,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run()
    _run()

    assert server.urls == ["http://127.0.0.1:8765"] * 2
    assert server.authors == ["seed"] * 2
    ((items, edges, actor),) = server.batches
    assert (len(items), len(edges), actor) == (69, 70, "seed")
    root = server.ids[0]
    assert capsys.readouterr().out == f"root: {root}\n" * 2
    backlog = [body for _, body in items[9:]]
    assert {str(body["title"]) for body in backlog} == {
        f"Backlog item {n}" for n in range(60)
    }
    assert all(edge["to_index"] == 0 for edge in edges[10:])
    assert [body.get("owner") for body in backlog[:4]] == ["dan", None, "josh", "Agent"]
    assert "priority" not in backlog[5]
    experiment = server.row_id("Membership check latency")
    assert [(target, len(points)) for target, points in server.metrics] == [
        (experiment, 10),
    ] * 2
    assert (
        server.edits
        == [
            (
                server.row_id(seed_local._BELIEF_TITLE),
                "judgement",
                "proven",
                "measured",
            ),
            (server.row_id("Backlog item 3"), "status", "abandoned", "superseded"),
        ]
        * 2
    )
    assert [start.idempotency_key for start in server.starts] == [
        seed_local._SESSION_KEY,
    ] * 2
    (name, manifest, records), _ = server.appends
    assert (name, manifest.records, len(records)) == ("main.jsonl", 40, 40)
    assert [record.idx for record in records] == list(range(40))


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("Membership check latency", "seeded experiment"),
        (seed_local._BELIEF_TITLE, "reset the preview database"),
        ("Backlog item 3", "reset the preview database"),
    ],
)
def test_a_graph_missing_a_seeded_row_stops_with_why(
    server: _Server,
    missing: str,
    message: str,
) -> None:
    _run()
    server.rows = [row for row in server.rows if row.title != missing]

    with pytest.raises(SystemExit, match=message):
        _run()


def test_the_writer_cycles_priority_label_and_new_item(
    server: _Server,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        if len(slept) == 3:
            raise _StoppedError
        slept.append(seconds)

    monkeypatch.setattr(time, "sleep", sleep)

    with pytest.raises(_StoppedError):
        _run("--writer", "--interval", "0.5")

    assert slept == [0.5] * 3
    root = server.ids[0]
    assert capsys.readouterr().out.splitlines() == [
        f"root: {root}",
        "priority -> 0",
        "label live-1",
        f"created {server.row_id('Live item 2')}",
    ]
    seeded = [row.id for row in server.rows if row.kind == "Issue"]
    assert server.edits[-1] == (root, "priority", 0, "")
    assert server.labels == [(seeded[7], "live-1")]
    items, edges, actor = server.batches[-1]
    assert [body["labels"] for _, body in items] == [["seed", "live"]]
    assert edges == [{"from_index": 0, "to_id": str(root), "edge_kind": "narrows"}]
    assert actor == "writer"


class _StoppedError(Exception):
    """Ends the writer's otherwise endless loop."""


@dataclass(slots=True, kw_only=True)
class _Row:
    id: uuid.UUID
    kind: str
    title: str
    labels: tuple[str, ...]


@dataclass(slots=True, kw_only=True)
class _Started:
    id: uuid.UUID


type _Batch = tuple[
    list[tuple[str, Mapping[str, object]]],
    list[Mapping[str, object]],
    str | None,
]


@dataclass(slots=True, kw_only=True)
class _Server:
    """What a preview server holds, and every write the script made to it."""

    rows: list[_Row] = field(default_factory=list)
    ids: list[uuid.UUID] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    batches: list[_Batch] = field(default_factory=list)
    metrics: list[tuple[uuid.UUID, list[MetricPoint]]] = field(default_factory=list)
    edits: list[tuple[uuid.UUID, str, object, str]] = field(default_factory=list)
    labels: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    starts: list[SessionStart] = field(default_factory=list)
    appends: list[tuple[str, ManifestBody, list[RecordBody]]] = field(
        default_factory=list,
    )

    def row_id(self, title: str) -> uuid.UUID:
        return next(row.id for row in self.rows if row.title == title)


class _FakeClient:
    """The ``Client`` surface the script calls, over a ``_Server``."""

    def __init__(self, server: _Server, base_url: str, *, author: str) -> None:
        self.server = server
        server.urls.append(base_url)
        server.authors.append(author)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb

    def list_kind(
        self,
        kind: str,
        *,
        limit: int,
        filters: Sequence[Filter],
    ) -> list[dict[str, str]]:
        (only,) = filters
        value = only.value
        rows = [
            row
            for row in self.server.rows
            if row.kind == kind
            and (row.title == value if only.op == "is" else value in row.labels)
        ]
        return [{"id": str(row.id)} for row in rows[:limit]]

    def submit_batch(
        self,
        items: Sequence[tuple[str, Mapping[str, object]]],
        *,
        edges: Sequence[Mapping[str, object]],
        actor: str | None,
    ) -> list[uuid.UUID]:
        self.server.batches.append((list(items), list(edges), actor))
        ids = [uuid.uuid4() for _ in items]
        for each, (kind, body) in zip(ids, items, strict=True):
            self.server.rows.append(
                _Row(
                    id=each,
                    kind=kind,
                    title=str(body["title"]),
                    # ``_issue`` builds them as a list of str; nothing else has any.
                    labels=tuple(cast(list[str], body.get("labels", []))),
                ),
            )
        self.server.ids.extend(ids)
        return ids

    def log_metrics(self, experiment_id: uuid.UUID, points: list[MetricPoint]) -> None:
        self.server.metrics.append((experiment_id, points))

    def edit(
        self,
        target_id: uuid.UUID,
        field_name: str,
        value: object,
        *,
        actor: str,
        reason: str = "",
    ) -> None:
        del actor
        self.server.edits.append((target_id, field_name, value, reason))

    def add_label(self, target_id: uuid.UUID, label: str, *, actor: str) -> None:
        del actor
        self.server.labels.append((target_id, label))

    def session_start(self, body: SessionStart) -> _Started:
        self.server.starts.append(body)
        return _Started(id=uuid.uuid5(uuid.NAMESPACE_URL, str(body.idempotency_key)))

    def append_records(
        self,
        session_id: uuid.UUID,
        *,
        name: str,
        manifest: ManifestBody,
        records: list[RecordBody],
    ) -> None:
        del session_id
        self.server.appends.append((name, manifest, records))


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    """Point the script's ``Client`` at an in-memory preview server."""
    held = _Server()

    def client(base_url: str, *, author: str) -> _FakeClient:
        return _FakeClient(held, base_url, author=author)

    monkeypatch.setattr(seed_local, "Client", client)
    return held


def _run(*flags: str) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "argv", ["seed_local.py", *flags])
        assert seed_local.main() == 0


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
