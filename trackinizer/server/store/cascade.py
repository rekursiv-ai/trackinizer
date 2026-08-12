""":class:`_CascadeAuditMixin` -- change emission, cascade, and purge.

The MRO base for every mutating mixin: :meth:`emit_change` writes the
``change_log`` row, updates running cost, and drives the ancestor
re-assessment cascade; :meth:`purge` deletes an inquiry and replays the
peer-side audits. Depends on nothing outbound, so it sits at the bottom of
the mixin dependency graph.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

import collections
import uuid

import asyncpg

from trackinizer.lib.postgres import Conn
from trackinizer.server.notify import (
    NOTIFICATION_BUFFER,
    Notification,
    notify_after_commit,
    tx,
)
from trackinizer.server.schema_gen import CHANGE_LOG_COLUMN_ORDER
from trackinizer.server.setter_dispatch import COLUMN_SPECS
from trackinizer.server.store.change_id_slot import (
    _consume_client_change_id,
)
from trackinizer.server.store.shared import _StoreShared
from trackinizer.server.values import list_or_none, vetted_sql
from trackinizer.types.change_log import Change, Snapshot
from trackinizer.types.cost import Cost
from trackinizer.types.edges import EDGE_POLICIES, Edge
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.inquiries import Inquiry, Issue


__all__ = [
    "_CascadeAuditMixin",
]


_NO_COST: Cost = Cost()
"""Zero-cost singleton shared as the default for cost-bearing methods.

``Cost`` is frozen with slots, so one shared instance is safe.
"""


# The change_log ``old_/new_`` mirror carries one column per audited inquiry
# field, in the single ``CHANGE_LOG_COLUMN_ORDER`` (the same list ``Snapshot``
# and the schema mirror derive from). A list column (``TEXT[]`` / ``UUID[]``) is
# coerced through ``list_or_none``; every other column binds raw. Deriving the
# mirror (rather than hand-listing 64 ``old_X``/``new_X`` entries) makes it
# structurally impossible for a new audited field to be silently dropped from
# the audit INSERT -- the GSI-01 bug class.
_MIRROR_LIST_COLUMNS: frozenset[str] = frozenset(
    col
    for col in CHANGE_LOG_COLUMN_ORDER
    if (COLUMN_SPECS[col].sql_type or "").endswith("[]")
)


def _snapshot_mirror(side: str, snap: Snapshot) -> dict[str, Any]:
    """Build the ``{side}_<col>`` audit-mirror entries for one Snapshot side.

    Derived from :data:`CHANGE_LOG_COLUMN_ORDER` so a new audited column flows
    in with no edit here; list columns pass through ``list_or_none`` (NULL stays
    NULL, a set becomes a plain list asyncpg binds to the array column).
    """
    return {
        f"{side}_{col}": (
            list_or_none(getattr(snap, col))
            if col in _MIRROR_LIST_COLUMNS
            else getattr(snap, col)
        )
        for col in CHANGE_LOG_COLUMN_ORDER
    }


_EMPTY_SNAPSHOT: Snapshot = Snapshot()
"""Empty-Snapshot singleton, the default for ``emit_change``'s ``old`` / ``new``."""


_NON_PROPAGATING_CHANGE_KINDS: frozenset[Change.Kind] = frozenset(
    {
        "created",
        "dependency_changed",
        "edge_removed",
        # An annotation edit (note / valence / labels / priority) leaves
        # the dependency structure intact, so it raises no ancestor
        # re-assessment -- unlike the structural ``edge_added`` /
        # ``edge_removed``, which do cascade.
        "edge_annotation_changed",
        "implicit_subs_opened",
        "implicit_subs_closed",
        # ``purge()`` drives its own cascade from a captured edge list, since
        # the row is about to be deleted and the normal cascade SELECT would
        # see nothing. Listing it here also keeps ``cascade=True`` safe if a
        # caller forgets the explicit ``cascade=False``.
        "purged",
    }
)


