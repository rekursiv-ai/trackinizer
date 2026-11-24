"""The sweep: keeps ``embedding.vectors`` current with the text of its sources.

Two kinds of source, each addressed as ``(owner_id, part, idx, field)``:

- a session record, ``(session, part, idx, 'content')``, when its kind is
  configured, its text is not empty, and it lies inside its part's live prefix;
- an inquiry's text, ``(inquiry, 0, 0, 'text')``: its configured fields joined, and
  each edge's note, ``(inquiry, 0, 0, 'edge:<edge_kind>:<to_id>')``, owned by the
  inquiry the edge starts from. A session owns its edges' notes, and its text only
  if ``AgentSession`` is configured, which it is not by default.

A source's rows are current when they carry its text's md5 and the configured
settings key; anything else is pending:

- a source with no current row is embedded, its old rows replaced;
- a source whose text is empty or gone, but has rows, loses them.

A round (:func:`sweep_once`) reads in this order, each page at most
``slice_units`` sources:

1. the records, then the inquiries, changed since the last round's snapshot and
   visible in this one's, newest change first, until none is pending; an edge
   write that touches a note changes the inquiry it starts from (``schema.080``);
2. one page of the backlog: every record, then every inquiry, newest first, from
   where the last page stopped; on a server's start this walk is how the table
   catches up;
3. once a walk has reached the oldest inquiry, rows of any other settings key,
   10,000 a round, then the other keys' indexes.

No connection is held while the model computes: each read and each write takes its
own, so the server's other work never waits on a forward pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import asyncio
import logging

from trackinizer.addons.embedding.model import Embedder
from trackinizer.addons.embedding.search import drop_other_indexes, ensure_index
from trackinizer.addons.embedding.units import Settings, text_units
from trackinizer.server.notify import tx
from trackinizer.server.values import manifest_bound, vec_to_text, vetted_sql


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Record

    from trackinizer.addons.addon import ServerContext
    from trackinizer.addons.embedding.units import IndexUnit
    from trackinizer.lib.postgres import DatabaseEngine


__all__ = ["Sweep", "SweepState", "sweep_once"]


_LOGGER: Final = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class SweepState:
    """Where the sweep stands between rounds; held in memory only.

    Attributes:
      since: The snapshot the last round read through; ``None`` before the first.
      after: The backlog's last source: a record's ``(created, session_id, part,
        idx)``, or an inquiry source's ``(created, inquiry_id, field)``; ``None``
        before the first page of each.
      owners: The backlog has passed the records and walks the inquiries.
      walked: The backlog has been walked to its oldest source.
      cleaned: Rows and indexes of other settings keys have been removed after
        the walk.
      worked: The last round embedded or deleted something.
      written: The units the last round wrote.

    """

    since: str | None = None
    after: tuple[object, ...] | None = None
    owners: bool = False
    walked: bool = False
    cleaned: bool = False
    worked: bool = False
    written: int = 0


# Not frozen: a server service's name and description are writable attributes.
@dataclass(slots=True, kw_only=True)
class Sweep:
    """Keeps every vector current with its text: the add-on's one server service."""

    model: Embedder
    settings: Settings
    slice_units: int
    interval_sec: float
    loaded: asyncio.Event
    name: str = "sweep"
    description: str = "Embeds new and changed text, and deletes stale vectors."

    async def run(self, context: ServerContext) -> None:
        """Load the model, make the key's index, then sweep until cancelled.

        Args:
          context: The server's store, whose engine the sweep reads and writes.

        """
        _LOGGER.info("embedding settings %s %r", self.settings.key, self.settings)
        await self.model.embed_query("warm")
        async with context.store.engine.acquire() as conn:
            await ensure_index(conn, self.settings)
        self.loaded.set()
        state = SweepState()
        while True:
            state = await sweep_once(
                context.store.engine,
                model=self.model,
                settings=self.settings,
                state=state,
                slice_units=self.slice_units,
            )
            if not state.worked:
                await asyncio.sleep(
                    self.interval_sec,
                )  # house-ignore[sleep] -- The wait between idle rounds.


