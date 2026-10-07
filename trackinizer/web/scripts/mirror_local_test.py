"""Tests for ``mirror_local.py``: refusals, selection, ordering, and reruns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import uuid

import httpx2
import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.lib.codec import from_plain, loads
from trackinizer.trax.profile import Profile
from trackinizer.web.scripts.mirror_local import (
    Activity,
    ReadOnlySource,
    SourceEdge,
    Transcript,
    interleave,
    local_target,
    mirror,
    mirror_key,
    missing_edges,
    record_windows,
    select_sessions,
    source_token,
    tally,
)
from trackinizer.wire.wire_session_ir import PartBody, RecordBody


if TYPE_CHECKING:
    from collections.abc import Callable


_SOURCE = "https://tracker.example"
_T0 = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "url",
    [
        "https://tracker.example",
        "http://10.0.0.5:8765",
        "http://127.0.0.2:8765",
        "http://localhost.example:8765",
    ],
)
def test_target_refuses_a_host_that_is_not_local(url: str) -> None:
    with pytest.raises(ClientError, match=r"only 127\.0\.0\.1, localhost or"):
        local_target(url, source=_SOURCE)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8809", "http://localhost:8809/", "http://[::1]:8809"],
)
def test_target_accepts_a_loopback_server(url: str) -> None:
    assert local_target(url, source=_SOURCE) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8765", "http://localhost:8765/", "http://[::1]:8765"],
)
def test_target_refuses_the_source_itself(url: str) -> None:
    with pytest.raises(ClientError, match="is the source"):
        local_target(url, source="http://127.0.0.1:8765")


def test_token_goes_only_to_the_profile_server() -> None:
    profile = Profile(url=_SOURCE, api_key="trax_secret")
    assert source_token(f"{_SOURCE}/", profile) == "trax_secret"
    assert source_token("http://127.0.0.1:8765", profile) == ""
    assert source_token("https://tracker.example.evil", profile) == ""


def test_key_is_stable_and_distinct_per_source_row() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    assert mirror_key(first) == mirror_key(first)
    assert mirror_key(first) not in {mirror_key(second), first}


def test_tally_counts_each_session_with_its_cli_and_rooms() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    events: list[dict[str, object]] = [
        {"session_id": str(a), "cli": "claude", "rooms": ["r1"]},
        {"session_id": str(b), "cli": "codex", "rooms": []},
        {"session_id": str(a), "cli": "claude", "rooms": ["r1"]},
    ]
    assert sorted(tally(events), key=lambda x: -x.events) == [
        Activity(session_id=a, cli="claude", rooms=("r1",), events=2),
        Activity(session_id=b, cli="codex", rooms=(), events=1),
    ]


def test_tally_keeps_a_session_whose_cli_is_unknown() -> None:
    """N1 sibling: ``FeedEvent.cli`` is optional; a null one is not a crash."""
    a = uuid.uuid4()
    assert tally([{"session_id": str(a), "cli": None, "rooms": []}]) == [
        Activity(session_id=a, cli="", rooms=(), events=1),
    ]


def test_a_session_with_no_rooms_or_owner_is_copied() -> None:
    """N1 sibling: ``AgentSession.rooms`` and ``owner`` are optional on the row."""
    rows = {**_ROWS, _B: {**_ROWS[_B], "rooms": None, "owner": None}}
    source, target = _Source(rows=rows), _Target()
    with _Fake(source.handle) as source_http, _Fake(target.handle) as target_http:
        report = mirror(
            ReadOnlySource(source_http),
            target_http,
            now=_T0,
            hours=24,
            sessions=2,
            records=10,
        )
    assert report.opened == 2


def _activity(cli: str, events: int, *rooms: str) -> Activity:
    return Activity(session_id=uuid.uuid4(), cli=cli, rooms=rooms, events=events)


def test_selection_covers_every_cli_then_new_rooms_busiest_first() -> None:
    claude_a = _activity("claude", 50, "a")
    claude_a2 = _activity("claude", 40, "a")
    claude_c = _activity("claude", 20, "c")
    codex = _activity("codex", 5, "b")
    gemini = _activity("gemini", 2)
    pool = [gemini, claude_a2, codex, claude_c, claude_a]
    # The quiet CLIs make the cut; room c beats the busier session in room a.
    assert select_sessions(pool, limit=4) == [claude_a, claude_c, codex, gemini]
    assert select_sessions(pool, limit=5) == [
        claude_a,
        claude_a2,
        claude_c,
        codex,
        gemini,
    ]
    assert select_sessions(pool, limit=2) == [claude_a, codex]


def _part(part: int, records: int) -> PartBody:
    return PartBody(part=part, name=f"{part}.jsonl", format="claude", records=records)


def test_windows_take_the_newest_records_from_the_last_part_back() -> None:
    old, new = _part(0, 10), _part(1, 3)
    assert record_windows([old, new], limit=5) == [(old, 7), (new, -1)]
    assert record_windows([new, old], limit=2) == [(new, 0)]
    assert record_windows([old, new], limit=100) == [(old, -1), (new, -1)]


def _record(idx: int, minute: float | None) -> RecordBody:
    return RecordBody(
        idx=idx,
        kind="UserMessage",
        timestamp=None if minute is None else _T0 + timedelta(minutes=minute),
        payload={"content": f"turn {idx}"},
        text=f"turn {idx}",
    )


def _transcript(*minutes: float | None) -> Transcript:
    records = tuple(_record(idx, minute) for idx, minute in enumerate(minutes))
    return Transcript(
        session_id=uuid.uuid4(),
        parts=((_part(0, len(records)), records),),
    )


def _runs(transcripts: list[Transcript], **kwargs: int) -> list[tuple[int, list[int]]]:
    order = [t.session_id for t in transcripts]
    return [
        (order.index(a.session_id), [r.idx for r in a.records])
        for a in interleave(transcripts, **kwargs)
    ]


def test_interleave_orders_records_by_clock_across_sessions() -> None:
    first, second = _transcript(0, 2, 3, 6), _transcript(1, 4)
    assert _runs([first, second]) == [
        (0, [0]),
        (1, [0]),
        (0, [1, 2]),
        (1, [1]),
        (0, [3]),
    ]


def test_interleave_keeps_each_part_in_idx_order() -> None:
    # The second record has no clock and the third steps back; both stay put,
    # and a leading clockless record takes its part's first clock.
    first, second = _transcript(5, None, 3, 6), _transcript(None, 4, 5.5)
    assert _runs([first, second]) == [(1, [0, 1]), (0, [0, 1, 2]), (1, [2]), (0, [3])]
    assert _runs([_transcript(1, 2), _transcript(None, 3)]) == [
        (0, [0, 1]),
        (1, [0, 1]),
    ]


def test_interleave_caps_each_append_at_the_batch() -> None:
    assert _runs([_transcript(0, 1, 2, 3, 4)], batch=2) == [
        (0, [0, 1]),
        (0, [2, 3]),
        (0, [4]),
    ]


def test_missing_edges_skips_present_and_uncopied_ends() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ids = {a: uuid.uuid4(), b: uuid.uuid4()}
    present = SourceEdge(from_id=a, to_id=b, edge_kind="narrows")
    absent = SourceEdge(from_id=b, to_id=a, edge_kind="produced_by")
    uncopied = SourceEdge(from_id=a, to_id=c, edge_kind="narrows")
    local = {(ids[a], ids[b], "narrows")}
    assert missing_edges([present, absent, uncopied], ids=ids, local=local) == [absent]


def test_mirror_reads_with_get_only_interleaves_and_reruns_add_nothing() -> None:
    source, target = _Source(), _Target()
    with _Fake(source.handle) as source_http, _Fake(target.handle) as target_http:
        reader = ReadOnlySource(source_http)
        first = mirror(reader, target_http, now=_T0, hours=24, sessions=2, records=10)
        second = mirror(reader, target_http, now=_T0, hours=24, sessions=2, records=10)
    assert source.methods == {"GET"}
    assert {a.session_id for a in first.sessions} == {_A, _B}
    assert (first.nodes, first.opened) == (5, 2)
    assert (first.written, first.ended, first.edges) == (7, 1, 2)
    alpha, beta = (target.keys[mirror_key(s)] for s in (_A, _B))
    assert [session for session, _ in target.order] == [
        alpha,
        beta,
        alpha,
        beta,
        alpha,
        beta,
        alpha,
    ]
    ended = from_plain(target.rows[alpha]["ended"], datetime)
    assert ended == _T0 + timedelta(hours=1)
    copied = {"title", "status", "labels", "description", "idempotency_key"}
    issue = target.rows[target.keys[mirror_key(_ISSUE)]]
    assert {key: issue[key] for key in copied - {"idempotency_key"}} == {
        "title": "Issue 1",
        "status": "active",
        "labels": ["mirror"],
        "description": "copied",
    }
    assert copied <= issue.keys()
    assert "owner" not in issue
    belief = target.rows[target.keys[mirror_key(_BELIEF)]]
    assert (belief["judgement"], belief["confidence"], belief["owner"]) == (
        "proven",
        0.8,
        "ada@example.com",
    )
    assert copied <= belief.keys()
    # The rerun finds every row, skips the ended session, re-sends the live one.
    assert (second.opened, second.written, second.skipped) == (0, 0, 3)
    assert (second.ended, second.edges) == (0, 0)
    assert len(target.rows) == first.nodes
    assert (first.unfilled, second.unfilled) == ((), ())


def test_a_rerun_finds_edges_older_than_any_window_of_the_target_graph() -> None:
    """N1-11: a target past the graph's window still has every copied edge."""
    source, target = _Source(), _Target(windowed=True)
    with _Fake(source.handle) as source_http, _Fake(target.handle) as target_http:
        reader = ReadOnlySource(source_http)
        first = mirror(reader, target_http, now=_T0, hours=24, sessions=2, records=10)
        second = mirror(reader, target_http, now=_T0, hours=24, sessions=2, records=10)
    assert (first.edges, second.edges) == (2, 0)
    assert target.edge_posts == 1


