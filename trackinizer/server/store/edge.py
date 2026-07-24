""":class:`_EdgeMixin` -- edge insertion, annotation, and removal.

Owns the edge write path: :meth:`add_edge` / :meth:`insert_edge_and_audit`
insert an edge and emit paired ``edge_added`` audits on both endpoints,
:meth:`set_edge_annotation` partial-updates an existing edge, and
:meth:`remove_edge` deletes one while driving the dependent-side cascade.
:meth:`_infer_produced_on_conn` stamps provenance after every real insert.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from trackinizer.lib.custom_types import ABSENT, Absent
from trackinizer.lib.postgres import Conn
from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.server.primitives import (
    infer_produced_endpoints,
    insert_edge,
    lookup_kind,
    validate_edge_priority,
    validate_edge_valence,
)
from trackinizer.server.store.cascade import _CascadeAuditMixin
from trackinizer.server.values import (
    canonical_strs,
    empty_optional_to_none,
)
from trackinizer.types.change_log import Snapshot
from trackinizer.types.edges import Edge
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from trackinizer.types.inquiries import Inquiry, Issue


__all__ = [
    "INFERRED_PROVENANCE_REASON",
    "_EdgeMixin",
]


INFERRED_PROVENANCE_REASON = "inferred provenance"
"""Stable prefix on the audit ``reason`` of an auto-inferred ``produces`` edge.

