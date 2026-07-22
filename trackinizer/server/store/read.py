""":class:`_ReadMixin` -- read-only inquiry, cost, and change queries.

A pure leaf: :meth:`get_inquiry`, :meth:`list_kind`, :meth:`next_issue`,
:meth:`cost_for`, :meth:`proves_belief`, :meth:`what_changed_for_me`,
:meth:`get_change`, and :meth:`list_changes` all read through
``self.engine`` and call no other mixin.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

from trackinizer.server.projection import (
    fetch_edges,
    fetch_edges_bulk,
    materialize,
)
from trackinizer.server.sql_fragments import (
    _COST_SUBTREE_SQL,
    _NEXT_ISSUE_SQL,
    _PROVES_BELIEF_SQL,
)
from trackinizer.server.store.shared import _StoreShared
from trackinizer.server.values import vetted_sql
from trackinizer.types.change_log import Change
from trackinizer.types.cost import Cost
from trackinizer.types.errors import NotFoundError
from trackinizer.types.inquiries import Inquiry, Issue
from trackinizer.wire.row_filter import RowFilter, match_filter
from trackinizer.wire.seq_ranges import SeqRange


__all__ = [
    "_ReadMixin",
    "_seq_range_clause",
]


def _seq_range_clause(
    params: list[object], seq_ranges: Sequence[SeqRange]
) -> str | None:
    """Lower a seq-range union to one parenthesized ``OR`` group, or ``None``.

    Appends each interval's present bounds to ``params`` (so positional
    placeholders stay in lockstep with the caller's running list) and
    returns the ``(... OR ...)`` clause. Empty ``seq_ranges`` returns
    ``None`` so the caller omits the clause entirely. Shared by every
    ``seq``-windowed reader (:meth:`Store.list_kind`,
    :meth:`Store.read_session_events`) so the disjoint-union lowering has
    exactly one implementation.
    """
    if not seq_ranges:
        return None
    disjuncts: list[str] = []
    for interval in seq_ranges:
        bounds: list[str] = []
        if interval.start is not None:
            params.append(interval.start)
            bounds.append(f"seq >= ${len(params)}")
        if interval.stop is not None:
            params.append(interval.stop)
            bounds.append(f"seq <= ${len(params)}")
        # The wire rejects bare ``..``, so ``bounds`` is never empty.
        disjuncts.append("(" + " AND ".join(bounds) + ")")
    return "(" + " OR ".join(disjuncts) + ")"


class _ReadMixin(_StoreShared):
    """Read-only inquiry, cost, and change queries for :class:`Store`."""

    async def get_inquiry(self, target_id: UUID) -> Inquiry | None:
        """Fetch any Inquiry by id; subclass dispatched from ``kind`` column."""
        async with self.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM inquiries WHERE id = $1",
                target_id,
            )
            if row is None:
                return None
            outbound, inbound = await fetch_edges(conn, target_id)
        return materialize(row, {target_id: outbound}, {target_id: inbound})

    async def list_kind(
        self,
        kind: Inquiry.InquiryKind,
        *,
        status: Inquiry.Status | None = None,
        limit: int = 50,
        offset: int = 0,
        seq_ranges: Sequence[SeqRange] = (),
        filters: Sequence[RowFilter] = (),
    ) -> list[Inquiry]:
        """Paginated list of one kind, optionally filtered.

        ``filters`` is the canonical filter pipeline shared with the
        trax CLI: every clause is evaluated before ``LIMIT``/``OFFSET``
        is applied, so a matching row is never silently dropped
        because it falls outside the natural recency window. Status /
        seq-range filters that lower cleanly into SQL are folded into
        the SELECT for efficiency; the rest are materialized and
        post-filtered in-process via :func:`match_filter`. The full
        scan is acceptable at this project's row-count (trackinizer is
        a personal/team inquiry log; rows are O(thousands)). If that
        ever changes, add SQL lowering for the hot ops without
        changing the contract here.

        ``seq_ranges`` is a union of inclusive intervals: the row's
        ``seq`` must fall in at least one. The intervals lower to one
        parenthesized ``OR`` group over the ``(kind, seq)`` index, so a
        disjoint selection (``222..260,279..``) is one indexed query, not
        one round-trip per interval.
        """
        params: list[object] = [kind]
        clauses = ["kind = $1"]
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if (seq_clause := _seq_range_clause(params, seq_ranges)) is not None:
            clauses.append(seq_clause)
        # ``id`` tie-breaks rows that share a ``created`` timestamp (always
        # true in a single transaction, sometimes true across them) so
        # offset pagination doesn't drop or duplicate rows under
        # concurrent inserts.
        order_sql = "ORDER BY created DESC, id DESC"
        if filters:
            # Post-filter pipeline: pull every row matching the SQL
            # prefilter (kind / status / seq-range), apply each clause
            # in-process, then window. This is the only shape that
            # keeps ``LIMIT`` honest -- pushing it into the SQL would
            # re-introduce the pre-filter truncation bug (Issue#256).
            #
            # One acquire spans both fetches so the row set and the
            # edge bulk-fetch hit the same pooled connection back to
            # back; a concurrent ``purge`` then can't slip in between
            # and leave us materializing edges for a row that no
            # longer exists. The non-filter branch already shared one
            # acquire for the same reason.
            sql = vetted_sql(
                "SELECT * FROM inquiries WHERE ",
                " AND ".join(clauses),
                " ",
                order_sql,
            )
            async with self.engine.acquire() as conn:
                rows = await conn.fetch(sql, *params)
                kept = [r for r in rows if all(match_filter(r, f) for f in filters)]
                window = kept[offset : offset + limit]
                outbound, inbound = await fetch_edges_bulk(
                    conn, [r["id"] for r in window]
                )
            return [materialize(row, outbound, inbound) for row in window]
        params.extend([limit, offset])
        sql = vetted_sql(
            "SELECT * FROM inquiries WHERE ",
            " AND ".join(clauses),
            " ",
            order_sql,
            " LIMIT $",
            str(len(params) - 1),
            " OFFSET $",
            str(len(params)),
        )
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            outbound, inbound = await fetch_edges_bulk(conn, [r["id"] for r in rows])
        return [materialize(row, outbound, inbound) for row in rows]

    async def next_issue(self) -> Issue | None:
        """Return the next active Issue whose prerequisites are all terminal."""
        async with self.engine.acquire() as conn:
            row = await conn.fetchrow(_NEXT_ISSUE_SQL)
            if row is None:
                return None
            rid = cast(UUID, row["id"])
            outbound, inbound = await fetch_edges(conn, rid)
        return cast(Issue, materialize(row, {rid: outbound}, {rid: inbound}))

    async def cost_for(self, subject_id: UUID, *, deep: bool = False) -> Cost | None:
        """Return the running cost for one inquiry, or ``None`` if missing.

        ``deep=True`` rolls up the Issue decomposition subtree via
        ``cost_subtree.sql``; ``deep=False`` reads the row's own
        running totals. Both paths return ``None`` for an unknown id so
        callers can distinguish "no such row" from "zero recorded cost".
        """
        async with self.engine.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM inquiries WHERE id = $1", subject_id
            )
            if exists is None:
                return None
            if deep:
                row = await conn.fetchrow(_COST_SUBTREE_SQL, subject_id)
            else:
                row = await conn.fetchrow(
                    "SELECT marginal_cost_agent_usd AS agent_usd, "
                    "marginal_cost_resource_usd AS resource_usd "
                    "FROM inquiries WHERE id = $1",
                    subject_id,
                )
        if row is None:
            return Cost()
        return Cost(
            agent_usd=float(row["agent_usd"]),
            resource_usd=float(row["resource_usd"]),
        )

    async def proves_belief(self, belief_id: UUID) -> list[Inquiry]:
        """Active load-bearing ``proves`` Artifacts for ``belief_id``.

        Each returned row is projected through :func:`fetch_edges_bulk`
        so its own provenance / citation fields are populated -- callers
        can drill into evidence chains without re-fetching.
        """
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(_PROVES_BELIEF_SQL, belief_id)
            outbound, inbound = await fetch_edges_bulk(conn, [r["id"] for r in rows])
        return [materialize(row, outbound, inbound) for row in rows]

    async def what_changed_for_me(
        self,
        agent: Inquiry.Actor,
        since: datetime,
        *,
        after_id: UUID | None = None,
        limit: int = 200,
    ) -> list[Change]:
        """Changes since ``(since, after_id)`` for ``agent``, paginated.

        The cursor is ``(created, id)``: cursor predicate is
        ``(c.created, c.id) > ($since, $after_id)``. Reading
        ``subscribers_snapshot`` keeps ``purged`` events visible
        because the subject row no longer exists to join against.

        Args:
          agent: Subscriber id to filter on.
          since: Lower bound on ``created``; combined with ``after_id``
            forms a (timestamp, id) cursor.
          after_id: Tie-breaker id for changes that share ``since``'s
            timestamp. Use the last id from the previous page; omit on
            the first page.
          limit: Maximum rows to return (1-1000). Default 200.

        Returns:
          changes: Up to ``limit`` matching :class:`Change` rows ordered
            by ``(created, id)`` ascending.

        """
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")
        # Single (created, id) tuple cursor for both first and
        # subsequent pages. The first-page form previously used
        # ``c.created > $1`` which dropped rows tied at exactly
        # ``since`` -- a caller polling with the last-seen ``created``
        # would miss those ties because with ``clock_timestamp()`` a
        # multi-row transaction shares one timestamp. The tuple
        # comparison with a min-UUID sentinel on page one mirrors the
        # next-page semantics exactly minus the tie.
        cursor_id = after_id if after_id is not None else UUID(int=0)
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.* FROM change_log c "
                "WHERE (c.created, c.id) > ($1, $2) "
                "AND $3 = ANY(c.subscribers_snapshot) "
                "ORDER BY c.created, c.id LIMIT $4",
                since,
                cursor_id,
                agent,
                limit,
            )
        return [Change.from_row(r) for r in rows]

    async def get_change(self, change_id: UUID) -> Change | None:
        """Fetch one ``change_log`` row by id; ``None`` when absent."""
        async with self.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM change_log WHERE id = $1", change_id
            )
        return None if row is None else Change.from_row(row)

    async def list_changes(
        self,
        *,
        since: datetime | None = None,
        after_id: UUID | None = None,
        actor: Inquiry.Actor | None = None,
        subject_id: UUID | None = None,
        subject_kind: Inquiry.InquiryKind | None = None,
        kind: Change.Kind | None = None,
        limit: int = 200,
    ) -> list[Change]:
        """Filtered, newest-first ``change_log`` slice.

        Backs ``GET /api/change_log`` (``docs/api.md`` 1.14-1.15). Every
        filter is optional and ANDed; ``since`` is an inclusive lower
        bound on ``created`` and ``after_id`` an exclusive id cursor for
        stable pagination within one timestamp.

        Args:
          since: Inclusive lower bound on ``created``.
          after_id: Exclusive id cursor; rows with this id or earlier in
            the ``(created DESC, id DESC)`` order are skipped.
          actor: Filter on the change author.
          subject_id: Filter on the mutated row id.
          subject_kind: Filter on the mutated row kind.
          kind: Filter on the change discriminator.
          limit: Maximum rows (1-1000). Default 200.

        Returns:
          changes: Up to ``limit`` matching :class:`Change` rows ordered
            by ``(created, id)`` descending.

        """
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")
        async with self.engine.acquire() as conn:
            clauses: list[str] = []
            args: list[object] = []
            for column, value in (
                ("created >=", since),
                ("actor =", actor),
                ("subject_id =", subject_id),
                ("subject_kind =", subject_kind),
                ("kind =", kind),
            ):
                if value is not None:
                    args.append(value)
                    clauses.append(f"{column} ${len(args)}")
            if after_id is not None:
                # The page order is ``(created, id)`` descending, so the cursor
                # must compare the same tuple: a bare ``id < after_id`` skips
                # older rows with larger UUIDs and dupes newer rows with
                # smaller ones (UUID order is unrelated to ``created``). Look
                # up the cursor row's ``created`` and bind both halves.
                cursor_created = await conn.fetchval(
                    "SELECT created FROM change_log WHERE id = $1", after_id
                )
                if cursor_created is None:
                    # An unknown cursor would make ``(created, id) < (NULL, ...)``
                    # NULL for every row -> a silent empty page that a pager
                    # mistakes for "end of results". 404 instead.
                    raise NotFoundError(f"change {after_id} not found")
                args.append(cursor_created)
                created_param = len(args)
                args.append(after_id)
                clauses.append(f"(created, id) < (${created_param}, ${len(args)})")
            where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
            args.append(limit)
            rows = await conn.fetch(
                vetted_sql(
                    "SELECT * FROM change_log ",
                    where,
                    "ORDER BY created DESC, id DESC LIMIT $",
                    str(len(args)),
                ),
                *args,
            )
        return [Change.from_row(r) for r in rows]