def test_rerun_names_a_picked_session_that_ended_here_without_records() -> None:
    # Copied as a graph row, a session ends here with no records; a later run
    # that picks it cannot append, since an ended session refuses records.
    rows = {**_ROWS, _B: {**_ROWS[_B], "status": "complete", "ended": _iso(_T0)}}
    source = _Source(rows=rows, graph_nodes=(_ISSUE, _CHILD, _BELIEF, _A, _B))
    target = _Target()
    with _Fake(source.handle) as source_http, _Fake(target.handle) as target_http:
        reader = ReadOnlySource(source_http)
        first = mirror(reader, target_http, now=_T0, hours=24, sessions=1, records=10)
        second = mirror(reader, target_http, now=_T0, hours=24, sessions=2, records=10)
    assert [a.session_id for a in first.sessions] == [_A]
    assert (first.unfilled, second.unfilled) == ((), (_B,))
    assert second.written == 0


_A, _B, _ISSUE, _CHILD, _BELIEF = (uuid.uuid4() for _ in range(5))


def _iso(moment: datetime) -> str:
    return moment.isoformat()


class _Fake(Client):
    """A ``Client`` whose requests reach ``handler`` instead of the network."""

    def __init__(self, handler: Callable[[httpx2.Request], httpx2.Response]) -> None:
        super().__init__("http://127.0.0.1:1")
        self._http.close()
        self._http = httpx2.Client(
            base_url=self.base_url,
            transport=httpx2.MockTransport(handler),
        )