# Audience: internal -- a background job; it returns where it stands, nothing it read.
async def sweep_once(
    engine: DatabaseEngine,
    *,
    model: Embedder,
    settings: Settings,
    state: SweepState,
    slice_units: int,
) -> SweepState:
    """Run one round: changed sources, a backlog page, then other keys' rows.

    Args:
      engine: The store's engine; a connection is taken per read and per write.
      model: Embeds the units.
      settings: What the rows are made under.
      state: Where the last round left off.
      slice_units: Sources per page, and texts per model call.

    Returns:
      state: Where this round leaves off.

    """
    records = _records(settings)
    owners = (
        _owners(settings) if settings.inquiry_fields or settings.edge_notes else None
    )
    async with engine.acquire() as conn:
        through = str(await conn.fetchval("SELECT pg_current_snapshot()::text"))
    worked = False
    written = 0
    if state.since is not None and state.since != through:
        for source in (records,) if owners is None else (records, owners):
            while page := await _changed(
                engine,
                source,
                since=state.since,
                through=through,
                limit=slice_units,
            ):
                written += await _embed(
                    engine,
                    page,
                    model=model,
                    settings=settings,
                    slice_units=slice_units,
                )
                worked = True
    after, walking_owners, walked, cleaned = (
        state.after,
        state.owners,
        state.walked,
        state.cleaned,
    )
    if not walked:
        source = records if owners is None or not walking_owners else owners
        page = await _backlog(engine, source, after=after, limit=slice_units)
        if not page and source is records and owners is not None:
            source, walking_owners = owners, True
            page = await _backlog(engine, source, after=None, limit=slice_units)
        if page:
            written += await _embed(
                engine,
                page,
                model=model,
                settings=settings,
                slice_units=slice_units,
            )
            after = tuple(page[-1][key] for key in source.keys)
            worked = True
        else:
            walked = True
    if walked and not cleaned:
        async with engine.acquire() as conn:
            # ``< OR >`` rather than ``<>`` so the planner reads ``vectors_settings``
            # instead of scanning the whole vectors table once all rows match ``$1``.
            deleted = await conn.fetchval(
                "WITH gone AS (DELETE FROM embedding.vectors WHERE ctid IN "
                "(SELECT ctid FROM embedding.vectors "
                "WHERE settings < $1 OR settings > $1 LIMIT 10000) "
                "RETURNING 1) SELECT count(*) FROM gone",
                settings.key,
            )
            if deleted:
                worked = True
            else:
                await drop_other_indexes(conn, settings)
                cleaned = True
    return SweepState(
        since=through,
        after=after,
        owners=walking_owners,
        walked=walked,
        cleaned=cleaned,
        worked=worked,
        written=written,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Source:
    """One kind of source, as the readers page it: ``$1`` its kinds, ``$2`` the key.

    Attributes:
      head: ``SELECT`` of the page's columns and ``FROM``, up to the ``WHERE``.
      test: What keeps a pending source of this kind, after the window or keyset.
      stamp: The column the change stamp is in.
      keyset: The walk's order, newest first.
      keys: The page columns that hold ``keyset``, in its order.
      kinds: The kinds ``$1`` binds.
      key: The settings key ``$2`` binds.

    """

    head: str
    test: str
    stamp: str
    keyset: tuple[str, ...]
    keys: tuple[str, ...]
    kinds: tuple[str, ...]
    key: str


# Audience: internal -- a fragment the sweep's readers complete.
def _records(settings: Settings) -> _Source:
    """Return the session records as a source."""
    join, predicate = manifest_bound("r")
    return _Source(
        head=vetted_sql("SELECT ", _COLUMNS, " FROM session_records r ", join),
        test=vetted_sql(predicate, " AND ", _PENDING),
        stamp="r.changed_xid",
        keyset=("r.created", "r.session_id", "r.part", "r.idx"),
        keys=("created", "owner_id", "part", "idx"),
        kinds=settings.session_kinds,
        key=settings.key,
    )


# An inquiry's sources, over ``i``: its text, each note of an edge it starts from, and
# each field whose rows remain though its source is gone, as an empty text. The kinds
# are bound, but the columns are written in: a column cannot be a parameter, and the
# add-on admits only ``INQUIRY_TEXT_COLUMNS``.
# Audience: internal -- a fragment the sweep's readers complete.
def _owners(settings: Settings) -> _Source:
    """Return the inquiries, owners of their texts and edge notes, as a source."""
    text = (
        vetted_sql(
            "CASE i.kind ",
            *(
                vetted_sql(
                    f"WHEN ($1::text[])[{ordinal}] THEN concat_ws(E'\\n\\n', ",
                    ", ".join(f"i.{column}" for column in columns),
                    ") ",
                )
                for ordinal, (_kind, columns) in enumerate(settings.inquiry_fields, 1)
            ),
            "END",
        )
        if settings.inquiry_fields
        else "''"
    )
    notes = (
        " UNION ALL SELECT 'edge:' || e.edge_kind || ':' || e.to_id, e.note "
        "FROM edges e WHERE e.from_id = i.id AND e.note IS NOT NULL"
        if settings.edge_notes
        else ""
    )
    return _Source(
        head=vetted_sql(
            "SELECT i.created, i.id AS owner_id, 0 AS part, 0 AS idx, u.field, u.text, "
            "md5(u.text) AS text_md5, u.text <> '' AS source FROM inquiries i "
            "CROSS JOIN LATERAL (WITH live AS (SELECT 'text' AS field, ",
            text,
            " AS text WHERE i.kind = ANY($1::text[])",
            notes,
            ") SELECT field, text FROM live UNION ALL SELECT v.field, '' "
            "FROM embedding.vectors v WHERE v.owner_id = i.id AND v.part = 0 "
            "AND v.idx = 0 AND v.chunk = 0 AND v.field <> 'content' "
            "AND v.field NOT IN (SELECT field FROM live)) u ",
        ),
        test=(
            "CASE WHEN u.text <> '' "
            "THEN NOT EXISTS (SELECT 1 FROM embedding.vectors v "
            "WHERE v.owner_id = i.id AND v.part = 0 AND v.idx = 0 "
            "AND v.field = u.field AND v.chunk = 0 AND v.settings = $2 "
            "AND v.text_md5 = md5(u.text)) "
            "ELSE EXISTS (SELECT 1 FROM embedding.vectors v "
            "WHERE v.owner_id = i.id AND v.part = 0 AND v.idx = 0 "
            "AND v.field = u.field) END"
        ),
        stamp="i.changed_xid",
        keyset=("i.created", "i.id", "u.field"),
        keys=("created", "owner_id", "field"),
        kinds=tuple(kind for kind, _columns in settings.inquiry_fields),
        key=settings.key,
    )


_COLUMNS: Final = (
    "r.created, r.session_id AS owner_id, r.part, r.idx, 'content' AS field, r.text, "
    "md5(r.text) AS text_md5, r.kind = ANY($1::text[]) AND r.text <> '' AS source"
)


# A source with no row of its text's md5 under the key, or a record that is no source
# but still has rows. ``$1`` is the configured kinds, ``$2`` the key.
# Audience: internal -- a fragment the sweep's readers complete.
_PENDING: Final = (
    "CASE WHEN r.kind = ANY($1::text[]) AND r.text <> '' "
    "THEN NOT EXISTS (SELECT 1 FROM embedding.vectors v "
    "WHERE v.owner_id = r.session_id AND v.part = r.part AND v.idx = r.idx "
    "AND v.field = 'content' AND v.chunk = 0 AND v.settings = $2 "
    "AND v.text_md5 = md5(r.text)) "
    "ELSE EXISTS (SELECT 1 FROM embedding.vectors v "
    "WHERE v.owner_id = r.session_id AND v.part = r.part AND v.idx = r.idx "
    "AND v.field = 'content') END"
)


# The sources the ``since`` snapshot could not see and ``through`` can: a transaction
# still open at ``since`` counts as unseen however old its xid, which a timestamp
# watermark would get wrong. A source written again after ``through`` is left for the
# next round. Written pages drop out of the pending test, so no keyset is needed.
# Audience: internal -- a background job; nothing it reads reaches a caller.
async def _changed(
    engine: DatabaseEngine,
    source: _Source,
    *,
    since: str,
    through: str,
    limit: int,
) -> list[Record]:
    """Return a page of pending sources changed after ``since``, newest change first."""
    stamp = source.stamp
    async with engine.acquire() as conn:
        return list(
            await conn.fetch(
                vetted_sql(
                    source.head,
                    "WHERE ",
                    stamp,
                    " >= pg_snapshot_xmin($3::text::pg_snapshot) AND ",
                    stamp,
                    " < pg_snapshot_xmax($4::text::pg_snapshot) "
                    "AND NOT pg_visible_in_snapshot(",
                    stamp,
                    ", $3::text::pg_snapshot) AND pg_visible_in_snapshot(",
                    stamp,
                    ", $4::text::pg_snapshot) AND ",
                    source.test,
                    " ORDER BY ",
                    stamp,
                    " DESC, ",
                    ", ".join(source.keyset[1:]),
                    " LIMIT $5",
                ),
                list(source.kinds),
                source.key,
                since,
                through,
                limit,
            ),
        )


# Audience: internal -- a background job; nothing it reads reaches a caller.
async def _backlog(
    engine: DatabaseEngine,
    source: _Source,
    *,
    after: tuple[object, ...] | None,
    limit: int,
) -> list[Record]:
    """Return the next page of pending sources older than ``after``, newest first."""
    keyset = (
        ""
        if after is None
        else vetted_sql(
            "(",
            ", ".join(source.keyset),
            ") < (",
            ", ".join(f"${at}" for at in range(4, 4 + len(after))),
            ") AND ",
        )
    )
    async with engine.acquire() as conn:
        return list(
            await conn.fetch(
                vetted_sql(
                    source.head,
                    "WHERE ",
                    keyset,
                    source.test,
                    " ORDER BY ",
                    ", ".join(f"{column} DESC" for column in source.keyset),
                    " LIMIT $3",
                ),
                list(source.kinds),
                source.key,
                limit,
                *(() if after is None else after),
            ),
        )


async def _embed(
    engine: DatabaseEngine,
    page: Sequence[Record],
    *,
    model: Embedder,
    settings: Settings,
    slice_units: int,
) -> int:
    """Embed a page's units in calls of ``slice_units``, write them; return how many."""
    units = [
        text_units(str(row["text"]), field=str(row["field"]), settings=settings)
        if row["source"]
        else ()
        for row in page
    ]
    texts = [unit.text for each in units for unit in each]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), slice_units):
        vectors.extend(await model.embed_batch(texts[start : start + slice_units]))
    held: list[tuple[Record, IndexUnit, list[float]]] = []
    records: list[Record] = []
    at = 0
    for row, each in zip(page, units, strict=True):
        if records and len(held) + len(each) > _WRITE_UNITS:
            await _write(engine, records, written=held, settings=settings)
            held.clear()
            records.clear()
        records.append(row)
        held.extend(
            (row, unit, vector)
            for unit, vector in zip(each, vectors[at : at + len(each)], strict=True)
        )
        at += len(each)
    await _write(engine, records, written=held, settings=settings)
    return len(texts)


