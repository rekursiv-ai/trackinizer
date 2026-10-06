""":class:`_SessionFeedMixin` -- what the console feed holds, counted.

The feed itself (``_SessionMixin.read_feed``) interleaves every session's
records into one stream. This module counts it: the facets
(:meth:`_SessionFeedMixin.read_feed_facets`) count a window by session, room
and record kind, and the histogram
(:meth:`_SessionFeedMixin.read_feed_histogram`) counts records per time bucket
over a span. All three reads take one :class:`FeedScope`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

import itertools

from trackinizer.lib.custom_json import convert
from trackinizer.server.notify import tx
from trackinizer.server.store.shared import _StoreShared
from trackinizer.server.values import manifest_bound, vetted_sql
from trackinizer.wire.wire_sessions import (
    FeedActorFacet,
    FeedBucket,
    FeedFacetsResponse,
    FeedHistogramResponse,
    FeedKindFacet,
    FeedRoomFacet,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg

    from trackinizer.lib.postgres import Conn


__all__ = [
    "CONVERSATION",
    "UNREADABLE",
    "WHOLE_FEED",
    "BucketGrid",
    "FeedScope",
    "_SessionFeedMixin",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedScope:
    """Which records a feed read keeps: any value of a field, every named field.

    An empty field does not filter. ``actors`` names sessions by routing name
    (``owner``), ``rooms`` by a room they joined, ``clis`` by the CLI they wrap;
    ``kinds`` names record kinds.
    """

    actors: tuple[str, ...] = ()
    rooms: tuple[str, ...] = ()
    clis: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()

    @property
    def names_sessions(self) -> bool:
        """Whether the scope picks sessions, which takes their ``inquiries`` row."""
        return bool(self.actors or self.rooms or self.clis)

    def clauses(self, params: list[object], *, kind: str) -> list[str]:
        """Return the SQL that keeps this scope, binding its values onto ``params``.

        Args:
          params: The query's bound values so far; each named field appends one.
          kind: The record-kind column. ``i`` must name the session's
            ``inquiries`` row.

        Returns:
          clauses: One clause per named field, to AND together.

        """
        out: list[str] = []
        for values, test in (
            (self.actors, "i.owner = ANY"),
            (self.rooms, "i.agentsession_rooms &&"),
            (self.clis, "i.agentsession_cli = ANY"),
            (self.kinds, f"{kind} = ANY"),
        ):
            if values:
                params.append(list(values))
                out.append(f"{test}(${len(params)}::text[])")
        return out


WHOLE_FEED: Final = FeedScope()
"""The scope that keeps every record."""

# The formatting audit's "messages only": what a person or agent said, as the
# transcript shows it. The transcript hides a message with no text and no attachment,
# so that is not counted. Claude marks what its harness writes on a user's turn
# ``isMeta``, and delivers a message sent mid-turn as a ``queued_command`` attachment
# whose origin says whether a person or a finished task sent it. Codex writes
# context of its own on a user's turn (``<codex_internal_context>``), and Claude
# Code a background task's notice (``<task-notification>``): no person typed
# either (``codexContext`` and ``taskNotification`` in the web app). A codex
# agent's message to another starts with the envelope codex writes before it, and
# one codex sealed, keeping its payload only as ciphertext, has nothing after it
# (``peerPayload`` in the web app). ``payload`` is ``json``, which each ``->``
# parses again, so the kind tests come first and only messages and context state
# are parsed.
_ATTACHMENT_COUNT: Final = (
    "coalesce(json_array_length(CASE"
    " WHEN json_typeof(e.payload -> 'attachments') = 'array'"
    " THEN e.payload -> 'attachments' END), 0)"
)

CONVERSATION: Final = (
    "((e.kind IN ('UserMessage', 'AssistantMessage', 'AgentToAgentMessage')"
    " AND (e.payload ->> 'content' ~ '[^[:space:]]'"
    " AND (e.kind <> 'AgentToAgentMessage' OR e.payload ->> 'content' !~"
    " '^Message Type: [A-Z_]+\\nTask name: [^\\n]*\\nSender: [^\\n]*\\nPayload:\\s*$')"
    f" OR {_ATTACHMENT_COUNT} > 0)"
    " AND (e.kind <> 'UserMessage'"
    " OR (e.payload -> 'extra' ->> 'isMeta') IS DISTINCT FROM 'true'"
    " AND coalesce(e.payload ->> 'content', '') !~ '^\\s*<(codex_internal_context( [a-z_]+=\"[^\"]*\")*"
    "|task-notification)>.*</(codex_internal_context|task-notification)>\\s*$'))"
    " OR (e.kind = 'ContextState' AND e.payload ->> 'kind' = 'queued_command'"
    " AND e.payload -> 'extra' -> 'attachment' -> 'origin' ->> 'kind' = 'human'))"
)
"""SQL true of a record, aliased ``e``, that is conversation (the facets' count)."""

# Reasoning sealed with no summary keeps only ciphertext, which feed reads leave
# out, and a codex peer message codex sealed has nothing after its envelope: the
# web app draws neither (``unreadable`` there), so the facets count neither.
UNREADABLE: Final = (
    "((e.kind = 'Thinking' AND coalesce(e.payload ->> 'content', '') = ''"
    " AND coalesce(e.payload ->> 'summary', '') = '')"
    " OR (e.kind = 'AgentToAgentMessage' AND e.payload ->> 'content' ~"
    " '^Message Type: [A-Z_]+\\nTask name: [^\\n]*\\nSender: [^\\n]*\\nPayload:\\s*$'"
    f" AND {_ATTACHMENT_COUNT} = 0))"
)
"""SQL true of a record, aliased ``e``, that has nothing to read."""


@dataclass(frozen=True, slots=True, kw_only=True)
class BucketGrid:
    """``count`` epoch-aligned buckets of ``seconds`` each, the first at ``start``."""

    start: datetime
    seconds: int
    count: int

    @property
    def end(self) -> datetime:
        """Where the last bucket ends."""
        return self.start + timedelta(seconds=self.seconds * self.count)

    @classmethod
    def covering(
        cls,
        since: datetime,
        until: datetime,
        *,
        buckets: int,
    ) -> BucketGrid:
        """Return the finest grid of at most ``buckets`` holding ``since`` to ``until``.

        Widths come from a ladder of round ones -- 1, 2, 5, 10, 15 and 30
        seconds, the same in minutes, 1, 2, 3, 6 and 12 hours -- then whole
        days. A span already aligned to one of them, ``buckets`` long, gets it.

        Args:
          since: The span's first instant.
          until: Its last; at or after ``since``.
          buckets: The most buckets the grid may hold, at least 2.

        Returns:
          grid: The grid from the bucket holding ``since`` through the one
            holding ``until``.

        Raises:
          ValueError: ``buckets`` is under 2. One bucket holds a span only when no
            multiple of its width falls inside it, and the epoch is a multiple of
            every width, so no single bucket holds a span across it.

        """
        if buckets < 2:
            raise ValueError(f"buckets must be at least 2, not {buckets}")
        lo, hi = _epoch_us(since), _epoch_us(until)
        day = _DAY_SEC * _US
        # Fewer days than the first would need more than ``buckets`` buckets; a
        # width longer than the span holds it in two, so the last always fits.
        days = range(max(1, (hi - lo) // (buckets * day)), (hi - lo) // day + 2)
        width = _US * next(
            seconds
            for seconds in itertools.chain(_WIDTHS_SEC, (_DAY_SEC * d for d in days))
            if hi // (seconds * _US) - lo // (seconds * _US) < buckets
        )
        return cls(
            start=_EPOCH + timedelta(microseconds=lo // width * width),
            seconds=width // _US,
            count=hi // width - lo // width + 1,
        )


class _SessionFeedMixin(_StoreShared):
    """Counts of the cross-session feed behind the multi-agent console."""

    async def read_feed_facets(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        scope: FeedScope,
    ) -> FeedFacetsResponse:
        """Count a feed window by session, room and record kind.

        Counts the records the feed shows for the same window and scope, less
        those with nothing to read (``UNREADABLE``): each session's records, how
        many are conversation, its newest and whether it ended; each room's
        records and sessions; each kind's records.

        Args:
          since: Inclusive lower bound on ``created``; unset reads from the start.
          until: Inclusive upper bound on ``created``; unset reads to the end.
          scope: Which sessions and record kinds to count.

        Returns:
          facets: The window by session, newest first, and by room and kind,
            largest first.

        """
        params: list[object] = []
        clauses: list[str] = [f"NOT {UNREADABLE}"]
        if since is not None:
            params.append(since)
            clauses.append(f"e.created >= ${len(params)}")
        if until is not None:
            params.append(until)
            clauses.append(f"e.created <= ${len(params)}")
        clauses.extend(FeedScope(kinds=scope.kinds).clauses(params, kind="e.kind"))
        if scope.names_sessions:
            picked = replace(scope, kinds=()).clauses(params, kind="")
            clauses.append(
                vetted_sql(
                    "e.session_id = ANY(ARRAY(SELECT i.id FROM inquiries i WHERE ",
                    " AND ".join(picked),
                    "))",
                ),
            )
        manifest_join, manifest_predicate = manifest_bound("w")
        # The window is MATERIALIZED so it is read first. Inlined, the planner walked
        # every part's manifest and probed its records by primary key, reading the
        # whole table: 10.8 s for a week of 9 million local records.
        sql = vetted_sql(
            "WITH windowed AS MATERIALIZED (SELECT e.session_id, e.part, e.idx, "
            "e.kind, e.created, ",
            CONVERSATION,
            " AS said FROM session_records e WHERE ",
            " AND ".join(clauses) or "TRUE",
            ") SELECT w.session_id, w.kind, count(*) AS records, "
            "count(*) FILTER (WHERE w.said) AS conversation, max(w.created) AS last "
            "FROM windowed w ",
            manifest_join,
            "WHERE ",
            manifest_predicate,
            " GROUP BY GROUPING SETS ((w.session_id), (w.kind))",
        )
        async with self.engine.acquire() as conn, tx(conn):
            # One snapshot for both reads: a session purged between them would
            # leave counts with no session to name.
            await conn.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
            )
            rows = await conn.fetch(sql, *params)
            # Read apart, by id: joined in, the planner guessed thousands of
            # sessions from the grouping and read every inquiry for a few dozen.
            sessions = {
                row["id"]: row
                for row in await conn.fetch(
                    "SELECT id, owner, agentsession_cli, agentsession_rooms, "
                    "agentsession_ended FROM inquiries WHERE id = ANY($1::uuid[])",
                    [
                        row["session_id"]
                        for row in rows
                        if row["session_id"] is not None
                    ],
                )
            }
        actors = sorted(
            (
                _actor_facet(row, sessions[row["session_id"]])
                for row in rows
                if row["session_id"] is not None
            ),
            key=lambda actor: (actor.last, actor.session_id),
            reverse=True,
        )
        kinds = sorted(
            (
                FeedKindFacet(
                    kind=convert(row["kind"], str),
                    count=convert(row["records"], int),
                )
                for row in rows
                if row["session_id"] is None
            ),
            key=lambda kind: (-kind.count, kind.kind),
        )
        return FeedFacetsResponse(
            actors=actors,
            rooms=_room_facets(actors),
            kinds=kinds,
        )

    async def read_feed_histogram(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        earliest: datetime,
        buckets: int,
        scope: FeedScope,
    ) -> FeedHistogramResponse:
        """Count the feed's records per time bucket, every bucket listed.

        The grid is :meth:`BucketGrid.covering` the span. Every bucket counts the
        records themselves, so the work grows with the records in the span, and
        ``earliest`` bounds it: nothing before it is counted, so a bucket that
        starts earlier counts only its records from ``earliest`` on. The count is
        of every stored record: unlike the feed, it includes the tail a compaction
        leaves past its part's manifest bound, at the times it was written.

        Args:
          since: The span's start; unset starts at the first record from
            ``earliest`` on.
          until: Its end; unset ends now.
          earliest: The earliest the span may start; an earlier ``since`` starts
            here instead.
          buckets: The most buckets to return, at least 2.
          scope: Which sessions and record kinds to count.

        Returns:
          histogram: Each bucket's start and record count, oldest first.

        """
        end = until.astimezone(UTC) if until is not None else datetime.now(UTC)
        params: list[object] = [earliest, end]
        clauses = ["r.created >= $1", "r.created <= $2"]
        clauses.extend(scope.clauses(params, kind="r.kind"))
        joined = (
            "JOIN inquiries i ON i.id = r.session_id " if scope.names_sessions else ""
        )
        first_sql = vetted_sql(
            "SELECT min(r.created) FROM session_records r ",
            joined,
            "WHERE ",
            " AND ".join(clauses),
        )
        async with self.engine.acquire() as conn:
            first = (
                since
                if since is not None
                else (
                    end
                    if (value := await conn.fetchval(first_sql, *params)) is None
                    else convert(value, datetime)
                )
            )
            earliest = earliest.astimezone(UTC)
            start = min(max(first.astimezone(UTC), earliest), end)
            grid = BucketGrid.covering(start, end, buckets=buckets)
            counts = await _count_buckets(conn, grid, scope, earliest=earliest)
        width = timedelta(seconds=grid.seconds)
        return FeedHistogramResponse(
            start=grid.start,
            end=grid.end,
            bucket_seconds=grid.seconds,
            counts=[
                FeedBucket(start=bucket, count=counts.get(bucket, 0))
                for bucket in (grid.start + i * width for i in range(grid.count))
            ],
        )


_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_US: Final = 1_000_000
_DAY_SEC: Final = 86_400
_WIDTHS_SEC: Final = (
    *(1, 2, 5, 10, 15, 30),
    *(60 * minutes for minutes in (1, 2, 5, 10, 15, 30)),
    *(3_600 * hours for hours in (1, 2, 3, 6, 12)),
)


def _epoch_us(at: datetime) -> int:
    """Microseconds from the epoch to ``at``; naive is local time, as asyncpg reads it."""
    return (at.astimezone(UTC) - _EPOCH) // timedelta(microseconds=1)


async def _count_buckets(
    conn: Conn,
    grid: BucketGrid,
    scope: FeedScope,
    *,
    earliest: datetime,
) -> dict[object, int]:
    """Count ``scope``'s records from ``earliest`` in each bucket of ``grid``, by start."""
    params: list[object] = [
        timedelta(seconds=grid.seconds),
        max(grid.start, earliest),
        grid.end,
    ]
    spanned = ["created >= $2", "created < $3"]
    if scope.names_sessions:
        # Records attach only to AgentSessions (``append_session_records`` refuses
        # any other kind), so the session row is read only to pick sessions.
        picked = replace(scope, kinds=()).clauses(params, kind="")
        spanned.append(
            vetted_sql(
                "session_id IN (SELECT i.id FROM inquiries i WHERE ",
                " AND ".join(picked),
                ")",
            ),
        )
    counted = vetted_sql("session_records WHERE ", " AND ".join(spanned))
    if scope.kinds:
        (kinds,) = FeedScope(kinds=scope.kinds).clauses(params, kind="kind")
        # No index holds the kind beside ``created``, so testing it on the span's
        # records read every one from the table: 0.23 s for a week of a benchmark of
        # 9 million. The span's sessions come from the ``created`` index instead,
        # and each one's records of these kinds from the ``(session_id, kind)``
        # one: 0.05 s. ``OFFSET 0`` keeps the planner from flattening that into a
        # join, which brought the table scan back.
        counted = vetted_sql(
            "(SELECT DISTINCT session_id FROM ",
            counted,
            ") s CROSS JOIN LATERAL (SELECT created FROM session_records "
            "WHERE session_id = s.session_id AND ",
            kinds,
            " AND created >= $2 AND created < $3 OFFSET 0) r",
        )
    sql = vetted_sql(
        "SELECT date_bin($1::interval, created, 'epoch') AS bucket, "
        "count(*) AS records FROM ",
        counted,
        " GROUP BY 1",
    )
    rows = await conn.fetch(sql, *params)
    return {row["bucket"]: convert(row["records"], int) for row in rows}


def _actor_facet(counted: asyncpg.Record, session: asyncpg.Record) -> FeedActorFacet:
    """Build one session's facet from its counts and its ``inquiries`` row."""
    session_id, last = counted["session_id"], counted["last"]
    assert isinstance(session_id, UUID)
    assert isinstance(last, datetime)
    return FeedActorFacet(
        actor=convert(session.get("owner"), str, default=""),
        session_id=session_id,
        cli=convert(session.get("agentsession_cli"), str, default="") or None,
        rooms=convert(session.get("agentsession_rooms"), list[str], default=[]),
        count=convert(counted["records"], int),
        conversation=convert(counted["conversation"], int),
        last=last,
        ended=(
            None
            if session["agentsession_ended"] is None
            else convert(session["agentsession_ended"], datetime)
        ),
    )


def _room_facets(actors: Sequence[FeedActorFacet]) -> list[FeedRoomFacet]:
    """Total each room's sessions, largest first."""
    counts: dict[str, int] = {}
    names: dict[str, set[str]] = {}
    for actor in actors:
        for room in actor.rooms:
            counts[room] = counts.get(room, 0) + actor.count
            names.setdefault(room, set()).add(actor.actor)
    return [
        FeedRoomFacet(room=room, count=counts[room], actors=sorted(names[room]))
        for room in sorted(counts, key=lambda room: (-counts[room], room))
    ]
