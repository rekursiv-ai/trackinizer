""":class:`_SubmitMixin` -- per-kind inquiry creation.

Every ``submit_X`` routes through :meth:`_submit_generic`, the single insert
path (idempotency probe -> reference validation -> row -> ``created`` change
-> kind-specific edges). :meth:`submit_batch` threads one shared connection
through each item so a mixed-kind batch commits or rolls back atomically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, cast
from uuid import UUID

import uuid

import asyncpg

from trackinizer.lib.custom_json import int_val
from trackinizer.lib.postgres import Conn
from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.server.primitives import (
    insert_inquiry,
    upsert_embedding,
    validate_list_references,
)
from trackinizer.server.store.change_id_slot import (
    _peek_client_change_id,
    set_client_change_id,
)
from trackinizer.server.store.edge import _EdgeMixin
from trackinizer.server.store.edit import _EditMixin
from trackinizer.server.values import canonical_strs
from trackinizer.types.edges import Edge
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.bodies import (
    BatchEdge,
    Citation,
    SubmitAgentSession,
    SubmitArtifact,
    SubmitBase,
    SubmitBelief,
    SubmitCodeChange,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
    SubmitWebResult,
    SubmitWebSearch,
)


__all__ = [
    "SUBMIT_METHOD",
    "PostInsert",
    "PreInsert",
    "_SubmitMixin",
    "_SubmitOnConn",
]


type PostInsert = Callable[[Conn, UUID, UUID], Awaitable[None]]
type PreInsert = Callable[[Conn], Awaitable[None]]


class _SubmitOnConn(Protocol):
    """A ``submit_X`` bound method that can join a caller's transaction.

    ``req`` is typed ``Any`` deliberately: each ``submit_X`` accepts its own
    concrete ``SubmitBase`` subtype (``SubmitIssue``, ``SubmitBelief``, ...). A
    Protocol parameter is contravariant, so a narrower ``SubmitBase`` would make
    every concrete method fail to satisfy this Protocol. The dispatch in
    ``submit_batch`` always passes a real ``SubmitBase``, and ``SUBMIT_METHOD``
    keys the right method by the body's concrete type, so runtime safety holds.
    """

    def __call__(
        self,
        req: Any,
        *,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
        conn: Conn | None,
    ) -> Awaitable[UUID]: ...


# Submit body type -> the ``Store`` method that creates that kind. Drives
# ``submit_batch`` dispatch (one shared transaction over mixed kinds) and the
# single-submit route's dispatch, so both read one source of truth.
SUBMIT_METHOD: dict[type[SubmitBase], str] = {
    SubmitIssue: "submit_issue",
    SubmitArtifact: "submit_artifact",
    SubmitExperiment: "submit_experiment",
    SubmitPaper: "submit_paper",
    SubmitBelief: "submit_belief",
    SubmitCodeChange: "submit_codechange",
    SubmitWebResult: "submit_webresult",
    SubmitWebSearch: "submit_websearch",
    SubmitAgentSession: "submit_agentsession",
}


class _SubmitMixin(_EditMixin, _EdgeMixin):
    """Per-kind inquiry creation for :class:`Store`."""

    async def submit_issue(
        self,
        req: SubmitIssue,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
        conn: Conn | None = None,
    ) -> UUID:
        async def post_insert(conn: Conn, issue_id: UUID, cause: UUID) -> None:
            for broader_id, priority in req.narrows:
                # ``narrows`` is stored narrower -> broader: this new (narrower)
                # issue is the subject/from-side, the broader issue the to-side.
                await self.insert_edge_and_audit(
                    conn,
                    subject_id=issue_id,
                    subject_kind="Issue",
                    to_id=broader_id,
                    edge_kind="narrows",
                    api_key_id=api_key_id,
                    actor=actor,
                    caused_by=cause,
                    priority=priority,
                )
            for prerequisite_id in req.requires:
                # ``requires`` is stored requirer -> prerequisite: this new issue
                # (the requirer) is the subject/from-side, the prerequisite it
                # waits on is the to-side.
                await self.insert_edge_and_audit(
                    conn,
                    subject_id=issue_id,
                    subject_kind="Issue",
                    to_id=prerequisite_id,
                    edge_kind="requires",
                    api_key_id=api_key_id,
                    actor=actor,
                    caused_by=cause,
                )

        return await self._submit_generic(
            req,
            kind="Issue",
            api_key_id=api_key_id,
            actor=actor,
            extras={
                "issue_kind": canonical_strs(req.issue_kind)
                if req.issue_kind
                else None,
                "issue_validation": req.validation,
                "issue_priority": req.priority,
            },
            post_insert=post_insert,
            conn=conn,
        )

    async def _submit_generic(
        self,
        req: SubmitBase,
        *,
        kind: Inquiry.InquiryKind,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
        extras: dict[str, Any] | None = None,
        pre_insert: PreInsert | None = None,
        post_insert: PostInsert | None = None,
        conn: Conn | None = None,
    ) -> UUID:
        """Single insert path for every submit_X.

        Order: idempotency probe -> ``pre_insert`` (kind-specific
        reference validation, run before any write so a bad ref wastes no
        embed work and writes no row) -> row -> ``created`` change ->
        ``post_insert`` (kind-specific edges: Issue decomposition /
        blockers or Belief citations), which receives ``cause`` so its
        own edge audits chain to the same ``created`` event.

        When ``req.idempotency_key`` is set, the server uses that UUID
        as the ``change_log.id`` of the ``created`` audit row. A repeat
        submit with the same key short-circuits via a probe before any
        write; a concurrent racer that beats the probe but loses the
        change_log PK race is recovered inside the txn the same way.
        Either path returns the *original* inquiry's server-minted id.

        The inquiry's ``id`` is always server-minted: clients have no
        way to predict it before the response. This closes the
        targeted-UUID race vector that a client-minted inquiries.id
        would expose. See ``docs/design_idempotency.md``.

        When ``conn`` is ``None`` this submit owns its transaction (the
        normal single-submit path: ``acquire`` + ``tx`` + buffered
        notify). When a connection is supplied, the insert joins the
        caller's open transaction -- :meth:`submit_batch` passes one
        shared connection so every item commits or rolls back together,
        and the caller then owns ``tx`` and ``notify_after_commit``.
        """
        if conn is not None:
            return await self._submit_on_conn(
                conn,
                req,
                kind=kind,
                api_key_id=api_key_id,
                actor=actor,
                extras=extras,
                pre_insert=pre_insert,
                post_insert=post_insert,
            )
        # Capture the effective idempotency key (body field or header-set slot)
        # BEFORE the tx, so the recovery path below can re-probe even on the
        # header-only path -- by the time we catch the collision, the slot has
        # been consumed by ``emit_change``. Both sources are client-influenced,
        # so a collision is a runtime 4xx (replay), never an assert.
        effective_key = req.idempotency_key or _peek_client_change_id()
        # Embed BEFORE opening the tx, mirroring ``set_title``: a network
        # embedder held inside the tx would pin the single PGlite connection
        # across the round-trip, serializing every other writer. The cost of a
        # wasted embed on the rare replay/bad-ref path is dominated by the
        # lock-contention win. The batch path cannot hoist (each item's embed
        # is unavoidably inside the batch's shared tx), so it lets
        # ``_submit_on_conn`` embed inline by passing ``embeddings=None``.
        embeddings = await self._embed_all(req.title)
        try:
            async with (
                notify_after_commit(),
                self.engine.acquire() as own_conn,
                tx(own_conn),
            ):
                return await self._submit_on_conn(
                    own_conn,
                    req,
                    kind=kind,
                    api_key_id=api_key_id,
                    actor=actor,
                    extras=extras,
                    pre_insert=pre_insert,
                    post_insert=post_insert,
                    embeddings=embeddings,
                )
        except asyncpg.UniqueViolationError as exc:
            # Classify by the violated constraint, not by whether a re-probe
            # finds a row (an implicit, fragile oracle). The idempotency-replay
            # collision is on the ``change_log`` PK (``change_log_pkey``) -- a
            # concurrent racer beat us to the same idempotency key (body field
            # OR header) after the probe said it was free. Any OTHER unique
            # source -- notably the partial index
            # ``uq_inquiries_live_session_owner`` (one live AgentSession per
            # routing name) -- is NOT a replay; re-raise it so
            # :meth:`start_session` re-reserves the next ``#N``. A PK collision
            # is only possible when a client key was used; without one
            # ``emit_change`` mints a fresh id. A batch (``conn`` supplied)
            # never reaches this branch -- :meth:`submit_batch` owns the tx and
            # lets the violation roll the whole batch back.
            if exc.constraint_name != "change_log_pkey" or effective_key is None:
                raise
            async with self.engine.acquire() as probe_conn:
                existing = await self._lookup_existing_by_change(
                    effective_key, kind, probe_conn
                )
            if existing is None:
                raise
            return existing

    async def _submit_on_conn(
        self,
        conn: Conn,
        req: SubmitBase,
        *,
        kind: Inquiry.InquiryKind,
        api_key_id: UUID | None,
        actor: Inquiry.Actor,
        extras: dict[str, Any] | None,
        pre_insert: PreInsert | None,
        post_insert: PostInsert | None,
        embeddings: list[tuple[str, list[float]]] | None = None,
    ) -> UUID:
        """Probe + insert one inquiry on an already-open transaction.

        Pure transaction body for :meth:`_submit_generic`: it assumes the
        caller has begun the transaction on ``conn`` and will commit or
        roll back. The idempotency probe reads on ``conn`` (not a fresh
        acquire) because the single PGlite connection is already held.

        A ``change_log`` PK collision propagates: the caller's ``tx``
        rolls back, and the single-submit path re-probes for the winner
        once the connection is released. In a batch the same propagation
        rolls every item back together.

        ``embeddings`` carries pre-computed ``(model, vector)`` pairs for
        ``req.title``: the single-submit path embeds before opening its tx
        (so the embedder round-trip doesn't pin the connection) and passes
        the result here, while the :meth:`submit_batch` path leaves it
        ``None`` and embeds inline -- its per-item embed is unavoidably
        inside the shared tx.
        """
        # The effective idempotency key is the body field if present, else the
        # slot the route layer set from an ``Idempotency-Key`` header. Both
        # land as the ``created`` event's ``change_log.id``, so both must
        # short-circuit a replay -- a header-only retry is just as much a
        # duplicate as a body-key retry. Peek (not consume) so ``emit_change``
        # still reads the slot on the first-write path.
        effective_key = req.idempotency_key or _peek_client_change_id()
        if effective_key is not None:
            existing = await self._lookup_existing_by_change(effective_key, kind, conn)
            if existing is not None:
                # Drain any externally-set slot (e.g., Idempotency-Key
                # header processed by the route layer before this body
                # field was parsed). Without this, the leftover UUID
                # leaks into the next submit's ``emit_change`` and
                # either collides on ``change_log.id`` or mis-attributes
                # the next mutation to this submit's logical change.
                set_client_change_id(None)
                return existing
        # Validate kind-specific references before any write: a bad ref must
        # fail the submit without inserting a row or emitting a ``created``
        # event. The single-submit path pre-computes ``embeddings`` before its
        # tx (see :meth:`_submit_generic`), so on that path an embed may run
        # ahead of this validation -- a wasted embed *compute* on the rare
        # bad-ref path, never a wasted ``inquiry_embeddings`` row, since the
        # upsert below is still gated behind the row insert.
        if pre_insert is not None:
            await pre_insert(conn)
        row_id = uuid.uuid4()
        # The batch path (``embeddings is None``) embeds inline inside its
        # shared tx, which it cannot avoid; the single path passes a value it
        # computed before opening its own tx.
        if embeddings is None:
            embeddings = await self._embed_all(req.title)
        # Stash the client-supplied idempotency key into the per-request
        # slot so ``emit_change`` picks it up on its first call inside
        # this transaction. Mirrors how ``Idempotency-Key`` is delivered for
        # edits: one contextvar, consumed once per logical mutation.
        # The ``try/finally`` guarantees the slot is cleared on every
        # exit path, including unexpected errors between set and
        # ``emit_change``; otherwise a leftover key would attach to a
        # subsequent submit's ``emit_change`` and collide.
        if req.idempotency_key is not None:
            set_client_change_id(req.idempotency_key)
        # Every row is attributed to an account, and the account must arrive
        # already resolved on the request: the route resolves it from the
        # authenticated identity (``api/submit.py`` / ``sessions_routes.py``)
        # and validates it is an active user. The Store does NOT invent a
        # fallback -- an absent account is a programming error at a caller that
        # skipped resolution, not a state to paper over with the spoofable
        # audit ``actor``. ``owner`` may be unset; ``account`` may never be.
        if not req.account:
            raise ValueError(
                "submit requires a resolved account; the route resolves it from "
                "the authenticated identity before calling the Store"
            )
        account = req.account
        try:
            # The optional base columns are nullable: an unset field stores
            # NULL, the single encoding of "absent". An unspecified owner is
            # genuinely unowned -- it is not stamped with the actor.
            await insert_inquiry(
                conn,
                row_id,
                kind,
                status=req.status,
                values={
                    "title": req.title,
                    "description": req.description,
                    "owner": req.owner,
                    "account": account,
                    "labels": canonical_strs(req.labels) if req.labels else None,
                    "subscribers": (
                        canonical_strs(req.subscribers) if req.subscribers else None
                    ),
                    **(extras or {}),
                },
            )
            for model, vec in embeddings:
                await upsert_embedding(conn, row_id, model, vec)
            cause, _ = await self.emit_change(
                conn,
                api_key_id=api_key_id,
                actor=actor,
                subject_id=row_id,
                subject_kind=kind,
                kind="created",
                marginal_cost=req.marginal_cost,
            )
            if post_insert is not None:
                await post_insert(conn, row_id, cause)
        finally:
            # Safety net: ``emit_change`` consumes the slot on its first
            # call, but an exception between the set and the consume would
            # leave the active key dangling -- whether it came from the body
            # field (set just above) or was installed by the route's
            # ``Idempotency-Key`` middleware on the header-only path (where
            # ``req.idempotency_key`` is ``None``). Clear unconditionally and
            # idempotently so neither source leaks into the next submit's
            # ``emit_change`` in the same task. ``set_client_change_id(None)``
            # is a no-op when the slot is already empty.
            set_client_change_id(None)
        return row_id

    async def _lookup_existing_by_change(
        self,
        idempotency_key: UUID,
        kind: Inquiry.InquiryKind,
        conn: Conn,
    ) -> UUID | None:
        """Return the original inquiry id iff this idempotency key was used.

        Probes ``change_log`` (where the client-supplied key lives) for
        the ``created`` event and reads its denormalized ``subject_kind``
        to verify the result is a row of the requested kind. A key reused
        for a different kind raises so the caller surfaces 409.

        Reads on the caller-supplied ``conn``: the probe runs while the
        submit already holds the single PGlite connection, so acquiring a
        second one would deadlock. A SELECT inside the open transaction is
        safe and sees the same snapshot.
        """
        row = await conn.fetchrow(
            "SELECT c.subject_id, c.kind AS change_kind, c.subject_kind "
            "FROM change_log c WHERE c.id = $1",
            idempotency_key,
        )
        if row is None:
            return None
        if row["change_kind"] != "created":
            raise ConflictError(
                f"idempotency_key {idempotency_key} already names a "
                f"{row['change_kind']} event, not a submit"
            )
        if row["subject_kind"] != kind:
            raise ConflictError(
                f"idempotency_key {idempotency_key} already created a "
                f"{row['subject_kind']}, not {kind}"
            )
        subject_id = row["subject_id"]
        assert subject_id is None or isinstance(subject_id, UUID)
        return subject_id

    async def submit_artifact(
        self,
        req: SubmitArtifact,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
        conn: Conn | None = None,
    ) -> UUID:
        return await self._submit_generic(
            req, kind="Artifact", api_key_id=api_key_id, actor=actor, conn=conn
        )

    async def submit_experiment(
        self,
        req: SubmitExperiment,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
        conn: Conn | None = None,
    ) -> UUID:
        async def pre_insert(conn: Conn) -> None:
            await validate_list_references(
                conn, req.codechanges or (), column="experiment_codechanges"
            )

        return await self._submit_generic(
            req,
            kind="Experiment",
            api_key_id=api_key_id,
            actor=actor,
            extras={
                "experiment_codechanges": (
                    list(req.codechanges) if req.codechanges else None
                ),
                "experiment_outcome": req.outcome,
                "experiment_config": req.config,
            },
            pre_insert=pre_insert,
            conn=conn,
        )

    async def submit_paper(
        self,
        req: SubmitPaper,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
        conn: Conn | None = None,
    ) -> UUID:
        return await self._submit_generic(
            req,
            kind="Paper",
            api_key_id=api_key_id,
            actor=actor,
            extras={
                "paper_abstract": req.abstract,
                "paper_authors": list(req.authors) if req.authors else None,
                "paper_publication_type": req.publication_type,
                "paper_venue": req.venue,
                "paper_subvenue": req.subvenue,
                "paper_publish_date": req.publish_date,
                "paper_source": req.source,
                "paper_google_scholar_cluster_id": req.google_scholar_cluster_id,
                "paper_google_scholar_cites_id": req.google_scholar_cites_id,
            },
            conn=conn,
        )

    async def submit_belief(
        self,
        req: SubmitBelief,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
        conn: Conn | None = None,
    ) -> UUID:
        async def post_insert(conn: Conn, belief_id: UUID, cause: UUID) -> None:
            # proves/favors store Artifact -> Belief (the citing evidence is the
            # younger child; the belief it bears on is the older parent), so the
            # cited Artifact is always the from-side and the Belief the to-side.
            # Each citation carries a signed valence (positive proves/favors,
            # negative disproves/disfavors). The inline-citation request lists
            # them on the Belief as its inbound (proved_by / favored_by) view.
            citation_kinds: tuple[tuple[Edge.Kind, Sequence[Citation]], ...] = (
                ("proves", req.proved_by),
                ("favors", req.favored_by),
            )
            for edge_kind, items in citation_kinds:
                for citation in items:
                    await self.insert_edge_and_audit(
                        conn,
                        subject_id=belief_id,
                        subject_kind="Belief",
                        to_id=citation.artifact_id,
                        edge_kind=edge_kind,
                        api_key_id=api_key_id,
                        actor=actor,
                        caused_by=cause,
                        require_to_kind=citation.artifact_kind,
                        cite_peer_as_from=True,
                        valence=citation.valence,
                    )

        return await self._submit_generic(
            req,
            kind="Belief",
            api_key_id=api_key_id,
            actor=actor,
            extras={
                "belief_judgement": req.judgement,
                "belief_confidence": req.confidence,
            },
            post_insert=post_insert,
            conn=conn,
        )

    async def submit_codechange(
        self,
        req: SubmitCodeChange,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
        conn: Conn | None = None,
    ) -> UUID:
        return await self._submit_generic(
            req,
            kind="CodeChange",
            api_key_id=api_key_id,
            actor=actor,
            extras={"codechange_sha": req.sha},
            conn=conn,
        )

    async def submit_webresult(
        self,
        req: SubmitWebResult,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
        conn: Conn | None = None,
    ) -> UUID:
        return await self._submit_generic(
            req,
            kind="WebResult",
            api_key_id=api_key_id,
            actor=actor,
            extras={"webresult_url": req.url},
            conn=conn,
        )

    async def submit_websearch(
        self,
        req: SubmitWebSearch,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
        conn: Conn | None = None,
    ) -> UUID:
        return await self._submit_generic(
            req,
            kind="WebSearch",
            api_key_id=api_key_id,
            actor=actor,
            extras={
                "websearch_query": req.query,
                "websearch_provider": req.provider,
            },
            conn=conn,
        )

    async def submit_agentsession(
        self,
        req: SubmitAgentSession,
        *,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
        conn: Conn | None = None,
    ) -> UUID:
        return await self._submit_generic(
            req,
            kind="AgentSession",
            api_key_id=api_key_id,
            actor=actor,
            extras={
                "agentsession_cli": req.cli,
                "agentsession_cli_session_id": req.cli_session_id,
                "agentsession_started": req.started,
                # No create-time ``ended``: born live. The lifecycle CHECK
                # ties ``ended`` to ``status = 'complete'``, set only via /end.
                "agentsession_rooms": (
                    canonical_strs(req.rooms) if req.rooms else None
                ),
                # Records who opened the session so the inbound-drain route can
                # authorize by matching the credential (G1). NULL under
                # --no-auth, which the drain check treats as a self-match.
                "agentsession_opened_by_api_key_id": api_key_id,
            },
            conn=conn,
        )

    async def submit_batch(
        self,
        items: Sequence[SubmitBase],
        *,
        edges: Sequence[BatchEdge] = (),
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor = "system",
    ) -> list[UUID]:
        """Create many inquiries and their edges in one all-or-nothing tx.

        Every item and every edge is created on one shared connection
        inside a single transaction: if any step fails, the whole batch
        rolls back and nothing -- no row, no edge -- is persisted. Returns
        the server-minted ids in input order.

        Each item routes to its per-kind ``submit_X`` (so kind-specific
        ``extras`` / citation / decomposition edges still apply) with the
        shared ``conn`` threaded through. Edges then reference rows by
        their item index (see :class:`BatchEdge`): inline-created targets
        have no id until this transaction commits, so an index is the only
        way to link two brand-new rows atomically.

        Args:
          items: Parsed submit bodies, one per row to create.
          edges: Edges linking the new rows, endpoints named by item index.
          api_key_id: Server-stamped credential id, applied to every item.
          actor: Audit actor applied to every item/edge lacking its own.

        Returns:
          ids: Server-minted inquiry ids, aligned to ``items`` order.

        """
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            ids: list[UUID] = []
            for item in items:
                method = cast(_SubmitOnConn, getattr(self, SUBMIT_METHOD[type(item)]))
                ids.append(
                    await method(
                        item,
                        api_key_id=api_key_id,
                        # Per-item actor override wins, mirroring the
                        # single-submit route's ``req.actor or email``.
                        actor=item.actor or actor,
                        conn=conn,
                    )
                )
            for edge in edges:
                await self._add_edge_on_conn(
                    conn,
                    from_id=edge.from_id
                    if edge.from_id is not None
                    else ids[int_val(edge.from_index, 0)],
                    to_id=edge.to_id
                    if edge.to_id is not None
                    else ids[int_val(edge.to_index, 0)],
                    edge_kind=edge.edge_kind,
                    priority=edge.priority,
                    note=edge.note,
                    valence=edge.valence,
                    labels=edge.labels,
                    api_key_id=api_key_id,
                    actor=actor,
                )
            return ids