# The most units one transaction writes, whole sources only: a page of the re-embed's
# 65,536 would otherwise send every vector's text in one statement.
_WRITE_UNITS: Final = 1_024


# One transaction a few sources: every source's rows go, then its current units come
# back, so a reader never sees a source half rewritten. A source with no units keeps
# none. Another writer (the re-embed beside the server's sweep) may commit a unit
# between the delete and the insert, so the insert overwrites a row it meets.
# Audience: internal -- a background job's writer; it returns nothing.
async def _write(
    engine: DatabaseEngine,
    page: Sequence[Record],
    *,
    written: Sequence[tuple[Record, IndexUnit, Sequence[float]]],
    settings: Settings,
) -> None:
    """Replace the rows of every source of ``page`` with the ``written`` units."""
    async with engine.acquire() as conn, tx(conn):
        await conn.execute(
            "DELETE FROM embedding.vectors v USING unnest($1::uuid[], $2::int[], "
            "$3::int[], $4::text[]) AS t(owner_id, part, idx, field) "
            "WHERE v.owner_id = t.owner_id AND v.part = t.part AND v.idx = t.idx "
            "AND v.field = t.field",
            [row["owner_id"] for row in page],
            [row["part"] for row in page],
            [row["idx"] for row in page],
            [row["field"] for row in page],
        )
        await conn.execute(
            "INSERT INTO embedding.vectors "
            "(owner_id, part, idx, field, chunk, text_md5, settings, embedding) "
            "SELECT owner_id, part, idx, field, chunk, text_md5, $8, "
            "embedding::halfvec FROM unnest($1::uuid[], $2::int[], $3::int[], "
            "$4::text[], $5::int[], $6::text[], $7::text[]) "
            "AS t(owner_id, part, idx, field, chunk, text_md5, embedding) "
            "ON CONFLICT (owner_id, part, idx, field, chunk) DO UPDATE SET "
            "text_md5 = EXCLUDED.text_md5, settings = EXCLUDED.settings, "
            "embedding = EXCLUDED.embedding, created = EXCLUDED.created",
            [row["owner_id"] for row, _unit, _vector in written],
            [row["part"] for row, _unit, _vector in written],
            [row["idx"] for row, _unit, _vector in written],
            [unit.field for _row, unit, _vector in written],
            [unit.chunk for _row, unit, _vector in written],
            [row["text_md5"] for row, _unit, _vector in written],
            [vec_to_text(vector) for _row, _unit, vector in written],
            settings.key,
        )