A consumer (UI, analytics) tells an inferred provenance edge from a
hand-recorded one by this prefix, so it is a pinned constant rather than an
inline string -- editing the human suffix never breaks that discrimination.
"""


class _EdgeMixin(_CascadeAuditMixin):
    """Edge insertion, annotation, and removal for :class:`Store`."""

    async def _add_edge_on_conn(
        self,
        conn: Conn,
        *,
        from_id: UUID,
        to_id: UUID,
        edge_kind: Edge.Kind,
        priority: Issue.Priority | None,
        note: str,
        valence: float | None,
        labels: Sequence[str],
        reason: str = "",
        caused_by: UUID | None = None,
        cascade: bool = True,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
    ) -> tuple[UUID | None, bool]:
        """Insert one edge + paired audits on an already-open transaction.

        Returns ``(change_id, created)``: ``created`` is ``True`` when a new
        edge row was inserted, ``False`` when the edge already existed and this
        upserted its annotations (or no-oped). ``change_id`` is the emitted
        change, or ``None`` for a pure no-op.

        ``caused_by`` chains the from-side audit to a triggering change (the
        inferred-``produces`` path passes the edge that caused the inference, so
        the audit trail shows the link). ``cascade=False`` suppresses the
        ancestor re-assessment walk on the from-side emit, used by the inferred
        edge so one user action does not double-cascade.
        """
        edge_labels = canonical_strs(labels)
        # Mirror ``insert_edge``'s "unset is NULL" normalization so the audit
        # Snapshot matches what the ``edges`` row actually stored: a
        # whitespace-only note lands as NULL on both, not raw on one.
        audit_note = cast("str | None", empty_optional_to_none(note))
        from_kind = await lookup_kind(conn, from_id)
        inserted, to_kind = await insert_edge(
            conn,
            from_id=from_id,
            from_kind=from_kind,
            to_id=to_id,
            edge_kind=edge_kind,
            priority=priority,
            note=note,
            valence=valence,
            labels=edge_labels,
        )
        if not inserted:
            # The edge already exists: creation is an upsert (symmetric with
            # ``label add`` and every other set), so apply the supplied
            # annotations to the existing edge instead of raising. Only the
            # annotations the caller actually passed are set -- a bare create
            # (no annotations) is a pure no-op. ``add_edge``'s defaults encode
            # "unset", so translate them to ABSENT for the annotation path.
            change_id = await self._set_edge_annotation_on_conn(
                conn,
                from_id=from_id,
                to_id=to_id,
                edge_kind=edge_kind,
                priority=priority if priority is not None else ABSENT,
                note=note or ABSENT,
                valence=valence if valence is not None else ABSENT,
                labels=edge_labels or ABSENT,
                reason=reason,
                require_existing=False,
                api_key_id=api_key_id,
                actor=actor,
            )
            return change_id, False
        cause, _ = await self.emit_change(
            conn,
            api_key_id=api_key_id,
            actor=actor,
            subject_id=from_id,
            subject_kind=from_kind,
            kind="edge_added",
            caused_by=caused_by,
            cascade=cascade,
            reason=reason,
            new=Snapshot(
                peer_id=to_id,
                peer_kind=to_kind,
                peer_edge_kind=edge_kind,
                edge_priority=priority,
                edge_note=audit_note,
                edge_valence=valence,
                edge_labels=edge_labels,
            ),
        )
        await self.emit_change(
            conn,
            api_key_id=api_key_id,
            actor=actor,
            subject_id=to_id,
            subject_kind=to_kind,
            kind="edge_added",
            caused_by=cause,
            cascade=False,
            reason=reason,
            new=Snapshot(
                peer_id=from_id,
                peer_kind=from_kind,
                peer_edge_kind=edge_kind,
                edge_priority=priority,
                edge_note=audit_note,
                edge_valence=valence,
                edge_labels=edge_labels,
            ),
        )
        await self._infer_produced_on_conn(
            conn,
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            caused_by=cause,
            api_key_id=api_key_id,
            actor=actor,
        )
        return cause, True

    async def _infer_produced_on_conn(
        self,
        conn: Conn,
        *,
        from_id: UUID,
        to_id: UUID,
        edge_kind: Edge.Kind,
        caused_by: UUID | None,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
    ) -> None:
        """Stamp ``younger produced_by older`` when the pair's edges warrant it.

        The definition of provenance (see :attr:`Inquiry.produced_by`): the first
        edge between two vertices infers that the younger was produced by the
        older. :func:`infer_produced_endpoints` reads the whole pair-edge set and
        applies the precedence/suppression rules, so this runs after every real
        edge insert and lets that helper decide whether to stamp. The stored
        ``produced_by`` edge points child -> parent (from=produced, to=producer).
        The inferred edge commits on the same transaction, so it lands
        atomically with its trigger.

        A direct ``produced_by`` insert short-circuits here so the inferred edge
        never re-enters the rule. ``caused_by`` chains the inferred edge's audit
        to the triggering edge's change, and ``cascade=False`` keeps the
        inference from doubling the triggering action's ancestor cascade.
        """
        if edge_kind == "produced_by":
            return
        endpoints = await infer_produced_endpoints(conn, from_id=from_id, to_id=to_id)
        if endpoints is None:
            return
        producer_id, produced_id, winner = endpoints
        await self._add_edge_on_conn(
            conn,
            # produced_by is stored child -> parent: from = produced (younger),
            # to = producer (older).
            from_id=produced_id,
            to_id=producer_id,
            edge_kind="produced_by",
            priority=None,
            note="",
            valence=None,
            labels=(),
            reason=f"{INFERRED_PROVENANCE_REASON}: first edge ({winner}) between the pair",
            caused_by=caused_by,
            cascade=False,
            api_key_id=api_key_id,
            actor=actor,
        )

    async def insert_edge_and_audit(
        self,
        conn: Conn,
        *,
        subject_id: UUID,
        subject_kind: Inquiry.InquiryKind,
        to_id: UUID,
        edge_kind: Edge.Kind,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
        caused_by: UUID,
        priority: Issue.Priority | None = None,
        valence: float | None = None,
        require_to_kind: Inquiry.InquiryKind | None = None,
        cite_peer_as_from: bool = False,
    ) -> bool:
        """Insert an edge and emit ``edge_added`` on both endpoints.

        Returns ``True`` iff the edge was newly inserted. Submit-time
        wiring uses this so the ``change_log`` carries every initial
        decomposition / citation / sequencing edge as paired first-class
        events on both endpoints, chained to ``caused_by``.

        ``require_to_kind`` lets callers (Belief citations, WebSearch
        results) verify a wire-declared kind against the actual stored
        kind without a separate ``lookup_kind`` round-trip.

        ``valence`` is the signed citation weight on ``proves``/``favors``
        edges; ``None`` for structural edges leaves the column NULL.

        ``cite_peer_as_from`` stores the edge as ``peer (to_id) -> subject``
        instead of ``subject -> peer``. Citations need it: ``proves``/``favors``
        store Artifact -> Belief, so the citing Artifact (the ``to_id`` peer) is
        the from-side. The paired audit is endpoint-symmetric, so only the
        stored row's direction (and which endpoint ``require_to_kind``
        validates) changes.
        """
        # ``to_id`` is always the cited peer regardless of stored direction;
        # ``cite_peer_as_from`` only chooses which physical column it lands in.
        if cite_peer_as_from:
            peer_kind = await lookup_kind(conn, to_id)
            from_id, from_kind, store_to_id = to_id, peer_kind, subject_id
        else:
            from_id, from_kind, store_to_id = subject_id, subject_kind, to_id
        inserted, store_to_kind = await insert_edge(
            conn,
            from_id=from_id,
            from_kind=from_kind,
            to_id=store_to_id,
            edge_kind=edge_kind,
            priority=priority,
            valence=valence,
        )
        # The cited peer's actual kind: the looked-up from-kind when swapped,
        # else the row's to-kind. Validate the wire-declared kind against it.
        to_kind = from_kind if cite_peer_as_from else store_to_kind
        if require_to_kind is not None and to_kind != require_to_kind:
            raise ConflictError(
                f"citation {to_id} declared as {require_to_kind} but is a {to_kind}"
            )
        if not inserted:
            # The edge already exists: a submit-time citation always carries a
            # concrete valence (priority for sequencing edges), so upsert it onto
            # the existing row rather than silently dropping the caller's value.
            # Symmetric with ``_add_edge_on_conn``'s create-is-upsert behavior.
            await self._set_edge_annotation_on_conn(
                conn,
                from_id=from_id,
                to_id=store_to_id,
                edge_kind=edge_kind,
                priority=priority if priority is not None else ABSENT,
                valence=valence if valence is not None else ABSENT,
                reason="",
                require_existing=False,
                api_key_id=api_key_id,
                actor=actor,
            )
            return False
        from_cause, _ = await self.emit_change(
            conn,
            api_key_id=api_key_id,
            actor=actor,
            subject_id=subject_id,
            subject_kind=subject_kind,
            kind="edge_added",
            caused_by=caused_by,
            new=Snapshot(
                peer_id=to_id,
                peer_kind=to_kind,
                peer_edge_kind=edge_kind,
                edge_priority=priority,
                edge_note="",
                edge_valence=valence,
                edge_labels=(),
            ),
        )
        await self.emit_change(
            conn,
            api_key_id=api_key_id,
            actor=actor,
            subject_id=to_id,
            subject_kind=to_kind,
            kind="edge_added",
            caused_by=from_cause,
            cascade=False,
            new=Snapshot(
                peer_id=subject_id,
                peer_kind=subject_kind,
                peer_edge_kind=edge_kind,
                edge_priority=priority,
                edge_note="",
                edge_valence=valence,
                edge_labels=(),
            ),
        )
        await self._infer_produced_on_conn(
            conn,
            from_id=from_id,
            to_id=store_to_id,
            edge_kind=edge_kind,
            caused_by=from_cause,
            api_key_id=api_key_id,
            actor=actor,
        )
        return True

    async def add_edge(
        self,
        *,
        from_id: UUID,
        to_id: UUID,
        edge_kind: Edge.Kind,
        priority: Issue.Priority | None = None,
        note: str = "",
        valence: float | None = None,
        labels: Sequence[str] = (),
        reason: str = "",
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
        conn: Conn | None = None,
    ) -> tuple[UUID | None, bool]:
        """Upsert one edge and emit its audits, returning ``(change_id, created)``.

        Creation is an upsert (symmetric with ``label add`` and every other
        set): a new edge inserts and emits paired ``edge_added`` audits; an
        existing edge has any supplied annotations applied (``created=False``).
        ``change_id`` is ``None`` for a pure no-op (existing edge, nothing to
        set).

        On an existing edge, supplied annotations are SET, not merged or
        cleared:

        * a supplied ``labels`` list REPLACES the existing labels wholesale
          (use :meth:`add_edge_label` / :meth:`remove_edge_label` to append /
          remove a single label);
        * ``None`` / empty ``priority`` / ``valence`` / ``note`` means
          "unset -- leave unchanged", NOT "clear to NULL"; clear an annotation
          via :meth:`set_edge_annotation` (the DELETE route passes ``None``).

        When ``conn`` is ``None`` this owns its transaction; when supplied
        the insert joins the caller's open transaction (e.g.
        :meth:`submit_batch` wiring create-time edges atomically with the
        new rows). The caller then owns ``tx`` / ``notify_after_commit``.
        """
        if conn is not None:
            return await self._add_edge_on_conn(
                conn,
                from_id=from_id,
                to_id=to_id,
                edge_kind=edge_kind,
                priority=priority,
                note=note,
                valence=valence,
                labels=labels,
                reason=reason,
                api_key_id=api_key_id,
                actor=actor,
            )
        async with (
            notify_after_commit(),
            self.engine.acquire() as own_conn,
            tx(own_conn),
        ):
            return await self._add_edge_on_conn(
                own_conn,
                from_id=from_id,
                to_id=to_id,
                edge_kind=edge_kind,
                priority=priority,
                note=note,
                valence=valence,
                labels=labels,
                reason=reason,
                api_key_id=api_key_id,
                actor=actor,
            )

    async def set_edge_annotation(
        self,
        *,
        from_id: UUID,
        to_id: UUID,
        edge_kind: Edge.Kind,
        priority: Issue.Priority | Absent | None = ABSENT,
        note: str | Absent | None = ABSENT,
        valence: float | Absent | None = ABSENT,
        labels: Sequence[str] | Absent | None = ABSENT,
        labels_delta: tuple[str, bool] | None = None,
        reason: str = "",
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Partial-update the annotation on an existing edge.

        Each annotation parameter is three-state: omit the kwarg (or
        pass :data:`ABSENT`) to leave the edge's current value, pass
        an explicit value to set it, or pass ``None`` to clear it to
        SQL NULL. The per-field edge routes in ``api/edge.py`` set one
        annotation per call (others ABSENT): a ``PUT`` passes the field's
        value, a ``DELETE`` passes ``None`` to clear it.

        ``labels_delta`` is the atomic single-label add/remove path:
        ``(label, include)`` applies ``add``/``discard`` to the labels
        read *under this method's row lock*, so concurrent ``PATCH``
        adds can't lost-update each other. Mutually exclusive with the
        whole-list ``labels`` overwrite.
        """
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            return await self._set_edge_annotation_on_conn(
                conn,
                from_id=from_id,
                to_id=to_id,
                edge_kind=edge_kind,
                priority=priority,
                note=note,
                valence=valence,
                labels=labels,
                labels_delta=labels_delta,
                reason=reason,
                require_existing=True,
                api_key_id=api_key_id,
                actor=actor,
            )

    async def _set_edge_annotation_on_conn(
        self,
        conn: Conn,
        *,
        from_id: UUID,
        to_id: UUID,
        edge_kind: Edge.Kind,
        priority: Issue.Priority | Absent | None = ABSENT,
        note: str | Absent | None = ABSENT,
        valence: float | Absent | None = ABSENT,
        labels: Sequence[str] | Absent | None = ABSENT,
        labels_delta: tuple[str, bool] | None = None,
        reason: str = "",
        require_existing: bool = True,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Annotate an existing edge on an already-open transaction.

        The shared core of :meth:`set_edge_annotation` (its own tx) and the
        upsert arm of :meth:`_add_edge_on_conn` (the caller's tx). When
        ``require_existing`` is ``True`` a missing edge raises
        :class:`NotFoundError`; the upsert caller passes ``False`` because it
        only reaches here after its own insert found the edge already present.
        """
        assert labels_delta is None or isinstance(labels, Absent), (
            "labels_delta (single add/remove) and labels (whole-list "
            "overwrite) are mutually exclusive"
        )
        # ``FOR UPDATE`` so a concurrent annotate / remove can't observe stale
        # state between this SELECT and the UPDATE.
        row = await conn.fetchrow(
            "SELECT from_kind, to_kind, priority, note, valence, labels "
            "FROM edges WHERE from_id = $1 AND to_id = $2 "
            "AND edge_kind = $3 FOR UPDATE",
            from_id,
            to_id,
            edge_kind,
        )
        if row is None:
            if require_existing:
                raise NotFoundError("edge not found")
            return None
        old_priority = cast(Issue.Priority | None, row["priority"])
        new_priority: Issue.Priority | None
        if isinstance(priority, Absent):
            new_priority = old_priority
        else:
            new_priority = priority
        validate_edge_priority(edge_kind, new_priority)
        old_note = cast(str | None, row["note"])
        old_valence = cast(float | None, row["valence"])
        old_labels: tuple[str, ...] | None = (
            None if row["labels"] is None else tuple(row["labels"] or ())
        )
        new_note: str | None
        if isinstance(note, Absent):
            new_note = old_note
        else:
            # Empty / whitespace note is absence: NULL, not '' (one encoding of
            # "unset", matching the inquiry write path).
            new_note = cast("str | None", empty_optional_to_none(note))
        # Normalize an explicitly-supplied valence through the single guard so
        # the annotation-edit path obeys the same invariant as create: a
        # structural edge rejects a valence (clean 4xx, not a DB CHECK 500); a
        # citation can never be cleared to NULL (an explicit ``None`` resets to
        # the default). An ABSENT (unchanged) valence keeps the stored value
        # verbatim -- it must NOT heal-on-touch, or a label/note edit on a
        # legacy NULL-valence citation would silently rewrite valence to the
        # default and attribute a phantom valence change to the mutator. Heal
        # legacy NULLs via a one-shot data migration instead.
        new_valence: float | None
        if isinstance(valence, Absent):
            new_valence = old_valence
        else:
            new_valence = validate_edge_valence(edge_kind, valence)
        new_labels: tuple[str, ...] | None
        if labels_delta is not None:
            raw_label, include = labels_delta
            # Canonicalize the single label the SAME way whole-list writes do
            # (strip / drop-blanks), so ``label add "  foo  "`` and a stored
            # ``foo`` are one entry and ``label del "  foo  "`` actually removes
            # it. A blank label is no label -- a caller error, not a silent no-op.
            normalized = canonical_strs((raw_label,))
            if len(normalized) != 1:
                raise ValidationError("edge label cannot be empty")
            label = normalized[0]
            # Preserve insertion order: an add appends a not-yet-present label to
            # the end, a remove filters it out. Sorting here would silently
            # reorder an edge's existing labels on every single-label mutation.
            current: list[str] = list(old_labels or ())
            if include:
                if label not in current:
                    current.append(label)
            else:
                current = [existing for existing in current if existing != label]
            # Removing the last label leaves the empty list -> NULL.
            new_labels = cast(
                "tuple[str, ...] | None",
                empty_optional_to_none(canonical_strs(current)),
            )
        elif isinstance(labels, Absent):
            new_labels = old_labels
        elif labels is None:
            new_labels = None
        else:
            new_labels = cast(
                "tuple[str, ...] | None",
                empty_optional_to_none(canonical_strs(labels)),
            )
        if (
            old_priority == new_priority
            and old_note == new_note
            and old_valence == new_valence
            and old_labels == new_labels
        ):
            return None
        from_kind = cast(Inquiry.InquiryKind, row["from_kind"])
        to_kind = cast(Inquiry.InquiryKind, row["to_kind"])
        await conn.execute(
            "UPDATE edges SET priority = $1, note = $2, valence = $3, "
            "labels = $4 WHERE from_id = $5 AND to_id = $6 AND edge_kind = $7",
            new_priority,
            new_note,
            new_valence,
            None if new_labels is None else list(new_labels),
            from_id,
            to_id,
            edge_kind,
        )
        cause, _ = await self.emit_change(
            conn,
            api_key_id=api_key_id,
            actor=actor,
            subject_id=from_id,
            subject_kind=from_kind,
            kind="edge_annotation_changed",
            reason=reason,
            old=Snapshot(
                peer_id=to_id,
                peer_kind=to_kind,
                peer_edge_kind=edge_kind,
                edge_priority=old_priority,
                edge_note=old_note,
                edge_valence=old_valence,
                edge_labels=old_labels,
            ),
            new=Snapshot(
                peer_id=to_id,
                peer_kind=to_kind,
                peer_edge_kind=edge_kind,
                edge_priority=new_priority,
                edge_note=new_note,
                edge_valence=new_valence,
                edge_labels=new_labels,
            ),
        )
        await self.emit_change(
            conn,
            api_key_id=api_key_id,
            actor=actor,
            subject_id=to_id,
            subject_kind=to_kind,
            kind="edge_annotation_changed",
            caused_by=cause,
            cascade=False,
            reason=reason,
            old=Snapshot(
                peer_id=from_id,
                peer_kind=from_kind,
                peer_edge_kind=edge_kind,
                edge_priority=old_priority,
                edge_note=old_note,
                edge_valence=old_valence,
                edge_labels=old_labels,
            ),
            new=Snapshot(
                peer_id=from_id,
                peer_kind=from_kind,
                peer_edge_kind=edge_kind,
                edge_priority=new_priority,
                edge_note=new_note,
                edge_valence=new_valence,
                edge_labels=new_labels,
            ),
        )
        return cause

    async def get_edge(
        self,
        *,
        from_id: UUID,
        to_id: UUID,
        edge_kind: Edge.Kind,
    ) -> Edge | None:
        """Fetch one edge by its ``(from_id, edge_kind, to_id)`` identity.

        Returns ``None`` when no such edge exists. Backs the read route
        ``GET /api/edges/<from>/<kind>/<to>`` (``docs/api.md`` 1.8).
        """
        async with self.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT from_id, from_kind, to_id, to_kind, edge_kind, "
                "priority, note, valence, labels FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = $3",
                from_id,
                to_id,
                edge_kind,
            )
        return None if row is None else Edge.from_row(row)

    async def add_edge_label(
        self,
        *,
        from_id: UUID,
        to_id: UUID,
        edge_kind: Edge.Kind,
        label: str,
        reason: str = "",
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Add ``label`` to an edge's labels; idempotent (no-op if present)."""
        return await self._mutate_edge_label(
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            label=label,
            include=True,
            reason=reason,
            api_key_id=api_key_id,
            actor=actor,
        )

    async def remove_edge_label(
        self,
        *,
        from_id: UUID,
        to_id: UUID,
        edge_kind: Edge.Kind,
        label: str,
        reason: str = "",
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Remove ``label`` from an edge's labels; idempotent (no-op if absent)."""
        return await self._mutate_edge_label(
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            label=label,
            include=False,
            reason=reason,
            api_key_id=api_key_id,
            actor=actor,
        )

    async def _mutate_edge_label(
        self,
        *,
        from_id: UUID,
        to_id: UUID,
        edge_kind: Edge.Kind,
        label: str,
        include: bool,
        reason: str = "",
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically add or remove one edge label.

        Delegates to ``set_edge_annotation``'s ``labels_delta`` path so
        the read-modify-write of the label set happens under that
        method's ``FOR UPDATE`` row lock in a single transaction; two
        concurrent adds can't lost-update each other. Idempotent: a
        no-op when the label is already present (add) or absent (remove),
        matching the ``PATCH`` semantics in ``docs/api.md`` 1.12.
        """
        return await self.set_edge_annotation(
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            labels_delta=(label, include),
            reason=reason,
            api_key_id=api_key_id,
            actor=actor,
        )

    async def remove_edge(
        self,
        *,
        from_id: UUID,
        to_id: UUID,
        edge_kind: Edge.Kind,
        reason: str = "",
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            # FOR UPDATE blocks a concurrent annotate/remove from seeing
            # stale state between this SELECT and the DELETE.
            row = await conn.fetchrow(
                "SELECT from_kind, to_kind, priority, note, valence, labels "
                "FROM edges WHERE from_id = $1 AND to_id = $2 "
                "AND edge_kind = $3 FOR UPDATE",
                from_id,
                to_id,
                edge_kind,
            )
            if row is None:
                return None
            from_kind = cast(Inquiry.InquiryKind, row["from_kind"])
            to_kind = cast(Inquiry.InquiryKind, row["to_kind"])
            priority = cast(Issue.Priority | None, row["priority"])
            note = cast(str | None, row["note"])
            valence = cast(float | None, row["valence"])
            edge_labels = None if row["labels"] is None else tuple(row["labels"] or ())
            # Capture every edge touching ``from_id`` before the DELETE.
            # The cascade re-walks ``edges`` live, so deleting first would
            # drop the removed edge: ``_parent_edges`` would miss it and the
            # dependent endpoint (notably the broader Issue on a
            # ``narrows`` removal) never gets its ``dependency_changed``
            # alert. Mirrors ``purge()``.
            captured_edges = await conn.fetch(
                "SELECT from_id, from_kind, to_id, to_kind, edge_kind, "
                "priority, note, valence, labels FROM edges "
                "WHERE from_id = $1 OR to_id = $1",
                from_id,
            )
            await conn.execute(
                "DELETE FROM edges WHERE from_id = $1 AND to_id = $2 "
                "AND edge_kind = $3",
                from_id,
                to_id,
                edge_kind,
            )
            cause, _ = await self.emit_change(
                conn,
                api_key_id=api_key_id,
                actor=actor,
                subject_id=from_id,
                subject_kind=from_kind,
                kind="edge_removed",
                reason=reason,
                old=Snapshot(
                    peer_id=to_id,
                    peer_kind=to_kind,
                    peer_edge_kind=edge_kind,
                    edge_priority=priority,
                    edge_note=note,
                    edge_valence=valence,
                    edge_labels=edge_labels,
                ),
                cascade=False,
            )
            # Drive the cascade ourselves with the pre-DELETE edge list
            # so the deleted edge's dependent endpoint still gets the
            # alert.
            await self._cascade_dependency_changed(
                conn,
                child_id=from_id,
                child_kind=from_kind,
                caused_by=cause,
                edge_rows=captured_edges,
            )
            await self.emit_change(
                conn,
                api_key_id=api_key_id,
                actor=actor,
                subject_id=to_id,
                subject_kind=to_kind,
                kind="edge_removed",
                caused_by=cause,
                cascade=False,
                reason=reason,
                old=Snapshot(
                    peer_id=from_id,
                    peer_kind=from_kind,
                    peer_edge_kind=edge_kind,
                    edge_priority=priority,
                    edge_note=note,
                    edge_valence=valence,
                    edge_labels=edge_labels,
                ),
            )
            return cause
