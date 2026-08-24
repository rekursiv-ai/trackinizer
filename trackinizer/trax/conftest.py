"""Shared trax CLI test fixtures.

``tmp_config_dir`` is autouse: every test gets a fresh tmp directory
in place of the real config dir so profile reads and writes
cannot bleed into the developer's real config. This closes the latent
class of "test mutated my real profile" failures and unblocks the
profile-related GRAMMAR.md examples once their xfails are lifted.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Any, Self, cast

import uuid

import pytest

from trackinizer.client.client import EdgeWrite
from trackinizer.lib.custom_types import Absent
from trackinizer.trax import cli
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.filters import (
    FILTER_FIELD_ALIASES,
    Filter,
)
from trackinizer.wire.refs import Ref, SeqRef, UuidRef
from trackinizer.wire.routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
)
from trackinizer.wire.row_filter import match_filter
from trackinizer.wire.seq_ranges import SeqRange
from trackinizer.wire.wire_metrics import (
    LogMetricsResponse,
    MetricPoint,
)
from trackinizer.wire.wire_metrics_query import (
    MetricMaskClause,
    MetricRankRow,
)
from trackinizer.wire.wire_sessions import (
    AppendEventsResponse,
    EventBody,
    SessionEnd,
    SessionEndResponse,
    SessionStart,
    SessionStartResponse,
)


_ABSENT = Absent()


def _seq_in_interval(seq: int, interval: SeqRange) -> bool:
    """Whether ``seq`` falls within one inclusive interval's present bounds."""
    if interval.start is not None and seq < interval.start:
        return False
    return not (interval.stop is not None and seq > interval.stop)


def _storage_view(row: dict[str, object]) -> dict[str, object]:
    """Re-key a bare-keyed fake row to canonical SQL storage columns.

    ``match_filter`` resolves a filter field through
    ``canonical_filter_field`` and looks the result up by key, so it
    expects storage-column keys (``issue_priority``, ``paper_source``,
    ...). The fake's rows -- and the CLI render path -- carry the
    ergonomic bare names (``priority``, ``source``); this view is built
    solely for the predicate, leaving the returned/rendered row intact.
    Sourced from ``FILTER_FIELD_ALIASES`` so no parallel map drifts.
    """
    view = dict(row)
    for alias, column in FILTER_FIELD_ALIASES.items():
        # ``kind``/``label``/... are base-field aliases whose canonical
        # column the row already carries (and ``kind`` collides with the
        # row's own discriminator); only fill a storage column the row
        # lacks, so a bare kind-specific key (``priority``) is promoted
        # without clobbering an existing canonical value.
        if alias in view and column not in view:
            view[column] = view[alias]
    return view


