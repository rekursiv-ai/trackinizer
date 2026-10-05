"""The console feed's scope, its facets and its histogram.

The bucket grid is arithmetic and is tested alone. Everything else runs against
real Postgres, since the properties are the database's: which rows the scope
keeps, which records count as conversation, and which a histogram counts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import itertools

from asyncpg.pool import PoolConnectionProxy

import pytest

from trackinizer.lib.agent.types.sessions import UncategorizedRecord
from trackinizer.lib.custom_json import json_freeze
from trackinizer.server.store.session_feed import (
    WHOLE_FEED,
    BucketGrid,
    FeedScope,
)
from trackinizer.wire.wire_sessions import (
    FeedActorFacet,
    FeedBucket,
    FeedKindFacet,
    FeedRoomFacet,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.server.store.core import Store


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# A UTC midnight, so a day's bucket starts on it.
_T0 = datetime(2026, 9, 1, tzinfo=UTC)
_HUMAN = '{"content": "Go ahead.", "extra": {}}'
_HARNESS = '{"content": "Stop hook feedback", "extra": {"isMeta": true}}'
_SAID = '{"content": "Done.", "attachments": {"py/tuple": []}, "extra": {}}'
_BLANK = '{"content": " \\n", "attachments": {"py/tuple": []}, "extra": {}}'
_ATTACHED = (
    '{"content": null, "attachments": {"py/tuple": [{"mime_descriptor": "image/png"}]},'
    ' "extra": {}}'
)
_QUEUED = (
    '{"kind": "queued_command", "content": null,'
    ' "extra": {"attachment": {"origin": {"kind": "%s"}}}}'
)
# A codex agent's message to another, in the envelope codex writes before it.
_PEER = (
    '{"content": "Message Type: %s\\nTask name: /root\\nSender: /root/prototype'
    '\\nPayload:\\n%s", "attachments": {"py/tuple": []}, "extra": {}}'
)
# What a harness writes on the user's turn, no person: codex's context, and the
# notice Claude Code gives when a background task ends.
_CODEX_CONTEXT = (
    '{"content": "<codex_internal_context source=\\"goal\\">\\nKeep going.\\n'
    '</codex_internal_context>", "attachments": {"py/tuple": []}, "extra": {}}'
)
_TASK_NOTICE = (
    '{"content": "<task-notification>\\n<status>completed</status>\\n'
    '<summary>Done</summary>\\n</task-notification>", "extra": {}}'
)


# ---- The bucket grid --------------------------------------------------------


def test_the_grid_is_the_finest_that_fits_on_multiples_of_its_width() -> None:
    grid = BucketGrid.covering(
        _T0 + timedelta(minutes=7),
        _T0 + timedelta(hours=1, minutes=7),
        buckets=120,
    )
    # 30 s would need 121 buckets: one more holds the end, since the span is an
    # hour long and starts on a bucket's start.
    assert grid == BucketGrid(start=_T0 + timedelta(minutes=7), seconds=60, count=61)


@pytest.mark.parametrize(
    "seconds",
    [
        *(1, 2, 5, 10, 15, 30),
        *(60 * minutes for minutes in (1, 2, 5, 10, 15, 30)),
        *(3_600 * hours for hours in (1, 2, 3, 6, 12)),
        *(86_400 * days for days in (1, 2, 3, 7, 30)),
    ],
)
def test_a_span_aligned_to_a_width_gets_that_width(seconds: int) -> None:
    # The minimap asks this way: a span starting and ending on its bucket size,
    # as many buckets as that makes, and it draws the size it asked for. (With
    # only a few buckets a finer width can fit the same span, and is returned.)
    width = timedelta(seconds=seconds)
    since = _EPOCH + (_T0 - _EPOCH) // width * width
    for buckets in (120, 1_000):
        grid = BucketGrid.covering(
            since,
            since + (buckets - 1) * width,
            buckets=buckets,
        )
        assert grid == BucketGrid(start=since, seconds=seconds, count=buckets)


def test_a_span_of_months_gets_whole_days() -> None:
    grid = BucketGrid.covering(
        _T0 - timedelta(days=121, hours=5),
        _T0 + timedelta(hours=7),
        buckets=120,
    )
    # 2026-09-01 is an odd day since the epoch, so its 2-day bucket began the day
    # before; the first bucket likewise starts a day before ``since``'s day.
    assert (grid.seconds, grid.count) == (2 * 86_400, 62)
    assert grid.start == _T0 - timedelta(days=123)


def test_an_instant_is_one_bucket() -> None:
    at = _T0 + timedelta(microseconds=1)
    assert BucketGrid.covering(at, at, buckets=2) == BucketGrid(
        start=_T0,
        seconds=1,
        count=1,
    )


def test_a_span_across_the_epoch_gets_a_grid() -> None:
    """The epoch is a bucket boundary at every width, so one bucket never holds it.

    Two always do, once a bucket is longer than the span; the width search must
    stop there, and refuse to look for one.
    """
    since, until = _EPOCH - timedelta(days=1), _EPOCH + timedelta(days=1)
    assert BucketGrid.covering(since, until, buckets=2) == BucketGrid(
        start=_EPOCH - timedelta(days=2),
        seconds=2 * 86_400,
        count=2,
    )
    with pytest.raises(ValueError, match="at least 2"):
        _ = BucketGrid.covering(since, until, buckets=1)


def test_every_grid_holds_its_span_in_at_most_the_buckets_asked() -> None:
    # Starts three years either side of 2026 on no round boundary, and spans from
    # a millisecond to ten years.
    for offset_sec, exponent, buckets in itertools.product(
        range(-(10**8), 10**8, 9_876_543),
        [quarter / 4 for quarter in range(-12, 35)],
        (2, 3, 7, 120, 1_000),
    ):
        since = _T0 + timedelta(seconds=offset_sec + 0.123_457)
        until = since + timedelta(seconds=10**exponent)
        grid = BucketGrid.covering(since, until, buckets=buckets)
        end = grid.start + timedelta(seconds=grid.seconds * grid.count)
        assert 1 <= grid.count <= buckets
        assert grid.start <= since <= until < end
        assert (grid.start - _EPOCH) % timedelta(seconds=grid.seconds) == timedelta(0)


# ---- The scope --------------------------------------------------------------


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_scope_keeps_any_value_of_a_field_and_every_named_field(
    integ_store: Store,
) -> None:
    for owner, cli, rooms in (
        ("a", "claude", ("lab",)),
        ("b", "codex", ("ops",)),
        ("c", "codex", ("lab", "ops")),
    ):
        session = await _session(integ_store, owner, cli=cli, rooms=rooms)
        await _records(
            integ_store,
            session,
            [("UserMessage", _T0, _HUMAN), ("ToolCall", _T0, "{}")],
        )

    async def kept(**scope: tuple[str, ...]) -> set[tuple[str, str]]:
        events = await integ_store.read_feed(scope=FeedScope(**scope))
        return {(event.actor, event.kind) for event in events}

    both = {"UserMessage", "ToolCall"}
    assert await kept(actors=("a", "b")) == {(a, k) for a in "ab" for k in both}
    assert await kept(rooms=("lab",)) == {(a, k) for a in "ac" for k in both}
    assert await kept(rooms=("lab", "ops")) == {(a, k) for a in "abc" for k in both}
    assert await kept(clis=("codex",)) == {(a, k) for a in "bc" for k in both}
    assert await kept(actors=("a", "c"), clis=("codex",)) == {("c", k) for k in both}
    assert await kept(kinds=("ToolCall",)) == {(a, "ToolCall") for a in "abc"}
    assert await kept(actors=("b",), kinds=("ToolCall", "UserMessage")) == {
        ("b", k) for k in both
    }
    assert await kept() == {(a, k) for a in "abc" for k in both}


# ---- The facets -------------------------------------------------------------


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_facets_count_a_window_by_session_room_and_kind(
    integ_store: Store,
) -> None:
    """Conversation is what a person or agent said, as the formatting audit drew it.

    A claude ``isMeta`` user turn is the harness's, a message with no text and no
    attachment shows nothing, and a queued command is a person's only when its
    origin says human. A record past its part's manifest bound is inert, so the
    feed never shows it and the facets do not count it.
    """
    talker = await _session(integ_store, "talker", cli="claude", rooms=("lab",))
    ended = _T0 + timedelta(hours=3)
    worker = await _session(
        integ_store,
        "worker",
        cli="codex",
        rooms=("lab", "ops"),
        ended=ended,
    )
    minute = timedelta(minutes=1)
    await _records(
        integ_store,
        talker,
        [
            ("UserMessage", _T0 + 1 * minute, _HUMAN),
            ("UserMessage", _T0 + 2 * minute, _HARNESS),
            ("AssistantMessage", _T0 + 3 * minute, _SAID),
            ("AgentToAgentMessage", _T0 + 4 * minute, _BLANK),
            ("AgentToAgentMessage", _T0 + 5 * minute, _ATTACHED),
            ("ContextState", _T0 + 6 * minute, _QUEUED % "human"),
            ("ContextState", _T0 + 7 * minute, _QUEUED % "task-notification"),
            ("ContextState", _T0 + 8 * minute, '{"kind": "date", "extra": {}}'),
            ("ToolCall", _T0 + 9 * minute, "{}"),
            ("AssistantMessage", _T0 + 10 * minute, _SAID),
        ],
        live=9,
    )
    await _records(
        integ_store,
        worker,
        [
            ("ToolCall", _T0 - 60 * minute, "{}"),
            ("AssistantMessage", _T0 + 11 * minute, _SAID),
            ("ToolCall", _T0 + 12 * minute, "{}"),
        ],
    )
    hour = timedelta(hours=1)

    facets = await integ_store.read_feed_facets(
        since=_T0,
        until=_T0 + hour,
        scope=WHOLE_FEED,
    )

    assert facets.actors == [
        FeedActorFacet(
            actor="worker",
            session_id=worker,
            cli="codex",
            rooms=["lab", "ops"],
            count=2,
            conversation=1,
            last=_T0 + 12 * minute,
            ended=ended,
        ),
        FeedActorFacet(
            actor="talker",
            session_id=talker,
            cli="claude",
            rooms=["lab"],
            count=9,
            conversation=4,
            last=_T0 + 9 * minute,
            ended=None,
        ),
    ]
    assert facets.rooms == [
        FeedRoomFacet(room="lab", count=11, actors=["talker", "worker"]),
        FeedRoomFacet(room="ops", count=2, actors=["worker"]),
    ]
    assert facets.kinds == [
        FeedKindFacet(kind="ContextState", count=3),
        FeedKindFacet(kind="AgentToAgentMessage", count=2),
        FeedKindFacet(kind="AssistantMessage", count=2),
        FeedKindFacet(kind="ToolCall", count=2),
        FeedKindFacet(kind="UserMessage", count=2),
    ]

    everything = await integ_store.read_feed_facets(
        since=None,
        until=None,
        scope=WHOLE_FEED,
    )
    assert [(a.actor, a.count) for a in everything.actors] == [
        ("worker", 3),
        ("talker", 9),
    ]

    said = await integ_store.read_feed_facets(
        since=_T0,
        until=_T0 + hour,
        scope=FeedScope(rooms=("ops",), kinds=("AssistantMessage",)),
    )
    assert [(a.actor, a.count, a.conversation) for a in said.actors] == [
        ("worker", 1, 1),
    ]
    assert said.rooms == [
        FeedRoomFacet(room="lab", count=1, actors=["worker"]),
        FeedRoomFacet(room="ops", count=1, actors=["worker"]),
    ]
    assert said.kinds == [FeedKindFacet(kind="AssistantMessage", count=1)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_facets_hold_one_snapshot_while_a_session_is_purged(
    integ_store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The facets count records, then read their sessions; a purge between holds off.

    Another connection purges the counted session just before the facets read its
    row. Both reads see the database as the first found it.
    """
    session = await _session(integ_store, "gone")
    await _records(integ_store, session, [("ToolCall", _T0, "{}")])
    fetch = PoolConnectionProxy.fetch

    async def purge_first(
        proxy: PoolConnectionProxy,
        query: str,
        *args: object,
    ) -> object:
        if query.startswith("SELECT id, owner"):
            await _purge(integ_store, session)
        return await fetch(proxy, query, *args)

    monkeypatch.setattr(PoolConnectionProxy, "fetch", purge_first)
    facets = await integ_store.read_feed_facets(
        since=None,
        until=None,
        scope=WHOLE_FEED,
    )

    assert [(actor.actor, actor.count) for actor in facets.actors] == [("gone", 1)]


