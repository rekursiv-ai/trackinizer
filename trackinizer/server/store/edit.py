""":class:`_EditMixin` -- per-field and per-list inquiry edits.

Every ``set_X`` setter delegates into :meth:`_set_field` (single editable
column) or :meth:`_mutate_list_field` (list-valued column), which run the
lock -> compare -> validate -> update -> audit -> notify pipeline once.
Cost edits (:meth:`add_cost`, :meth:`set_cost_axis`) route through the same
audited ``marginal_cost`` emit on :class:`_CascadeAuditMixin`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

import asyncpg

from trackinizer.lib.postgres import Conn
from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.server.primitives import (
    lookup_kind,
    upsert_embedding,
    validate_list_references,
)
from trackinizer.server.setter_dispatch import (
    COLUMN_SPECS,
    NO_HOOKS,
    RUNTIME_HOOKS,
)
from trackinizer.server.store.cascade import _CascadeAuditMixin
from trackinizer.server.values import (
    empty_optional_to_none,
    vetted_sql,
)
from trackinizer.types.change_log import Change, Snapshot
from trackinizer.types.cost import Cost
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.inquiries import (
    Belief,
    Inquiry,
    Issue,
    Paper,
    is_valid_source,
)


__all__ = [
    "_EditMixin",
]


class _EditMixin(_CascadeAuditMixin):
    """Per-field and per-list inquiry edits for :class:`Store`."""

    async def set_title(
        self,
        target_id: UUID,
        value: str,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        if not value.strip():
            # Mirror ``SubmitBase.title`` / ``EditTitle.title``
            # ``min_length=1`` + non-blank validator so a direct Store caller
            # can't blank a required field that the route validates. A
            # whitespace-only title is no title.
            raise ConflictError("title cannot be empty")
        # Embed *before* opening the transaction. The submit path does
        # the same: a network embedder would otherwise hold the row's
        # ``FOR UPDATE`` lock for the round-trip, blocking every other
        # writer touching this Inquiry. The cost of a wasted embed on
        # the rare dedup-hit is dominated by the lock-contention win.
        new_vecs = await self._embed_all(value)
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            row = await self._read_field(conn, target_id, "title")
            if row["title"] == value:
                return None
            await self._update_field(conn, target_id, "title", value)
            for model, vec in new_vecs:
                await upsert_embedding(conn, target_id, model, vec)
            change_id, _ = await self._emit_field_change(
                conn,
                target_id,
                row["kind"],
                "title",
                Snapshot(title=row["title"]),
                Snapshot(title=value),
                api_key_id=api_key_id,
                actor=actor,
            )
            return change_id

    async def _read_field(
        self,
        conn: Conn,
        target_id: UUID,
        column: str,
        *,
        expected_kinds: frozenset[Inquiry.InquiryKind] | None = None,
    ) -> asyncpg.Record:
        """Read ``<column>, kind`` for one inquiry under a row lock.

        ``FOR UPDATE`` is load-bearing: every ``set_X`` reads then
        decides whether to write; without the lock a concurrent purge
        or competing edit between the two steps races into a
        lost-update or a phantom audit row against a deleted subject.

        Args:
          conn: Active connection inside a transaction.
          target_id: Row to read.
          column: Single inquiries column to read alongside ``kind``.
          expected_kinds: Closed-set gate. When non-None, the row's
            ``kind`` must be in this set or a :class:`ConflictError`
            is raised before the caller can attempt a schema-forbidden
            UPDATE.

        Returns:
          row: Record with ``column`` and ``kind`` populated.

        Raises:
          NotFoundError: Subject is missing.
          ConflictError: Subject's kind is outside ``expected_kinds``.

        """
        row = await conn.fetchrow(
            vetted_sql(
                "SELECT ", column, ", kind FROM inquiries WHERE id = $1 FOR UPDATE"
            ),
            target_id,
        )
        if row is None:
            raise NotFoundError(f"inquiry {target_id} not found")
        if expected_kinds is not None and row["kind"] not in expected_kinds:
            raise ConflictError(
                f"inquiry {target_id} is a {row['kind']}; "
                f"{column} is only valid on {sorted(expected_kinds)}"
            )
        return row

    async def _update_field(
        self,
        conn: Conn,
        target_id: UUID,
        column: str,
        value: Any,
    ) -> None:
        """UPDATE one inquiries column + bump modified.

        Re-enforces ``ColumnSpec.immutable`` at the root write path so
        any future setter that bypasses :meth:`_set_field` (admin
        tools, bulk migrations) still cannot silently mutate
        immutable columns; the only correction path remains
        supersession.
        """
        spec = COLUMN_SPECS[column]
        if spec.immutable:
            raise ConflictError(
                f"{column!r} is immutable; correct by supersession "
                "(submit a fresh row), not in-place edit"
            )
        # The one place "unset is NULL" is enforced for every UPDATE path:
        # _set_field, the list add/remove path, and set_source all funnel
        # here, so a nullable column never stores an empty sentinel
        # regardless of which setter wrote it. Required columns keep their
        # value (their own guards reject empties upstream). A ``min_items``
        # column (``issue_kind``) is exempt: emptying it must hit the DB
        # CHECK (you supersede the row, not blank the field), so an empty
        # value is left to fail rather than collapsed to a passing NULL.
        if not spec.required and spec.min_items == 0:
            value = empty_optional_to_none(value)
        await conn.execute(
            vetted_sql(
                "UPDATE inquiries SET ",
                column,
                " = $1, modified = clock_timestamp() WHERE id = $2",
            ),
            value,
            target_id,
        )

    async def _emit_field_change(
        self,
        conn: Conn,
        target_id: UUID,
        subject_kind: Inquiry.InquiryKind,
        change_kind: Change.Kind,
        old: Snapshot,
        new: Snapshot,
        *,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
        reason: str = "",
        extra_subscribers: tuple[Inquiry.Actor, ...] = (),
        caused_by: UUID | None = None,
    ) -> tuple[UUID, tuple[str, ...]]:
        """Tiny convenience to keep set_X call shape readable.

        Returns ``(change_id, subscribers)`` mirroring
        :meth:`emit_change` so callers can post-commit notify with the
        subscriber list captured at change-emit time.
        """
        return await self.emit_change(
            conn,
            caused_by=caused_by,
            api_key_id=api_key_id,
            actor=actor,
            subject_id=target_id,
            subject_kind=subject_kind,
            kind=change_kind,
            old=old,
            new=new,
            reason=reason,
            extra_subscribers=extra_subscribers,
        )

    async def set_description(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="description",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def _set_field(
        self,
        target_id: UUID,
        value: Any,
        *,
        column: str,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> UUID | None:
        """Drive one editable column through lock -> compare -> validate
        -> update -> audit -> notify.

        The single mutation pipeline that every ``set_X`` setter
        delegates into. Replaces 15 hand-written copies of the same
        five-step recipe -- previously every setter independently
        composed the lock, the dedup, the optional validator, the
        ``_update_field`` call, the snapshot pair, and the
        ``_emit_field_change``. One missed step in any of them produced
        a Disease-C bug. Now each setter is one line that names the
        column; the pipeline runs from :class:`ColumnSpec` metadata on
        the dataclass plus the matching :data:`RUNTIME_HOOKS` entry.

        Args:
          target_id: Inquiry to mutate.
          value: New value, in whatever shape the public setter
            accepts (e.g. ``Sequence[Inquiry.Actor]`` for ``subscribers``).
          column: inquiries column name; resolves to a
            :class:`ColumnSpec` via :data:`COLUMN_SPECS` and
            (optionally) a behavioral override via :data:`RUNTIME_HOOKS`.
          api_key_id: Server-stamped ``api_keys.id`` of the credential
            used by the authenticated caller; ``None`` for tests /
            programmatic callers without an auth context.
          actor: Free-form audit string recorded on the emitted change
            row. See the Auth section of ``docs/design.md`` for the credential-vs-actor
            split.
          reason: Optional free-form reason; only forwarded when the
            spec's ``supports_reason`` is true (status, judgement,
            confidence).

        """
        spec = COLUMN_SPECS[column]
        if spec.immutable:
            raise ConflictError(
                f"{column!r} is immutable; correct by supersession "
                "(submit a fresh row), not in-place edit"
            )
        hooks = RUNTIME_HOOKS.get(column, NO_HOOKS)
        expected = (
            cast(frozenset[Inquiry.InquiryKind], spec.applies_to_inquiry_kinds)
            if spec.applies_to_inquiry_kinds is not None
            else None
        )
        # Unset is one encoding: SQL NULL. Collapse the *normalized* value --
        # the normalizer (``canonical_strs``) drops blanks, so a non-empty
        # input like ``["", "  "]`` becomes empty only after normalization.
        # Collapsing here, after normalize, keeps the no-op dedup and the audit
        # snapshot below in agreement with what ``_update_field`` stores.
        # ``empty_optional_to_none`` leaves a falsy-but-valid scalar
        # (issue_priority 0, belief_confidence 0.0) untouched; required and
        # ``min_items`` columns are exempt so an empty value reaches their
        # guards / DB CHECK instead of collapsing. Closed-set Literal columns
        # (``belief_judgement``, ``paper_venue``) take this collapse for
        # a blank string -- ``""`` is not a valid member, so "blank clears to
        # NULL" is the only sensible reading; the typed setter signature plus
        # wire-layer validation reject ``""`` before it reaches here, so no
        # runtime guard is added for an input the type system already forbids.
        collapsible = not spec.required and spec.min_items == 0
        if value is None:
            new_value = None
            storage_value = None
        else:
            new_value = hooks.normalize(value)
            if collapsible and empty_optional_to_none(new_value) is None:
                new_value = None
                storage_value = None
            else:
                storage_value = hooks.encode(new_value)
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            row = await self._read_field(
                conn, target_id, column, expected_kinds=expected
            )
            old_value = hooks.decode_old(row[column])
            if old_value == new_value:
                return None
            if hooks.validate is not None:
                await hooks.validate(conn, new_value)
            # A DB CHECK violation (e.g. emptying a ``min_items`` column like
            # ``issue_kind``) surfaces to callers as a clean ``ConflictError``,
            # mirroring the atomic list-mutation path, not a raw 500.
            try:
                await self._update_field(conn, target_id, column, storage_value)
            except asyncpg.CheckViolationError as exc:
                raise ConflictError(
                    f"check constraint violated: {exc.detail or exc!s}"
                ) from exc
            # ``old_value`` is None when subscribers was unset (NULL); the
            # notify fan-out wants the concrete pre-edit set, so coalesce.
            extra_subs = (
                cast(tuple[Inquiry.Actor, ...], old_value or ())
                if hooks.notify_old_subscribers
                else ()
            )
            change_id, _ = await self._emit_field_change(
                conn,
                target_id,
                row["kind"],
                cast(Change.Kind, column),
                Snapshot(**{column: old_value}),
                Snapshot(**{column: new_value}),
                api_key_id=api_key_id,
                actor=actor,
                reason=reason if spec.supports_reason else "",
                extra_subscribers=extra_subs,
            )
            return change_id

    async def set_owner(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="owner",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_account(
        self,
        target_id: UUID,
        value: Inquiry.Actor,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Re-point the row's account to ``value``.

        ``account`` is required (NOT NULL): unlike ``owner`` there is no
        clear-to-NULL path. This setter does NOT itself check that ``value``
        is an active user -- that is an authorization concern enforced at the
        route boundary (``api/edit.py`` via ``auth.assert_account_active``),
        where the authenticated identity and ``users.status`` are in scope.
        The Store guarantees the column stays POPULATED: a blank / whitespace-only
        account is rejected here (the NOT NULL column accepts ``''``, which is not
        a real account), so a direct Store caller cannot blank a required field
        the route validates.

        Raises:
          ConflictError: ``value`` is empty or whitespace-only.

        """
        if not value.strip():
            raise ConflictError("account cannot be empty")
        return await self._set_field(
            target_id,
            value,
            column="account",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_status(
        self,
        target_id: UUID,
        value: Inquiry.Status,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="status",
            api_key_id=api_key_id,
            actor=actor,
            reason=reason,
        )

    async def transition_status(
        self,
        target_id: UUID,
        *,
        expected_from: Inquiry.Status,
        to: Inquiry.Status,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> UUID | None:
        """Compare-and-set status: refuse if current ≠ ``expected_from``.

        Same race-class as the OG ``subscribe_self``: the CLI's
        ``close`` / ``done`` / ``wontfix`` / ``invalidate`` commands
        all want "transition this Issue from active to <terminal>",
        but ``set_status`` unconditionally overwrites. Two agents
        racing to close the same Issue both succeed; if one meant
        ``complete`` and one meant ``abandoned``, last writer wins.
        ``transition_status`` holds the row lock across the read of
        ``status`` and the UPDATE, so the second writer sees the
        post-edit state and gets ``ConflictError``.
        """
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            row = await self._read_field(conn, target_id, "status")
            current = cast(Inquiry.Status, row["status"])
            if current != expected_from:
                raise ConflictError(
                    f"status transition rejected: expected {expected_from!r}, "
                    f"found {current!r}"
                )
            if expected_from == to:
                return None
            await self._update_field(conn, target_id, "status", to)
            change_id, _ = await self._emit_field_change(
                conn,
                target_id,
                row["kind"],
                "status",
                Snapshot(status=current),
                Snapshot(status=to),
                api_key_id=api_key_id,
                actor=actor,
                reason=reason,
            )
            return change_id

    async def set_judgement(
        self,
        target_id: UUID,
        value: Belief.Judgement | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="belief_judgement",
            api_key_id=api_key_id,
            actor=actor,
            reason=reason,
        )

    async def transition_judgement(
        self,
        target_id: UUID,
        *,
        expected_from: Belief.Judgement | None,
        to: Belief.Judgement | None,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> UUID | None:
        """Compare-and-set judgement: refuse if current != ``expected_from``.

        The judgement analogue of :meth:`transition_status`: two
        reviewers racing to record a verdict on the same Belief would
        otherwise silently clobber each other (last writer wins). Holding
        the row lock across the read of ``judgement`` and the UPDATE makes
        the second writer observe the post-edit state and get a
        :class:`ConflictError`.
        """
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            row = await self._read_field(
                conn,
                target_id,
                "belief_judgement",
                expected_kinds=frozenset({"Belief"}),
            )
            current = cast("Belief.Judgement | None", row["belief_judgement"])
            if current != expected_from:
                raise ConflictError(
                    f"judgement transition rejected: expected {expected_from!r}, "
                    f"found {current!r}"
                )
            if expected_from == to:
                return None
            await self._update_field(conn, target_id, "belief_judgement", to)
            change_id, _ = await self._emit_field_change(
                conn,
                target_id,
                row["kind"],
                "belief_judgement",
                Snapshot(belief_judgement=current),
                Snapshot(belief_judgement=to),
                api_key_id=api_key_id,
                actor=actor,
                reason=reason,
            )
            return change_id

    async def set_confidence(
        self,
        target_id: UUID,
        value: float | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="belief_confidence",
            api_key_id=api_key_id,
            actor=actor,
            reason=reason,
        )

    async def set_issue_kind(
        self,
        target_id: UUID,
        value: Sequence[Issue.Kind] | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        if value is not None and not value:
            raise ConflictError("issue_kind must have at least one entry")
        return await self._set_field(
            target_id,
            value,
            column="issue_kind",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_validation(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="issue_validation",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_priority(
        self,
        target_id: UUID,
        value: Issue.Priority | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="issue_priority",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_outcome(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="experiment_outcome",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_config(
        self,
        target_id: UUID,
        value: dict[str, object] | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="experiment_config",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_abstract(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="paper_abstract",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_authors(
        self,
        target_id: UUID,
        value: Sequence[str] | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="paper_authors",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def add_author(
        self,
        target_id: UUID,
        author: str,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically append one author to a Paper's byline."""
        return await self._mutate_list_field(
            target_id,
            author,
            column="paper_authors",
            api_key_id=api_key_id,
            actor=actor,
            include=True,
        )

    async def _mutate_list_field(
        self,
        target_id: UUID,
        item: Any,
        *,
        column: str,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
        include: bool,
        validate_item: (Callable[[Conn], Awaitable[None]] | None) = None,
    ) -> UUID | None:
        """Generic atomic add/remove on a list-valued column.

        The race the OG ``subscribe_self`` fix prevented (GET inquiry
        → mutate list → POST edit, with two writers clobbering one
        another) shows up for every multi-valued column: ``subscribers``,
        ``labels``, ``issue_kind``, ``codechanges``. This driver
        consolidates the read-lock-decide-update-audit pipeline once;
        each public ``add_X``/``remove_X`` is one-line delegation.

        Args:
          target_id: Inquiry to mutate.
          item: The element to add or remove. Must be a member of the
            column's element type (a label string, an Issue.Kind, a
            CodeChange UUID, an actor id).
          column: ``COLUMN_SPECS`` key (e.g. ``"labels"``).
          api_key_id: Server-stamped ``api_keys.id`` of the credential
            used by the authenticated caller; ``None`` for tests /
            programmatic callers without an auth context.
          actor: Free-form audit string.
          include: True to add; False to remove.
          validate_item: Optional async predicate run against the
            connection before the UPDATE. Used by ``add_codechange``
            to verify the target UUID resolves to a CodeChange row.

        """
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            return await self._mutate_list_field_on_conn(
                conn,
                target_id,
                item,
                column=column,
                api_key_id=api_key_id,
                actor=actor,
                include=include,
                validate_item=validate_item,
            )

    async def _mutate_list_field_on_conn(
        self,
        conn: Conn,
        target_id: UUID,
        item: Any,
        *,
        column: str,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
        include: bool,
        validate_item: (Callable[[Conn], Awaitable[None]] | None) = None,
    ) -> UUID | None:
        """Atomic add/remove on a list column, on an already-open transaction.

        The shared core of :meth:`_mutate_list_field` (its own tx) and any
        caller already inside a transaction (e.g. :meth:`_resume_session`
        applying ``--resume`` rooms). Returns the change id, or ``None`` for an
        idempotent no-op (add-present / remove-absent).
        """
        spec = COLUMN_SPECS[column]
        hooks = RUNTIME_HOOKS.get(column, NO_HOOKS)
        expected = (
            cast(frozenset[Inquiry.InquiryKind], spec.applies_to_inquiry_kinds)
            if spec.applies_to_inquiry_kinds is not None
            else None
        )
        row = await self._read_field(conn, target_id, column, expected_kinds=expected)
        # ``old_snapshot`` preserves a stored NULL as None for the audit old side
        # (unset vs explicitly-cleared); ``working`` is the collapsed concrete
        # set the add/remove arithmetic operates on. Stay typed-loose for the
        # Snapshot spread below; ``_set_field`` uses the same pattern. Tightening
        # the annotation to a concrete element type makes basedpyright fail to
        # unify against every possible Snapshot field type.
        old_snapshot: Any = hooks.decode_old(row[column])
        # Normalize the stored set the same way the new item is normalized, so
        # add/remove membership is consistent: a stored ``"Smith "`` and an added
        # ``"Smith"`` are one byline entry, not two.
        working: tuple[Any, ...] = hooks.normalize(tuple(old_snapshot or ()))
        normalized = hooks.normalize((item,))
        if len(normalized) != 1:
            raise ConflictError(f"{column} item must be non-empty")
        normalized_item = normalized[0]
        # A byline (``paper_authors``) is an ordered list where duplicates are
        # significant: add always appends, remove drops only the first match.
        # Every other list column is a canonical set: add is a no-op when
        # present, remove drops every occurrence.
        if not spec.is_byline and include and normalized_item in working:
            return None
        if not include and normalized_item not in working:
            return None
        if validate_item is not None:
            await validate_item(conn)
        # Removing the last item leaves the empty set, which is absence: collapse
        # to None so the audit snapshot matches the NULL that ``_update_field``
        # stores (one encoding of "unset"). A ``min_items`` column
        # (``issue_kind``) is exempt -- the empty set must reach the DB CHECK and
        # raise, not become NULL.
        new_value: Any
        if include:
            new_value = (*working, normalized_item)
        else:
            if spec.is_byline:
                # Drop only the first occurrence: a repeated byline name is two
                # distinct contributors, so one removal clears one slot.
                idx = working.index(normalized_item)
                remainder = working[:idx] + working[idx + 1 :]
            else:
                remainder = tuple(x for x in working if x != normalized_item)
            new_value = (
                remainder
                if COLUMN_SPECS[column].min_items > 0
                else empty_optional_to_none(remainder)
            )
        # DB CHECK violations (e.g. ``array_length >= 1``) surface to direct
        # callers as a clean ``ConflictError``; route callers see the same shape
        # via the FastAPI handler.
        try:
            await self._update_field(
                conn,
                target_id,
                column,
                None if new_value is None else hooks.encode(new_value),
            )
        except asyncpg.CheckViolationError as exc:
            raise ConflictError(
                f"check constraint violated: {exc.detail or exc!s}"
            ) from exc
        extra_subs = working if hooks.notify_old_subscribers else ()
        change_id, _ = await self._emit_field_change(
            conn,
            target_id,
            row["kind"],
            cast(Change.Kind, column),
            # ``cast(Any, {...})`` because the kwargs spread can't be statically
            # matched against Snapshot's per-field types (the column name is only
            # known at runtime).
            Snapshot(**cast(Any, {column: old_snapshot})),
            Snapshot(**cast(Any, {column: new_value})),
            api_key_id=api_key_id,
            actor=actor,
            extra_subscribers=extra_subs,
        )
        return change_id

    async def remove_author(
        self,
        target_id: UUID,
        author: str,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically remove one author from a Paper's byline."""
        return await self._mutate_list_field(
            target_id,
            author,
            column="paper_authors",
            api_key_id=api_key_id,
            actor=actor,
            include=False,
        )

    async def set_publication_type(
        self,
        target_id: UUID,
        value: Paper.PublicationType | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="paper_publication_type",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_venue(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="paper_venue",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_subvenue(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="paper_subvenue",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_publish_date(
        self,
        target_id: UUID,
        value: datetime | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="paper_publish_date",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_source(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Edit a :class:`Paper`'s ``source`` -- the one self-describing
        identifier whose scheme prefix names its kind (``arXiv:``, ``doi:``,
        ``http(s)://``, ``isbn:``, ...).

        Enforces the same ``<scheme>:<rest>`` shape as the create boundary
        (``SubmitPaper``) so the rule holds on both paths; a bare value that
        drops the scheme is a clean ``ConflictError`` (4xx), not a silent write.
        """
        if value is not None and value.strip() and not is_valid_source(value):
            raise ConflictError(
                "source must be a scheme-tagged identifier '<scheme>:<rest>' "
                f"(e.g. arXiv:2405.16391, doi:10.1/x); got {value!r}"
            )
        return await self._set_field(
            target_id,
            value,
            column="paper_source",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_google_scholar_cluster_id(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Edit a :class:`Paper`'s ``google_scholar_cluster_id`` (Scholar
        data-cid; the paper's stable Scholar identity, present when indexed).

        A plain optional identifier -- no scheme validation (unlike ``source``);
        an empty value clears it to NULL through ``_set_field``.
        """
        return await self._set_field(
            target_id,
            value,
            column="paper_google_scholar_cluster_id",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_google_scholar_cites_id(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Edit a :class:`Paper`'s ``google_scholar_cites_id`` (Scholar cites_id;
        the cited-by pivot handle, present only once the paper has citations).

        A plain optional identifier -- no scheme validation (unlike ``source``);
        an empty value clears it to NULL through ``_set_field``.
        """
        return await self._set_field(
            target_id,
            value,
            column="paper_google_scholar_cites_id",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_query(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="websearch_query",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_provider(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="websearch_provider",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_labels(
        self,
        target_id: UUID,
        value: Sequence[str] | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="labels",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_subscribers(
        self,
        target_id: UUID,
        value: Sequence[Inquiry.Actor],
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="subscribers",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def add_subscriber(
        self,
        target_id: UUID,
        subscriber: Inquiry.Actor,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically add ``subscriber`` to ``target_id``'s subscribers.

        ``subscriber`` may differ from ``actor`` (the actor performing
        the change). The self-subscribe convenience is the CLI
        ``trax watch`` verb, which passes ``subscriber=actor``.
        """
        return await self._mutate_list_field(
            target_id,
            subscriber,
            column="subscribers",
            api_key_id=api_key_id,
            actor=actor,
            include=True,
        )

    async def remove_subscriber(
        self,
        target_id: UUID,
        subscriber: Inquiry.Actor,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically remove ``subscriber`` from ``target_id``'s subscribers.

        Mirrors :meth:`add_subscriber`; ``subscriber`` may differ from
        ``actor``.
        """
        return await self._mutate_list_field(
            target_id,
            subscriber,
            column="subscribers",
            api_key_id=api_key_id,
            actor=actor,
            include=False,
        )

    async def add_label(
        self,
        target_id: UUID,
        label: str,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically add one label."""
        return await self._mutate_list_field(
            target_id,
            label,
            column="labels",
            api_key_id=api_key_id,
            actor=actor,
            include=True,
        )

    async def remove_label(
        self,
        target_id: UUID,
        label: str,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically remove one label."""
        return await self._mutate_list_field(
            target_id,
            label,
            column="labels",
            api_key_id=api_key_id,
            actor=actor,
            include=False,
        )

    async def add_issue_kind(
        self,
        target_id: UUID,
        kind: Issue.Kind,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically add one issue_kind to an Issue's category set."""
        return await self._mutate_list_field(
            target_id,
            kind,
            column="issue_kind",
            api_key_id=api_key_id,
            actor=actor,
            include=True,
        )

    async def remove_issue_kind(
        self,
        target_id: UUID,
        kind: Issue.Kind,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically remove one issue_kind."""
        return await self._mutate_list_field(
            target_id,
            kind,
            column="issue_kind",
            api_key_id=api_key_id,
            actor=actor,
            include=False,
        )

    async def add_codechange(
        self,
        target_id: UUID,
        codechange_id: UUID,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically append one CodeChange UUID to an Experiment.

        Validates that ``codechange_id`` is an existing ``CodeChange``
        row (matches submit/set semantics).
        """
        return await self._mutate_list_field(
            target_id,
            codechange_id,
            column="experiment_codechanges",
            api_key_id=api_key_id,
            actor=actor,
            include=True,
            validate_item=lambda conn: validate_list_references(
                conn, [codechange_id], column="experiment_codechanges"
            ),
        )

    async def remove_codechange(
        self,
        target_id: UUID,
        codechange_id: UUID,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically remove one CodeChange UUID from an Experiment."""
        return await self._mutate_list_field(
            target_id,
            codechange_id,
            column="experiment_codechanges",
            api_key_id=api_key_id,
            actor=actor,
            include=False,
        )

    async def set_codechanges(
        self,
        target_id: UUID,
        value: Sequence[UUID] | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="experiment_codechanges",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_sha(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="codechange_sha",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_url(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="webresult_url",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_cli(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="agentsession_cli",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_cli_session_id(
        self,
        target_id: UUID,
        value: str | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="agentsession_cli_session_id",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_started(
        self,
        target_id: UUID,
        value: datetime | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="agentsession_started",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def set_rooms(
        self,
        target_id: UUID,
        value: Sequence[str] | None,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        return await self._set_field(
            target_id,
            value,
            column="agentsession_rooms",
            api_key_id=api_key_id,
            actor=actor,
        )

    async def add_room(
        self,
        target_id: UUID,
        room: str,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically add one room to a session's membership."""
        return await self._mutate_list_field(
            target_id,
            room,
            column="agentsession_rooms",
            api_key_id=api_key_id,
            actor=actor,
            include=True,
        )

    async def remove_room(
        self,
        target_id: UUID,
        room: str,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> UUID | None:
        """Atomically remove one room from a session's membership."""
        return await self._mutate_list_field(
            target_id,
            room,
            column="agentsession_rooms",
            api_key_id=api_key_id,
            actor=actor,
            include=False,
        )

    async def add_cost(
        self,
        target_id: UUID,
        delta: Cost,
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
            # Resolve the id first so an unknown target still raises
            # NotFoundError (404), matching ``set_cost_axis`` -- the
            # zero-guard must not short-circuit a missing-row error.
            kind = await lookup_kind(conn, target_id)
            # A zero delta is a no-op for a *known* row: it would write a
            # marginal_cost audit row and cascade ``dependency_changed`` for
            # no actual change. Mirror ``set_cost_axis``'s short-circuit so
            # both cost paths obey one rule.
            if not delta:
                return None
            change_id, _ = await self.emit_change(
                conn,
                api_key_id=api_key_id,
                actor=actor,
                subject_id=target_id,
                subject_kind=kind,
                kind="marginal_cost",
                cost_delta=delta,
                reason=reason,
            )
            return change_id

    async def set_cost_axis(
        self,
        target_id: UUID,
        axis: Literal["agent_usd", "resource_usd"],
        value: float,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> UUID | None:
        """Overwrite one cost axis to ``value``; ``0`` clears it.

        Backs ``PUT`` / ``DELETE`` on ``marginal_cost_*``. Computes the
        signed delta against the current value under a row lock, then routes
        through the same audited ``marginal_cost`` emit as :meth:`add_cost`,
        keeping a single write path for cost. ``emit_change``'s floor guard
        rejects a delta that would drive the total negative.
        """
        if value < 0:
            raise ConflictError(f"{axis} cannot be negative")
        column = f"marginal_cost_{axis}"
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            row = await conn.fetchrow(
                vetted_sql(
                    "SELECT kind, ",
                    column,
                    " AS current FROM inquiries WHERE id = $1 FOR UPDATE",
                ),
                target_id,
            )
            if row is None:
                raise NotFoundError("inquiry not found")
            delta = Cost(**{axis: value - float(row["current"])})
            if not delta:
                return None
            change_id, _ = await self.emit_change(
                conn,
                api_key_id=api_key_id,
                actor=actor,
                subject_id=target_id,
                subject_kind=cast(Inquiry.InquiryKind, row["kind"]),
                kind="marginal_cost",
                cost_delta=delta,
                reason=reason,
            )
            return change_id