def _json(status: int, body: object) -> httpx2.Response:
    return httpx2.Response(status, json=body)


@dataclass(slots=True, kw_only=True)
class _Source:
    """The source server: two sessions, a small graph, and every method seen."""

    rows: Mapping[uuid.UUID, Mapping[str, object]] = field(
        default_factory=lambda: _ROWS,
    )
    graph_nodes: tuple[uuid.UUID, ...] = (_ISSUE, _CHILD, _BELIEF, _A)
    methods: set[str] = field(default_factory=set)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.methods.add(request.method)
        path, params = request.url.path, request.url.params
        if request.method != "GET":
            return _json(405, {"detail": "read only"})
        if path == "/api/web/feed":
            events = [self._event(s) for s in (_A, _B, _A, _B, _A)]
            return _json(200, {"events": events})
        if path == "/api/web/graph":
            light = ("id", "kind", "seq", "title", "status", "created")
            nodes = [{k: self.rows[n][k] for k in light} for n in self.graph_nodes]
            return _json(200, {"nodes": nodes, "edges": _EDGES})
        if path == "/api/inquiries":
            bounds = (r.split("..") for r in params.get_list("seq_range"))
            seqs = {seq for a, b in bounds for seq in range(int(a), int(b) + 1)}
            rows = [
                r
                for r in self.rows.values()
                if r["kind"] == params["kind"] and r["seq"] in seqs
            ]
            return _json(200, rows)
        _, _, kind, raw, *rest = path.split("/")
        row = self.rows[uuid.UUID(raw)]
        if kind == "inquiries":
            return _json(200, row)
        name, records = _TRANSCRIPTS[uuid.UUID(raw)]
        if rest == ["parts"]:
            part = {
                "part": 0,
                "name": name,
                "format": "claude",
                "records": len(records),
            }
            return _json(200, {"parts": [part]})
        after = int(params["after_idx"])
        page = [r for r in records if from_plain(r["idx"], int) > after]
        page = page[: int(params["limit"])]
        return _json(200, {"part": 0, "records": page})

    def _event(self, session: uuid.UUID) -> dict[str, object]:
        row = self.rows[session]
        rooms = from_plain(row["rooms"], list[str], default=[])
        return {"session_id": str(session), "cli": row["cli"], "rooms": rooms}