@pytest.fixture(autouse=True)
def tmp_config_dir(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolate profile state so tests cannot touch the real config.

    Redirects ``XDG_CONFIG_HOME`` rather than patching module globals: the
    module builds each path inline from ``config_dir``, so moving the env var
    exercises the same resolution production uses. Also clears
    ``TRACKINIZER_PROFILE`` and ``TRACKINIZER_URL`` so an exported shell
    variable does not bleed into the test process.
    """
    root = cast(Any, tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    (root / "rekursiv-ai" / "trax" / "profiles").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)
    monkeypatch.delenv("TRACKINIZER_URL", raising=False)


class FakeClient:
    """In-memory client stub recording every method call."""

    def __init__(self) -> None:
        self.base_url = "http://fake"
        self.author = ""
        self.api_key = ""
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        # Edges already created, keyed like the server's unique constraint
        # (from_id, to_id, edge_kind) -> stored annotations. ``add_edge`` is an
        # upsert: a repeat is recorded in ``calls`` and returns
        # ``EdgeWrite(created=False, changed=...)``, mirroring the server, where
        # ``changed`` is True iff the supplied annotations differ from stored.
        self._edges: dict[tuple[uuid.UUID, uuid.UUID, str], dict[str, object]] = {}
        self.target_id = uuid.uuid4()
        self.child_low_id = uuid.uuid4()
        self.child_high_id = uuid.uuid4()
        self.other_child_id = uuid.uuid4()
        self.rows: list[dict[str, object]] = [
            {
                "id": str(self.target_id),
                "kind": "Issue",
                "seq": 1,
                "status": "active",
                "title": "row",
                "owner": "alice",
                "labels": ["urgent"],
            },
            {
                "id": str(self.child_low_id),
                "kind": "Issue",
                "seq": 4,
                "status": "active",
                "title": "low child",
                "issue_kind": ["bug"],
                "priority": 30,
                "created": "2026-05-18T00:00:02",
            },
            {
                "id": str(self.child_high_id),
                "kind": "Issue",
                "seq": 5,
                "status": "active",
                "title": "high child",
                "issue_kind": ["bug"],
                "priority": 10,
                "created": "2026-05-18T00:00:01",
            },
            {
                "id": str(self.other_child_id),
                "kind": "Issue",
                "seq": 6,
                "status": "active",
                "title": "task child",
                "issue_kind": ["task"],
                "priority": 0,
                "created": "2026-05-18T00:00:03",
            },
            {
                "id": str(uuid.uuid4()),
                "kind": "Experiment",
                "seq": 2,
                "status": "complete",
                "title": "experiment row",
                "outcome": "works",
            },
            {
                "id": str(uuid.uuid4()),
                "kind": "Belief",
                "seq": 3,
                "status": "active",
                "title": "foo then bar",
                "judgement": "proven",
                "confidence": 0.95,
            },
        ]
        self.evidence_low_id = uuid.uuid4()
        self.evidence_high_id = uuid.uuid4()
        self.rows.extend(
            [
                {
                    "id": str(self.evidence_low_id),
                    "kind": "Paper",
                    "seq": 4,
                    "status": "active",
                    "title": "weak evidence",
                },
                {
                    "id": str(self.evidence_high_id),
                    "kind": "Experiment",
                    "seq": 2,
                    "status": "active",
                    "title": "strong evidence",
                    "outcome": "supports",
                },
            ]
        )
        self.detail: dict[str, object] = {
            "self": self.rows[0],
            # ``blocks`` reads the inbound (backlinks) view of ``requires``:
            # rows that require this one (it is their prerequisite).
            "backlinks": {
                "requires": [
                    {
                        "id": str(self.other_child_id),
                        "kind": "Issue",
                        "seq": 6,
                        "status": "active",
                        "title": "task child",
                        "priority": 10,
                        "valence": 0.7,
                        "labels": ["edge"],
                        "note": "must land first",
                    }
                ],
            },
            "edges": {
                "proves": [
                    {
                        "id": str(self.evidence_low_id),
                        "kind": "Paper",
                        "seq": 4,
                        "status": "active",
                        "title": "weak evidence",
                        "valence": 0.2,
                    },
                    {
                        "id": str(self.evidence_high_id),
                        "kind": "Experiment",
                        "seq": 2,
                        "status": "active",
                        "title": "strong evidence",
                        "valence": 0.9,
                    },
                ],
            },
        }
        self.changes: list[dict[str, object]] = [
            {
                "created": "2026-05-18T00:00:00",
                "kind": "created",
                "subject_kind": "Issue",
                "subject_id": str(self.target_id),
                "author": "alice",
            }
        ]
        self.cost_payload: dict[str, float] = {"agent_usd": 1.0, "resource_usd": 2.0}
        self.next_payload: dict[str, object] | None = {
            "id": str(self.target_id),
            "kind": "Issue",
            "seq": 5,
            "priority": 20,
            "title": "next issue",
            "status": "active",
        }

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        self.close()

    def resolve_id(self, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        self.calls.append(("resolve_id", (ref,), {}))
        if isinstance(ref, SeqRef):
            row = next(
                (
                    row
                    for row in self.rows
                    if row.get("kind") == ref.kind and row.get("seq") == ref.seq
                ),
                None,
            )
            if row is None:
                return ref.kind, self.target_id
            return ref.kind, uuid.UUID(str(row["id"]))
        return "Issue", ref.uuid

    def resolve_ids(
        self, refs: Sequence[Ref]
    ) -> list[tuple[Inquiry.InquiryKind, uuid.UUID]]:
        self.calls.append(("resolve_ids", (tuple(refs),), {}))
        return [self.resolve_id(ref) for ref in refs]

    def transition_owner(
        self,
        target_id: uuid.UUID,
        *,
        expected_from: Inquiry.Actor | None,
        to: Inquiry.Actor | None,
        actor: Inquiry.Actor,
    ) -> None:
        self.calls.append(
            (
                "transition_owner",
                (target_id,),
                {"expected_from": expected_from, "to": to, "actor": actor},
            )
        )

    def transition_status(
        self,
        target_id: uuid.UUID,
        *,
        expected_from: str,
        to: str,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> None:
        self.calls.append(
            (
                "transition_status",
                (target_id,),
                {
                    "expected_from": expected_from,
                    "to": to,
                    "actor": actor,
                    "reason": reason,
                },
            )
        )

    def submit(self, kind: Inquiry.InquiryKind, body: dict[str, object]) -> uuid.UUID:
        self.calls.append(("submit", (kind, body), {}))
        return self.target_id

    def submit_batch(
        self,
        items: Sequence[tuple[Inquiry.InquiryKind, Any]],
        *,
        edges: Sequence[Any] = (),
    ) -> list[uuid.UUID]:
        self.calls.append(("submit_batch", (tuple(items),), {"edges": tuple(edges)}))
        # One distinct id per item so callers can map inline targets back.
        return [self.target_id if i == 0 else uuid.uuid4() for i in range(len(items))]

    def list_kind(
        self,
        kind: Inquiry.InquiryKind,
        *,
        status: Inquiry.Status | None = None,
        limit: int = 200,
        offset: int = 0,
        seq_ranges: Sequence[SeqRange] = (),
        filters: Sequence[Filter] = (),
    ) -> list[dict[str, Any]]:
        self.calls.append(
            (
                "list_kind",
                (kind,),
                {
                    "status": status,
                    "limit": limit,
                    "offset": offset,
                    "seq_ranges": tuple(seq_ranges),
                    "filters": tuple(filters),
                },
            )
        )
        rows = [row for row in self.rows if row.get("kind") == kind]
        if seq_ranges:
            # Mirror the server's OR-of-intervals union: a row survives if its
            # seq falls in any interval. A single query, so overlaps never
            # double-list a row.
            rows = [
                row
                for row in rows
                if any(
                    _seq_in_interval(int(cast(int, row.get("seq") or 0)), interval)
                    for interval in seq_ranges
                )
            ]
        # Mirror the real SQL ``ORDER BY created DESC, id DESC`` so a
        # unit test that buries a needle past the recency window
        # reproduces the same failure mode an integration test
        # would. Rows without a ``created`` key sort first (the
        # baseline FakeClient fixture is timestamp-less and
        # historically depended on insertion order).
        rows = sorted(
            rows,
            key=lambda r: (
                str(r.get("created") or ""),
                str(r.get("id") or ""),
            ),
            reverse=True,
        )
        # Server-side filter evaluation, mirroring the real ``/api/list``
        # behaviour: every filter is applied before the limit/offset
        # window so a matching row that lives past the recency boundary
        # still surfaces. ``status`` is folded into the same pipeline.
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        for filt in filters:
            rows = [row for row in rows if match_filter(_storage_view(row), filt)]
        return rows[offset : offset + limit]

    def list_kind_all(
        self,
        kind: Inquiry.InquiryKind,
        *,
        status: Inquiry.Status | None = None,
        seq_ranges: Sequence[SeqRange] = (),
        filters: Sequence[Filter] = (),
    ) -> list[dict[str, Any]]:
        """Page past the cap, mirroring the real client's whole-collection fetch.

        Records a ``list_kind_all`` call, then pages via ``list_kind`` in
        ``MAX_LIST_LIMIT`` chunks until a short page -- the same loop (and the
        same cap constant) the real client runs, so tests exercise the
        pagination contract and never drift from the server's real limit.
        """
        self.calls.append(
            (
                "list_kind_all",
                (kind,),
                {
                    "status": status,
                    "seq_ranges": tuple(seq_ranges),
                    "filters": tuple(filters),
                },
            )
        )
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.list_kind(
                kind,
                status=status,
                limit=MAX_LIST_LIMIT,
                offset=offset,
                seq_ranges=seq_ranges,
                filters=filters,
            )
            rows.extend(page)
            if len(page) < MAX_LIST_LIMIT:
                return rows
            offset += MAX_LIST_LIMIT

    def get_inquiry(
        self, ref: Ref
    ) -> tuple[Inquiry.InquiryKind, uuid.UUID, dict[str, Any]]:
        self.calls.append(("get_inquiry", (ref,), {}))
        if isinstance(ref, UuidRef):
            target_id = ref.uuid
        else:
            match_row = next(
                (
                    row
                    for row in self.rows
                    if row.get("kind") == ref.kind and row.get("seq") == ref.seq
                ),
                None,
            )
            target_id = (
                uuid.UUID(str(match_row["id"]))
                if match_row is not None
                else self.target_id
            )
        row = next(
            (row for row in self.rows if row.get("id") == str(target_id)),
            self.rows[0],
        )
        # Return the shared ``detail`` envelope (backlinks + edges) with
        # ``self`` swapped to the actual row. Tests assume every row carries
        # the canonical relation/backlink set; per-row detail customisation
        # is not modeled.
        detail = dict(self.detail)
        detail["self"] = row
        return (
            cast(Inquiry.InquiryKind, row.get("kind", "Issue")),
            target_id,
            detail,
        )

    def next_issue(self) -> dict[str, Any] | None:
        self.calls.append(("next_issue", (), {}))
        return self.next_payload

    def version(self) -> str:
        self.calls.append(("version", (), {}))
        return "testsha"

    def wait_until_ready(
        self,
        *,
        timeout_sec: float = 30.0,
        probe_interval_sec: float = 0.25,
        alive: Callable[[], bool] | None = None,
    ) -> None:
        """Record a readiness wait; the in-memory fake is always ready.

        Args:
          timeout_sec: Maximum readiness wait requested by the caller.
          probe_interval_sec: Requested delay between readiness probes.
          alive: Optional server-liveness oracle.

        """
        self.calls.append(
            (
                "wait_until_ready",
                (),
                {
                    "timeout_sec": timeout_sec,
                    "probe_interval_sec": probe_interval_sec,
                    "alive": alive,
                },
            )
        )

    def search(
        self,
        query: str,
        *,
        kind: Inquiry.InquiryKind | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.calls.append(("search", (query,), {"kind": kind, "limit": limit}))
        return list(self.rows)

    def recent_changes(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.calls.append(("recent_changes", (), {"limit": limit}))
        return list(self.changes)

    def cost_for(self, target_id: uuid.UUID, *, deep: bool = False) -> dict[str, float]:
        self.calls.append(("cost_for", (target_id,), {"deep": deep}))
        return dict(self.cost_payload)

    def edit(
        self,
        target_id: uuid.UUID,
        field: str,
        value: object,
        *,
        actor: str,
        reason: str = "",
    ) -> None:
        self.calls.append(
            ("edit", (target_id, field, value), {"actor": actor, "reason": reason})
        )

    def add_cost(
        self,
        target_id: uuid.UUID,
        field: str,
        value: float,
        *,
        actor: str,
        reason: str = "",
    ) -> None:
        self.calls.append(
            ("add_cost", (target_id, field, value), {"actor": actor, "reason": reason})
        )

    def add_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: str,
        priority: int | None = None,
        note: str = "",
        valence: float | None = None,
        labels: Sequence[str] | None = (),
        reason: str = "",
    ) -> EdgeWrite:
        # ``labels=None`` is the explicit clear-to-empty: the real client cannot
        # carry it on the upsert (the store collapses empty), so it threads the
        # clear through ``annotate_edge`` after the POST. Mirror that here so a
        # test sees the same two-call shape (TRAX-CLI-004).
        self.calls.append(
            (
                "add_edge",
                (from_id, to_id, edge_kind),
                {
                    "actor": actor,
                    "priority": priority,
                    "note": note,
                    "valence": valence,
                    "labels": () if labels is None else tuple(labels),
                    "reason": reason,
                },
            )
        )
        # Mirror the real upsert contract: a brand-new edge is created; a repeat
        # applies any supplied annotations to the stored edge (no error). Only
        # the annotations the caller passed are set, matching the server's
        # "defaults encode unset" translation. ``changed`` is True iff a value
        # actually differs from what is already stored.
        key = (from_id, to_id, edge_kind)
        supplied: dict[str, object] = {}
        if priority is not None:
            supplied["priority"] = priority
        if note:
            supplied["note"] = note
        if valence is not None:
            supplied["valence"] = valence
        if labels:
            supplied["labels"] = tuple(labels)
        created = key not in self._edges
        if created:
            self._edges[key] = supplied
            changed_flag = True
        else:
            stored = self._edges[key]
            changed_flag = any(stored.get(k) != v for k, v in supplied.items())
            stored.update(supplied)
        if labels is None:
            # The clear routes through annotate_edge, the path that writes NULL.
            self.annotate_edge(from_id, to_id, edge_kind, actor=actor, labels=[])
            self._edges[key].pop("labels", None)
            return EdgeWrite(created=created, changed=True)
        return EdgeWrite(created=created, changed=changed_flag)

    def remove_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: str,
    ) -> None:
        self.calls.append(
            ("remove_edge", (from_id, to_id, edge_kind), {"actor": actor})
        )

    def annotate_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: str,
        priority: int | Absent | None = _ABSENT,
        note: str | Absent | None = _ABSENT,
        valence: float | Absent | None = _ABSENT,
        labels: Sequence[str] | Absent | None = _ABSENT,
    ) -> None:
        metadata: dict[str, object] = {"actor": actor}
        if not isinstance(priority, Absent):
            metadata["priority"] = priority
        if not isinstance(note, Absent):
            metadata["note"] = note
        if not isinstance(valence, Absent):
            metadata["valence"] = valence
        if not isinstance(labels, Absent):
            metadata["labels"] = None if labels is None else tuple(labels)
        self.calls.append(("annotate_edge", (from_id, to_id, edge_kind), metadata))

    def purge(self, target_id: uuid.UUID, *, actor: str, reason: str = "") -> None:
        self.calls.append(("purge", (target_id,), {"actor": actor, "reason": reason}))

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        self.calls.append(("session_start", (body,), {}))
        return SessionStartResponse(id=self.target_id, seq=1)

    def append_events(
        self, session_id: uuid.UUID, events: Sequence[EventBody]
    ) -> AppendEventsResponse:
        self.calls.append(("append_events", (session_id, events), {}))
        return AppendEventsResponse(appended=len(list(events)), skipped=0)

    def read_events(
        self,
        session_id: uuid.UUID,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        seq_ranges: Sequence[SeqRange] = (),
        kind: str | None = None,
    ) -> list[EventBody]:
        self.calls.append(
            (
                "read_events",
                (session_id,),
                {
                    "limit": limit,
                    "offset": offset,
                    "seq_ranges": tuple(seq_ranges),
                    "kind": kind,
                },
            )
        )
        return []

    def log_metrics(
        self, experiment_id: uuid.UUID, points: Sequence[MetricPoint]
    ) -> LogMetricsResponse:
        self.calls.append(("log_metrics", (experiment_id, points), {}))
        return LogMetricsResponse(logged=len(list(points)), skipped=0)

    def read_metrics(
        self,
        experiment_id: uuid.UUID,
        *,
        key: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[MetricPoint]:
        self.calls.append(
            (
                "read_metrics",
                (experiment_id,),
                {"key": key, "limit": limit, "offset": offset},
            )
        )
        return []

    def query_metrics(
        self,
        experiment_id: uuid.UUID,
        *,
        masks: Sequence[MetricMaskClause],
        sort: str | None = None,
        limit: int | None = None,
    ) -> list[MetricPoint]:
        self.calls.append(
            (
                "query_metrics",
                (experiment_id,),
                {"masks": tuple(masks), "sort": sort, "limit": limit},
            )
        )
        return []

    def write_metrics_masked(
        self,
        experiment_id: uuid.UUID,
        *,
        masks: Sequence[MetricMaskClause],
        value: float,
    ) -> int:
        self.calls.append(
            (
                "write_metrics_masked",
                (experiment_id,),
                {"masks": tuple(masks), "value": value},
            )
        )
        return 1

    def rank_metrics(
        self,
        experiment_ids: Sequence[uuid.UUID],
        *,
        masks: Sequence[MetricMaskClause],
        sort: str | None = None,
        limit: int | None = None,
    ) -> list[MetricRankRow]:
        self.calls.append(
            (
                "rank_metrics",
                (tuple(experiment_ids),),
                {"masks": tuple(masks), "sort": sort, "limit": limit},
            )
        )
        return []

    def session_end(
        self, session_id: uuid.UUID, body: SessionEnd | None = None
    ) -> SessionEndResponse:
        self.calls.append(("session_end", (session_id, body), {}))
        return SessionEndResponse(id=session_id)

    def enqueue_inbound(self, session_id: uuid.UUID, text: str) -> int:
        self.calls.append(("enqueue_inbound", (session_id, text), {}))
        return 1

    def drain_inbound(
        self, session_id: uuid.UUID
    ) -> list[tuple[str, str | None, str | None]]:
        self.calls.append(("drain_inbound", (session_id,), {}))
        return []

    def send_message(
        self, actor: str, text: str, *, room: str | None = None
    ) -> list[uuid.UUID]:
        self.calls.append(("send_message", (actor, text), {"room": room}))
        return [self.target_id]

    def add_subscriber(
        self, target_id: uuid.UUID, subscriber: str, *, actor: str
    ) -> None:
        self.calls.append(("add_subscriber", (target_id, subscriber), {"actor": actor}))

    def remove_subscriber(
        self, target_id: uuid.UUID, subscriber: str, *, actor: str
    ) -> None:
        self.calls.append(
            ("remove_subscriber", (target_id, subscriber), {"actor": actor})
        )

    def add_label(self, target_id: uuid.UUID, label: str, *, actor: str) -> None:
        self.calls.append(("add_label", (target_id, label), {"actor": actor}))

    def remove_label(self, target_id: uuid.UUID, label: str, *, actor: str) -> None:
        self.calls.append(("remove_label", (target_id, label), {"actor": actor}))

    def add_issue_kind(
        self, target_id: uuid.UUID, issue_kind: str, *, actor: str
    ) -> None:
        self.calls.append(("add_issue_kind", (target_id, issue_kind), {"actor": actor}))

    def remove_issue_kind(
        self, target_id: uuid.UUID, issue_kind: str, *, actor: str
    ) -> None:
        self.calls.append(
            ("remove_issue_kind", (target_id, issue_kind), {"actor": actor})
        )

    def add_codechange(
        self, target_id: uuid.UUID, codechange_id: uuid.UUID, *, actor: str
    ) -> None:
        self.calls.append(
            ("add_codechange", (target_id, codechange_id), {"actor": actor})
        )

    def remove_codechange(
        self, target_id: uuid.UUID, codechange_id: uuid.UUID, *, actor: str
    ) -> None:
        self.calls.append(
            ("remove_codechange", (target_id, codechange_id), {"actor": actor})
        )

    def add_author(self, target_id: uuid.UUID, author: str, *, actor: str) -> None:
        self.calls.append(("add_author", (target_id, author), {"actor": actor}))

    def remove_author(self, target_id: uuid.UUID, author: str, *, actor: str) -> None:
        self.calls.append(("remove_author", (target_id, author), {"actor": actor}))


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def run(argv: list[str], client: FakeClient) -> None:
    cli.parse_and_run(argv, client_factory=lambda: cast(Any, client))
