""":class:`_SessionMixin` -- agent-session lifecycle and the event feed.

Owns session open/resume/end (:meth:`start_session`, :meth:`_resume_session`,
:meth:`end_session`), routing-name reservation, and the append/read seam for
captured turns (:meth:`append_events`, :meth:`read_session_events`,
:meth:`read_feed`). Sessions are ``AgentSession`` inquiries, so submit and
edit machinery is reused through the composed :class:`Store`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

import asyncpg

from trackinizer.lib.custom_json import DataclassCodec, JSONValue, json_unfreeze
from trackinizer.lib.postgres import Conn
from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.server.store.change_id_slot import (
    _peek_client_change_id,
    set_client_change_id,
)
from trackinizer.server.store.edit import _EditMixin
from trackinizer.server.store.read import seq_range_clause
from trackinizer.server.store.submit import _SubmitMixin
from trackinizer.server.values import vetted_sql
from trackinizer.types.change_log import Snapshot
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.bodies import SubmitAgentSession
from trackinizer.wire.routes import DEFAULT_LIST_LIMIT
from trackinizer.wire.seq_ranges import SeqRange
from trackinizer.wire.wire_sessions import EventBody, FeedEvent


__all__ = [
    "_SessionMixin",
]


def _strip_postgres_nuls(value: JSONValue) -> JSONValue:
    """Drop non-displayable NUL artifacts that PostgreSQL JSONB cannot store."""
    if isinstance(value, str):
        return value.replace("\0", "")
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, JSONValue], value)
        return {
            key.replace("\0", ""): _strip_postgres_nuls(item)
            for key, item in mapping.items()
        }
    if isinstance(value, Sequence):
        sequence = cast(Sequence[JSONValue], value)
        return [_strip_postgres_nuls(item) for item in sequence]
    return value


class _SessionMixin(_SubmitMixin, _EditMixin):
    """Agent-session lifecycle and the cross-session event feed.

    Bases name the concerns this mixin consumes -- it submits sessions
    (``_SubmitMixin``) and edits their fields (``_EditMixin``). ``_EditMixin`` is
    already in ``_SubmitMixin``'s linearization, so listing it is redundant for
    the MRO; it is kept as documentation of the direct dependency.
    """

    async def reserve_session_actor(self, requested: str) -> str:
        """Return a routing name reserved for the session's lifetime, never reused.

        The routing handle is the session ``owner`` (an :class:`Inquiry.Actor`
        string); two ``trax run`` sessions cannot share one, or ``@actor``
        addressing would be ambiguous. Handles are monotonic, like a sequence:
        when ``requested`` is already held by ANY AgentSession -- live or ended
        -- append the smallest ``#N`` suffix that is free. An ended session
        keeps its handle so a later ``--resume`` reclaims its original name.
        Audit authorship and the routing handle never diverge -- the returned
        name becomes the session owner.

        This picks the next free suffix from a point-in-time read; the
        ``uq_inquiries_live_session_owner`` partial unique index is the
        race-proof backstop for concurrent *live* starts, so :meth:`start_session`
        retries this reservation when a concurrent start takes the same name
        between read and insert.
        """
        async with self.engine.acquire() as conn:
            taken = {
                row["owner"]
                for row in await conn.fetch(
                    "SELECT owner FROM inquiries "
                    "WHERE kind = 'AgentSession' AND owner IS NOT NULL",
                )
            }
        if requested not in taken:
            return requested
        suffix = 2
        while f"{requested}#{suffix}" in taken:
            suffix += 1
        return f"{requested}#{suffix}"

    async def start_session(
        self,
        req: SubmitAgentSession,
        *,
        requested_actor: str,
        api_key_id: UUID | None = None,
        max_reserve_attempts: int = 16,
    ) -> tuple[UUID, str, int]:
        """Open or RESUME a session, returning ``(id, granted_owner, next_seq)``.

        Reserves the next free ``@actor`` name (:meth:`reserve_session_actor`)
        and inserts the row, retrying when the live-owner partial unique index
        (``uq_inquiries_live_session_owner``) rejects a name a concurrent start
        grabbed in the read-to-insert window. The DB index -- not the advisory
        read -- is the arbiter, so two racing starts for the same actor get
        distinct ``#N`` names instead of both winning.

        Resume: when ``req.cli_session_id`` is non-null and matches an existing
        AgentSession's ``agentsession_cli_session_id``, re-attach that session
        instead of minting a new one -- same id, same granted handle -- and
        re-open it if ended (clear ``ended``, status back to ``active``), so the
        resumed run continues the original log. ``next_seq`` is the event log's
        continuation point (``max(seq)+1``, or 0 for a fresh session) so the
        caller seeds its sequence and appends rather than colliding at seq 0.

        Idempotent: when this request reuses a prior start's idempotency key,
        the original session id AND its granted owner are replayed -- the
        change_log is probed for the key BEFORE reserving, so a retry never
        burns a fresh ``#N`` suffix.

        Returns:
          session_id: The server-minted (or re-attached) inquiry id.
          granted_actor: The routing name stamped on the row.
          next_seq: The event log's next free seq (``max(seq)+1``, 0 if empty).

        """
        # Resume correlation BEFORE reserve/replay: a known cli_session_id
        # re-attaches the original session. Guard non-null so two fresh
        # (null-id) runs never correlate to each other.
        if req.cli_session_id is not None:
            resumed = await self._resume_session(
                req, api_key_id=api_key_id, actor=requested_actor
            )
            if resumed is not None:
                return resumed
        # Replay BEFORE reserving: a same-key retry must return the original
        # (id, owner), not reserve a fresh name. The key lives on the body
        # field or the header-set slot, exactly as ``_submit_generic`` reads it.
        effective_key = req.idempotency_key or _peek_client_change_id()
        if effective_key is not None:
            async with self.engine.acquire() as conn:
                existing = await self._lookup_existing_by_change(
                    effective_key, "AgentSession", conn
                )
                if existing is not None:
                    owner = cast(
                        str,
                        await conn.fetchval(
                            "SELECT owner FROM inquiries WHERE id = $1", existing
                        ),
                    )
                    next_seq = await self._next_event_seq(conn, existing)
                    # Drain the peeked slot so the leftover key can't leak into
                    # the next same-context write's ``emit_change`` (mirrors
                    # ``_submit_on_conn``'s replay-return clear).
                    set_client_change_id(None)
                    return existing, owner, next_seq
        last_error: asyncpg.UniqueViolationError | None = None
        # Bounded so a pathological storm can't spin forever. Each retry advances
        # the ``#N`` suffix past a name a concurrent start grabbed, so far more
        # attempts than concurrent live sessions for one actor could ever need;
        # exhausting it means a pathological storm and surfaces as a
        # ``ConflictError`` rather than a bare loop-exit or a raw asyncpg leak.
        for _ in range(max_reserve_attempts):
            granted = await self.reserve_session_actor(requested_actor)
            try:
                session_id = await self.submit_agentsession(
                    req.model_copy(update={"owner": granted}),
                    api_key_id=api_key_id,
                    actor=granted,
                )
            except asyncpg.UniqueViolationError as exc:
                if exc.constraint_name != "uq_inquiries_live_session_owner":
                    raise
                # Another start took ``granted`` first; reserve again to mint
                # the next suffix and retry.
                last_error = exc
                continue
            # ``submit_agentsession`` returns the row id -- but under a concurrent
            # same-key race it may be an IDEMPOTENCY REPLAY of the winner's row
            # (the change_log PK collision recovery in ``_submit_generic``), not
            # our fresh insert. In that case ``granted`` is the name WE reserved,
            # not the committed owner, and the event log is the winner's (not
            # empty). Read back the authoritative ``(owner, next_seq)`` for the
            # returned id so a racing retry echoes the winner's receipt, not a
            # mismatched ``(granted, 0)``.
            async with self.engine.acquire() as conn:
                owner = cast(
                    str,
                    await conn.fetchval(
                        "SELECT owner FROM inquiries WHERE id = $1", session_id
                    ),
                )
                next_seq = await self._next_event_seq(conn, session_id)
            return session_id, owner, next_seq
        # Exhausted the retry budget: surface a clean 409, not a raw asyncpg
        # leak or a bare loop-exit. The chained cause keeps the diagnostic.
        raise ConflictError(
            f"could not reserve a routing name for {requested_actor!r} after "
            f"{max_reserve_attempts} attempts"
        ) from last_error

    @staticmethod
    async def _next_event_seq(conn: Conn, session_id: UUID) -> int:
        """The event log's next free seq for ``session_id`` (``max(seq)+1``, 0 if empty)."""
        last = await conn.fetchval(
            "SELECT max(seq) FROM agent_session_events WHERE session_id = $1",
            session_id,
        )
        return 0 if last is None else int(last) + 1

    async def _resume_session(
        self, req: SubmitAgentSession, *, api_key_id: UUID | None, actor: Inquiry.Actor
    ) -> tuple[UUID, str, int] | None:
        """Re-attach (and re-open if ended) the session for this CLI session id.

        Returns ``(id, owner, next_seq)`` on a match, else ``None`` (no prior
        session this caller may resume -> caller mints a fresh one).

        Resume CORRELATION, not access control: a session re-attaches only to
        the ``api_key_id`` that opened it, matched ``IS NOT DISTINCT FROM`` so a
        ``--no-auth`` None==None pairing still resolves. This routes the right
        capture log back to the right ``trax run`` -- a credentialed caller
        resuming another principal's ``cli_session_id`` gets ``None`` (mints a
        fresh session) rather than silently appending to a stranger's log. It is
        NOT an authorization boundary: AgentSessions are a shared workspace
        (events/read/end/drain are writer/viewer-gated, not owner-gated);
        ``opened_by_api_key_id`` is attribution + this resume key only.
        Under ``--no-auth`` ALL sessions stamp ``opened_by = NULL``, so the
        scope degrades to ``cli_session_id`` alone; a no-auth caller
        resuming a known ``cli_session_id`` re-attaches it, as intended for a
        single-tenant local deploy.

        Re-opening an ended session clears ``agentsession_ended`` and restores
        ``status='active'`` in one statement so the lifecycle CHECK (``ended``
        set iff ``status='complete'``) never sees an intermediate desync -- the
        live<-ended mirror of ``end_session``'s atomic live->ended move. The
        re-open audit is attributed to ``actor`` -- the resuming request's
        ``--as`` string (the system-wide author label; the verified identity is
        the audit row's ``api_key_id``, not this free string), NOT the original
        owner. Any new ``req.rooms`` are applied -- on a live OR a re-opened
        re-attach -- so ``--resume --room X`` joins X rather than dropping it.

        Resume deliberately does NOT re-validate the session's ``account``. The
        account is mutable stamped data, not a live permission check: it is set
        at create and persists until something explicitly re-stamps it (via
        ``set_account``). A user disabled after the stamp does not rewrite
        anyone's existing stamp, and resume -- like a plain field edit -- never
        revalidates it. Resume inherits the stamp as-is; this is the consistent
        behavior, not a carve-out.
        """
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            row = await conn.fetchrow(
                "SELECT id, owner, status, agentsession_ended, agentsession_rooms "
                "FROM inquiries WHERE kind = 'AgentSession' "
                "AND agentsession_cli_session_id = $1 "
                "AND agentsession_opened_by_api_key_id IS NOT DISTINCT FROM $2 "
                "ORDER BY seq LIMIT 1 FOR UPDATE",
                req.cli_session_id,
                api_key_id,
            )
            if row is None:
                return None
            session_id = cast(UUID, row["id"])
            owner = str(row["owner"])
            if row["agentsession_ended"] is not None:
                # Re-open: move ended -> live in one statement so the lifecycle
                # CHECK never observes (ended set, status active). Attribute the
                # audit to the resuming caller, not the original owner.
                await conn.execute(
                    "UPDATE inquiries SET agentsession_ended = NULL, "
                    "status = 'active', modified = clock_timestamp() WHERE id = $1",
                    session_id,
                )
                await self._emit_field_change(
                    conn,
                    session_id,
                    "AgentSession",
                    "agentsession_ended",
                    Snapshot(agentsession_ended=row["agentsession_ended"]),
                    new=Snapshot(agentsession_ended=None),
                    api_key_id=api_key_id,
                    actor=actor,
                )
                if row["status"] != "active":
                    await self._emit_field_change(
                        conn,
                        session_id,
                        "AgentSession",
                        "status",
                        Snapshot(status=row["status"]),
                        new=Snapshot(status="active"),
                        api_key_id=api_key_id,
                        actor=actor,
                    )
            # Apply any new rooms from the resuming request: ``--resume --room X``
            # must join X. ``_mutate_list_field_on_conn`` is idempotent (a re-add
            # of an existing room is a no-op) and runs on this open tx.
            if req.rooms:
                for room in req.rooms:
                    await self._mutate_list_field_on_conn(
                        conn,
                        session_id,
                        room,
                        column="agentsession_rooms",
                        api_key_id=api_key_id,
                        actor=actor,
                        include=True,
                    )
            next_seq = await self._next_event_seq(conn, session_id)
            return session_id, owner, next_seq

    async def resolve_live_sessions(
        self, actor: str, *, room: str | None = None
    ) -> list[tuple[UUID, tuple[str, ...]]]:
        """Live ``AgentSession`` ids + rooms owned by ``actor`` (optionally scoped).

        The name-resolution half of messaging: ``@actor`` (and ``@actor:room``)
        resolves to the live sessions a message should reach. Live is
        ``ended IS NULL``; ``room`` (when given) must be a member of the row's
        ``agentsession_rooms``. Each session's rooms are returned alongside its
        id so the caller can attach the ``[room] sender:`` context and enforce
        the bare-``@actor`` ambiguity rule (a session in several rooms cannot be
        addressed without naming one). Ordered by ``(created, id)`` so the
        ordering is stable even when two rows share a creation instant.

        Returns:
          sessions: ``(session_id, rooms)`` pairs, oldest first.

        """
        clauses = [
            "kind = 'AgentSession'",
            "agentsession_ended IS NULL",
            "owner = $1",
        ]
        params: list[object] = [actor]
        if room is not None:
            params.append(room)
            clauses.append(f"${len(params)} = ANY(agentsession_rooms)")
        where = " AND ".join(clauses)
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(
                vetted_sql(
                    "SELECT id, agentsession_rooms FROM inquiries WHERE ",
                    where,
                    " ORDER BY created, id",
                ),
                *params,
            )
        return [(row["id"], tuple(row["agentsession_rooms"] or ())) for row in rows]

    async def append_events(
        self,
        session_id: UUID,
        events: Sequence[EventBody],
    ) -> tuple[int, int]:
        """Append agent-session events idempotently; return ``(appended, skipped)``.

        The storage seam for session capture (the one function ``trax run``
        sync drives, and the swap point if the event store moves to
        ClickHouse later). Each wire body is rebuilt into a typed
        :class:`AgentSessionEvent` (``ev.to_event``) before storage, so its
        ``kind == type(message).__name__`` invariant runs here, on the hot
        path -- a forged body whose ``kind`` disagrees with its ``message``
        is rejected, not persisted.

        ``PRIMARY KEY (session_id, seq)`` makes a retried batch a no-op:
        ``ON CONFLICT DO NOTHING RETURNING`` reports exactly the rows this
        call newly wrote, so ``appended`` and ``skipped`` are exact even
        under a concurrent same-session appender (no count-subtraction race).

        Raises:
          NotFoundError: ``session_id`` is not an existing inquiry.
          ConflictError: ``session_id`` is not an ``AgentSession`` row (an
            Issue / Belief is not a session), or it has already ended -- a
            dead session's poller will never inject the events, mirroring the
            ``/inbound`` enqueue route's ended-session 409.
          ValueError: A body's ``kind`` disagrees with its ``message`` type.

        """
        if not events:
            return (0, 0)
        # Build typed events first: ``to_event`` runs the kind/message
        # invariant, so a mismatch fails before any row is written.
        typed = [ev.to_event(session_id) for ev in events]
        rows = [
            (
                session_id,
                e.seq,
                e.model,
                e.kind,
                e.timestamp,
                json_unfreeze(_strip_postgres_nuls(DataclassCodec.to_json(e.message))),
            )
            for e in typed
        ]
        # One transaction so the kind check, the insert, and the row-accounting
        # are atomic: a concurrent same-session appender can neither slip events
        # under a non-session guard nor inflate the count between the two reads.
        # ``notify_after_commit`` lets a successful append wake live subscribers
        # (the SPA session view, a ``trax run`` injection listener) -- the
        # session id is an inquiry id, so the existing ``/api/web/subscribe``
        # fanout carries it with no new payload shape.
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            # Read kind + liveness under the row lock so a concurrent ``end``
            # can't slip between the check and the insert: events must attach
            # only to a LIVE AgentSession.
            session = await conn.fetchrow(
                "SELECT kind, agentsession_ended FROM inquiries "
                "WHERE id = $1 FOR UPDATE",
                session_id,
            )
            if session is None:
                raise NotFoundError(f"session {session_id} not found")
            if session["kind"] != "AgentSession":
                raise ConflictError(
                    f"inquiry {session_id} is not an AgentSession "
                    f"(kind={session['kind']!r}); events may only attach to a session"
                )
            if session["agentsession_ended"] is not None:
                # The session is closed; its poller is gone and will never
                # inject these events. Reject (no row written), mirroring the
                # ``/inbound`` enqueue route's ended-session 409.
                raise ConflictError(
                    f"session {session_id} has ended; cannot append events"
                )
            # ``ON CONFLICT DO NOTHING RETURNING`` reports exactly the rows
            # this statement newly inserted -- the seqs that did NOT collide.
            # A single ``unnest`` INSERT (not ``executemany``, which cannot
            # RETURNING) makes the accounting exact and race-free: a
            # concurrent same-session commit cannot inflate the count because
            # we count returned rows, not a whole-session ``count(*)`` delta.
            try:
                inserted = await conn.fetch(
                    "INSERT INTO agent_session_events "
                    "(session_id, seq, model, kind, timestamp, message) "
                    "SELECT * FROM unnest("
                    "$1::uuid[], $2::int[], $3::text[], $4::text[], "
                    "$5::timestamptz[], $6::jsonb[]) "
                    "ON CONFLICT (session_id, seq) DO NOTHING "
                    "RETURNING seq",
                    [r[0] for r in rows],
                    [r[1] for r in rows],
                    [r[2] for r in rows],
                    [r[3] for r in rows],
                    [r[4] for r in rows],
                    [r[5] for r in rows],
                )
            except asyncpg.ForeignKeyViolationError as exc:
                # The ``session_id`` FK to ``inquiries`` failed: the session
                # was purged in the race window between the kind check and the
                # insert. It is gone, so this is a clean 404 -- not a raw 409
                # that would leak the constraint name.
                raise NotFoundError(f"session {session_id} not found") from exc
            appended = len(inserted)
            # Only a real append wakes subscribers: a fully-duplicate retry
            # (appended == 0) is a no-op and must stay silent.
            if appended:
                self._buffer_notification(session_id)
        return (appended, len(events) - appended)

    async def read_session_events(
        self,
        session_id: UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
        seq_ranges: Sequence[SeqRange] = (),
        kind: str | None = None,
    ) -> list[EventBody]:
        """Read a window of one session's events in ``seq`` order.

        The read half of the capture seam. Returns typed
        :class:`EventBody`s ordered by ``seq`` (out-of-order appends sort
        correctly because ``seq`` is the order key, not insertion time).
        ``seq_ranges`` is a union of inclusive intervals, sharing the
        ``OR``-of-intervals lowering with :meth:`list_kind`; ``kind``
        narrows to one turn kind. ``limit`` bounds the window so a caller
        never pulls an arbitrarily large session into memory at once.
        """
        clauses = ["session_id = $1"]
        params: list[object] = [session_id]
        if (seq_clause := seq_range_clause(params, seq_ranges)) is not None:
            clauses.append(seq_clause)
        if kind is not None:
            params.append(kind)
            clauses.append(f"kind = ${len(params)}")
        where = " AND ".join(clauses)
        params.append(limit if limit is not None else DEFAULT_LIST_LIMIT)
        limit_pos = len(params)
        params.append(offset)
        offset_pos = len(params)
        sql = vetted_sql(
            "SELECT seq, kind, timestamp, model, message "
            "FROM agent_session_events WHERE ",
            where,
            " ORDER BY seq LIMIT $",
            str(limit_pos),
            " OFFSET $",
            str(offset_pos),
        )
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [
            EventBody(
                seq=row["seq"],
                kind=row["kind"],
                timestamp=row["timestamp"],
                model=row["model"],
                message=row["message"] or {},
            )
            for row in rows
        ]

    async def read_feed(
        self,
        *,
        after: tuple[datetime, UUID, int] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        room: str | None = None,
        actor: str | None = None,
        limit: int = 200,
        tail: bool = False,
    ) -> list[FeedEvent]:
        """Read a cross-session window of captured turns, oldest first.

        The backing read for the multi-agent console: it interleaves turns from
        every session into one stream ordered by ``(created, session_id, seq)``
        -- the server write clock, since per-session ``seq`` is not comparable
        across sessions. Each turn is joined to its ``AgentSession`` row for the
        routing context the console shows (``actor`` = owner, ``rooms``, ``cli``).

        Args:
          after: Exclusive composite keyset cursor ``(created, session_id,
            seq)`` to resume past. The cursor is composite (not bare
            ``created``) because the order key is, so a page boundary falling
            inside a same-``created`` group does not skip the rows after it.
          since: Inclusive lower bound on ``created`` -- a historical window's
            start (distinct from ``after``, which is the exclusive resume
            cursor); combine with ``until`` to bound a fixed window.
          until: Inclusive upper bound on ``created``; bounds a window's end.
          room: When set, only turns from sessions joined to this room.
          actor: When set, only turns from sessions owned by this routing name.
          limit: Max turns returned; callers page by advancing ``after``.
          tail: When true, return the *newest* ``limit`` turns (the live tail's
            first page) instead of the oldest, so a large backlog does not force
            the console to replay from the beginning before it catches up. The
            result is still returned oldest-first; only which end is taken
            differs.

        Returns:
          events: ``FeedEvent``s ordered by ``(created, session_id, seq)``.

        """
        clauses = ["i.kind = 'AgentSession'"]
        params: list[object] = []
        if after is not None:
            params.extend(after)
            n = len(params)
            clauses.append(
                f"(e.created, e.session_id, e.seq) > (${n - 2}, ${n - 1}, ${n})"
            )
        if since is not None:
            params.append(since)
            clauses.append(f"e.created >= ${len(params)}")
        if until is not None:
            params.append(until)
            clauses.append(f"e.created <= ${len(params)}")
        if room is not None:
            params.append(room)
            clauses.append(f"${len(params)} = ANY(i.agentsession_rooms)")
        if actor is not None:
            params.append(actor)
            clauses.append(f"i.owner = ${len(params)}")
        params.append(limit)
        where = " AND ".join(clauses)
        # ``tail`` takes the newest page (DESC) then restores ascending order in
        # Python, so the wire shape is always oldest-first regardless of which
        # end was read.
        order = "DESC" if tail else "ASC"
        sql = vetted_sql(
            "SELECT e.session_id, e.seq, e.kind, e.created, e.timestamp, "
            "e.model, e.message, i.owner, i.agentsession_rooms, i.agentsession_cli "
            "FROM agent_session_events e JOIN inquiries i ON i.id = e.session_id "
            "WHERE ",
            where,
            " ORDER BY e.created ",
            order,
            ", e.session_id ",
            order,
            ", e.seq ",
            order,
            " LIMIT $",
            str(len(params)),
        )
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        if tail:
            rows = list(reversed(rows))
        return [
            FeedEvent(
                session_id=row["session_id"],
                actor=row["owner"] or "",
                rooms=list(row["agentsession_rooms"] or []),
                cli=row["agentsession_cli"],
                seq=row["seq"],
                kind=row["kind"],
                created=row["created"],
                timestamp=row["timestamp"],
                model=row["model"],
                message=row["message"] or {},
            )
            for row in rows
        ]

    async def end_session(
        self,
        session_id: UUID,
        *,
        ended: datetime,
        cli_session_id: str | None = None,
        api_key_id: UUID | None = None,
        actor: Inquiry.Actor,
    ) -> datetime:
        """Close an ``AgentSession`` atomically: stamp ``ended`` + ``status``.

        The terminal status is always ``complete``: the lifecycle CHECK ties
        ``ended IS NOT NULL`` to ``status='complete'``, so no other status is
        legal for an ended session. There is therefore no ``status`` parameter
        -- ``end_session`` cannot land a desynced (ended, non-complete) row.

        The whole close is one transaction so a partial failure can never
        leave a zombie session -- ``ended`` set (invisible to messaging,
        which keys on ``ended IS NULL``) while ``status`` stays non-terminal.
        Replaces the route's prior 2-3 separate single-statement txns. When
        ``cli_session_id`` is given it is backfilled in the same tx. Each
        column still emits its own audit row (the per-kind change_log CHECKs
        forbid one row carrying unrelated deltas, so a single composite emit
        is impossible), but all share the transaction, so the audit trail is
        all-or-nothing too.

        Idempotent: a retry that reuses the original close's idempotency key
        (the ``Idempotency-Key`` header, carried in ``_CLIENT_CHANGE_ID``)
        replays the original success and returns -- the ``agentsession_ended``
        change_log row is the anchor, keyed by that UUID. A second close with
        a *different* (or no) key is a genuine duplicate and is rejected.

        Returns:
          ended: The committed ``agentsession_ended`` timestamp -- the value
            just stamped, or, on an idempotent replay, the originally-stored
            one. The route echoes THIS so two same-key /end calls return the
            identical receipt rather than two fresh ``now()`` timestamps.

        Raises:
          NotFoundError: ``session_id`` is not an existing inquiry.
          ConflictError: the row is not an ``AgentSession``, or it is already
            ended by a *different* idempotency key (a genuine second close).

        """
        # The only legal terminal status for an ended AgentSession; the
        # lifecycle CHECK forbids any other once ``ended`` is set.
        status: Inquiry.Status = "complete"
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            row = await conn.fetchrow(
                "SELECT kind, status, agentsession_ended, "
                "agentsession_cli_session_id FROM inquiries "
                "WHERE id = $1 FOR UPDATE",
                session_id,
            )
            if row is None:
                raise NotFoundError(f"inquiry {session_id} not found")
            if row["kind"] != "AgentSession":
                raise ConflictError(
                    f"inquiry {session_id} is a {row['kind']}; "
                    "only an AgentSession can be ended"
                )
            if row["agentsession_ended"] is not None:
                # Already ended. A retry that reuses the *same* idempotency
                # key as the original close is a replay -> return the original
                # 200 receipt, not a 409. The close emits one change_log row per
                # mutated field, and ``emit_change`` stamps the client key (K)
                # on whichever fires FIRST -- the cli_session_id backfill when
                # present, else the ended emit. So probe by (id, subject) alone:
                # K is unique and bound to this one logical close, so any
                # change_log row carrying it for this session proves the retry.
                # Requiring ``kind='agentsession_ended'`` would falsely 409 an
                # end-with-backfill retry whose K landed on the cli row. A
                # different key (or no key) finds no row -> genuine second
                # close -> keep the 409.
                replay_key = _peek_client_change_id()
                if replay_key is not None and (
                    await conn.fetchval(
                        "SELECT 1 FROM change_log WHERE id = $1 AND subject_id = $2",
                        replay_key,
                        session_id,
                    )
                    is not None
                ):
                    # Drain the peeked slot so the leftover key can't leak into
                    # the next same-context write's ``emit_change`` (mirrors
                    # ``_submit_on_conn``'s replay-return clear).
                    set_client_change_id(None)
                    # Replay echoes the COMMITTED ended, not the retry's fresh
                    # request-time value, so the receipt is byte-identical.
                    return cast(datetime, row["agentsession_ended"])
                # Genuine second close. Drain the peeked slot before raising:
                # the replay-success path above drains, but this raise path did
                # not, so the externally-set key leaked into the next mutation
                # in the same task (same bug class as the submit F30 fix). Clear
                # unconditionally so neither outcome leaks the key forward.
                set_client_change_id(None)
                raise ConflictError(
                    f"session {session_id} has already ended; cannot end again"
                )
            if (
                cli_session_id is not None
                and cli_session_id != row["agentsession_cli_session_id"]
            ):
                await self._update_field(
                    conn, session_id, "agentsession_cli_session_id", cli_session_id
                )
                await self._emit_field_change(
                    conn,
                    session_id,
                    "AgentSession",
                    "agentsession_cli_session_id",
                    Snapshot(
                        agentsession_cli_session_id=row["agentsession_cli_session_id"]
                    ),
                    new=Snapshot(agentsession_cli_session_id=cli_session_id),
                    api_key_id=api_key_id,
                    actor=actor,
                )
            # Stamp ``ended`` and ``status`` in ONE statement: the lifecycle
            # CHECK (``ended`` set iff ``status='complete'``) is evaluated per
            # statement, so two separate UPDATEs would expose the intermediate
            # (ended set, status still 'active') desync and violate it. One
            # UPDATE moves the row straight from live to (ended, complete).
            await conn.execute(
                "UPDATE inquiries SET agentsession_ended = $1, status = $2, "
                "modified = clock_timestamp() WHERE id = $3",
                ended,
                status,
                session_id,
            )
            # Audit rows are separate change_log inserts (one change_kind per
            # row); they don't touch the inquiries CHECK.
            await self._emit_field_change(
                conn,
                session_id,
                "AgentSession",
                "agentsession_ended",
                Snapshot(agentsession_ended=None),
                new=Snapshot(agentsession_ended=ended),
                api_key_id=api_key_id,
                actor=actor,
            )
            if row["status"] != status:
                await self._emit_field_change(
                    conn,
                    session_id,
                    "AgentSession",
                    "status",
                    Snapshot(status=row["status"]),
                    new=Snapshot(status=status),
                    api_key_id=api_key_id,
                    actor=actor,
                )
            return ended