def _row(
    kind: str,
    seq: int,
    source_id: uuid.UUID,
    **extra: object,
) -> dict[str, object]:
    return {
        "id": str(source_id),
        "kind": kind,
        "seq": seq,
        "title": f"{kind} {seq}",
        "status": "active",
        "created": _iso(_T0 + timedelta(seconds=seq)),
        "labels": ["mirror"],
        "owner": None,
        "description": "copied",
        **extra,
    }


_ROWS: Mapping[uuid.UUID, dict[str, object]] = {
    _ISSUE: _row("Issue", 1, _ISSUE),
    _CHILD: _row("Issue", 2, _CHILD),
    _BELIEF: _row(
        "Belief",
        1,
        _BELIEF,
        judgement="proven",
        confidence=0.8,
        owner="ada@example.com",
    ),
    _A: _row(
        "AgentSession",
        7,
        _A,
        cli="claude",
        rooms=["r1"],
        owner="alpha",
        status="complete",
        started=_iso(_T0),
        ended=_iso(_T0 + timedelta(hours=1)),
    ),
    _B: _row("AgentSession", 8, _B, cli="codex", rooms=["r2"], owner="beta"),
}

_TRANSCRIPTS: Mapping[uuid.UUID, tuple[str, list[dict[str, object]]]] = {
    session: (
        f"{session}.jsonl",
        [
            {
                "idx": idx,
                "kind": "AssistantMessage",
                "timestamp": _iso(_T0 + timedelta(minutes=minute)),
                "payload": {"content": f"{minute}"},
                "text": f"{minute}",
            }
            for idx, minute in enumerate(minutes)
        ],
    )
    for session, minutes in ((_A, (0, 2, 4, 6)), (_B, (1, 3, 5)))
}

_EDGES: list[dict[str, object]] = [
    {"from_id": str(_CHILD), "to_id": str(_ISSUE), "edge_kind": "narrows"},
    {"from_id": str(_BELIEF), "to_id": str(_A), "edge_kind": "produced_by"},
]


