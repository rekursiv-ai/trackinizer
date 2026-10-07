#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Copy recent sessions and the newest graph from a server into a local one.

The source is read with GET requests only; the local server is written through
its own ingest routes, so the copy is shaped as real capture shapes it.

Sessions: the busiest sessions active in the last --hours, covering every CLI
and then as many rooms as fit, each with its AgentSession row, parts, manifests
and newest --records records. Records land in clock order across sessions, so
the console feed interleaves them as the source's did. Graph: the source's
/api/web/graph nodes, with title, kind, status, labels, owner and description,
and the edges between them.

The local server mints its own ids. Each source row is created under a key
derived from its source id, so a rerun finds what it wrote and copies only what
is missing. The target must be a local server (127.0.0.1, localhost or [::1])
other than the source. The source defaults to the active trax profile's server,
and only that server gets the profile's token.

Examples:
  ./mirror_local.py --to http://127.0.0.1:8809
  ./mirror_local.py --to http://127.0.0.1:8809 --hours 6 --sessions 4 --records 500
  ./mirror_local.py --from http://127.0.0.1:8765 --to http://127.0.0.1:8809

'''
# fmt: on

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

import argparse
import itertools
import time
import uuid

from trackinizer.client.client import Client, server_url
from trackinizer.client.errors import ClientError
from trackinizer.lib.codec import from_plain
from trackinizer.trax.profile import load_profile
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.bodies import BATCH_MAX_ITEMS
from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.seq_ranges import SeqRange, format_interval
from trackinizer.wire.wire_session_ir import (
    MAX_RECORD_BATCH,
    ManifestBody,
    PartBody,
    RecordBody,
)
from trackinizer.wire.wire_sessions import SessionEnd, SessionStart


if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Sequence

    from trackinizer.lib.codec import PlainTree
    from trackinizer.trax.profile import Profile


_KEY_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "trackinizer-mirror-local")
_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_ACTOR: Final = "mirror"


def main() -> int:
    """Mirror the source into the local server and print what was copied.

    Returns:
      result: Process exit code (0 on success).

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args())
    profile = load_profile()
    source_url = server_url(flags.source or profile.url, "--from")
    target_url = local_target(flags.to, source=source_url)
    token = source_token(source_url, profile)
    # A day's records read in pages of 1000; one page can take seconds.
    with (
        Client(source_url, api_key=token, timeout_sec=120.0) as source,
        Client(target_url, author=_ACTOR, timeout_sec=120.0) as target,
    ):
        report = mirror(
            ReadOnlySource(source),
            target,
            now=datetime.now(UTC),
            hours=flags.hours,
            sessions=flags.sessions,
            records=flags.records,
        )
    print(_summary(report, source=source_url, target=target_url))
    return 0


def local_target(url: str, *, source: str) -> str:
    """Return ``url`` normalized, or raise when it is not a local non-source server.

    Args:
      url: The server to write.
      source: The server read from.

    Returns:
      target: ``url`` without its trailing slash.

    Raises:
      ClientError: ``url`` is malformed, names a host other than 127.0.0.1,
        localhost or [::1], or reaches the same server as ``source``.

    """
    target = server_url(url, "--to")
    if urlsplit(target).hostname not in _LOOPBACK:
        raise ClientError(
            f"refusing to write {target}: only 127.0.0.1, localhost or [::1] "
            "may be a target",
        )
    if _endpoint(target) == _endpoint(source):
        raise ClientError(f"refusing to write {target}: it is the source")
    return target


def source_token(url: str, profile: Profile) -> str:
    """Return the profile's token when ``url`` is the profile's server, else ``""``."""
    return profile.api_key if _endpoint(url) == _endpoint(profile.url) else ""


def mirror_key(source_id: UUID) -> UUID:
    """Return the idempotency key a source row is created under, and found by again."""
    return uuid.uuid5(_KEY_NAMESPACE, str(source_id))


@dataclass(frozen=True, slots=True, kw_only=True)
class Activity:
    """How busy one source session was in the sampled feed."""

    session_id: UUID
    cli: str
    rooms: tuple[str, ...]
    events: int


def tally(events: Iterable[Mapping[str, object]]) -> list[Activity]:
    """Count feed events per session, keeping each session's CLI and rooms.

    Args:
      events: Feed events, as ``/api/web/feed`` serves them.

    Returns:
      activity: One entry per session seen.

    """
    counts: Counter[UUID] = Counter()
    last: dict[UUID, Mapping[str, object]] = {}
    for event in events:
        session_id = UUID(from_plain(event.get("session_id"), str))
        counts[session_id] += 1
        last[session_id] = event
    return [
        Activity(
            session_id=session_id,
            cli=from_plain(last[session_id].get("cli"), str, default=""),
            rooms=tuple(
                from_plain(last[session_id].get("rooms"), list[str], default=[]),
            ),
            events=count,
        )
        for session_id, count in counts.items()
    ]


def select_sessions(activity: Sequence[Activity], *, limit: int) -> list[Activity]:
    """Pick up to ``limit`` sessions, busiest first.

    The busiest session of each CLI comes first, so a CLI that was quiet in the
    window still appears; then sessions that add a room not yet covered; then
    the busiest of the rest.

    Args:
      activity: Every session seen, with its event count.
      limit: How many to pick.

    Returns:
      chosen: The picked sessions, busiest first.

    """
    ranked = sorted(activity, key=_busiest)
    chosen: list[Activity] = []
    for wanted in (_new_cli, _new_room, _any):
        for candidate in ranked:
            if (
                len(chosen) < limit
                and candidate not in chosen
                and wanted(candidate, chosen)
            ):
                chosen.append(candidate)
    return sorted(chosen, key=_busiest)


def record_windows(
    parts: Sequence[PartBody],
    *,
    limit: int,
) -> list[tuple[PartBody, int]]:
    """Return each part to read with the ``after_idx`` that reads its newest records.

    The server numbers a session's files in the order they first arrived, so the
    budget of ``limit`` records is spent from the highest part backwards.

    Args:
      parts: The session's parts, as the source lists them.
      limit: How many records to read in all.

    Returns:
      windows: ``(part, after_idx)`` pairs, oldest part first.

    """
    windows: list[tuple[PartBody, int]] = []
    budget = limit
    for part in sorted(parts, key=lambda p: p.part, reverse=True):
        take = min(budget, part.records)
        if take:
            windows.append((part, part.records - take - 1))
        budget -= take
    return windows[::-1]


@dataclass(frozen=True, slots=True, kw_only=True)
class Transcript:
    """One source session's newest records, part by part, oldest part first."""

    session_id: UUID
    parts: tuple[tuple[PartBody, tuple[RecordBody, ...]], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Append:
    """One append request: consecutive records of one session's part."""

    session_id: UUID
    part: PartBody
    records: tuple[RecordBody, ...]


def interleave(
    transcripts: Sequence[Transcript],
    *,
    batch: int = MAX_RECORD_BATCH,
) -> list[Append]:
    """Order every record by its clock across sessions, as appends of up to ``batch``.

    The server stamps a record's ``created`` as it lands and the console feed
    orders by ``created``, so writing in clock order is what interleaves the
    sessions there. A record's clock is the latest timestamp at or before it in
    its part, or the part's first timestamp before any: a part keeps its ``idx``
    order where timestamps step back or are missing.

    Args:
      transcripts: The sessions, busiest first; that rank breaks clock ties.
      batch: Most records per append.

    Returns:
      appends: The append requests, in the order to send them.

    """
    slots = sorted(
        (
            _Slot(
                clock=(clock, rank, part.part, record.idx),
                session_id=transcript.session_id,
                part=part,
                record=record,
            )
            for rank, transcript in enumerate(transcripts)
            for part, records in transcript.parts
            for clock, record in zip(_clocks(records), records, strict=True)
        ),
        key=lambda slot: slot.clock,
    )
    runs = itertools.groupby(slots, key=lambda slot: (slot.session_id, slot.part.part))
    return [
        Append(
            session_id=chunk[0].session_id,
            part=chunk[0].part,
            records=tuple(slot.record for slot in chunk),
        )
        for _, run in runs
        for chunk in itertools.batched(run, batch)
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceNode:
    """One source inquiry to recreate: its id, kind, creation time and row."""

    id: UUID
    kind: Inquiry.InquiryKind
    created: datetime
    row: Mapping[str, object]


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceEdge:
    """One source edge, with its valence when it has one."""

    from_id: UUID
    to_id: UUID
    edge_kind: str
    valence: float | None = None


def missing_edges(
    edges: Sequence[SourceEdge],
    *,
    ids: Mapping[UUID, UUID],
    local: Collection[tuple[UUID, UUID, str]],
) -> list[SourceEdge]:
    """Return the source edges whose ends were copied and which the target lacks.

    Args:
      edges: The source's edges.
      ids: Local id of each copied source row.
      local: The target's edges as ``(from_id, to_id, edge_kind)``.

    Returns:
      missing: The edges still to add, in source order.

    """
    return [
        edge
        for edge in edges
        if edge.from_id in ids
        and edge.to_id in ids
        and (ids[edge.from_id], ids[edge.to_id], edge.edge_kind) not in local
    ]


class ReadOnlySource:
    """A source server narrowed to GET requests.

    The mirror holds only this, never the ``Client`` inside it, so no code path
    here can send the source a write.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def get(self, path: str, **params: object) -> PlainTree:
        """Send one GET request."""
        return self._client.get(path, params=params)

    def parts(self, session_id: UUID) -> list[PartBody]:
        """List a session's parts."""
        return self._client.read_session_parts(session_id)

    def records(
        self,
        session_id: UUID,
        *,
        part: int,
        after_idx: int,
    ) -> list[RecordBody]:
        """Read one part's records after ``after_idx``, ciphertext included."""
        return self._client.read_session_records(
            session_id,
            part=part,
            after_idx=after_idx,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Report:
    """What one mirror run copied, and how long each step took."""

    sessions: tuple[Activity, ...]
    nodes: int
    opened: int
    written: int
    skipped: int
    ended: int
    edges: int
    edge_failures: tuple[str, ...]
    unfilled: tuple[UUID, ...]
    """Picked sessions that ended here before their records were copied."""

    seconds: Mapping[str, float]


def mirror(
    source: ReadOnlySource,
    target: Client,
    *,
    now: datetime,
    hours: float,
    sessions: int,
    records: int,
) -> Report:
    """Copy recent sessions and the newest graph from ``source`` into ``target``.

    Every row is created in source order before any record is appended, and a
    session is ended only after its records land, because an ended session
    refuses appends.

    Args:
      source: The server to read.
      target: The local server to write.
      now: The end of the session window.
      hours: The window's length.
      sessions: How many sessions to copy.
      records: The most records to copy per session.

    Returns:
      report: Counts and per-step timings.

    """
    clock = _Stopwatch()
    chosen = select_sessions(_sample(source, now=now, hours=hours), limit=sessions)
    picked = [_node(source.get(f"/api/inquiries/{a.session_id}")) for a in chosen]
    transcripts = [_transcript(source, a.session_id, limit=records) for a in chosen]
    clock.lap("read sessions")
    graph, edges = _read_graph(source)
    clock.lap("read graph")
    nodes = list({node.id: node for node in (*picked, *graph)}.values())
    ids, live, opened = _create(target, nodes)
    clock.lap("create rows")
    written, skipped, unfilled = _append(target, transcripts, ids=ids, live=live)
    clock.lap("append records")
    ended = _end(target, nodes, ids=ids, live=live)
    clock.lap("end sessions")
    added, failures = _add_edges(target, edges, ids=ids)
    clock.lap("add edges")
    return Report(
        sessions=tuple(chosen),
        nodes=len(nodes),
        opened=opened,
        written=written,
        skipped=skipped,
        ended=ended,
        edges=added,
        edge_failures=tuple(failures),
        unfilled=unfilled,
        seconds=clock.laps,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Slot:
    """One record with the key that places it in the interleaved order."""

    clock: tuple[datetime, int, int, int]
    session_id: UUID
    part: PartBody
    record: RecordBody


class _Stopwatch:
    """Seconds spent in each named step."""

    def __init__(self) -> None:
        self.laps: dict[str, float] = {}
        self._start = time.perf_counter()

    def lap(self, name: str) -> None:
        """Close the step ``name`` and start the next."""
        now = time.perf_counter()
        self.laps[name] = now - self._start
        self._start = now


def _endpoint(url: str) -> tuple[str, int, str]:
    """Return what makes two URLs one server: host (loopback as one), port, path."""
    parts = urlsplit(server_url(url, "url"))
    host = "loopback" if parts.hostname in _LOOPBACK else parts.hostname or ""
    return host, parts.port or (443 if parts.scheme == "https" else 80), parts.path


def _busiest(activity: Activity) -> tuple[int, str]:
    return -activity.events, str(activity.session_id)


def _new_cli(candidate: Activity, chosen: Sequence[Activity]) -> bool:
    return candidate.cli not in {c.cli for c in chosen}


def _new_room(candidate: Activity, chosen: Sequence[Activity]) -> bool:
    return bool(set(candidate.rooms) - {room for c in chosen for room in c.rooms})


def _any(candidate: Activity, chosen: Sequence[Activity]) -> bool:
    del candidate, chosen
    return True


def _clocks(records: Sequence[RecordBody]) -> list[datetime]:
    """Return each record's clock: the latest timestamp so far, else the first."""
    known = [r.timestamp for r in records if r.timestamp is not None]
    clock = known[0] if known else datetime.min.replace(tzinfo=UTC)
    clocks: list[datetime] = []
    for record in records:
        if record.timestamp is not None and record.timestamp > clock:
            clock = record.timestamp
        clocks.append(clock)
    return clocks


# Paging the whole window would read every record of the day (tens of thousands); a
# dozen evenly spread pages estimate each session's share of it.
def _sample(source: ReadOnlySource, *, now: datetime, hours: float) -> list[Activity]:
    """Tally sessions over 12 feed pages of 250 spread evenly across the window."""
    events: list[dict[str, object]] = []
    for k in range(12):
        since = now - timedelta(hours=hours * (12 - k) / 12)
        page = from_plain(
            source.get("/api/web/feed", since=since.isoformat(), limit=250),
            dict[str, object],
        )
        events.extend(
            from_plain(page.get("events"), list[dict[str, object]], default=[]),
        )
    return tally(events)


def _transcript(source: ReadOnlySource, session_id: UUID, *, limit: int) -> Transcript:
    """Read a session's newest ``limit`` records, part by part."""
    windows = record_windows(source.parts(session_id), limit=limit)
    return Transcript(
        session_id=session_id,
        parts=tuple(
            # A live part may have grown since it was listed; its manifest, read
            # first, is what bounds the records the copy declares.
            (
                part,
                tuple(
                    r
                    for r in source.records(session_id, part=part.part, after_idx=after)
                    if r.idx < part.records
                ),
            )
            for part, after in windows
        ),
    )


def _read_graph(source: ReadOnlySource) -> tuple[list[SourceNode], list[SourceEdge]]:
    """Read the graph's nodes as whole rows, and its edges."""
    graph = from_plain(source.get("/api/web/graph", limit=1000), dict[str, object])
    seqs: dict[str, list[int]] = {}
    for node in from_plain(graph.get("nodes"), list[dict[str, object]], default=[]):
        seqs.setdefault(from_plain(node.get("kind"), str), []).append(
            from_plain(node.get("seq"), int),
        )
    nodes = [
        _node(row)
        for kind, kind_seqs in seqs.items()
        for row in _rows_by_seq(source, kind, kind_seqs)
    ]
    edges = [
        SourceEdge(
            from_id=UUID(from_plain(edge.get("from_id"), str)),
            to_id=UUID(from_plain(edge.get("to_id"), str)),
            edge_kind=from_plain(edge.get("edge_kind"), str),
            valence=from_plain(edge["valence"], float) if "valence" in edge else None,
        )
        for edge in from_plain(graph.get("edges"), list[dict[str, object]], default=[])
    ]
    return nodes, edges


# The graph's light nodes lack description, labels and owner. Reading each node by id
# costs a request per node; by seq it is a few requests per kind. 200 seqs per request
# keeps the URL a few KB, under what proxies on the way accept.
def _rows_by_seq(
    source: ReadOnlySource,
    kind: str,
    seqs: Sequence[int],
) -> list[dict[str, object]]:
    """Read the whole rows of ``kind`` with the given seqs."""
    rows: list[dict[str, object]] = []
    for chunk in itertools.batched(sorted(set(seqs)), 200):
        ranges = [
            format_interval(SeqRange(start=run[0], stop=run[-1]))
            for run in _runs(chunk)
        ]
        rows.extend(
            from_plain(
                source.get(
                    "/api/inquiries",
                    kind=kind,
                    limit=MAX_LIST_LIMIT,
                    seq_range=ranges,
                ),
                list[dict[str, object]],
            ),
        )
    return rows


def _runs(seqs: Sequence[int]) -> list[list[int]]:
    """Split sorted ints into runs of consecutive values."""
    runs: list[list[int]] = []
    for seq in seqs:
        if runs and runs[-1][-1] + 1 == seq:
            runs[-1].append(seq)
        else:
            runs.append([seq])
    return runs


def _node(row: object) -> SourceNode:
    fields = from_plain(row, dict[str, object])
    created = from_plain(fields.get("created"), datetime)
    return SourceNode(
        id=UUID(from_plain(fields.get("id"), str)),
        # A kind this server lacks is refused by its submit route, by name.
        kind=cast(Inquiry.InquiryKind, from_plain(fields.get("kind"), str)),
        created=created,
        row=fields,
    )


# Sessions open through the capture route and every other kind lands in a batch between
# them, so local ``created`` keeps the source's order. A batch replays rows that exist
# under their key; a session is looked up first, since opening one that exists would be
# a second start.
def _create(
    target: Client,
    nodes: Sequence[SourceNode],
) -> tuple[dict[UUID, UUID], set[UUID], int]:
    """Create every node in source order."""
    ids: dict[UUID, UUID] = {}
    live: set[UUID] = set()
    opened = 0
    pending: list[SourceNode] = []
    for node in sorted(nodes, key=lambda n: (n.created, str(n.id))):
        if node.kind != "AgentSession":
            pending.append(node)
            continue
        ids |= _submit(target, pending)
        pending = []
        local, is_live, is_new = _open(target, node)
        ids[node.id] = local
        if is_live:
            live.add(node.id)
        opened += is_new
    ids |= _submit(target, pending)
    return ids, live, opened


def _submit(target: Client, nodes: Sequence[SourceNode]) -> dict[UUID, UUID]:
    """Create non-session rows in batches; return their local ids."""
    ids: dict[UUID, UUID] = {}
    for chunk in itertools.batched(nodes, BATCH_MAX_ITEMS):
        items: list[tuple[Inquiry.InquiryKind, Mapping[str, object]]] = [
            (n.kind, _submit_body(n)) for n in chunk
        ]
        ids |= zip(
            (n.id for n in chunk),
            target.submit_batch(items, actor=_ACTOR),
            strict=True,
        )
    return ids


def _submit_body(node: SourceNode) -> dict[str, object]:
    """Return the submit fields copied for a non-session row."""
    copied = ("title", "status", "labels", "owner", "description")
    if node.kind == "Belief":
        copied += ("judgement", "confidence")
    body = {key: node.row.get(key) for key in copied}
    body["idempotency_key"] = str(mirror_key(node.id))
    return {key: value for key, value in body.items() if value is not None}


def _open(target: Client, node: SourceNode) -> tuple[UUID, bool, bool]:
    """Find or open a session; return its local id, whether live, whether new."""
    key = mirror_key(node.id)
    try:
        change = from_plain(target.get(f"/api/change_log/{key}"), dict[str, object])
    except ClientError as err:
        if err.status_code != 404:
            raise
    else:
        local = UUID(from_plain(change.get("subject_id"), str))
        row = from_plain(target.get(f"/api/inquiries/{local}"), dict[str, object])
        return (
            local,
            from_plain(row.get("ended"), datetime, default=None) is None,
            False,
        )
    row = node.row
    started = target.session_start(
        SessionStart(
            cli=from_plain(row.get("cli"), str),
            title=from_plain(row.get("title"), str, default="") or None,
            started=from_plain(row.get("started"), datetime, default=None),
            actor=from_plain(row.get("owner"), str, default="") or None,
            rooms=from_plain(row.get("rooms"), list[str], default=[]) or None,
            idempotency_key=key,
        ),
    )
    return started.id, True, True


# A session ended here refuses appends, and the server reopens one only for a start
# that names its ``cli_session_id``, which most sessions lack. One that an earlier run
# copied as a bare graph row therefore cannot take the records a later run picks it
# for; the caller is told rather than shown it as copied.
def _append(
    target: Client,
    transcripts: Sequence[Transcript],
    *,
    ids: Mapping[UUID, UUID],
    live: Collection[UUID],
) -> tuple[int, int, tuple[UUID, ...]]:
    """Append live sessions' records in clock order; return counts and the unfilled."""
    unfilled = tuple(
        t.session_id
        for t in transcripts
        if t.session_id not in live and not target.read_session_parts(ids[t.session_id])
    )
    written = skipped = 0
    for append in interleave([t for t in transcripts if t.session_id in live]):
        part = append.part
        response = target.append_records(
            ids[append.session_id],
            name=part.name,
            manifest=ManifestBody(
                name=part.name,
                metadata=part.metadata,
                ir_id=part.ir_id
                or uuid.uuid5(_KEY_NAMESPACE, f"{append.session_id}/{part.name}"),
                format=part.format,
                records=part.records,
            ),
            records=append.records,
        )
        written += response.written
        skipped += response.skipped
    return written, skipped, unfilled


def _end(
    target: Client,
    nodes: Sequence[SourceNode],
    *,
    ids: Mapping[UUID, UUID],
    live: Collection[UUID],
) -> int:
    """End each live local session whose source session ended; return how many."""
    ended = 0
    for node in nodes:
        when = from_plain(node.row.get("ended"), datetime, default=None)
        if node.id not in live or when is None:
            continue
        target.session_end(
            ids[node.id],
            SessionEnd(
                ended=when,
                cli_session_id=from_plain(
                    node.row.get("cli_session_id"),
                    str,
                    default="",
                )
                or None,
                actor=from_plain(node.row.get("owner"), str, default="") or None,
            ),
        )
        ended += 1
    return ended


# The batch route stops at its first refused item, so the rest are resent without it.
def _add_edges(
    target: Client,
    edges: Sequence[SourceEdge],
    *,
    ids: Mapping[UUID, UUID],
) -> tuple[int, list[str]]:
    """Add the edges the target lacks; return how many landed, and each refusal."""
    copied = [edge for edge in edges if edge.from_id in ids and edge.to_id in ids]
    local = _local_edges(target, {ids[edge.from_id] for edge in copied})
    items = [
        _edge_item(edge, ids=ids)
        for edge in missing_edges(copied, ids=ids, local=local)
    ]
    added = 0
    failures: list[str] = []
    while items:
        chunk, items = items[:BATCH_MAX_ITEMS], items[BATCH_MAX_ITEMS:]
        response = from_plain(
            target.post("/api/edges/batch", body={"items": chunk}),
            dict[str, object],
        )
        results = from_plain(response.get("items"), list[dict[str, object]], default=[])
        done = len(
            list(itertools.takewhile(lambda r: from_plain(r.get("ok"), bool), results)),
        )
        added += done
        if done < len(chunk):
            failures.append(
                f"{chunk[done]}: {from_plain(results[done].get('error'), str)}",
            )
            items = chunk[done + 1 :] + items
    return added, failures


# Read by each copied edge's ``from`` end, not from the graph route: that returns only
# the newest nodes, so on a large target an older copied edge looks missing, is posted
# again, upserts as a no-op, and is counted as added.
def _local_edges(
    target: Client,
    subjects: Iterable[UUID],
) -> set[tuple[UUID, UUID, str]]:
    """Return the target's outbound edges of ``subjects``: ``(from, to, kind)``."""
    local: set[tuple[UUID, UUID, str]] = set()
    for subject in subjects:
        view = from_plain(target.get(f"/api/web/get/{subject}"), dict[str, object])
        for kind, peers in from_plain(view.get("edges"), dict[str, object]).items():
            local.update(
                (subject, UUID(from_plain(peer.get("id"), str)), kind)
                for peer in from_plain(peers, list[dict[str, object]])
            )
    return local


def _edge_item(edge: SourceEdge, *, ids: Mapping[UUID, UUID]) -> dict[str, object]:
    item: dict[str, object] = {
        "from_id": str(ids[edge.from_id]),
        "to_id": str(ids[edge.to_id]),
        "edge_kind": edge.edge_kind,
        "actor": _ACTOR,
    }
    if edge.valence is not None:
        item["valence"] = edge.valence
    return item


def _summary(report: Report, *, source: str, target: str) -> str:
    clis = Counter(a.cli for a in report.sessions)
    rooms = {room for a in report.sessions for room in a.rooms}
    timings = ", ".join(f"{name} {sec:.1f}s" for name, sec in report.seconds.items())
    lines = [
        f"{source} -> {target}",
        (
            f"sessions: {len(report.sessions)} ({dict(clis)}, {len(rooms)} rooms), "
            f"{report.opened} opened, {report.ended} ended"
        ),
        f"records: {report.written} written, {report.skipped} already there",
        (
            f"rows: {report.nodes}; edges: {report.edges} added, "
            f"{len(report.edge_failures)} refused"
        ),
        *(f"  refused {failure}" for failure in report.edge_failures),
        *(
            f"not copied: session {session} ended here before it was picked; "
            "its records need a fresh --to database"
            for session in report.unfilled
        ),
        f"time: {timings}",
    ]
    return "\n".join(lines)


class _Flags(Protocol):
    source: str
    to: str
    hours: float
    sessions: int
    records: int


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument(
        "--from",
        dest="source",
        default="",
        help="Server to read (GET only); default: the active trax profile's.",
    )
    parser.add_argument(
        "--to",
        default="http://127.0.0.1:8765",
        help="Local server to write.",
    )
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--sessions", type=int, default=12)
    parser.add_argument("--records", type=int, default=3000, help="Most per session.")


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