class _CascadeAuditMixin(_StoreShared):
    """Change emission, ancestor cascade, and purge for :class:`Store`."""

    async def emit_change(
        self,
        conn: Conn,
        *,
        subject_id: UUID,
        subject_kind: Inquiry.InquiryKind,
        kind: Change.Kind,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
        caused_by: UUID | None = None,
        reason: str = "",
        marginal_cost: Cost = _NO_COST,
        cost_delta: Cost = _NO_COST,
        old: Snapshot = _EMPTY_SNAPSHOT,
        new: Snapshot = _EMPTY_SNAPSHOT,
        cascade: bool = True,
        extra_subscribers: tuple[Inquiry.Actor, ...] = (),
    ) -> tuple[UUID, tuple[str, ...]]:
        """Insert one ``change_log`` row; return ``(id, subscribers)``.

        Adds ``marginal_cost`` to the subject's running totals and
        captures the subscriber list at change-emit time, both via a
        single ``UPDATE ... RETURNING``. The captured subscribers land
        on ``change_log.subscribers_snapshot`` and are returned to the
        caller so post-commit notification can route without consulting
        the (possibly-purged) ``inquiries`` row.

        Returns:
          change_id: New ``change_log`` row id.
          subscribers: Tuple of agent ids subscribed at this moment;
            empty when the subject is already purged.

        Idempotency: when the client supplied a key (via the
        ``Idempotency-Key`` contextvar) and a row with that ``change_log.id``
        already exists, the call short-circuits with the original change's
        id and subscribers. Edits take this replay path directly. Submits
        (``kind="created"``) can't: the racer's subject_id differs from the
        committed row's, so the method re-raises ``UniqueViolationError`` to
        let ``_submit_generic`` roll back its tx and re-probe.

        """
        client_change_id = _consume_client_change_id()
        change_id = client_change_id or uuid.uuid4()

        async def _apply() -> tuple[Cost, Cost, tuple[Inquiry.Actor, ...]]:
            """Run the cost UPDATE and ``change_log`` INSERT for this change.

            Pulled into a closure so the client-supplied-id path can wrap
            both statements in a savepoint and roll them back atomically
            on a unique-key collision (the retry case).
            """
            # ``clock_timestamp()`` is the wall clock at this statement, not
            # the transaction-start timestamp ``now()`` returns. Without it,
            # multiple changes in one transaction share a tie-broken
            # timestamp and late-committing transactions can land with a
            # ``created`` earlier than already-observed rows -- breaking
            # ``what_changed_for_me``'s ``since`` cursor semantics.
            agent_delta = marginal_cost.agent_usd + cost_delta.agent_usd
            resource_delta = marginal_cost.resource_usd + cost_delta.resource_usd
            # Floor guard lives in the WHERE clause so the UPDATE itself
            # is atomic: a delta that would drive either running total
            # negative matches no row, the row stays put, and a caller
            # without an outer transaction cannot leave a negative
            # balance behind before the raise propagates. The presence
            # probe is folded into the same statement via a CTE so a
            # row purged between two statements cannot collapse a
            # floor-refused delta into a silent zero-delta audit emit:
            # Postgres runs all sub-statements of one statement against
            # one snapshot, so ``probe.id`` and ``upd.new_*`` always
            # agree on whether the row was present at evaluation time.
            # The probe takes ``FOR SHARE`` so it serializes against
            # purge's ``FOR UPDATE``: a concurrent purge cannot commit a
            # DELETE between the probe finding the row and the UPDATE
            # touching it, which would otherwise leave ``probe.id``
            # non-NULL while ``upd`` matched nothing -- a phantom
            # zero-delta audit for an already-deleted subject.
            cost_row = await conn.fetchrow(
                "WITH probe AS ( "
                "    SELECT id FROM inquiries WHERE id = $3 FOR SHARE "
                "), "
                "upd AS ( "
                "    UPDATE inquiries "
                "    SET marginal_cost_agent_usd    = marginal_cost_agent_usd    + $1, "
                "        marginal_cost_resource_usd = marginal_cost_resource_usd + $2, "
                "        modified                   = clock_timestamp() "
                "    WHERE id = $3 "
                "      AND marginal_cost_agent_usd    + $1 >= 0 "
                "      AND marginal_cost_resource_usd + $2 >= 0 "
                "    RETURNING marginal_cost_agent_usd    - $1 AS old_agent, "
                "              marginal_cost_resource_usd - $2 AS old_resource, "
                "              marginal_cost_agent_usd    AS new_agent, "
                "              marginal_cost_resource_usd AS new_resource, "
                "              subscribers AS current_subscribers "
                ") "
                "SELECT probe.id AS existing_id, "
                "       upd.old_agent, upd.old_resource, "
                "       upd.new_agent, upd.new_resource, "
                "       upd.current_subscribers "
                "FROM probe LEFT JOIN upd ON true",
                agent_delta,
                resource_delta,
                subject_id,
            )
            if cost_row is None:
                # Genuine tombstone: the row was already gone at the
                # statement's snapshot, so the probe matched nothing
                # and the outer SELECT produced no rows. Audit proceeds
                # with zero deltas.
                old_cost = new_cost = Cost()
                subs: tuple[Inquiry.Actor, ...] = ()
            elif cost_row["new_agent"] is None:
                # Row present at snapshot but the floor refused the
                # delta: ``probe.id`` is non-NULL and the LEFT JOIN
                # filled ``upd.*`` with NULLs.
                raise ConflictError(
                    f"cost_delta would drive marginal_cost negative; "
                    f"agent_delta={agent_delta}, "
                    f"resource_delta={resource_delta}"
                )
            else:
                old_cost = Cost(
                    agent_usd=float(cost_row["old_agent"]),
                    resource_usd=float(cost_row["old_resource"]),
                )
                new_cost = Cost(
                    agent_usd=float(cost_row["new_agent"]),
                    resource_usd=float(cost_row["new_resource"]),
                )
                subs = cast(
                    "tuple[Inquiry.Actor, ...]",
                    tuple(cost_row["current_subscribers"] or ()),
                )
            if extra_subscribers:
                # Update ``seen`` in-loop so duplicates *within*
                # ``extra_subscribers`` are also collapsed.
                seen: set[Inquiry.Actor] = set(subs)
                extras: list[Inquiry.Actor] = []
                for extra in extra_subscribers:
                    if extra not in seen:
                        seen.add(extra)
                        extras.append(extra)
                subs = subs + tuple(extras)
            # Edge-peer presence-equivalence CHECK on change_log requires
            # ``edge_note`` and ``edge_labels`` to be non-NULL whenever
            # their side's ``peer_id`` is non-NULL. The Edge dataclass
            # now lets these store as NULL on the edges table (a
            # cleared annotation), so coerce None to ``""`` / ``[]``
            # only on the audit side, only when peer is present.
            old_edge_note = (
                ""
                if old.peer_id is not None and old.edge_note is None
                else old.edge_note
            )
            old_edge_labels = (
                []
                if old.peer_id is not None and old.edge_labels is None
                else list_or_none(old.edge_labels)
            )
            new_edge_note = (
                ""
                if new.peer_id is not None and new.edge_note is None
                else new.edge_note
            )
            new_edge_labels = (
                []
                if new.peer_id is not None and new.edge_labels is None
                else list_or_none(new.edge_labels)
            )
            columns: dict[str, Any] = {
                "id": change_id,
                "api_key_id": api_key_id,
                "actor": actor,
                "subject_id": subject_id,
                "subject_kind": subject_kind,
                "kind": kind,
                "caused_by": caused_by,
                "reason": reason,
                "subscribers_snapshot": list(subs),
                "old_title": old.title,
                "old_description": old.description,
                "old_labels": list_or_none(old.labels),
                "old_owner": old.owner,
                "old_account": old.account,
                "old_subscribers": list_or_none(old.subscribers),
                "old_peer_id": old.peer_id,
                "old_peer_kind": old.peer_kind,
                "old_peer_edge_kind": old.peer_edge_kind,
                "old_edge_priority": old.edge_priority,
                "old_edge_note": old_edge_note,
                "old_edge_valence": old.edge_valence,
                "old_edge_labels": old_edge_labels,
                # The per-column old_/new_ mirror is DERIVED from
                # CHANGE_LOG_COLUMN_ORDER (see _snapshot_mirror), so a new audited
                # field can't be dropped from the audit INSERT (GSI-01 class). The
                # composite marginal_cost axes come from old_cost/new_cost (not a
                # Snapshot field) and stay explicit.
                **_snapshot_mirror("old", old),
                "old_marginal_cost_agent_usd": old_cost.agent_usd,
                "old_marginal_cost_resource_usd": old_cost.resource_usd,
                "new_peer_id": new.peer_id,
                "new_peer_kind": new.peer_kind,
                "new_peer_edge_kind": new.peer_edge_kind,
                "new_edge_priority": new.edge_priority,
                "new_edge_note": new_edge_note,
                "new_edge_valence": new.edge_valence,
                "new_edge_labels": new_edge_labels,
                **_snapshot_mirror("new", new),
                "new_marginal_cost_agent_usd": new_cost.agent_usd,
                "new_marginal_cost_resource_usd": new_cost.resource_usd,
            }
            col_names = ", ".join(columns)
            placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
            await conn.execute(
                vetted_sql(
                    "INSERT INTO change_log (",
                    col_names,
                    ") VALUES (",
                    placeholders,
                    ")",
                ),
                *columns.values(),
            )
            return old_cost, new_cost, subs

        if client_change_id is None:
            _, _, subscribers = await _apply()
        else:
            # Client supplied the id; a second INSERT with the same id
            # (retry after the response was lost) collides on the PK.
            # An explicit savepoint rolls the cost UPDATE back together
            # with the INSERT so the retry returns the original outcome
            # without double-charging cost or re-firing the cascade.
            await conn.execute("SAVEPOINT emit_change")
            try:
                _, _, subscribers = await _apply()
            except asyncpg.UniqueViolationError as err:
                await conn.execute("ROLLBACK TO SAVEPOINT emit_change")
                existing = await conn.fetchrow(
                    "SELECT actor, subject_id, kind, subscribers_snapshot "
                    "FROM change_log WHERE id = $1",
                    client_change_id,
                )
                if existing is None:
                    raise
                # ``kind="created"`` is the submit path; the caller
                # ``_submit_on_conn`` mints ``subject_id`` server-side
                # per attempt, so the racer's subject_id always differs
                # from the committed row's. Re-raise so the outer
                # ``tx()`` rolls back the half-written inquiry row and
                # the caller's ``except UniqueViolationError`` re-probes
                # ``change_log`` for the winner's subject_id.
                if kind == "created":
                    raise
                # Edit path. Replay is identified by (actor, subject,
                # kind) matching the original row. Field-level drift
                # (different reason, cost, snapshot) within a matching
                # tuple is silently treated as replay; the
                # originally-committed values win. A genuinely
                # different operation (different subject or kind) is
                # a client bug and 409s.
                # See ``docs/design_idempotency.md``.
                if (
                    existing["actor"] == actor
                    and existing["subject_id"] == subject_id
                    and existing["kind"] == kind
                ):
                    return client_change_id, cast(
                        "tuple[str, ...]",
                        tuple(existing["subscribers_snapshot"] or ()),
                    )
                raise ConflictError(
                    f"idempotency_key {client_change_id} already used "
                    "for a different operation"
                ) from err
            else:
                await conn.execute("RELEASE SAVEPOINT emit_change")
        self._buffer_notification(subject_id)
        if cascade and kind not in _NON_PROPAGATING_CHANGE_KINDS:
            await self._cascade_dependency_changed(
                conn,
                child_id=subject_id,
                child_kind=subject_kind,
                caused_by=change_id,
            )
        return change_id, subscribers

    async def _cascade_dependency_changed(
        self,
        conn: Conn,
        *,
        child_id: UUID,
        child_kind: Inquiry.InquiryKind,
        caused_by: UUID,
        edge_rows: Sequence[asyncpg.Record] | None = None,
    ) -> None:
        """Emit ancestor re-assessment alerts through parent edges.

        STORAGE direction is uniform and never varies: every edge is stored
        ``from = younger child, to = older parent``. This method's "dependent"
        endpoint is a SEPARATE, cascade-only concept -- which side gets the
        ``dependency_changed`` alert -- read from ``EdgeKindPolicy``, not from the
        storage direction. Do not conflate the two.

        One rule covers every kind: for each edge touching the changed subject,
        alert that edge's ``cascade_dependent`` endpoint (from ``EdgeKindPolicy``)
        -- unless that endpoint IS the subject, which self-suppresses. There is no
        per-kind branch in the mechanism; kinds differ only in their policy value.
        Most kinds (provenance, supersession, ``requires``) name the stored
        ``from`` child as dependent (its state derives from the parent), so a
        change to the ``to`` parent alerts the ``from`` side. ``narrows`` and the
        ``proves``/``favors`` citations name the ``to`` side dependent instead --
        the broader goal rolls up its narrower issues' state, and a cited claim
        leans on its evidence, so a change to the stored ``from`` child alerts the
        ``to`` parent -- but both run through the same single rule, not a special
        case.

        ``edge_rows`` lets ``purge()`` hand in a pre-captured edge list
        (its child's edges are about to be cascade-deleted, so the
        live SELECT would miss them). Each row must carry ``from_id,
        from_kind, to_id, to_kind, edge_kind, note, valence, labels``.
        Subsequent ancestor hops re-query live via the same path.

        Iterative BFS over the dependency DAG: a recursive walk would
        blow the Python stack on long chains, even though edge
        acyclicity bounds the visited set. Each (parent, edge) hop
        emits one ``dependency_changed`` row and then re-queues its own
        ancestors.
        """
        # Each edge in the walk fires exactly one ``dependency_changed``
        # row -- including multiple distinct edges from the same parent
        # (e.g. a Belief that both ``proves`` and ``favors``
        # cites the same Artifact gets two events, one per edge). The
        # ``walked`` set keeps the BFS frontier bounded so a parent
        # reachable via N edges doesn't trigger N redundant ancestor
        # re-walks; the per-edge emit is independent.
        walked: set[UUID] = {child_id}
        # ``deque`` gives O(1) ``popleft``; ``list.pop(0)`` was O(n)
        # so a long ancestor chain accidentally turned BFS into O(n²).
        frontier: collections.deque[
            tuple[UUID, Inquiry.InquiryKind, UUID, Sequence[asyncpg.Record] | None]
        ] = collections.deque([(child_id, child_kind, caused_by, edge_rows)])
        while frontier:
            cur_id, cur_kind, cur_cause, cur_edges = frontier.popleft()
            for parent_id, parent_kind, edge in await self._parent_edges(
                conn, cur_id, cur_edges
            ):
                change_id, _ = await self.emit_change(
                    conn,
                    actor="librarian",
                    subject_id=parent_id,
                    subject_kind=parent_kind,
                    kind="dependency_changed",
                    caused_by=cur_cause,
                    new=Snapshot(
                        peer_id=cur_id,
                        peer_kind=cur_kind,
                        peer_edge_kind=edge["edge_kind"],
                        edge_priority=cast(Issue.Priority | None, edge["priority"]),
                        edge_note=edge["note"],
                        edge_valence=cast(float | None, edge["valence"]),
                        edge_labels=tuple(edge["labels"] or ()),
                    ),
                    cascade=False,
                )
                # Queue the parent's ancestors only once per parent,
                # regardless of how many distinct edges led us there.
                if parent_id not in walked:
                    walked.add(parent_id)
                    frontier.append((parent_id, parent_kind, change_id, None))

    async def _parent_edges(
        self,
        conn: Conn,
        child_id: UUID,
        edge_rows: Sequence[asyncpg.Record] | None,
    ) -> list[tuple[UUID, Inquiry.InquiryKind, asyncpg.Record]]:
        """Resolve every (dependent, dependency_edge) pair touching ``child_id``.

        Reads the :class:`EdgeKindPolicy` registry for the dependent
        endpoint of each edge kind: ``cascade_dependent="from"`` for
        provenance / supersession / ``requires``;
        ``cascade_dependent="to"`` for ``narrows`` and the ``proves`` /
        ``favors`` citations (the cited claim is re-assessed). When
        ``edge_rows`` is None, queries live; otherwise iterates the
        caller-supplied list (used by ``purge`` to capture edges before
        FK cascade deletes them).

        Assumes single-version deployment: a stored ``edge_kind`` outside this
        Store's :data:`EDGE_POLICIES` raises ``KeyError`` (fail-fast)
        rather than silently dropping the cascade. A mixed-version cluster that
        writes kinds an older reader doesn't know would need a read-side guard
        here; today every writer and reader share one kind set.
        """
        if edge_rows is None:
            edge_rows = await conn.fetch(
                "SELECT from_id, from_kind, to_id, to_kind, "
                "edge_kind, priority, note, valence, labels FROM edges "
                "WHERE from_id = $1 OR to_id = $1",
                child_id,
            )
        out: list[tuple[UUID, Inquiry.InquiryKind, asyncpg.Record]] = []
        for row in edge_rows:
            edge_kind = cast(Edge.Kind, row["edge_kind"])
            policy = EDGE_POLICIES[edge_kind]
            if policy.cascade_dependent == "to":
                dependent_id = cast(UUID, row["to_id"])
                dependent_kind = cast(Inquiry.InquiryKind, row["to_kind"])
            else:
                dependent_id = cast(UUID, row["from_id"])
                dependent_kind = cast(Inquiry.InquiryKind, row["from_kind"])
            # The dependent itself is the row being changed/purged; do
            # not self-alert.
            if dependent_id == child_id:
                continue
            out.append((dependent_id, dependent_kind, row))
        return out

    def _buffer_notification(self, subject_id: UUID) -> None:
        """Queue a notify payload for post-commit publish.

        Silently no-ops when no buffer is active. Callers that mutate
        through the documented Store API always wrap in
        :func:`notify_after_commit`; tests and one-off helpers that
        drive :meth:`emit_change` directly should not crash on a
        missing buffer.
        """
        buffer = NOTIFICATION_BUFFER.get()
        if buffer is None:
            return
        buffer.append(Notification(engine=self.engine, subject_id=subject_id))

    async def purge(
        self,
        target_id: UUID,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> UUID | None:
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            row = await conn.fetchrow(
                "SELECT kind, owner FROM inquiries WHERE id = $1 FOR UPDATE",
                target_id,
            )
            if row is None:
                raise NotFoundError("inquiry not found")
            owner = row.get("owner")
            if owner is not None:
                raise ConflictError(
                    f"inquiry is owned by {owner!r}; release its owner before purge"
                )
            kind = cast(Inquiry.InquiryKind, row["kind"])
            parent_edges = await conn.fetch(
                "SELECT from_id, from_kind, to_id, to_kind, edge_kind, "
                "priority, note, valence, labels FROM edges "
                "WHERE from_id = $1 OR to_id = $1",
                target_id,
            )
            # Emit while the row still exists so ``subscribers_snapshot``
            # captures who was watching right before the DELETE.
            cause, _ = await self.emit_change(
                conn,
                api_key_id=api_key_id,
                actor=actor,
                subject_id=target_id,
                subject_kind=kind,
                kind="purged",
                reason=reason,
                cascade=False,
            )
            await self._cascade_dependency_changed(
                conn,
                child_id=target_id,
                child_kind=kind,
                caused_by=cause,
                edge_rows=parent_edges,
            )
            for edge in parent_edges:
                await self._emit_purge_edge_removed(
                    conn,
                    target_id=target_id,
                    target_kind=kind,
                    edge=edge,
                    caused_by=cause,
                    api_key_id=api_key_id,
                    actor=actor,
                )
            await conn.execute("DELETE FROM inquiries WHERE id = $1", target_id)
            return cause

    async def _emit_purge_edge_removed(
        self,
        conn: Conn,
        *,
        target_id: UUID,
        target_kind: Inquiry.InquiryKind,
        edge: asyncpg.Record,
        caused_by: UUID,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
    ) -> None:
        """Emit the peer-side ``edge_removed`` audit row for a purged subject."""
        if edge["from_id"] == target_id:
            subject_id = cast(UUID, edge["to_id"])
            subject_kind = cast(Inquiry.InquiryKind, edge["to_kind"])
        else:
            subject_id = cast(UUID, edge["from_id"])
            subject_kind = cast(Inquiry.InquiryKind, edge["from_kind"])
        await self.emit_change(
            conn,
            api_key_id=api_key_id,
            actor=actor,
            subject_id=subject_id,
            subject_kind=subject_kind,
            kind="edge_removed",
            caused_by=caused_by,
            cascade=False,
            old=Snapshot(
                peer_id=target_id,
                peer_kind=target_kind,
                peer_edge_kind=cast(Edge.Kind, edge["edge_kind"]),
                edge_priority=cast(Issue.Priority | None, edge["priority"]),
                edge_note=edge["note"],
                edge_valence=cast(float | None, edge["valence"]),
                edge_labels=tuple(edge["labels"] or ()),
            ),
        )