@dataclass(slots=True, kw_only=True)
class _Target:
    """The local server's ingest routes, as far as the mirror relies on them.

    ``windowed`` stands for a target with more nodes than its graph route
    returns: the graph shows none of the mirror's edges.
    """

    windowed: bool = False
    keys: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    rows: dict[uuid.UUID, dict[str, object]] = field(default_factory=dict)
    records: set[tuple[uuid.UUID, str, int]] = field(default_factory=set)
    order: list[tuple[uuid.UUID, int]] = field(default_factory=list)
    edges: set[tuple[str, str, str]] = field(default_factory=set)
    edge_posts: int = 0

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        raw = loads(request.content or b"null")
        body = {} if raw is None else from_plain(raw, dict[str, object])
        if request.method == "GET":
            return self._get(path)
        if path == "/api/sessions/start":
            local = self._create(body, kind="AgentSession", owner=body["actor"])
            return _json(201, {"id": str(local), "seq": 0, "actor": body["actor"]})
        if path == "/api/inquiries/batch":
            items = from_plain(body["items"], list[dict[str, object]])
            ids = [
                self._create(item, kind=from_plain(item["kind"], str)) for item in items
            ]
            return _json(200, {"ids": [str(i) for i in ids]})
        if path == "/api/edges/batch":
            self.edge_posts += 1
            items = from_plain(body["items"], list[dict[str, object]])
            for item in items:
                from_id, to_id, kind = (
                    from_plain(item[key], str)
                    for key in ("from_id", "to_id", "edge_kind")
                )
                self.edges.add((from_id, to_id, kind))
            return _json(200, {"ok": True, "items": [{"ok": True}] * len(items)})
        _, _, _, raw, verb = path.split("/")
        row = self.rows[uuid.UUID(raw)]
        if row["ended"] is not None:
            return _json(409, {"detail": "session has ended"})
        if verb == "end":
            row["ended"] = body["ended"]
            return _json(200, {"id": raw, "ended": body["ended"]})
        return _json(200, self._append(uuid.UUID(raw), body))

    def _get(self, path: str) -> httpx2.Response:
        if path == "/api/web/graph":
            edges = [
                {"from_id": f, "to_id": t, "edge_kind": k}
                for f, t, k in self.edges
                if not self.windowed
            ]
            return _json(200, {"nodes": [], "edges": edges})
        if path.startswith("/api/web/get/"):
            subject = path.removeprefix("/api/web/get/")
            if uuid.UUID(subject) not in self.rows:
                return _json(404, {"detail": "not found"})
            outbound: dict[str, list[dict[str, object]]] = {}
            for f, t, k in sorted(self.edges):
                if f == subject:
                    outbound.setdefault(k, []).append({"id": t})
            return _json(200, {"self": {}, "edges": outbound, "backlinks": {}})
        if path.endswith("/parts"):
            session = uuid.UUID(path.split("/")[3])
            names = sorted({name for s, name, _ in self.records if s == session})
            parts = [
                {"part": part, "name": name, "format": "", "records": 0}
                for part, name in enumerate(names)
            ]
            return _json(200, {"parts": parts})
        _, _, kind, raw = path.split("/")
        if kind == "change_log":
            local = self.keys.get(uuid.UUID(raw))
            if local is None:
                return _json(404, {"detail": "change not found"})
            return _json(200, {"id": raw, "subject_id": str(local)})
        return _json(200, self.rows[uuid.UUID(raw)])

    def _create(self, body: Mapping[str, object], **row: object) -> uuid.UUID:
        key = uuid.UUID(from_plain(body["idempotency_key"], str))
        if key not in self.keys:
            self.keys[key] = uuid.uuid4()
            self.rows[self.keys[key]] = {**body, "ended": None, **row}
        return self.keys[key]

    def _append(self, session: uuid.UUID, body: Mapping[str, object]) -> object:
        name = from_plain(body["name"], str)
        written = 0
        records = from_plain(body["records"], list[dict[str, object]])
        for record in records:
            idx = from_plain(record["idx"], int)
            if (session, name, idx) not in self.records:
                self.records.add((session, name, idx))
                self.order.append((session, idx))
                written += 1
        return {"part": 0, "written": written, "skipped": len(records) - written}


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