# ---- The conversation feed ----------------------------------------------------


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_conversation_feed_keeps_what_the_facets_count(
    integ_store: Store,
) -> None:
    """``conversation=True`` keeps exactly the records the facets count as said.

    It narrows the other filters' records, so another session's message stays out.
    A codex peer message codex sealed, its envelope with nothing after it, says
    nothing; nor does what a harness wrote on the user's turn.
    """
    session = await _session(integ_store, "talker")
    other = await _session(integ_store, "other")
    minute = timedelta(minutes=1)
    turns = (
        ("UserMessage", _HUMAN),
        ("UserMessage", _HARNESS),
        ("AssistantMessage", _SAID),
        ("AgentToAgentMessage", _BLANK),
        ("AgentToAgentMessage", _ATTACHED),
        ("ContextState", _QUEUED % "human"),
        ("ContextState", _QUEUED % "task-notification"),
        ("ToolCall", "{}"),
        ("AgentToAgentMessage", _PEER % ("MESSAGE", "")),
        ("AgentToAgentMessage", _PEER % ("FINAL_ANSWER", "Implemented.")),
        ("UserMessage", _CODEX_CONTEXT),
        ("UserMessage", _TASK_NOTICE),
    )
    await _records(
        integ_store,
        session,
        [(kind, _T0 + i * minute, payload) for i, (kind, payload) in enumerate(turns)],
    )
    await _records(integ_store, other, [("ContextState", _T0, _QUEUED % "human")])
    talker = FeedScope(actors=("talker",))

    said = await integ_store.read_feed(scope=talker, conversation=True)
    newest = await integ_store.read_feed(
        scope=talker,
        conversation=True,
        tail=True,
        limit=2,
    )
    facets = await integ_store.read_feed_facets(since=None, until=None, scope=talker)

    assert [(event.seq, event.kind) for event in said] == [
        (0, "UserMessage"),
        (2, "AssistantMessage"),
        (4, "AgentToAgentMessage"),
        (5, "ContextState"),
        (9, "AgentToAgentMessage"),
    ]
    assert [event.seq for event in newest] == [5, 9]
    assert [actor.conversation for actor in facets.actors] == [len(said)]
    assert len(await integ_store.read_feed(scope=talker)) == len(turns)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_facets_count_no_record_with_nothing_to_read(
    integ_store: Store,
) -> None:
    """Reasoning sealed with no summary, and a sealed codex peer message, count nowhere.

    The web app draws neither (``unreadable``); reasoning with a summary counts.
    """
    session = await _session(integ_store, "thinker")
    minute = timedelta(minutes=1)
    turns = (
        ("Thinking", '{"content": null, "encrypted": "", "summary": null}'),
        ("Thinking", '{"content": null, "encrypted": "", "summary": "Planned."}'),
        ("AgentToAgentMessage", _PEER % ("MESSAGE", "")),
        ("AgentToAgentMessage", _PEER % ("FINAL_ANSWER", "Done.")),
    )
    await _records(
        integ_store,
        session,
        [(kind, _T0 + i * minute, payload) for i, (kind, payload) in enumerate(turns)],
    )

    facets = await integ_store.read_feed_facets(
        since=None,
        until=None,
        scope=FeedScope(actors=("thinker",)),
    )

    assert [(actor.count, actor.conversation) for actor in facets.actors] == [(2, 1)]
    assert facets.kinds == [
        FeedKindFacet(kind="AgentToAgentMessage", count=1),
        FeedKindFacet(kind="Thinking", count=1),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_every_feed_read_shows_only_each_part_s_live_prefix(
    integ_store: Store,
) -> None:
    """A record past its part's manifest count, or in a part with none, is inert."""
    session = await _session(integ_store, "talker")
    minute = timedelta(minutes=1)
    await _records(
        integ_store,
        session,
        [("AssistantMessage", _T0 + i * minute, _SAID) for i in range(3)],
        live=2,
    )
    async with integ_store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_records (session_id, part, idx, kind, created, "
            "payload) VALUES ($1, 1, 0, 'AssistantMessage', $2, $3::json)",
            session,
            _T0 + 3 * minute,
            _SAID,
        )

    reads = (
        await integ_store.read_feed(),
        await integ_store.read_feed(tail=True),
        await integ_store.read_feed(conversation=True),
        await integ_store.read_feed(
            scope=FeedScope(kinds=("AssistantMessage",)),
            tail=True,
        ),
    )
    for events in reads:
        assert [(event.part, event.seq) for event in events] == [(0, 0), (0, 1)]


# Archived before tuples became plain arrays: the tag sits in an untyped payload,
# where nothing fails on it.
_OLD_UNCATEGORIZED = (
    '{"kind": "attachment", "payload": {"names": {"py/tuple": ["Bash"]}}}'
)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_row_stored_in_the_old_format_reads_back_untagged(
    integ_store: Store,
) -> None:
    """A resume and the feed both see an archived row in the current shape."""
    session = await _session(integ_store, "archivist")
    await _records(
        integ_store,
        session,
        [("UncategorizedRecord", _T0, _OLD_UNCATEGORIZED)],
    )

    (row,) = await integ_store.read_session_records(session, part=0)
    events = [
        event for event in await integ_store.read_feed() if event.session_id == session
    ]

    assert row.record() == UncategorizedRecord(
        kind="attachment",
        payload=json_freeze({"names": ["Bash"]}),
    )
    assert [
        event.model_dump(mode="json")["message"]["payload"] for event in events
    ] == [
        {"names": ["Bash"]},
    ]


# ---- The histogram ----------------------------------------------------------


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_histogram_counts_records_in_each_bucket(integ_store: Store) -> None:
    hour, day = timedelta(hours=1), timedelta(days=1)
    first = await _session(integ_store, "first")
    second = await _session(integ_store, "second", cli="codex")
    await _records(
        integ_store,
        first,
        [
            ("ToolCall", _T0 + 10 * hour + timedelta(minutes=5), "{}"),
            ("UserMessage", _T0 + 10 * hour + timedelta(minutes=40), _HUMAN),
            ("ToolCall", _T0 + day + 5 * hour, "{}"),
            ("ToolCall", _T0 + 3 * day - timedelta(seconds=1), "{}"),
        ],
    )
    await _records(
        integ_store,
        second,
        [("ToolCall", _T0 + 11 * hour + timedelta(minutes=50), "{}")],
    )

    days = await integ_store.read_feed_histogram(
        since=_T0,
        until=_T0 + 3 * day - timedelta(seconds=1),
        earliest=_T0,
        buckets=3,
        scope=WHOLE_FEED,
    )
    assert (days.start, days.end, days.bucket_seconds) == (_T0, _T0 + 3 * day, 86_400)
    assert days.counts == [
        FeedBucket(start=_T0, count=3),
        FeedBucket(start=_T0 + day, count=1),
        FeedBucket(start=_T0 + 2 * day, count=1),
    ]
    whole = (_T0, _T0 + 3 * day - timedelta(seconds=1))
    # Two hours in half hours: a session's records outside them are not counted,
    # whether the scope names sessions, record kinds or both.
    morning = (_T0 + 10 * hour, _T0 + 12 * hour - timedelta(seconds=1))
    assert await _counts(integ_store, *morning, buckets=4) == [1, 1, 0, 1]
    for scope, by_day, by_half_hour in (
        (FeedScope(clis=("codex",)), [1, 0, 0], [0, 0, 0, 1]),
        (FeedScope(kinds=("ToolCall",)), [2, 1, 1], [1, 0, 0, 1]),
        (FeedScope(actors=("first",), kinds=("ToolCall",)), [1, 1, 1], [1, 0, 0, 0]),
    ):
        assert await _counts(integ_store, *whole, buckets=3, scope=scope) == by_day, (
            scope
        )
        assert (
            await _counts(integ_store, *morning, buckets=4, scope=scope) == by_half_hour
        ), scope


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_histogram_without_since_starts_at_the_first_record(
    integ_store: Store,
) -> None:
    hour = timedelta(hours=1)
    session = await _session(integ_store, "a")
    await _records(
        integ_store,
        session,
        [
            ("ToolCall", _T0 + 5.5 * hour, "{}"),
            ("ToolCall", _T0 + 6.2 * hour, "{}"),
        ],
    )

    histogram = await integ_store.read_feed_histogram(
        since=None,
        until=_T0 + 7 * hour,
        earliest=_T0,
        buckets=2,
        scope=WHOLE_FEED,
    )

    assert histogram.counts == [
        FeedBucket(start=_T0 + 4 * hour, count=1),
        FeedBucket(start=_T0 + 6 * hour, count=1),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_histogram_starts_no_earlier_than_earliest(integ_store: Store) -> None:
    """``earliest`` bounds how much history a read counts, whatever ``since`` says.

    A bucket that starts before it counts only the records from it on.
    """
    hour, minute = timedelta(hours=1), timedelta(minutes=1)
    session = await _session(integ_store, "a")
    await _records(
        integ_store,
        session,
        [
            ("ToolCall", _T0 + offset, "{}")
            for offset in (-3 * hour, 10 * minute, 40 * minute)
        ],
    )

    # A month back starts at 00:20, in the hour from midnight; unset starts at the
    # first record from 00:20 on, in the half hour from 00:30.
    for since, first in ((_T0 - timedelta(days=30), _T0), (None, _T0 + 30 * minute)):
        histogram = await integ_store.read_feed_histogram(
            since=since,
            until=_T0 + hour,
            earliest=_T0 + 20 * minute,
            buckets=2,
            scope=WHOLE_FEED,
        )
        assert histogram.counts == [
            FeedBucket(start=first, count=1),
            FeedBucket(start=_T0 + hour, count=0),
        ], since


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_empty_feed_is_one_empty_bucket(integ_store: Store) -> None:
    histogram = await integ_store.read_feed_histogram(
        since=None,
        until=_T0,
        earliest=_T0 - timedelta(days=7),
        buckets=120,
        scope=WHOLE_FEED,
    )
    assert histogram.counts == [FeedBucket(start=_T0, count=0)]


# ---- Helpers ----------------------------------------------------------------


async def _session(
    store: Store,
    owner: str,
    *,
    cli: str = "claude",
    rooms: Sequence[str] = (),
    ended: datetime | None = None,
) -> UUID:
    """Insert an AgentSession row as capture leaves it."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
            "agentsession_cli, agentsession_rooms, agentsession_ended) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), $2, "
            "'tester@example.com', $3, $3, $4, $5, $6)",
            session_id,
            "complete" if ended else "active",
            owner,
            cli,
            list(rooms) or None,
            ended,
        )
    return session_id


async def _records(
    store: Store,
    session_id: UUID,
    records: Sequence[tuple[str, datetime, str]],
    *,
    live: int | None = None,
) -> None:
    """Store ``(kind, created, payload)`` records at idx 0.., the first ``live`` live."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_records (session_id, part, idx, kind, created, "
            "payload) SELECT $1, 0, t.idx - 1, t.kind, t.created, t.payload::json "
            "FROM unnest($2::text[], $3::timestamptz[], $4::text[]) "
            "WITH ORDINALITY AS t(kind, created, payload, idx)",
            session_id,
            [kind for kind, _, _ in records],
            [created for _, created, _ in records],
            [payload for _, _, payload in records],
        )
        await conn.execute(
            "INSERT INTO session_manifests (session_id, part, name, metadata, "
            "ir_id, format, records) VALUES ($1, 0, 's.jsonl', '{}'::json, $2, "
            "'claude', $3)",
            session_id,
            uuid4(),
            len(records) if live is None else live,
        )


async def _purge(store: Store, session_id: UUID) -> None:
    """Delete a session from a connection of its own; its records go with it."""
    async with store.engine.acquire() as conn:
        await conn.execute("DELETE FROM inquiries WHERE id = $1", session_id)


async def _counts(
    store: Store,
    since: datetime,
    until: datetime,
    *,
    buckets: int,
    scope: FeedScope = WHOLE_FEED,
) -> list[int]:
    """Read a histogram that may start at ``_T0``, as its bucket counts."""
    histogram = await store.read_feed_histogram(
        since=since,
        until=until,
        earliest=_T0,
        buckets=buckets,
        scope=scope,
    )
    return [bucket.count for bucket in histogram.counts]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
