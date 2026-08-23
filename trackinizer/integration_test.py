"""End-to-end integration suite (Postgres-backed; session-scoped engine; marked)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import asyncio
import json
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

import asyncpg
import httpx
import pytest

from trackinizer.conftest import new_uuid
from trackinizer.server import web
from trackinizer.server.api import (
    edit,
    metrics_routes,
    query as query_module,
    sessions_routes,
)
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    make_test_identity,
)
from trackinizer.server.api.idempotency import ChangeIdMiddleware
from trackinizer.server.auth import (
    BOOTSTRAP_ADMIN_ENV,
    BOOTSTRAP_TOKEN_FILE_ENV,
    bootstrap_admin,
    create_api_key,
    current_user,
    revoke_api_key,
)
from trackinizer.server.config import Config
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.store.change_id_slot import (
    set_client_change_id,
)
from trackinizer.server.store.core import Store
from trackinizer.server.store.edge import INFERRED_PROVENANCE_REASON
from trackinizer.types.agent_session_events import (
    AssistantMessage,
    ToolResult,
    UserMessage,
)
from trackinizer.types.cost import Cost
from trackinizer.types.edges import Edge
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from trackinizer.types.inquiries import (
    AgentSession,
    ArtifactEdge,
    Belief,
    Experiment,
    Issue,
    Paper,
)
from trackinizer.wire.bodies import (
    BatchEdge,
    Citation,
    SubmitAgentSession,
    SubmitArtifact,
    SubmitBelief,
    SubmitCodeChange,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
    SubmitWebSearch,
)
from trackinizer.wire.filters import Filter
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_sessions import EventBody


async def _seed_active_user(store: Store, email: str) -> None:
    """Seed ``email`` as an ACTIVE ``users`` row so session routes accept it.

    The session-start route resolves the request's ``account`` from the
    authenticated identity's email and rejects (422) any email that is not an
    active user. Route tests inject an identity but truncate the ``users``
    table per test, so each must seed its own principal first.
    """
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'u', 'writer', 'active') "
            "ON CONFLICT (email) DO NOTHING",
            uuid.uuid4(),
            email,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestIntegrationEndToEnd:
    """End-to-end cascade behaviour against a session-scoped Postgres.

    The shared ``integ_engine`` fixture amortises pool/codec setup; each
    test pays only a ``TRUNCATE`` reset.
    """

    async def test_per_kind_seq_is_monotonic(self, integ_store: Store) -> None:
        i1 = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i1")
        )
        i2 = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i2")
        )
        c1 = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="c1")
        )
        assert cast(Issue, await integ_store.get_inquiry(i1)).seq == 1
        assert cast(Issue, await integ_store.get_inquiry(i2)).seq == 2
        assert cast(Belief, await integ_store.get_inquiry(c1)).seq == 1

    async def test_session_events_append_read_and_dedup(
        self, integ_store: Store
    ) -> None:
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="codex",
                cli_session_id="t1",
            )
        )
        batch = [
            EventBody(
                seq=0, kind="UserMessage", message=UserMessage(text="hi").to_json()
            ),
            EventBody(seq=1, kind="AssistantMessage", model="gpt-5.5"),
            EventBody(
                seq=2,
                kind="AssistantMessage",
                message=AssistantMessage(text="ok").to_json(),
            ),
        ]
        appended, skipped = await integ_store.append_events(sid, batch)
        assert (appended, skipped) == (3, 0)

        # A retried batch is a no-op on (session_id, seq).
        appended2, skipped2 = await integ_store.append_events(sid, batch)
        assert (appended2, skipped2) == (0, 3)

        events = await integ_store.read_session_events(sid)
        assert [e.seq for e in events] == [0, 1, 2]
        assert events[1].kind == "AssistantMessage"
        assert events[1].model == "gpt-5.5"
        assert events[0].to_event(sid).message == UserMessage(text="hi")

    async def test_log_metrics_append_read_and_dedup(self, integ_store: Store) -> None:
        """Metric points log, read back in (key, step) order, and dedup.

        The wandb ``log()`` analogue: ``(experiment_id, key, step)`` is the
        dedup key, so a retried batch is a no-op and reports ``logged=0``.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        batch = [
            MetricPoint(key="loss", step=0, value=0.9),
            MetricPoint(key="loss", step=1, value=0.5),
            MetricPoint(key="acc", step=0, value=0.1, kind="scalar"),
        ]
        logged, skipped = await integ_store.log_metrics(eid, batch)
        assert (logged, skipped) == (3, 0)

        # A retried batch is a no-op on (experiment_id, key, step).
        logged2, skipped2 = await integ_store.log_metrics(eid, batch)
        assert (logged2, skipped2) == (0, 3)

        # Read back in (key, step) order: acc before loss, loss ascending.
        points = await integ_store.read_metrics(eid)
        assert [(p.key, p.step, p.value) for p in points] == [
            ("acc", 0, 0.1),
            ("loss", 0, 0.9),
            ("loss", 1, 0.5),
        ]

        # ``key`` narrows to one metric.
        loss_only = await integ_store.read_metrics(eid, key="loss")
        assert [p.step for p in loss_only] == [0, 1]

    async def test_log_metrics_within_batch_duplicate_first_wins(
        self, integ_store: Store
    ) -> None:
        """Two points with the same (key, step) in ONE batch: first wins.

        ``ON CONFLICT DO NOTHING`` dedups an intra-statement duplicate against
        the first-inserted row, so the batch reports ``logged=1, skipped=1`` and
        the first value is stored (the second is silently dropped) -- the same
        idempotency contract as a cross-batch retry, pinned here because the
        intra-batch case has subtle Postgres semantics.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        logged, skipped = await integ_store.log_metrics(
            eid,
            [
                MetricPoint(key="loss", step=0, value=0.1),
                MetricPoint(key="loss", step=0, value=0.2),
            ],
        )
        assert (logged, skipped) == (1, 1)
        points = await integ_store.read_metrics(eid)
        assert [(p.key, p.step, p.value) for p in points] == [("loss", 0, 0.1)]

    async def test_log_metrics_timestamp_round_trips(self, integ_store: Store) -> None:
        """A tz-aware ``timestamp`` round-trips through ``TIMESTAMPTZ``."""
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        ts = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        await integ_store.log_metrics(
            eid, [MetricPoint(key="loss", step=0, value=1.0, timestamp=ts)]
        )
        points = await integ_store.read_metrics(eid)
        assert points[0].timestamp == ts

    async def test_read_metrics_offset_skips_rows(self, integ_store: Store) -> None:
        """``offset`` skips leading rows in (key, step) order."""
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        await integ_store.log_metrics(
            eid,
            [MetricPoint(key="loss", step=i, value=float(i)) for i in range(3)],
        )
        page = await integ_store.read_metrics(eid, limit=1, offset=1)
        assert [(p.key, p.step) for p in page] == [("loss", 1)]

    async def test_log_metrics_concurrent_appenders_exact_no_loss(
        self, integ_store: Store
    ) -> None:
        """Concurrent overlapping batches: no lost writes, exact per-call counts.

        Two ``log_metrics`` calls race on one experiment with overlapping
        ``(key, step)`` ranges. The union must be stored exactly once (no loss,
        no duplicate), and each call's ``logged + skipped`` must equal its own
        batch size -- proving the ``ON CONFLICT DO NOTHING RETURNING`` accounting
        is race-correct (counts returned rows, not a whole-table delta).
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        a = [MetricPoint(key="loss", step=i, value=1.0) for i in range(100)]
        b = [MetricPoint(key="loss", step=i, value=2.0) for i in range(50, 150)]
        (la, sa), (lb, sb) = await asyncio.gather(
            integ_store.log_metrics(eid, a),
            integ_store.log_metrics(eid, b),
        )
        back = await integ_store.read_metrics(eid, limit=10_000)
        assert len(back) == 150  # union 0..149: no loss, no duplicate
        assert la + lb == 150  # total newly-written == distinct rows
        assert (la + sa, lb + sb) == (100, 100)  # each call accounts for its batch

    async def test_log_metrics_large_batch_at_cap(self, integ_store: Store) -> None:
        """A max-size batch inserts and reads back in order at scale."""
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        pts = [MetricPoint(key="loss", step=i, value=float(i)) for i in range(10_000)]
        logged, skipped = await integ_store.log_metrics(eid, pts)
        assert (logged, skipped) == (10_000, 0)
        back = await integ_store.read_metrics(eid, limit=10_000)
        assert len(back) == 10_000
        assert back[0].step == 0
        assert back[-1].step == 9999

    async def test_experiment_config_empty_dict_round_trips(
        self, integ_store: Store
    ) -> None:
        """An empty ``config={}`` is a real value, stored/read as ``{}`` not NULL.

        ``primitives.insert_inquiry`` binds ``config`` raw (a dict is neither str
        nor sequence, so ``empty_optional_to_none`` never collapses ``{}`` to
        NULL) -- this pins that the empty-dict is preserved, not silently
        dropped.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run", config={})
        )
        row = cast(Experiment, await integ_store.get_inquiry(eid))
        assert row.config == {}

    async def test_experiment_config_reorder_is_noop_edit(
        self, integ_store: Store
    ) -> None:
        """Re-setting a key-reordered but equal config is a no-op (no phantom row).

        JSONB dedup relies on Python dict ``==`` (order-insensitive) in
        ``_set_field``; a reordered-equal config must return ``None`` (no change)
        rather than write a phantom ``experiment_config`` audit row.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com", title="run", config={"a": 1, "b": 2}
            )
        )
        change_id = await integ_store.set_config(
            eid, {"b": 2, "a": 1}, actor="scientist"
        )
        assert change_id is None

    async def test_log_metrics_latest_value_per_key_via_join(
        self, integ_store: Store
    ) -> None:
        """The latest-value roll-up is a DISTINCT ON over the PK index.

        No summary column: ``DISTINCT ON (key) ... ORDER BY key, step DESC``
        reads the last point per key straight off ``(experiment_id, key,
        step)``.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        await integ_store.log_metrics(
            eid,
            [
                MetricPoint(key="loss", step=0, value=0.9),
                MetricPoint(key="loss", step=5, value=0.2),
                MetricPoint(key="acc", step=3, value=0.7),
            ],
        )
        async with integ_store.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT ON (key) key, value FROM experiment_metrics "
                "WHERE experiment_id = $1 ORDER BY key, step DESC",
                eid,
            )
        assert {r["key"]: r["value"] for r in rows} == {"loss": 0.2, "acc": 0.7}

    async def test_metrics_db_rejects_negative_step(self, integ_store: Store) -> None:
        """The ``step >= 0`` DB CHECK backstops the wire ``ge=0`` guard.

        Mirrors ``agent_session_events.seq``'s CHECK: a direct-SQL writer that
        bypasses the wire cannot persist a negative step.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        async with integ_store.engine.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO experiment_metrics (experiment_id, key, step, value) "
                    "VALUES ($1, 'loss', -1, 0.5)",
                    eid,
                )

    async def test_metrics_db_rejects_bad_key(self, integ_store: Store) -> None:
        """The key length/non-blank DB CHECK backstops the wire.

        The wire ``MetricPoint.key`` is ``Field(min_length=1, max_length=512)``
        + non-blank, and ``read_metrics`` reconstructs it, so a stored empty,
        blank, or over-long key would 500 the read. The CHECK stops a
        direct-SQL / bulk-load writer from persisting one -- completing the
        backstop set alongside the step / value / kind CHECKs.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        async with integ_store.engine.acquire() as conn:
            for bad_key, step in (("", 0), ("   ", 1), ("x" * 513, 2)):
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(
                        "INSERT INTO experiment_metrics "
                        "(experiment_id, key, step, value) VALUES ($1, $2, $3, 0.5)",
                        eid,
                        bad_key,
                        step,
                    )

    async def test_metrics_db_rejects_non_finite_value(
        self, integ_store: Store
    ) -> None:
        """The finiteness DB CHECK backstops the wire ``allow_inf_nan=False``.

        The wire rejects NaN/±Inf and ``read_metrics`` reconstructs the value,
        so a stored non-finite value would 500 the read (NaN/±Inf are valid
        float8 but not valid JSON numbers). The CHECK stops a direct-SQL /
        bulk-load writer from persisting one -- completing the backstop set
        alongside the ``step >= 0`` and ``kind = 'scalar'`` CHECKs.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        # Each non-finite literal is a fixed SQL constant (not interpolated
        # input); one INSERT per value, distinct steps to avoid a PK collision
        # masking the CHECK.
        inserts = (
            (
                "INSERT INTO experiment_metrics (experiment_id, key, step, value) "
                "VALUES ($1, 'loss', 0, 'NaN'::float8)"
            ),
            (
                "INSERT INTO experiment_metrics (experiment_id, key, step, value) "
                "VALUES ($1, 'loss', 1, 'Infinity'::float8)"
            ),
            (
                "INSERT INTO experiment_metrics (experiment_id, key, step, value) "
                "VALUES ($1, 'loss', 2, '-Infinity'::float8)"
            ),
        )
        async with integ_store.engine.acquire() as conn:
            for stmt in inserts:
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(stmt, eid)

    async def test_metrics_db_rejects_non_scalar_kind(self, integ_store: Store) -> None:
        """The ``kind = 'scalar'`` DB CHECK backstops the wire ``Literal``.

        The wire closes ``kind`` to ``"scalar"`` and ``read_metrics``
        reconstructs that Literal, so a stored non-scalar row would 500 the read
        (the same failure class the value finiteness guard closes). The CHECK
        stops a direct-SQL / bulk-load writer from persisting one -- mirroring
        the ``agent_session_events.kind`` CHECK.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run")
        )
        async with integ_store.engine.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO experiment_metrics "
                    "(experiment_id, key, step, value, kind) "
                    "VALUES ($1, 'loss', 0, 0.5, 'histogram')",
                    eid,
                )

    async def test_experiment_config_round_trips_and_edits(
        self, integ_store: Store
    ) -> None:
        """``config`` stores a JSON object, reads back as a dict, and edits.

        The wandb ``wandb.init(config=...)`` analogue: hyperparameters ride on
        the Experiment row as one JSONB object, opaque to trackinizer.
        """
        cfg: dict[str, object] = {
            "lr": 3e-4,
            "batch": 32,
            "nested": {"warmup": 100},
            "tags": ["a"],
        }
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run", config=cfg)
        )
        row = cast(Experiment, await integ_store.get_inquiry(eid))
        assert row.config == cfg  # dict in, dict out (asyncpg jsonb codec)

        # Edit via set_config: overwrite the whole object.
        new_cfg: dict[str, object] = {"lr": 1e-4, "batch": 64}
        await integ_store.set_config(eid, new_cfg, actor="scientist")
        row2 = cast(Experiment, await integ_store.get_inquiry(eid))
        assert row2.config == new_cfg

        # Clear to NULL.
        await integ_store.set_config(eid, None, actor="scientist")
        row3 = cast(Experiment, await integ_store.get_inquiry(eid))
        assert row3.config is None

    async def test_log_metrics_rejects_non_experiment(self, integ_store: Store) -> None:
        """A metric may only attach to an Experiment; other kinds 409, missing 404."""
        belief = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="b")
        )
        with pytest.raises(ConflictError, match="not an Experiment"):
            await integ_store.log_metrics(
                belief, [MetricPoint(key="loss", step=0, value=1.0)]
            )
        with pytest.raises(NotFoundError, match="not found"):
            await integ_store.log_metrics(
                new_uuid(), [MetricPoint(key="loss", step=0, value=1.0)]
            )

    async def test_append_events_rejected_on_ended_session(
        self, integ_store: Store
    ) -> None:
        """An ended session rejects further events (its poller is gone).

        ``append_events`` guards liveness symmetrically with the ``/inbound``
        enqueue route: a closed session 409s and writes no row.
        """
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="run", cli="codex")
        )
        await integ_store.append_events(sid, [EventBody(seq=0, kind="UserMessage")])
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="u")
        with pytest.raises(ConflictError, match="has ended"):
            await integ_store.append_events(sid, [EventBody(seq=1, kind="UserMessage")])
        # No new row: only the pre-end event remains.
        events = await integ_store.read_session_events(sid)
        assert [e.seq for e in events] == [0]

    async def test_start_session_correlates_resume_by_cli_session_id(
        self, integ_store: Store
    ) -> None:
        """A start with a known ``cli_session_id`` re-attaches the prior session.

        Resume: the same CLI session id maps back to the original AgentSession
        (same id, same granted handle), re-opens it if ended, and reports the
        event log's continuation seq so the resumed run appends instead of
        forking a new seq=0 log.
        """
        # Fresh start with a cli_session_id, capture one event, then end it.
        sid, owner, seq0 = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="abc123",
            ),
            requested_actor="scientist",
        )
        assert seq0 == 0  # fresh log starts at 0
        await integ_store.append_events(sid, [EventBody(seq=0, kind="UserMessage")])
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor=owner)

        # Resume: same cli_session_id re-attaches the SAME session + handle,
        # re-opens it, and reports the continuation seq (1, after the seq-0 event).
        rsid, rowner, rseq = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="abc123",
            ),
            requested_actor="scientist",
        )
        assert rsid == sid
        assert rowner == owner
        assert rseq == 1  # continues after the prior seq-0 event
        # The session is live again (re-opened), so append succeeds.
        await integ_store.append_events(sid, [EventBody(seq=1, kind="UserMessage")])
        events = await integ_store.read_session_events(sid)
        assert [e.seq for e in events] == [0, 1]

    async def test_resume_is_principal_scoped(self, integ_store: Store) -> None:
        """A resume only re-attaches a session opened by the SAME credential (B2).

        Correlating on ``cli_session_id`` alone would let any writer resume
        another principal's session (cross-principal hijack). Resume must be
        scoped to ``opened_by_api_key_id``, exactly as the inbound-drain route
        is. A different credential gets a FRESH session, not the other's.
        """
        alice_key, bob_key = uuid.uuid4(), uuid.uuid4()
        async with integ_store.engine.acquire() as conn:
            for kid, email in ((alice_key, "alice@x"), (bob_key, "bob@x")):
                uid = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO users (id, email, name, role, status) "
                    "VALUES ($1, $2, 'u', 'writer', 'active')",
                    uid,
                    email,
                )
                await conn.execute(
                    "INSERT INTO api_keys (id, user_id, name, prefix, "
                    "secret_hash, role) VALUES ($1, $2, 'k', $3, 'h', 'writer')",
                    kid,
                    uid,
                    str(kid)[:8],
                )
        # Alice opens a session with a cli_session_id.
        a_sid, _a_owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="a",
                cli="claude",
                cli_session_id="shared-id",
            ),
            requested_actor="alice",
            api_key_id=alice_key,
        )
        # Bob, knowing the cli_session_id, must NOT resume Alice's session.
        b_sid, _b_owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="b",
                cli="claude",
                cli_session_id="shared-id",
            ),
            requested_actor="bob",
            api_key_id=bob_key,
        )
        assert b_sid != a_sid  # Bob got a fresh session, not Alice's

        # Alice resuming her OWN session re-attaches it.
        a_resume, _, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="a",
                cli="claude",
                cli_session_id="shared-id",
            ),
            requested_actor="alice",
            api_key_id=alice_key,
        )
        assert a_resume == a_sid

    async def test_resume_no_auth_matches_by_cli_session_id_alone(
        self, integ_store: Store
    ) -> None:
        """In ``--no-auth`` (api_key_id=None) resume scopes on cli_session_id only.

        Every no-auth session stamps ``opened_by = NULL``, so the
        ``IS NOT DISTINCT FROM NULL`` scope degrades to the cli_session_id --
        and a None-credentialed resume re-attaches the original session. (There
        is no per-principal isolation to enforce in no-auth mode.)
        """
        sid, _owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="noauth-id",
            ),
            requested_actor="scientist",
            api_key_id=None,
        )
        resumed, _, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="noauth-id",
            ),
            requested_actor="scientist",
            api_key_id=None,
        )
        assert resumed == sid  # re-attached, not a fresh session

    async def test_resume_applies_rooms_on_live_session(
        self, integ_store: Store
    ) -> None:
        """Resuming a still-LIVE session (not ended) also joins new rooms.

        The rooms-apply runs whether or not the session was ended, so a resume
        that adds a room to a live re-attach joins it (and re-adding an existing
        room is an idempotent no-op).
        """
        sid, _owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="live-id",
                rooms=["a"],
            ),
            requested_actor="scientist",
        )
        # No end_session: the session is still live. Resume with a new room.
        resumed, _, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="live-id",
                rooms=["a", "b"],
            ),
            requested_actor="scientist",
        )
        assert resumed == sid
        row = cast(AgentSession, await integ_store.get_inquiry(sid))
        assert set(row.rooms or ()) == {"a", "b"}

    async def test_resume_applies_new_body_fields(self, integ_store: Store) -> None:
        """A resume applies new SubmitAgentSession fields, e.g. rooms (B7).

        ``trax run --resume --room workshop`` must JOIN the new room, not
        silently drop it. The resume tx applies the request's rooms.
        """
        sid, owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="rooms-id",
                rooms=["a"],
            ),
            requested_actor="scientist",
        )
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor=owner)
        # Resume with a new room set.
        rsid, _, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="rooms-id",
                rooms=["a", "b"],
            ),
            requested_actor="scientist",
        )
        assert rsid == sid
        row = cast(AgentSession, await integ_store.get_inquiry(sid))
        assert set(row.rooms or ()) == {"a", "b"}

    async def test_resume_audit_attributed_to_resuming_caller(
        self, integ_store: Store
    ) -> None:
        """The re-open audit names the resuming caller, not the original owner (B3)."""
        sid, owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="audit-id",
            ),
            requested_actor="alice",
        )
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor=owner)
        # A different actor resumes; the re-open change must credit THEM.
        await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="audit-id",
            ),
            requested_actor="bob",
        )
        async with integ_store.engine.acquire() as conn:
            actor = await conn.fetchval(
                "SELECT actor FROM change_log WHERE subject_id = $1 "
                "AND kind = 'agentsession_ended' ORDER BY created DESC LIMIT 1",
                sid,
            )
        assert actor == "bob"

    async def test_start_session_fresh_when_no_cli_session_id_match(
        self, integ_store: Store
    ) -> None:
        """A null or unmatched ``cli_session_id`` opens a fresh session.

        Two fresh runs with no cli_session_id must NOT correlate to each other
        (the null-match trap): each gets its own session + handle.
        """
        sid_a, owner_a, _ = await integ_store.start_session(
            SubmitAgentSession(account="tester@example.com", title="a", cli="claude"),
            requested_actor="scientist",
        )
        sid_b, owner_b, _ = await integ_store.start_session(
            SubmitAgentSession(account="tester@example.com", title="b", cli="claude"),
            requested_actor="scientist",
        )
        assert sid_a != sid_b
        assert (owner_a, owner_b) == ("scientist", "scientist#2")

    async def test_start_session_replay_drains_leaked_change_id(
        self, integ_store: Store
    ) -> None:
        """A ``start_session`` replay must not leak the change-id into the next write.

        ``start_session`` peeks the client change-id (``_CLIENT_CHANGE_ID``) for
        its replay probe and returns early on a hit WITHOUT consuming it. If it
        leaves the slot set, the NEXT write in the same context wrongly consumes
        that key and either collides on ``change_log.id`` or mis-attributes the
        mutation. The replay-return path must drain the slot, exactly as
        ``_submit_on_conn`` does. (HTTP is safe via task-scoping + middleware;
        this guards direct/same-context callers.)
        """
        key = new_uuid()
        # First start mints the session under key K.
        set_client_change_id(key)
        sid, _owner, _seq = await integ_store.start_session(
            SubmitAgentSession(account="tester@example.com", title="run", cli="codex"),
            requested_actor="alice",
        )
        # Replay the same start: K is on the slot, the probe peeks it, returns
        # the original (id, owner) -- and MUST drain the slot.
        set_client_change_id(key)
        replay_sid, _, _ = await integ_store.start_session(
            SubmitAgentSession(account="tester@example.com", title="run", cli="codex"),
            requested_actor="alice",
        )
        assert replay_sid == sid
        # The leaked key must NOT survive into a subsequent keyless write.
        new_sid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="after")
        )
        async with integ_store.engine.acquire() as conn:
            change_id = await conn.fetchval(
                "SELECT id FROM change_log WHERE subject_id = $1 AND kind = 'created'",
                new_sid,
            )
        assert change_id != key

    async def test_end_session_replay_drains_leaked_change_id(
        self, integ_store: Store
    ) -> None:
        """An ``end_session`` replay must not leak the change-id into the next write."""
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="run", cli="codex")
        )
        key = new_uuid()
        set_client_change_id(key)
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="u")
        # Replay the same end under the same key: probe peeks, returns -- drain.
        set_client_change_id(key)
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="u")
        # A subsequent keyless write must not consume the leaked key.
        new_sid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="after")
        )
        async with integ_store.engine.acquire() as conn:
            change_id = await conn.fetchval(
                "SELECT id FROM change_log WHERE subject_id = $1 AND kind = 'created'",
                new_sid,
            )
        assert change_id != key

    async def test_end_session_always_lands_complete(self, integ_store: Store) -> None:
        """``end_session`` hardcodes ``status='complete'`` -- no free status param.

        The only legal terminal status for an ended AgentSession is
        ``complete`` (the lifecycle CHECK ties ``ended IS NOT NULL`` to
        ``status='complete'``). ``end_session`` therefore takes no ``status``
        argument; it always stamps ``complete``, so a caller cannot land a
        desynced (ended, non-complete) row through it.
        """
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="run", cli="codex")
        )
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="u")
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, agentsession_ended FROM inquiries WHERE id = $1", sid
            )
        assert row is not None
        assert row["status"] == "complete"
        assert row["agentsession_ended"] is not None

    async def test_reserve_session_actor_suffixes_across_all_sessions(
        self, integ_store: Store
    ) -> None:
        """A routing name is reserved for a session's LIFETIME, never reused.

        A free name passes through; a name held by ANY session (live or ended)
        gets the smallest free ``#N`` suffix. Handles are monotonic, like a
        sequence: ending a session does NOT free its name, because the session
        may resume later and must reclaim its original handle.
        """
        # Free name: returned unchanged.
        assert await integ_store.reserve_session_actor("scientist") == "scientist"

        # Open a session owning "scientist".
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com", title="s", cli="claude", owner="scientist"
            ),
            actor="scientist",
        )
        assert await integ_store.reserve_session_actor("scientist") == "scientist#2"

        # A second "scientist" pushes the next reservation to #3.
        await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="s2",
                cli="claude",
                owner="scientist#2",
            ),
            actor="scientist#2",
        )
        assert await integ_store.reserve_session_actor("scientist") == "scientist#3"

        # Ending the first does NOT free "scientist": the handle is held for the
        # session's lifetime (it may resume). Reservation still skips to #3.
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="scientist")
        assert await integ_store.reserve_session_actor("scientist") == "scientist#3"

    async def test_concurrent_session_starts_get_distinct_routing_names(
        self, integ_store: Store
    ) -> None:
        """Two concurrent starts for one actor get distinct routing names.

        The advisory reserve-then-insert window let both starts read the name
        as free and both win it. The live-owner partial unique index plus
        ``start_session``'s retry make the DB the arbiter, so one gets
        ``scientist`` and the other ``scientist#2`` -- never both ``scientist``.
        """
        results = await asyncio.gather(
            integ_store.start_session(
                SubmitAgentSession(
                    account="tester@example.com", title="a", cli="codex"
                ),
                requested_actor="scientist",
            ),
            integ_store.start_session(
                SubmitAgentSession(
                    account="tester@example.com", title="b", cli="codex"
                ),
                requested_actor="scientist",
            ),
        )
        granted = sorted(name for _, name, _ in results)
        assert granted == ["scientist", "scientist#2"]
        # Both rows persisted with the names they were granted.
        for sid, name, _ in results:
            row = cast(AgentSession, await integ_store.get_inquiry(sid))
            assert row.owner == name

    async def test_start_session_same_key_replays_original_owner(
        self, integ_store: Store
    ) -> None:
        """A same-key start retry replays the original (id, owner), no phantom.

        ``start_session`` probes the idempotency key BEFORE reserving, so a
        retry returns the original ``alice`` instead of reserving a fresh
        ``alice#2`` (which would burn a suffix and leave the routing handle
        wrong). Two same-key starts therefore yield the identical id and owner.
        """
        key = new_uuid()
        first_id, first_owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="a",
                cli="codex",
                idempotency_key=key,
            ),
            requested_actor="alice",
        )
        retry_id, retry_owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="a",
                cli="codex",
                idempotency_key=key,
            ),
            requested_actor="alice",
        )
        assert retry_id == first_id
        assert first_owner == "alice"
        assert retry_owner == "alice"

    async def test_agentsession_lifecycle_check_forbids_zombies(
        self, integ_store: Store
    ) -> None:
        """The DB CHECK ties ``ended`` to ``status='complete'`` for sessions.

        Both zombie states are unrepresentable at storage: born ended while
        not complete (A2a), or marked complete while ended stays NULL (A2b).
        A live session may still be abandoned/invalidated (ended NULL).
        """
        # A2a: a create-time ``ended`` (status defaults 'active') is rejected.
        # The submit body no longer carries ``ended``; drive the raw column
        # to prove the storage CHECK is the enforcement point.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with integ_store.engine.acquire() as conn:
                await insert_inquiry(
                    conn,
                    new_uuid(),
                    "AgentSession",
                    values={
                        "title": "zombie",
                        "account": "tester@example.com",
                        "agentsession_ended": datetime.now(UTC),
                    },
                )

        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="live", cli="codex")
        )
        # A2b: completing without stamping ended (set_status alone) -> 409.
        with pytest.raises(ConflictError, match="check constraint"):
            await integ_store.set_status(sid, "complete", actor="u")

        # /end stamps ended + status=complete together -> succeeds.
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="u")
        ended = await integ_store.get_inquiry(sid)
        assert ended is not None
        assert ended.status == "complete"

        # A live session can be abandoned with ``ended`` left NULL.
        sid2 = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="aband", cli="codex")
        )
        await integ_store.set_status(sid2, "abandoned", actor="u")
        abandoned = cast(AgentSession, await integ_store.get_inquiry(sid2))
        assert abandoned.status == "abandoned"
        assert abandoned.ended is None

    async def test_session_start_route_renegotiates_actor(
        self, integ_store: Store
    ) -> None:
        """``POST /api/sessions/start`` returns a live-unique granted actor."""
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.state.config = Config()
        app.include_router(sessions_routes.router)

        # Session-authed (api_key_id=None) so the created change_log row needs
        # no api_keys FK row.
        async def _identity() -> object:
            return make_test_identity(api_key_id=None)

        await _seed_active_user(integ_store, TEST_USER_EMAIL)
        app.dependency_overrides[current_user] = _identity
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                first = await http.post(
                    "/api/sessions/start",
                    json={"cli": "claude", "actor": "scientist"},
                )
                assert first.status_code == 201, first.text
                assert first.json()["actor"] == "scientist"
                # A second concurrent start with the same name is suffixed.
                second = await http.post(
                    "/api/sessions/start",
                    json={"cli": "claude", "actor": "scientist"},
                )
                assert second.status_code == 201, second.text
                assert second.json()["actor"] == "scientist#2"

                # ``rooms`` round-trips: start with membership, read it back
                # off the stored AgentSession row.
                roomed = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "eng", "rooms": ["sear", "lab"]},
                )
                assert roomed.status_code == 201, roomed.text
                sid = uuid.UUID(roomed.json()["id"])
        finally:
            app.dependency_overrides.pop(current_user, None)
        row = cast(AgentSession, await integ_store.get_inquiry(sid))
        assert row.rooms is not None
        assert sorted(row.rooms) == ["lab", "sear"]

    async def test_metrics_and_config_round_trip_through_http(
        self, integ_store: Store
    ) -> None:
        """End-to-end: log metrics + read them + read config over HTTP.

        Proves the full chain the SPA depends on: ``POST/GET
        /api/experiments/{id}/metrics`` (the metrics routes) and the ``config``
        surfacing in ``GET /api/web/get/{id}`` (the detail view). Config rides on
        the submit; metrics are logged then read back in (key, step) order.
        """
        cfg: dict[str, object] = {"lr": 3e-4, "batch": 32}
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run", config=cfg)
        )
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        app.include_router(metrics_routes.router)
        web.attach(app)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None)

        await _seed_active_user(integ_store, TEST_USER_EMAIL)
        app.dependency_overrides[current_user] = _identity
        app.dependency_overrides[web.optional_identity] = _identity
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                logged = await http.post(
                    f"/api/experiments/{eid}/metrics",
                    json={
                        "points": [
                            {"key": "loss", "step": 0, "value": 0.9},
                            {"key": "loss", "step": 1, "value": 0.5},
                            {"key": "acc", "step": 0, "value": 0.4},
                        ]
                    },
                )
                assert logged.status_code == 200, logged.text
                assert logged.json() == {"logged": 3, "skipped": 0}

                read = await http.get(f"/api/experiments/{eid}/metrics")
                assert read.status_code == 200, read.text
                pts = read.json()["points"]
                assert [(p["key"], p["step"], p["value"]) for p in pts] == [
                    ("acc", 0, 0.4),
                    ("loss", 0, 0.9),
                    ("loss", 1, 0.5),
                ]

                # ``config`` surfaces on the SPA detail view verbatim.
                detail = await http.get(f"/api/web/get/{eid}")
                assert detail.status_code == 200, detail.text
                assert detail.json()["self"]["config"] == cfg

                # An over-cap batch is a clean 422 at the boundary (not a 500 or
                # a memory-pinning mega-INSERT), and writes nothing.
                over = {
                    "points": [
                        {"key": "loss", "step": i, "value": 1.0} for i in range(10_001)
                    ]
                }
                over_resp = await http.post(
                    f"/api/experiments/{eid}/metrics", json=over
                )
                assert over_resp.status_code == 422, over_resp.text
                still = await http.get(f"/api/experiments/{eid}/metrics")
                assert len(still.json()["points"]) == 3  # unchanged
        finally:
            app.dependency_overrides.pop(current_user, None)
            app.dependency_overrides.pop(web.optional_identity, None)

    async def test_rooms_field_edits_round_trip_through_route(
        self, integ_store: Store
    ) -> None:
        """Rooms is editable via the generated field routes (PUT/PATCH/DELETE).

        Regression: ``AgentSession.rooms`` declares ``list_verb_stem`` so the
        wire table generates ``set_rooms`` / ``add_room`` / ``remove_room``
        routes. Before the backing Store methods existed every such edit 500'd
        (the SPA exposes a rooms editor). Proves the full chain -- route ->
        Store setter -> audited change -> stored row -- works for set, add, and
        remove, with each edit landing an auditable ``agentsession_rooms``
        change row.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.state.config = Config()
        app.include_router(sessions_routes.router)
        app.include_router(edit.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="editor@test")

        await _seed_active_user(integ_store, "editor@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                start = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "router-eng", "rooms": ["sear"]},
                )
                sid = start.json()["id"]

                # PUT overwrites the whole membership.
                put = await http.put(
                    f"/api/agentsession/{sid}/rooms",
                    json={"value": ["lab", "ops"], "actor": "editor@test"},
                )
                assert put.status_code == 200, put.text
                row = cast(AgentSession, await integ_store.get_inquiry(uuid.UUID(sid)))
                assert sorted(row.rooms or ()) == ["lab", "ops"]

                # PATCH add joins one more room.
                added = await http.patch(
                    f"/api/agentsession/{sid}/rooms",
                    json={"op": "add", "value": "sear", "actor": "editor@test"},
                )
                assert added.status_code == 200, added.text
                row = cast(AgentSession, await integ_store.get_inquiry(uuid.UUID(sid)))
                assert sorted(row.rooms or ()) == ["lab", "ops", "sear"]

                # PATCH remove leaves the rest.
                removed = await http.patch(
                    f"/api/agentsession/{sid}/rooms",
                    json={"op": "sub", "value": "lab", "actor": "editor@test"},
                )
                assert removed.status_code == 200, removed.text
                row = cast(AgentSession, await integ_store.get_inquiry(uuid.UUID(sid)))
                assert sorted(row.rooms or ()) == ["ops", "sear"]
        finally:
            app.dependency_overrides.pop(current_user, None)

        # Each edit emitted an auditable ``agentsession_rooms`` change row that
        # PRESERVES the old/new value -- not just a change kind with a NULL
        # snapshot. Regression for the AgentSession audit-loss bug: the
        # change_log lacked ``old_/new_agentsession_rooms`` mirror columns, so
        # the value was silently dropped from the INSERT.
        async with integ_store.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT old_agentsession_rooms, new_agentsession_rooms "
                "FROM change_log "
                "WHERE subject_id = $1 AND kind = 'agentsession_rooms' "
                "ORDER BY created",
                uuid.UUID(sid),
            )
        assert len(rows) == 3
        # First edit (PUT set) overwrote ["sear"] -> ["lab", "ops"].
        assert sorted(rows[0]["old_agentsession_rooms"]) == ["sear"]
        assert sorted(rows[0]["new_agentsession_rooms"]) == ["lab", "ops"]
        # Last edit (remove "lab") landed ["ops", "sear"] as the new snapshot.
        assert sorted(rows[2]["new_agentsession_rooms"]) == ["ops", "sear"]

    async def test_send_resolves_actor_room_to_live_session(
        self, integ_store: Store
    ) -> None:
        """``POST /api/messages`` resolves ``@actor:room`` to live sessions.

        Proves the step-9 name-resolution front door: a send addressed to a
        routing name (optionally room-scoped) reaches the live session's queue,
        and a non-matching room delivers nothing (drop-if-absent).
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.state.config = Config()
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="sender@test")

        await _seed_active_user(integ_store, "sender@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                start = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "scientist", "rooms": ["sear"]},
                )
                sid = start.json()["id"]

                # Room-scoped send to the right room reaches the session.
                hit = await http.post(
                    "/api/messages",
                    json={"actor": "scientist", "room": "sear", "text": "go"},
                )
                assert hit.status_code == 200, hit.text
                assert hit.json()["delivered"] == [sid]

                # Wrong room matches nothing (undelivered).
                miss = await http.post(
                    "/api/messages",
                    json={"actor": "scientist", "room": "other", "text": "go"},
                )
                assert miss.json()["delivered"] == []

                # The reaching send landed in the session's inbound queue,
                # carrying the attested sender and the routed room so the
                # poller can render the ``[room] sender:`` injection context.
                drain = await http.get(f"/api/sessions/{sid}/inbound")
                msgs = drain.json()["messages"]
                assert [m["text"] for m in msgs] == ["go"]
                assert msgs[0]["source"] == "sender@test"
                assert msgs[0]["room"] == "sear"
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_send_bare_actor_rejects_multi_room_session(
        self, integ_store: Store
    ) -> None:
        """A bare ``@actor`` send is rejected when the session spans >1 room.

        A single PTY interleaves every room's messages, so an unscoped send to
        a multi-room session has no room context to inject; the server returns
        409 telling the caller to name a room.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.state.config = Config()
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="sender@test")

        await _seed_active_user(integ_store, "sender@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "multi", "rooms": ["a", "b"]},
                )
                # Bare send: ambiguous across rooms a and b -> 409.
                bare = await http.post(
                    "/api/messages", json={"actor": "multi", "text": "go"}
                )
                assert bare.status_code == 409, bare.text
                assert "address one explicitly" in bare.json()["detail"]
                # Naming a room resolves it.
                scoped = await http.post(
                    "/api/messages",
                    json={"actor": "multi", "room": "a", "text": "go"},
                )
                assert scoped.status_code == 200, scoped.text
                assert len(scoped.json()["delivered"]) == 1
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_send_is_idempotent_on_key_replay(self, integ_store: Store) -> None:
        """A replayed ``Idempotency-Key`` enqueues once, not once per retry.

        The send path writes no ``change_log`` row, so it cannot dedup the way
        DB writes do; ``send_once`` records the delivered receipt per key and
        short-circuits a replay. Without this a client retry (lost response)
        double-injects into the live session.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.state.config = Config()
        # The dedup key reaches the route via request.state, set by the
        # middleware from the Idempotency-Key header.
        app.add_middleware(ChangeIdMiddleware)
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="sender@test")

        await _seed_active_user(integ_store, "sender@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                start = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "idem", "rooms": ["sear"]},
                )
                sid = start.json()["id"]
                key = str(uuid.uuid4())
                body = {"actor": "idem", "room": "sear", "text": "once"}
                first = await http.post(
                    "/api/messages", json=body, headers={"Idempotency-Key": key}
                )
                replay = await http.post(
                    "/api/messages", json=body, headers={"Idempotency-Key": key}
                )
                assert first.json()["delivered"] == [sid]
                # Replay returns the same receipt, but does not enqueue again.
                assert replay.json()["delivered"] == [sid]
                drain = await http.get(f"/api/sessions/{sid}/inbound")
                assert [m["text"] for m in drain.json()["messages"]] == ["once"]
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_send_concurrent_same_key_enqueues_once(
        self, integ_store: Store
    ) -> None:
        """Two concurrent same-key sends enqueue exactly one message.

        The check->resolve->enqueue->record steps used to be separate, so two
        racing same-``Idempotency-Key`` sends could both pass the dedup check
        and double-enqueue. ``send_once`` makes dedup+enqueue+record one atomic
        critical section, so the drain holds a single copy.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.state.config = Config()
        # The dedup key reaches the route via request.state, set by the
        # middleware from the Idempotency-Key header.
        app.add_middleware(ChangeIdMiddleware)
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="sender@test")

        await _seed_active_user(integ_store, "sender@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                start = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "race", "rooms": ["sear"]},
                )
                sid = start.json()["id"]
                key = str(uuid.uuid4())
                body = {"actor": "race", "room": "sear", "text": "once"}
                first, second = await asyncio.gather(
                    http.post(
                        "/api/messages", json=body, headers={"Idempotency-Key": key}
                    ),
                    http.post(
                        "/api/messages", json=body, headers={"Idempotency-Key": key}
                    ),
                )
                assert first.json()["delivered"] == [sid]
                assert second.json()["delivered"] == [sid]
                drain = await http.get(f"/api/sessions/{sid}/inbound")
                # Exactly one copy despite two concurrent same-key sends.
                assert [m["text"] for m in drain.json()["messages"]] == ["once"]
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_send_skips_session_ended_after_resolve(
        self, integ_store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session ended between resolve and enqueue is not queued to.

        ``resolve_live_sessions`` and the enqueue are separate steps; a
        concurrent ``end`` in that window must not strand a message in a queue
        nobody drains. The route re-checks ``ended`` immediately before
        enqueue. Simulated by stubbing ``resolve`` to return the now-ended
        session so the route's pre-enqueue liveness re-check fires.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.state.config = Config()
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="sender@test")

        await _seed_active_user(integ_store, "sender@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                start = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "ending", "rooms": ["sear"]},
                )
                sid = uuid.UUID(start.json()["id"])
                # Close via ``end_session`` (ended + status=complete together)
                # so the AgentSession lifecycle CHECK holds.
                await integ_store.end_session(
                    sid, ended=datetime.now(UTC), actor="ending"
                )

                async def _stale(
                    actor: str, *, room: str | None = None
                ) -> list[tuple[uuid.UUID, tuple[str, ...]]]:
                    del actor, room
                    return [(sid, ("sear",))]

                monkeypatch.setattr(integ_store, "resolve_live_sessions", _stale)
                resp = await http.post(
                    "/api/messages",
                    json={"actor": "ending", "room": "sear", "text": "late"},
                )
                assert resp.json()["delivered"] == []
                assert app.state.inbound.pending(sid) == 0
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_session_start_rejects_blank_rooms(self, integ_store: Store) -> None:
        """Blank/whitespace room names are rejected at the API boundary (422).

        Mirrors the subscribers validator: a blank entry is almost certainly a
        client bug and silently dropping it (storage canonicalization) hides
        it. ``rooms=["  "]`` is a 422, not a 201 with empty membership.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.state.config = Config()
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None)

        app.dependency_overrides[current_user] = _identity
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                resp = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "eng", "rooms": ["  "]},
                )
                assert resp.status_code == 422, resp.text
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_read_feed_interleaves_sessions_and_filters(
        self, integ_store: Store
    ) -> None:
        """The console feed interleaves turns across sessions, in time order.

        Proves the multi-agent console's backing read: turns from two sessions
        come back ordered by the server write clock (``created``), each tagged
        with its session's routing identity (``actor``/``rooms``/``cli``); the
        ``room``/``actor`` filters and the ``since`` keyset cursor narrow it.
        """
        # ``owner`` is the routing identity the feed surfaces as ``actor``;
        # the start route fills it from the granted name (here set directly).
        sci = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="sci",
                cli="codex",
                owner="scientist",
                rooms=["sear"],
            )
        )
        eng = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="eng",
                cli="claude",
                owner="eng",
                rooms=["lab"],
            )
        )
        # Interleave appends so created-order spans both sessions.
        await integ_store.append_events(
            sci,
            [
                EventBody(
                    seq=0, kind="UserMessage", message=UserMessage(text="a").to_json()
                )
            ],
        )
        await integ_store.append_events(
            eng,
            [
                EventBody(
                    seq=0, kind="UserMessage", message=UserMessage(text="b").to_json()
                )
            ],
        )
        await integ_store.append_events(
            sci, [EventBody(seq=1, kind="AssistantMessage")]
        )

        feed = await integ_store.read_feed()
        # All three turns, oldest first, each carrying its session context.
        assert len(feed) == 3
        assert [f.actor for f in feed] == ["scientist", "eng", "scientist"]
        assert feed[0].rooms == ["sear"]
        assert feed[0].cli == "codex"
        assert feed[1].cli == "claude"
        # Time-ordered by created across sessions.
        assert [f.created for f in feed] == sorted(f.created for f in feed)

        # Room filter narrows to one session.
        sear_only = await integ_store.read_feed(room="sear")
        assert {f.actor for f in sear_only} == {"scientist"}

        # Actor filter likewise.
        eng_only = await integ_store.read_feed(actor="eng")
        assert {f.actor for f in eng_only} == {"eng"}

        # The composite keyset cursor resumes strictly past the given event.
        after_first = await integ_store.read_feed(
            after=(feed[0].created, feed[0].session_id, feed[0].seq)
        )
        assert [f.actor for f in after_first] == ["eng", "scientist"]

        # ``tail`` returns the newest page (still oldest-first), so a backlog
        # does not force a replay from the beginning.
        tail2 = await integ_store.read_feed(limit=2, tail=True)
        assert [f.actor for f in tail2] == ["eng", "scientist"]

    async def test_read_feed_composite_cursor_spans_created_ties(
        self, integ_store: Store
    ) -> None:
        """A page boundary inside a same-``created`` group skips no rows.

        The order key is ``(created, session_id, seq)``; a bare-``created``
        cursor would resume at ``> created`` and drop every tied row after the
        boundary. Force two rows in different sessions to share one ``created``
        instant, page with ``limit=1``, and assert the second is reached via
        the composite cursor.
        """
        a = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com", title="a", cli="codex", owner="a-actor"
            )
        )
        b = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com", title="b", cli="codex", owner="b-actor"
            )
        )
        # Force an exact ``created`` tie across the two sessions (the column
        # defaults to clock_timestamp(); set it explicitly here).
        tie = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        async with integ_store.engine.acquire() as conn:
            for sid in (a, b):
                await conn.execute(
                    "INSERT INTO agent_session_events "
                    "(session_id, seq, kind, created, message) "
                    "VALUES ($1, 0, 'UserMessage', $2, '{}'::jsonb)",
                    sid,
                    tie,
                )

        page1 = await integ_store.read_feed(limit=1)
        assert len(page1) == 1
        first = page1[0]
        assert first.created == tie
        # Resume past the first via the composite cursor; the tied second row
        # (same created, different session) must still be returned.
        page2 = await integ_store.read_feed(
            after=(first.created, first.session_id, first.seq)
        )
        assert [f.session_id for f in page2] == [
            sid for sid in (a, b) if sid != first.session_id
        ]

    async def test_inbound_enqueue_then_drain_round_trips(
        self, integ_store: Store
    ) -> None:
        """``POST``/``GET`` ``/inbound`` round-trips; sender is route-attested.

        The enqueued ``source`` is the authenticated principal -- the enqueue
        request body carries no ``source`` field at all, so a client that
        sends one is rejected (422) rather than having it silently ignored.
        Draining empties the queue (drop-if-absent: a second drain is empty).
        """
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com", title="inbox", cli="claude"
            )
        )
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.state.config = Config()
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="router@test")

        app.dependency_overrides[current_user] = _identity
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                # A client-sent ``source`` is forbidden, not ignored.
                forged = await http.post(
                    f"/api/sessions/{sid}/inbound",
                    json={"text": "check the logs", "source": "forged"},
                )
                assert forged.status_code == 422, forged.text

                enq = await http.post(
                    f"/api/sessions/{sid}/inbound",
                    json={"text": "check the logs"},
                )
                assert enq.status_code == 200, enq.text
                assert enq.json()["queued"] == 1

                drain = await http.get(f"/api/sessions/{sid}/inbound")
                assert drain.status_code == 200, drain.text
                messages = drain.json()["messages"]
                assert [m["text"] for m in messages] == ["check the logs"]
                # Source is the authenticated principal, attested by the route.
                assert messages[0]["source"] == "router@test"

                # Drain emptied it: a second drain returns nothing.
                again = await http.get(f"/api/sessions/{sid}/inbound")
                assert again.json()["messages"] == []

                # Unknown session -> 404 on both verbs.
                missing = uuid.uuid4()
                r404 = await http.post(
                    f"/api/sessions/{missing}/inbound", json={"text": "x"}
                )
                assert r404.status_code == 404

                # After the session ends, enqueue is rejected (REV-09): no
                # poller will ever drain it, and the end drains any stragglers.
                end = await http.post(f"/api/sessions/{sid}/end", json={})
                assert end.status_code == 200, end.text
                # REV-31: end with no ``ended`` body still stamps a real time.
                assert end.json()["ended"] is not None
                rejected = await http.post(
                    f"/api/sessions/{sid}/inbound", json={"text": "late"}
                )
                assert rejected.status_code == 409
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_append_notifies_subscribers_on_real_append(
        self, integ_store: Store
    ) -> None:
        """A real append fires a Postgres NOTIFY for this session.

        Proves only the integration-unique fact: an append flows through
        native ``LISTEN/NOTIFY`` and the frame carries this session id (so the
        SPA / poller wakes). The *dedup-is-silent* property (appended == 0 ->
        no notify) is proven deterministically by the unit test
        ``store_test.test_append_events_notifies_on_real_append``; asserting it
        here would require a fragile "no frame within N seconds" negative that
        flakes under parallel load when a sibling test shares the channel.
        """
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="live", cli="claude")
        )
        stream = integ_store.engine.listen(NOTIFY_CHANNEL)
        try:
            # Prime the subscription BEFORE appending: ``listen`` is an async
            # generator whose queue registers only on first advance, so a
            # NOTIFY fired before that first ``anext`` would be published to
            # zero subscribers and lost. Start the wait first, then append.
            first = asyncio.ensure_future(anext(stream))
            await asyncio.sleep(0)  # let the generator reach ``await q.get()``
            appended, _ = await integ_store.append_events(
                sid, [EventBody(seq=0, kind="UserMessage")]
            )
            assert appended == 1
            # Skip any interleaved foreign-session frames (shared channel).
            payload = json.loads(await asyncio.wait_for(first, timeout=10.0))
            while payload.get("id") != str(sid):
                payload = json.loads(
                    await asyncio.wait_for(anext(stream), timeout=10.0)
                )
        finally:
            await stream.aclose()

    async def test_web_get_surfaces_agentsession_fields(
        self, integ_store: Store
    ) -> None:
        """``/api/web/get`` projects an AgentSession's kind-specific columns.

        Proves the Phase-1a web read path against the real schema: the
        ``agentsession_*`` columns surface as bare ``cli`` / ``cli_session_id``
        / ``started`` keys, ISO-formatted timestamps, and a live session
        (``ended IS NULL``) omits ``ended``.
        """
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="view",
                cli="claude",
                cli_session_id="sess-9",
            ),
        )

        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        web.attach(app)

        async def _identity_override() -> object:
            return make_test_identity()

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                r = await http.get(f"/api/web/get/{sid}")
                assert r.status_code == 200, r.text
                self_view = r.json()["self"]
                assert self_view["kind"] == "AgentSession"
                assert self_view["cli"] == "claude"
                assert self_view["cli_session_id"] == "sess-9"
                # Minted with no explicit ``started``; a live session has no
                # ``ended`` key at all.
                assert "ended" not in self_view
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_append_rejects_kind_message_mismatch(
        self, integ_store: Store
    ) -> None:
        """A body whose ``kind`` disagrees with its ``message`` type is rejected.

        Routing append through ``AgentSessionEvent`` runs its invariant, so a
        forged body (``kind="UserMessage"`` carrying an ``AssistantMessage``)
        cannot land in the store.
        """
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="run", cli="codex")
        )
        forged = EventBody(
            seq=0,
            kind="UserMessage",
            message=AssistantMessage(text="not a user message").to_json(),
        )
        with pytest.raises(ValueError, match="disagrees with message type"):
            await integ_store.append_events(sid, [forged])
        assert await integ_store.read_session_events(sid) == []

    async def test_append_rejects_non_session_parent(self, integ_store: Store) -> None:
        """Events may only attach to an ``AgentSession``, not an Issue."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="not a session")
        )
        with pytest.raises(ConflictError, match="not an AgentSession"):
            await integ_store.append_events(
                issue_id, [EventBody(seq=0, kind="UserMessage")]
            )

    async def test_concurrent_appends_never_report_negative_skipped(
        self, integ_store: Store
    ) -> None:
        """Two concurrent same-session appends each account exactly, no race.

        The append counts the rows its own ``ON CONFLICT DO NOTHING
        RETURNING`` statement wrote, so a concurrent same-session appender
        cannot inflate the count or drive ``skipped`` negative; disjoint
        ``seq`` ranges each land in full.
        """
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="race", cli="codex")
        )
        batch_a = [EventBody(seq=s, kind="UserMessage") for s in range(10)]
        batch_b = [EventBody(seq=s, kind="UserMessage") for s in range(10, 20)]
        (app_a, skip_a), (app_b, skip_b) = await asyncio.gather(
            integ_store.append_events(sid, batch_a),
            integ_store.append_events(sid, batch_b),
        )
        assert (app_a, skip_a) == (10, 0)
        assert (app_b, skip_b) == (10, 0)
        assert len(await integ_store.read_session_events(sid)) == 20

    async def test_session_events_large_message_round_trips(
        self, integ_store: Store
    ) -> None:
        """A large message stays whole (Postgres TOAST, no app-level offload)."""
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="big", cli="codex")
        )
        big_text = "x" * 20_000
        await integ_store.append_events(
            sid,
            [
                EventBody(
                    seq=0,
                    kind="ToolResult",
                    message=ToolResult(content=big_text).to_json(),
                )
            ],
        )
        events = await integ_store.read_session_events(sid)
        assert events[0].to_event(sid).message == ToolResult(content=big_text)

    async def test_session_events_cascade_on_session_purge(
        self, integ_store: Store
    ) -> None:
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="run", cli="claude")
        )
        await integ_store.append_events(sid, [EventBody(seq=0, kind="UserMessage")])
        await integ_store.purge(sid, actor="user")
        # FK ON DELETE CASCADE drops the events with the Session row.
        assert await integ_store.read_session_events(sid) == []

    async def test_session_ingest_http_start_events_end(
        self, integ_store: Store
    ) -> None:
        """End-to-end ingest over HTTP: start -> events -> end, with dedup.

        Drives the real ``sessions_routes`` against the ASGI app so the wire
        bodies, the store seam, and the SQL all agree.
        """
        # A bare app wired to the integ store (no lifespan, so it does not
        # bootstrap a second DB and clobber app.state). Mirrors the
        # web-read integration test's setup.
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.include_router(sessions_routes.router)
        # Session-authed (api_key_id=None) so the created change_log row
        # carries no api_keys FK -- the test DB has no seeded key.
        identity = make_test_identity(api_key_id=None)

        await _seed_active_user(integ_store, TEST_USER_EMAIL)

        async def _identity_override() -> object:
            return identity

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                r = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "cli_session_id": "t1"},
                )
                assert r.status_code == 201, r.text
                session_id = r.json()["id"]

                events = {
                    "events": [
                        {
                            "seq": 0,
                            "kind": "UserMessage",
                            "message": UserMessage(text="hi").to_json(),
                        },
                        {"seq": 1, "kind": "AssistantMessage", "model": "gpt-5.5"},
                    ]
                }
                r = await http.post(f"/api/sessions/{session_id}/events", json=events)
                assert r.status_code == 200, r.text
                assert r.json() == {"appended": 2, "skipped": 0}

                # Retried batch is idempotent.
                r = await http.post(f"/api/sessions/{session_id}/events", json=events)
                assert r.json() == {"appended": 0, "skipped": 2}

                r = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json={"ended": "2026-05-31T16:00:00Z"},
                )
                assert r.status_code == 200, r.text

                # Events persisted in order; the Session is now complete.
                stored = await integ_store.read_session_events(uuid.UUID(session_id))
                assert [e.seq for e in stored] == [0, 1]
                sess = await integ_store.get_inquiry(uuid.UUID(session_id))
                assert sess is not None
                assert sess.status == "complete"

                # Unknown session id -> 404.
                r = await http.post(f"/api/sessions/{new_uuid()}/events", json=events)
                assert r.status_code == 404, r.text
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_session_end_retry_same_key_replays_not_409(
        self, integ_store: Store
    ) -> None:
        """A retried POST /end with the same Idempotency-Key replays 200.

        The original close raised three audit emits but is anchored on the
        ``agentsession_ended`` change_log row keyed by the request's
        idempotency UUID; a same-key retry replays the original success
        instead of 409-ing on the already-ended guard. A *different* key
        ending the already-closed session is a genuine second close -> 409.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.add_middleware(ChangeIdMiddleware)

        # The already-ended-different-key path raises ConflictError; translate
        # it to 409 (as app.py's conflict_handler does) so the test sees the
        # HTTP status, not a re-raised exception.
        async def _conflict_to_409(request: Request, exc: Exception) -> JSONResponse:
            del request
            return JSONResponse(status_code=409, content={"detail": str(exc)})

        app.add_exception_handler(ConflictError, _conflict_to_409)
        app.include_router(sessions_routes.router)
        identity = make_test_identity(api_key_id=None)

        await _seed_active_user(integ_store, TEST_USER_EMAIL)

        async def _identity_override() -> object:
            return identity

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                r = await http.post("/api/sessions/start", json={"cli": "codex"})
                assert r.status_code == 201, r.text
                session_id = r.json()["id"]

                key = str(uuid.uuid4())
                # Empty body: the route stamps a fresh ``now()``. The replay
                # must echo the COMMITTED ended, so two same-key calls return
                # the IDENTICAL timestamp -- not two distinct ``now()`` values.
                body: dict[str, str] = {}
                first = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                assert first.status_code == 200, first.text

                # Same key -> replay the original receipt (same committed
                # ended), not 409 and not a fresh now().
                retry = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                assert retry.status_code == 200, retry.text
                assert retry.json() == first.json()
                assert retry.json()["ended"] == first.json()["ended"]

                # A different key on the already-ended session -> 409.
                other = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json=body,
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                )
                assert other.status_code == 409, other.text
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_session_end_with_cli_backfill_retry_replays_not_409(
        self, integ_store: Store
    ) -> None:
        """End-with-cli-backfill retry replays 200 (the K-on-cli-row case).

        When the close ALSO backfills ``cli_session_id``, that emit fires first
        and consumes the client key K, so K lands on the
        ``agentsession_cli_session_id`` change_log row, not the
        ``agentsession_ended`` one. The replay probe (now keyed on id+subject,
        not kind) must still match, so a same-key retry replays 200 instead of
        falsely 409-ing.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.add_middleware(ChangeIdMiddleware)

        async def _conflict_to_409(request: Request, exc: Exception) -> JSONResponse:
            del request
            return JSONResponse(status_code=409, content={"detail": str(exc)})

        app.add_exception_handler(ConflictError, _conflict_to_409)
        app.include_router(sessions_routes.router)
        identity = make_test_identity(api_key_id=None)

        await _seed_active_user(integ_store, TEST_USER_EMAIL)

        async def _identity_override() -> object:
            return identity

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                # Start WITHOUT a cli_session_id so the end-time backfill is a
                # real change (the cli emit then consumes K).
                r = await http.post("/api/sessions/start", json={"cli": "codex"})
                assert r.status_code == 201, r.text
                session_id = r.json()["id"]

                key = str(uuid.uuid4())
                body = {"ended": "2026-05-31T16:00:00Z", "cli_session_id": "vendor-9"}
                first = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                assert first.status_code == 200, first.text

                # Same key + same body: the backfill put K on the cli row, but
                # the retry still replays 200 (probe by id+subject).
                retry = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                assert retry.status_code == 200, retry.text
                assert retry.json() == first.json()
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_read_session_events_http_paginates_and_filters(
        self, integ_store: Store
    ) -> None:
        """GET .../events pages and filters per api.md grammar (4.3)."""
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="read", cli="codex")
        )
        await integ_store.append_events(
            sid,
            [
                EventBody(seq=0, kind="UserMessage"),
                EventBody(seq=1, kind="ToolResult"),
                EventBody(seq=2, kind="AssistantMessage"),
                EventBody(seq=3, kind="ToolResult"),
            ],
        )
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.inbound = InboundQueue()
        app.include_router(sessions_routes.router)
        identity = make_test_identity(api_key_id=None)

        async def _identity_override() -> object:
            return identity

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                # Page: limit/offset window over the seq order.
                r = await http.get(
                    f"/api/sessions/{sid}/events", params={"limit": 2, "offset": 1}
                )
                assert r.status_code == 200, r.text
                assert [e["seq"] for e in r.json()["events"]] == [1, 2]

                # seq range, inclusive, with 0 allowed (events start at 0).
                r = await http.get(
                    f"/api/sessions/{sid}/events",
                    params={"seq_range": "0..1"},
                )
                assert [e["seq"] for e in r.json()["events"]] == [0, 1]

                # Disjoint union: repeated ``seq_range`` ORs the intervals,
                # the same reader inquiries use, against real Postgres.
                r = await http.get(
                    f"/api/sessions/{sid}/events",
                    params=[("seq_range", "0..0"), ("seq_range", "3..")],
                )
                assert [e["seq"] for e in r.json()["events"]] == [0, 3]

                # Event seq starts at 0, so a negative bound is a 400.
                r = await http.get(
                    f"/api/sessions/{sid}/events",
                    params={"seq_range": "-1..1"},
                )
                assert r.status_code == 400, r.text

                # kind filter.
                r = await http.get(
                    f"/api/sessions/{sid}/events", params={"kind": "ToolResult"}
                )
                assert [e["seq"] for e in r.json()["events"]] == [1, 3]

                # bad limit -> 400; unknown kind -> 400.
                assert (
                    await http.get(f"/api/sessions/{sid}/events", params={"limit": 0})
                ).status_code == 400
                assert (
                    await http.get(
                        f"/api/sessions/{sid}/events", params={"kind": "nope"}
                    )
                ).status_code == 400
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_child_change_alerts_parent_without_flipping_judgement(
        self,
        integ_store: Store,
    ) -> None:
        exp_id = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="positive measurement",
                codechanges=[],
                outcome="ok",
            )
        )
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="system works",
                judgement="proven",
                subscribers=["alice"],
                proved_by=[
                    Citation(
                        artifact_id=exp_id, artifact_kind="Experiment", valence=1.0
                    )
                ],
            )
        )
        await integ_store.set_status(exp_id, "complete", actor="user")
        belief = cast(Belief, await integ_store.get_inquiry(belief_id))
        assert belief.judgement == "proven"
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM change_log WHERE subject_id = $1 "
                "AND kind = 'dependency_changed'",
                belief_id,
            )
        assert row is not None
        assert row["new_peer_id"] == exp_id
        assert row["actor"] == "librarian"
        assert row["subscribers_snapshot"] == ["alice"]

    async def test_blocked_task_skipped_by_next_issue(
        self,
        integ_store: Store,
    ) -> None:
        blocker_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="first")
        )
        await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="dependent",
                requires=[blocker_id],
            ),
        )
        nxt = await integ_store.next_issue()
        assert nxt is not None
        assert nxt.id == blocker_id
        await integ_store.set_status(blocker_id, "complete", actor="user")
        nxt2 = await integ_store.next_issue()
        assert nxt2 is not None
        assert nxt2.title == "dependent"

    async def test_submit_belief_keeps_author_judgement(
        self,
        integ_store: Store,
    ) -> None:
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com", title="bare proven", judgement="proven"
            ),
        )
        assert (
            cast(Belief, await integ_store.get_inquiry(belief_id)).judgement == "proven"
        )

    async def test_edge_cycles_are_rejected(self, integ_store: Store) -> None:
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="user",
        )
        with pytest.raises(ConflictError, match="cycle"):
            await integ_store.add_edge(
                from_id=b_id,
                to_id=a_id,
                edge_kind="requires",
                actor="user",
            )

    async def test_edge_cycle_check_is_per_kind(self, integ_store: Store) -> None:
        """Acyclicity is per-edge-kind; cross-kind paths aren't cycles."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="parent issue")
        )
        belief_id = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="a belief")
        )
        # Issue produced_by -> Belief
        await integ_store.add_edge(
            from_id=issue_id,
            to_id=belief_id,
            edge_kind="produced_by",
            actor="user",
        )
        # Belief supersedes Issue -- not a cycle in either relation,
        # but the pre-fix global walk would have flagged it.
        await integ_store.add_edge(
            from_id=belief_id,
            to_id=issue_id,
            edge_kind="supersedes",
            actor="user",
        )

    async def test_add_edge_existing_edge_overwrites_annotations(
        self, integ_store: Store
    ) -> None:
        """Re-adding an edge applies the supplied annotations (upsert, no error).

        Edge creation is an upsert: a second ``add_edge`` with a new ``note``
        does NOT raise -- it overwrites, exactly as ``note to X`` sets a value
        anywhere else. User annotations are never silently dropped.
        """
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="user",
            note="first",
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="user",
            note="second",
        )
        edge = await integ_store.get_edge(
            from_id=a_id, to_id=b_id, edge_kind="requires"
        )
        assert edge is not None
        assert edge.note == "second"

    async def test_set_edge_annotation_is_partial_update(
        self, integ_store: Store
    ) -> None:
        """Omitted fields stay unchanged; explicit ``""`` / ``[]`` clears."""
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="user",
            note="original",
            labels=["important"],
        )
        # Update only the note; labels must survive untouched.
        await integ_store.set_edge_annotation(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            note="revised",
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT note, labels FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = $3",
                a_id,
                b_id,
                "requires",
            )
        assert row is not None
        assert row["note"] == "revised"
        assert list(row["labels"]) == ["important"]

    async def test_edge_without_note_or_labels_stores_null(
        self, integ_store: Store
    ) -> None:
        """An edge created with no note/labels stores NULL, not '' / '{}'.

        Edge metadata follows the same "unset is NULL" rule as inquiry
        columns: an absent annotation is NULL on the live edges table (the
        change-log mirror coerces it for its presence CHECK separately).
        """
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=a_id, to_id=b_id, edge_kind="requires", actor="u"
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT note, labels FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = $3",
                a_id,
                b_id,
                "requires",
            )
        assert row is not None
        assert row["note"] is None
        assert row["labels"] is None

    async def test_clearing_edge_note_and_last_label_stores_null(
        self, integ_store: Store
    ) -> None:
        """Blanking an edge note or removing its last label stores NULL."""
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="u",
            note="n",
            labels=["only"],
        )
        await integ_store.set_edge_annotation(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            note="",
            actor="u",
        )
        await integ_store.remove_edge_label(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            label="only",
            actor="u",
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT note, labels FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = $3",
                a_id,
                b_id,
                "requires",
            )
        assert row is not None
        assert row["note"] is None
        assert row["labels"] is None

    async def test_edge_label_add_preserves_insertion_order(
        self, integ_store: Store
    ) -> None:
        """Sequential ``label add`` keeps insertion order; it must not re-sort.

        Each single-label mutation previously rebuilt the set and ``sorted()``
        it, silently reordering an edge's existing labels alphabetically. Adding
        labels in a non-alphabetical sequence and asserting the stored order
        catches that regression (A3).
        """
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=a_id, to_id=b_id, edge_kind="requires", actor="u"
        )
        for label in ("zeta", "alpha", "mu"):
            await integ_store.add_edge_label(
                from_id=a_id, to_id=b_id, edge_kind="requires", label=label, actor="u"
            )
        async with integ_store.engine.acquire() as conn:
            labels = await conn.fetchval(
                "SELECT labels FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = $3",
                a_id,
                b_id,
                "requires",
            )
        assert list(labels) == ["zeta", "alpha", "mu"]

    async def test_remove_narrows_cascades_to_broader(
        self,
        integ_store: Store,
    ) -> None:
        """Removing ``narrows`` must still alert the broader Issue.

        Regression: cascade re-queried ``edges`` live, after DELETE; the
        broader Issue (the dependent endpoint for ``narrows``)
        never got the ``dependency_changed`` row.
        """
        broader_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com", title="goal", subscribers=["alice"]
            ),
        )
        narrower_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="sub-task",
                narrows=[(broader_id, None)],
            ),
        )
        # Sanity: setup wired the edge.
        # Now remove it and assert the broader Issue gets a cascade alert.
        await integ_store.remove_edge(
            from_id=narrower_id,
            to_id=broader_id,
            edge_kind="narrows",
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            # Scope to the narrows cascade: the broader Issue also
            # ``produced`` the narrower (inferred from the first edge at setup),
            # so an unscoped latest-row read could pick up that edge's cascade.
            row = await conn.fetchrow(
                "SELECT * FROM change_log WHERE subject_id = $1 "
                "AND kind = 'dependency_changed' "
                "AND new_peer_edge_kind = 'narrows' "
                "ORDER BY created DESC LIMIT 1",
                broader_id,
            )
        assert row is not None
        assert row["new_peer_id"] == narrower_id
        assert row["new_peer_edge_kind"] == "narrows"

    async def test_purge_emits_edge_removed_for_peers(
        self,
        integ_store: Store,
    ) -> None:
        blocked_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="blocked")
        )
        blocker_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="blocker")
        )
        # requires is stored from=requirer, to=prerequisite: the blocked issue
        # requires the blocker (prerequisite).
        await integ_store.add_edge(
            from_id=blocked_id,
            to_id=blocker_id,
            edge_kind="requires",
            actor="user",
        )
        await integ_store.purge(blocker_id, actor="user")
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT old_peer_id, old_peer_edge_kind FROM change_log "
                "WHERE subject_id = $1 AND kind = 'edge_removed'",
                blocked_id,
            )
        assert row is not None
        assert row["old_peer_id"] == blocker_id
        assert row["old_peer_edge_kind"] == "requires"

    async def test_submit_canonicalizes_labels_and_subscribers(
        self,
        integ_store: Store,
    ) -> None:
        """Strip, drop blanks, dedup at write."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="i",
                labels=["x", "x", "  y  ", "", "z"],
                subscribers=["alice", "alice", "bob"],
            ),
        )
        issue = cast(Issue, await integ_store.get_inquiry(issue_id))
        assert issue.labels == ("x", "y", "z")
        assert issue.subscribers == ("alice", "bob")

    async def test_set_source_updates_self_describing_id(
        self,
        integ_store: Store,
    ) -> None:
        """``set_source`` writes the single scheme-tagged ``source`` identifier."""
        paper_id = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="https://x"),
        )
        await integ_store.set_source(paper_id, "doi:10.1/abc", actor="user")
        paper = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert paper.source == "doi:10.1/abc"

    async def test_google_scholar_cluster_id_round_trips_on_submit_and_edit(
        self,
        integ_store: Store,
    ) -> None:
        """``google_scholar_cluster_id`` persists via submit and is editable.

        The Scholar cluster handle coexists with ``source`` (e.g. a DOI) on one
        Paper: cite by DOI, keep the Scholar identity separately. A plain optional
        identifier -- set at create, re-pointed via ``set_google_scholar_cluster_id``,
        cleared with an empty value.
        """
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="p",
                source="doi:10.1/abc",
                google_scholar_cluster_id="12345678901234567890",
            ),
        )
        paper = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert paper.google_scholar_cluster_id == "12345678901234567890"
        # Coexists with source -- both stored on the one row.
        assert paper.source == "doi:10.1/abc"
        # Re-point via the setter.
        await integ_store.set_google_scholar_cluster_id(paper_id, "99999", actor="user")
        paper = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert paper.google_scholar_cluster_id == "99999"
        # An empty value clears to NULL (the "unset is NULL" rule).
        await integ_store.set_google_scholar_cluster_id(paper_id, "", actor="user")
        paper = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert paper.google_scholar_cluster_id is None

    async def test_google_scholar_cluster_id_edit_is_audited(
        self,
        integ_store: Store,
    ) -> None:
        """A ``google_scholar_cluster_id`` edit records old/new values in change_log.

        The change_log ``old_/new_`` mirror derives from CHANGE_LOG_COLUMN_ORDER;
        this pins that the mirror carries the field's transition (no silent audit
        loss -- the GSI-01 class).
        """
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="p",
                google_scholar_cluster_id="old-id",
            ),
        )
        await integ_store.set_google_scholar_cluster_id(
            paper_id, "new-id", actor="user"
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT old_paper_google_scholar_cluster_id, "
                "new_paper_google_scholar_cluster_id "
                "FROM change_log WHERE subject_id = $1 "
                "AND kind = 'paper_google_scholar_cluster_id'",
                paper_id,
            )
        assert row is not None
        assert row["old_paper_google_scholar_cluster_id"] == "old-id"
        assert row["new_paper_google_scholar_cluster_id"] == "new-id"

    async def test_set_source_rejects_unschemed_value(
        self,
        integ_store: Store,
    ) -> None:
        """The edit path enforces the same ``<scheme>:<rest>`` shape as create.

        ``SubmitPaper`` already rejects a bare source at the wire; ``set_source``
        must too, so the rule holds on both paths -- a bare ``2405.16391`` is a
        clean ``ConflictError``, and the stored source is left unchanged.
        """
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com", title="p", source="arXiv:2405.16391"
            ),
        )
        with pytest.raises(ConflictError, match="scheme-tagged"):
            await integ_store.set_source(paper_id, "2405.16391", actor="user")
        # The pre-edit source survives the rejected write.
        paper = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert paper.source == "arXiv:2405.16391"

    async def test_paper_bib_fields_round_trip(
        self,
        integ_store: Store,
    ) -> None:
        """Submit a Paper with every bib field, read it back, edit, re-read."""
        published = datetime(2024, 5, 16, tzinfo=UTC)
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="Attention Is All You Need",
                abstract="The dominant sequence transduction models...",
                authors=["Vaswani", "Shazeer", "Parmar"],
                publication_type="inproceedings",
                venue="NeurIPS",
                subvenue="Main",
                publish_date=published,
                source="arXiv:1706.03762",
            ),
        )
        paper = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert paper.abstract == "The dominant sequence transduction models..."
        assert paper.authors == ("Vaswani", "Shazeer", "Parmar")
        assert paper.publication_type == "inproceedings"
        assert paper.venue == "NeurIPS"
        assert paper.subvenue == "Main"
        assert paper.publish_date == published
        assert paper.source == "arXiv:1706.03762"

        # Edit a closed-set field and a list field, then re-read.
        await integ_store.set_publication_type(paper_id, "article", actor="u")
        await integ_store.set_venue(paper_id, "JMLR", actor="u")
        await integ_store.add_author(paper_id, "Uszkoreit", actor="u")
        edited = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert edited.publication_type == "article"
        assert edited.venue == "JMLR"
        assert edited.authors == ("Vaswani", "Shazeer", "Parmar", "Uszkoreit")

    async def test_byline_add_remove_preserve_duplicates(
        self,
        integ_store: Store,
    ) -> None:
        """A byline treats duplicate authors as distinct.

        Add always appends, remove drops only the first match (not a set
        collapse).
        """
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="dup byline",
                authors=["Smith", "Jones", "Smith"],
            ),
        )
        # add of an already-present author appends a second entry.
        await integ_store.add_author(paper_id, "Smith", actor="u")
        added = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert added.authors == ("Smith", "Jones", "Smith", "Smith")
        # remove drops only the first occurrence.
        await integ_store.remove_author(paper_id, "Smith", actor="u")
        removed = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert removed.authors == ("Jones", "Smith", "Smith")

    async def test_invalid_publication_type_rejected_by_check(
        self,
        integ_store: Store,
    ) -> None:
        """An out-of-set publication_type written directly hits the DB CHECK."""
        paper_id = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p")
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with integ_store.engine.acquire() as conn:
                await conn.execute(
                    "UPDATE inquiries SET paper_publication_type = 'PUNCHCARD' "
                    "WHERE id = $1",
                    paper_id,
                )

    async def test_free_text_venue_accepted(
        self,
        integ_store: Store,
    ) -> None:
        """``venue`` is free text now -- an arbitrary series name is stored."""
        paper_id = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="k", venue="KDD"),
        )
        paper = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert paper.venue == "KDD"

    async def test_submit_paper_blank_venue_stores_null(
        self,
        integ_store: Store,
    ) -> None:
        """A whitespace-only ``venue`` collapses to NULL at the insert boundary.

        ``venue`` is free text now, so the closed-set Literal no longer rules
        out blanks; the insert must honor the same "unset is NULL" contract the
        edit path (``set_venue``) and the neighboring nullable text columns do.
        """
        paper_id = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", venue="   "),
        )
        paper = cast(Paper, await integ_store.get_inquiry(paper_id))
        assert paper.venue is None

    async def test_filter_by_venue(
        self,
        integ_store: Store,
    ) -> None:
        """A ``venue = NeurIPS`` filter selects only the matching Paper."""
        neurips = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="n", venue="NeurIPS"),
        )
        await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="i", venue="ICML")
        )
        rows = await integ_store.list_kind(
            "Paper",
            filters=(Filter(field="paper_venue", op="is", value="NeurIPS"),),
        )
        ids = {r.id for r in rows}
        assert neurips in ids
        assert all(cast(Paper, r).venue == "NeurIPS" for r in rows)

    async def test_add_edge_emits_audit_on_both_endpoints(
        self,
        integ_store: Store,
    ) -> None:
        """Symmetric edge_added: both endpoints get a change_log row."""
        issue_a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        issue_b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=issue_a,
            to_id=issue_b,
            edge_kind="requires",
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT subject_id FROM change_log "
                "WHERE kind = 'edge_added' AND "
                "(new_peer_id = $1 OR new_peer_id = $2)",
                issue_a,
                issue_b,
            )
        assert {r["subject_id"] for r in rows} == {issue_a, issue_b}

    async def test_cost_rollup_honors_ownership(
        self,
        integ_store: Store,
    ) -> None:
        broader_issue_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="root",
                marginal_cost=Cost(agent_usd=0.01),
            )
        )
        await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="child task",
                narrows=[(broader_issue_id, None)],
                marginal_cost=Cost(agent_usd=0.04),
            )
        )
        self_only = await integ_store.cost_for(broader_issue_id, deep=False)
        assert self_only is not None
        assert self_only.agent_usd == pytest.approx(0.01)
        rolled = await integ_store.cost_for(broader_issue_id, deep=True)
        assert rolled is not None
        assert rolled.agent_usd == pytest.approx(0.05)

    async def test_narrower_change_alerts_narrows(
        self,
        integ_store: Store,
    ) -> None:
        """The cascade walks narrower -> broader along ``narrows``.

        Regression: the cascade was inverted for ``narrows``, so a
        narrower Issue changing never alerted its broader parent.
        """
        broader_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com", title="goal", subscribers=["alice"]
            ),
        )
        narrower_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="sub-task",
                narrows=[(broader_id, None)],
            ),
        )
        await integ_store.set_status(narrower_id, "complete", actor="user")
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM change_log "
                "WHERE subject_id = $1 AND kind = 'dependency_changed'",
                broader_id,
            )
        assert row is not None
        assert row["new_peer_id"] == narrower_id
        assert row["new_peer_edge_kind"] == "narrows"
        assert row["subscribers_snapshot"] == ["alice"]

    async def test_submit_with_idempotency_key_is_idempotent(
        self,
        integ_store: Store,
    ) -> None:
        """A repeated submit with the same ``idempotency_key`` returns the same row."""
        key = new_uuid()
        first = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i1", idempotency_key=key),
        )
        second = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i1", idempotency_key=key),
        )
        # Server-minted: first == second, but NOT equal to the key.
        assert first == second
        assert first != key
        async with integ_store.engine.acquire() as conn:
            inquiry_count = await conn.fetchval(
                "SELECT COUNT(*) FROM inquiries WHERE id = $1", first
            )
            # The idempotency_key lives on change_log.id of the
            # ``created`` event for the resulting inquiry.
            created_change_id = await conn.fetchval(
                "SELECT id FROM change_log WHERE subject_id = $1 AND kind = 'created'",
                first,
            )
        assert inquiry_count == 1
        assert created_change_id == key

    async def test_concurrent_submit_with_idempotency_key_is_idempotent(
        self,
        integ_store: Store,
    ) -> None:
        key = new_uuid()
        first, second = await asyncio.gather(
            integ_store.submit_issue(
                SubmitIssue(
                    account="tester@example.com", title="i1", idempotency_key=key
                ),
            ),
            integ_store.submit_issue(
                SubmitIssue(
                    account="tester@example.com", title="i1", idempotency_key=key
                ),
            ),
        )
        assert first == second
        assert first != key  # server-minted
        async with integ_store.engine.acquire() as conn:
            inquiry_count = await conn.fetchval(
                "SELECT COUNT(*) FROM inquiries WHERE id = $1", first
            )
            change_count = await conn.fetchval(
                "SELECT COUNT(*) FROM change_log WHERE subject_id = $1", first
            )
        assert inquiry_count == 1
        assert change_count == 1

    async def test_submit_codechange_with_sha_writes_created_change(
        self,
        integ_store: Store,
    ) -> None:
        codechange_id = await integ_store.submit_codechange(
            SubmitCodeChange(
                account="tester@example.com", title="commit", sha="e99c4980"
            ),
            actor="Agent",
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT kind, new_codechange_sha FROM change_log WHERE subject_id = $1",
                codechange_id,
            )
        assert row is not None
        assert row["kind"] == "created"
        assert row["new_codechange_sha"] is None

    async def test_submit_with_idempotency_key_kind_mismatch_raises(
        self,
        integ_store: Store,
    ) -> None:
        """A second submit at a different kind with the same key is a conflict."""
        key = new_uuid()
        await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i", idempotency_key=key),
        )
        with pytest.raises(ConflictError, match="not Belief"):
            await integ_store.submit_belief(
                SubmitBelief(
                    account="tester@example.com", title="c", idempotency_key=key
                ),
            )

    async def test_next_issue_skips_superseded_issues(
        self,
        integ_store: Store,
    ) -> None:
        """An Issue with a ``supersedes`` successor must not be scheduled."""
        old_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="old")
        )
        new_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="new")
        )
        await integ_store.add_edge(
            from_id=new_id,
            to_id=old_id,
            edge_kind="supersedes",
            actor="user",
        )
        nxt = await integ_store.next_issue()
        assert nxt is not None
        assert nxt.id == new_id

    async def test_proves_belief_excludes_superseded_artifact(
        self,
        integ_store: Store,
    ) -> None:
        """A superseded Artifact stops counting as currently-true evidence."""
        old_exp = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com", title="old", codechanges=[], outcome="ok"
            ),
        )
        new_exp = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com", title="new", codechanges=[], outcome="ok"
            ),
        )
        await integ_store.set_status(old_exp, "complete", actor="user")
        await integ_store.set_status(new_exp, "complete", actor="user")
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="c",
                judgement="proven",
                proved_by=[
                    Citation(
                        artifact_id=old_exp, artifact_kind="Experiment", valence=1.0
                    )
                ],
            ),
        )
        # Initially old_exp is currently-true evidence.
        assert [e.id for e in await integ_store.proves_belief(belief_id)] == [old_exp]
        # Once new_exp supersedes old_exp, old_exp drops out.
        await integ_store.add_edge(
            from_id=new_exp,
            to_id=old_exp,
            edge_kind="supersedes",
            actor="user",
        )
        assert await integ_store.proves_belief(belief_id) == []

    async def test_favored_by_stores_artifact_to_belief(
        self,
        integ_store: Store,
    ) -> None:
        """``favors`` stores Artifact -> Belief and projects favored_by.

        A belief is favored *by* its evidence, so the stored edge has the
        Artifact on the from-side and the Belief on the to-side (the citing
        evidence is the younger child pointing up to the older claim). The
        Belief reads the citation back through its ``favored_by`` projection,
        which carries the signed valence.
        """
        exp_id = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="weak support",
                codechanges=[],
                outcome="ok",
            ),
        )
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="plausibly works",
                favored_by=[
                    Citation(
                        artifact_id=exp_id, artifact_kind="Experiment", valence=0.5
                    )
                ],
            ),
        )
        # The Belief projects the experiment in favored_by, carrying valence.
        belief = cast(Belief, await integ_store.get_inquiry(belief_id))
        assert belief.favored_by == (
            ArtifactEdge(id=exp_id, kind="Experiment", valence=0.5),
        )
        # The stored edge is from=Experiment (evidence), to=Belief.
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT from_id, from_kind, to_id, to_kind FROM edges "
                "WHERE edge_kind = 'favors'",
            )
        assert row is not None
        assert row["from_id"] == exp_id
        assert row["from_kind"] == "Experiment"
        assert row["to_id"] == belief_id
        assert row["to_kind"] == "Belief"

    async def test_negative_valence_favors_stores_artifact_to_belief(
        self,
        integ_store: Store,
    ) -> None:
        """A negative-valence ``favors`` is the old ``disfavors``: same edge kind.

        For-vs-against folded into the sign of valence, so counter-evidence is a
        ``favors`` edge (stored Artifact -> Belief) carrying a negative valence,
        not a separate ``disfavors`` kind.
        """
        exp_id = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="counter-evidence",
                codechanges=[],
                outcome="ok",
            ),
        )
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="probably wrong",
                favored_by=[
                    Citation(
                        artifact_id=exp_id, artifact_kind="Experiment", valence=-0.5
                    )
                ],
            ),
        )
        belief = cast(Belief, await integ_store.get_inquiry(belief_id))
        assert belief.favored_by == (
            ArtifactEdge(id=exp_id, kind="Experiment", valence=-0.5),
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT from_id, from_kind, to_id, to_kind, valence FROM edges "
                "WHERE edge_kind = 'favors'",
            )
        assert row is not None
        assert row["from_id"] == exp_id
        assert row["from_kind"] == "Experiment"
        assert row["to_id"] == belief_id
        assert row["to_kind"] == "Belief"
        assert row["valence"] == -0.5

    async def test_submit_experiment_rejects_non_codechange(
        self,
        integ_store: Store,
    ) -> None:
        """``Experiment.codechanges`` UUIDs are FK-free; validator catches drift."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="not a sha")
        )
        with pytest.raises(ConflictError, match="CodeChange"):
            await integ_store.submit_experiment(
                SubmitExperiment(
                    account="tester@example.com",
                    title="x",
                    codechanges=[issue_id],
                    outcome="",
                ),
            )

    async def test_set_codechanges_rejects_unknown_id(
        self,
        integ_store: Store,
    ) -> None:
        exp_id = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com", title="e", codechanges=[], outcome=""
            ),
        )
        bogus = new_uuid()
        with pytest.raises(ConflictError, match="not found"):
            await integ_store.set_codechanges(exp_id, [bogus], actor="user")

    async def test_submit_belief_rejects_declared_kind_mismatch(
        self,
        integ_store: Store,
    ) -> None:
        """Wire-declared citation kind must match the actual stored kind."""
        exp_id = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com", title="exp", codechanges=[], outcome="ok"
            ),
        )
        with pytest.raises(ConflictError, match="declared as"):
            await integ_store.submit_belief(
                SubmitBelief(
                    account="tester@example.com",
                    title="bad citation",
                    # Declared as Paper but actually an Experiment.
                    proved_by=[
                        Citation(artifact_id=exp_id, artifact_kind="Paper", valence=1.0)
                    ],
                ),
            )

    async def test_subscribers_edit_routes_to_removed_subscriber(
        self,
        integ_store: Store,
    ) -> None:
        """A just-removed subscriber sees the audit row that removed them.

        Regression: ``subscribers_snapshot`` captured the post-edit list,
        so unsubscribe events were invisible to the removed agent.
        """
        issue_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com", title="i", subscribers=["alice", "bob"]
            ),
        )
        await integ_store.set_subscribers(issue_id, ["bob"], actor="user")
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT subscribers_snapshot FROM change_log "
                "WHERE subject_id = $1 AND kind = 'subscribers'",
                issue_id,
            )
        assert row is not None
        assert "alice" in row["subscribers_snapshot"]
        assert "bob" in row["subscribers_snapshot"]

    async def test_add_label_is_atomic_and_idempotent(
        self,
        integ_store: Store,
    ) -> None:
        """``add_label`` is race-safe (the OG ``subscribe_self`` pattern)."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i")
        )
        await asyncio.gather(
            integ_store.add_label(issue_id, "x", actor="alice"),
            integ_store.add_label(issue_id, "y", actor="bob"),
            integ_store.add_label(issue_id, "z", actor="carol"),
        )
        # Re-adding "x" is idempotent.
        await integ_store.add_label(issue_id, "x", actor="alice")
        issue = cast(Issue, await integ_store.get_inquiry(issue_id))
        assert issue.labels is not None
        assert sorted(issue.labels) == ["x", "y", "z"]
        await integ_store.remove_label(issue_id, "y", actor="bob")
        issue = cast(Issue, await integ_store.get_inquiry(issue_id))
        assert issue.labels is not None
        assert sorted(issue.labels) == ["x", "z"]

    async def test_remove_issue_kind_rejects_empty_via_db_check(
        self,
        integ_store: Store,
    ) -> None:
        """The non-empty invariant on ``Issue.issue_kind`` lives in the
        DB CHECK (``array_length(issue_kind, 1) >= 1``) and propagates
        from :class:`ColumnSpec.min_items`. Removing the last kind
        surfaces as ``ConflictError``, not as a silent empty array.
        """
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i", issue_kind=["bug"])
        )
        with pytest.raises(ConflictError, match="check constraint"):
            await integ_store.remove_issue_kind(issue_id, "bug", actor="alice")
        # The pre-removal state is preserved.
        issue = cast(Issue, await integ_store.get_inquiry(issue_id))
        assert issue.issue_kind == ("bug",)
        # Adding a second kind then removing the original is allowed.
        await integ_store.add_issue_kind(issue_id, "task", actor="alice")
        await integ_store.remove_issue_kind(issue_id, "bug", actor="alice")
        issue = cast(Issue, await integ_store.get_inquiry(issue_id))
        assert issue.issue_kind == ("task",)

    async def test_transition_status_is_compare_and_set(
        self,
        integ_store: Store,
    ) -> None:
        """Concurrent ``close`` + ``done`` cannot both succeed."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i")
        )
        await integ_store.transition_status(
            issue_id, expected_from="active", to="complete", actor="alice"
        )
        # Second transition from "active" fails: the row is already complete.
        with pytest.raises(ConflictError, match="expected 'active'"):
            await integ_store.transition_status(
                issue_id, expected_from="active", to="abandoned", actor="bob"
            )
        issue = cast(Issue, await integ_store.get_inquiry(issue_id))
        assert issue.status == "complete"

    async def test_submit_batch_returns_ids_in_order(
        self,
        integ_store: Store,
    ) -> None:
        """``submit_batch`` collapses N round-trips into one and returns ordered ids."""
        items: list[Any] = [
            SubmitIssue(
                account="tester@example.com", title="issue", idempotency_key=new_uuid()
            ),
            SubmitArtifact(
                account="tester@example.com", title="art", idempotency_key=new_uuid()
            ),
            SubmitPaper(
                account="tester@example.com",
                title="p",
                source="https://x",
                idempotency_key=new_uuid(),
            ),
        ]
        ids = await integ_store.submit_batch(items)
        # Reads back in submit order.
        kinds = [(await integ_store.get_inquiry(rid)).__class__.__name__ for rid in ids]
        assert kinds == ["Issue", "Artifact", "Paper"]

    async def test_submit_batch_failure_persists_no_rows(
        self,
        integ_store: Store,
    ) -> None:
        """A failing item rolls the whole batch back: zero rows persist.

        The real-DB proof of atomicity the mock tier cannot give. A key is
        first committed as an Issue; the batch's second item reuses that key
        for a different kind (Artifact), which
        ``_lookup_existing_by_change`` rejects with a kind mismatch. The
        batch's first (otherwise valid) Issue must roll back too.
        """
        seeded_key = new_uuid()
        await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com", title="seed", idempotency_key=seeded_key
            )
        )
        issues_before = len(await integ_store.list_kind("Issue"))
        with pytest.raises(ConflictError, match="already created"):
            await integ_store.submit_batch(
                [
                    SubmitIssue(
                        account="tester@example.com",
                        title="i1",
                        idempotency_key=new_uuid(),
                    ),
                    SubmitArtifact(
                        account="tester@example.com",
                        title="a1",
                        idempotency_key=seeded_key,
                    ),
                ]
            )
        # The batch's new Issue did not persist; only the seed remains.
        assert len(await integ_store.list_kind("Issue")) == issues_before
        assert await integ_store.list_kind("Artifact") == []

    async def test_submit_batch_creates_rows_and_edge_atomically(
        self,
        integ_store: Store,
    ) -> None:
        """Root + inline target + linking edge all land in one request."""
        ids = await integ_store.submit_batch(
            [
                SubmitIssue(
                    account="tester@example.com",
                    title="root",
                    idempotency_key=new_uuid(),
                ),
                SubmitIssue(
                    account="tester@example.com",
                    title="blocker",
                    idempotency_key=new_uuid(),
                ),
            ],
            edges=[BatchEdge(from_index=0, to_index=1, edge_kind="requires")],
        )
        root_id, blocker_id = ids
        edge = await integ_store.get_edge(
            from_id=root_id, to_id=blocker_id, edge_kind="requires"
        )
        assert edge is not None

    async def test_submit_batch_deep_chain_wires_nonroot_from_index(
        self,
        integ_store: Store,
    ) -> None:
        """A deep chain (Issue#425 item 6) wires edges whose SOURCE is non-root.

        The deep-cursor create flattens ``belief produced websearch produced
        paper`` into a batch of ``produced_by`` edges (stored child -> parent:
        from=produced/younger, to=producer/older). The second edge's
        ``from_index`` is the paper (item 2), pointing at the websearch (item 1)
        as its producer -- NOT the root belief (item 0). Existing batch tests
        only exercise ``from_index=0``; this pins that a non-root source index
        resolves correctly against real Postgres, building belief->search->paper
        rather than belief->{search,paper}.
        """
        ids = await integ_store.submit_batch(
            [
                SubmitBelief(
                    account="tester@example.com",
                    title="bet",
                    idempotency_key=new_uuid(),
                ),  # 0
                SubmitWebSearch(
                    account="tester@example.com",
                    title="check",
                    query="q",
                    idempotency_key=new_uuid(),
                ),  # 1
                SubmitPaper(
                    account="tester@example.com",
                    title="hit",
                    idempotency_key=new_uuid(),
                ),  # 2
            ],
            edges=[
                # search produced_by belief; paper produced_by search.
                BatchEdge(from_index=1, to_index=0, edge_kind="produced_by"),
                BatchEdge(from_index=2, to_index=1, edge_kind="produced_by"),
            ],
        )
        belief_id, search_id, paper_id = ids
        # The paper was produced_by the search (deep), NOT the belief.
        assert (
            await integ_store.get_edge(
                from_id=paper_id, to_id=search_id, edge_kind="produced_by"
            )
            is not None
        )
        assert (
            await integ_store.get_edge(
                from_id=paper_id, to_id=belief_id, edge_kind="produced_by"
            )
            is None
        )

    async def test_submit_batch_edge_to_existing_target_by_id(
        self,
        integ_store: Store,
    ) -> None:
        """A batch edge may point at a pre-existing row by UUID."""
        existing = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="existing")
        )
        ids = await integ_store.submit_batch(
            [
                SubmitIssue(
                    account="tester@example.com",
                    title="root",
                    idempotency_key=new_uuid(),
                )
            ],
            edges=[BatchEdge(from_index=0, to_id=existing, edge_kind="requires")],
        )
        edge = await integ_store.get_edge(
            from_id=ids[0], to_id=existing, edge_kind="requires"
        )
        assert edge is not None

    async def test_submit_batch_duplicate_edge_upserts_and_commits(
        self,
        integ_store: Store,
    ) -> None:
        """A duplicate edge in a batch is an idempotent upsert, not a failure.

        Edge creation is an upsert everywhere (symmetric with ``label add`` and
        every other set): a pre-existing edge does not abort the batch. The root
        row commits and the duplicate edge no-ops (or applies supplied
        annotations) instead of raising and rolling the batch back.
        """
        target = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="target")
        )
        first = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="first")
        )
        await integ_store.add_edge(
            from_id=first,
            to_id=target,
            edge_kind="requires",
            actor="user",
        )
        issues_before = len(await integ_store.list_kind("Issue"))
        ids = await integ_store.submit_batch(
            [
                SubmitIssue(
                    account="tester@example.com",
                    title="root",
                    idempotency_key=new_uuid(),
                )
            ],
            edges=[
                # Reuse first->target; the duplicate upserts, the root commits.
                BatchEdge(from_id=first, to_id=target, edge_kind="requires")
            ],
        )
        # The root row committed (one new Issue), not rolled back.
        assert len(await integ_store.list_kind("Issue")) == issues_before + 1
        assert len(ids) == 1

    async def test_add_edge_on_existing_edge_upserts_annotations(
        self,
        integ_store: Store,
    ) -> None:
        """Re-creating an edge WITH annotations overwrites them (upsert, no 409).

        This is the load-bearing invariant: ``proves ... note to X`` on an
        edge that already exists SETS the note -- it does not raise
        "already exists" and send the caller to a different endpoint. A bare
        re-create (no annotations) stays a no-op.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        first_change, first_created = await integ_store.add_edge(
            from_id=a, to_id=b, edge_kind="requires", actor="user"
        )
        assert first_created is True
        assert first_change is not None

        # A bare re-create is a pure no-op (nothing to set).
        bare_change, bare_created = await integ_store.add_edge(
            from_id=a, to_id=b, edge_kind="requires", actor="user"
        )
        assert bare_created is False
        assert bare_change is None

        # A re-create carrying annotations OVERWRITES them (no raise). ``requires``
        # is a structural edge, so its annotations are note / priority (valence is
        # citation-only and rejected by the edges CHECK on a non-citation edge).
        ann_change, ann_created = await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            note="degradation half",
            priority=3,
            actor="user",
        )
        assert ann_created is False  # existing edge, not a fresh create
        assert ann_change is not None  # but a change WAS emitted (annotated)
        edge = await integ_store.get_edge(from_id=a, to_id=b, edge_kind="requires")
        assert edge is not None
        assert edge.note == "degradation half"
        assert edge.priority == 3

    async def test_citation_create_without_valence_defaults_not_null(
        self, integ_store: Store
    ) -> None:
        """A proves/favors edge created with no valence stores the default, not NULL.

        The citation-valence invariant ("a citation is never NULL") holds at the
        single storage boundary regardless of entry path, so a bare ``add_edge``
        on a citation kind stores ``CITATION_VALENCE_DEFAULT`` rather than NULL.
        """
        paper = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="arXiv:1.2")
        )
        belief = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=paper, to_id=belief, edge_kind="proves", actor="user"
        )
        async with integ_store.engine.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT valence FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = 'proves'",
                paper,
                belief,
            )
        assert stored == 0.5

    async def test_clearing_citation_valence_resets_to_default_not_null(
        self, integ_store: Store
    ) -> None:
        """``set_edge_annotation(valence=None)`` on a citation heals to the default.

        A citation can never be stored NULL; an explicit clear resets the valence
        to ``CITATION_VALENCE_DEFAULT`` instead of writing NULL (which the model
        forbids on a citation).
        """
        paper = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="arXiv:1.3")
        )
        belief = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=paper, to_id=belief, edge_kind="proves", valence=0.9, actor="user"
        )
        await integ_store.set_edge_annotation(
            from_id=paper, to_id=belief, edge_kind="proves", valence=None, actor="user"
        )
        async with integ_store.engine.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT valence FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = 'proves'",
                paper,
                belief,
            )
        assert stored == 0.5

    async def test_valence_on_structural_edge_annotation_raises_4xx(
        self, integ_store: Store
    ) -> None:
        """A valence on a structural edge is a clean ValidationError, not a DB 500.

        The annotation-edit path runs the same ``validate_edge_valence`` guard as
        create, so a valence on ``requires`` surfaces as a 4xx ValidationError,
        not a raw mid-transaction CHECK violation.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=a, to_id=b, edge_kind="requires", actor="user"
        )
        with pytest.raises(ValidationError, match="valence"):
            await integ_store.set_edge_annotation(
                from_id=a, to_id=b, edge_kind="requires", valence=0.5, actor="user"
            )

    async def test_remove_edge_label_normalizes_whitespace(
        self, integ_store: Store
    ) -> None:
        """Atomic edge-label remove canonicalizes the label, matching whole-list.

        A stored ``foo`` must be removed by ``remove_edge_label("  foo  ")``,
        since the single-label path canonicalizes the same way whole-list writes
        do -- otherwise the atomic op silently no-ops on a whitespace variant.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=a, to_id=b, edge_kind="requires", labels=["foo"], actor="user"
        )
        change = await integ_store.remove_edge_label(
            from_id=a, to_id=b, edge_kind="requires", label="  foo  ", actor="user"
        )
        assert change is not None
        edge = await integ_store.get_edge(from_id=a, to_id=b, edge_kind="requires")
        assert edge is not None
        assert edge.labels is None

    async def test_inline_citation_upserts_valence_on_existing_edge(
        self, integ_store: Store
    ) -> None:
        """An inline Belief citation applies its valence to an already-present edge.

        ``insert_edge_and_audit`` (the submit-time citation path) must upsert the
        caller's valence onto an existing edge, not silently drop it -- symmetric
        with ``add_edge``'s create-is-upsert behavior.
        """
        paper = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="arXiv:1.4")
        )
        # First Belief stamps the proves edge with valence 0.7.
        await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="b1",
                proved_by=[
                    Citation(artifact_id=paper, artifact_kind="Paper", valence=0.7)
                ],
            )
        )
        belief1 = (await integ_store.list_kind("Belief"))[0].id
        # A second submit citing the SAME (paper -> belief1) pair with a fresh
        # valence must overwrite, not no-op. Re-cite via a direct belief edit
        # path: submit a citation on belief1 again with a different valence.
        await integ_store.add_edge(
            from_id=paper,
            to_id=belief1,
            edge_kind="proves",
            valence=-0.9,
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT valence FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = 'proves'",
                paper,
                belief1,
            )
        assert stored == -0.9

    async def test_add_edge_records_reason_on_both_audit_rows(
        self,
        integ_store: Store,
    ) -> None:
        """An edge create's ``reason`` lands on both paired ``edge_added`` rows.

        Edge mutations accept a ``reason`` (the body carries it), exactly like
        inquiry field edits. It must reach ``change_log.reason`` on both the
        from- and to-side audit rows, not be silently dropped.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            actor="user",
            reason="because the analyst asked",
        )
        async with integ_store.engine.acquire() as conn:
            # Scope to the requires audit rows: the first edge between the
            # pair also infers a ``produced_by`` edge (its own reason), so filter
            # to this edge's audits rather than every edge_added on the pair.
            reasons = {
                r["reason"]
                for r in await conn.fetch(
                    "SELECT reason FROM change_log WHERE kind = 'edge_added' "
                    "AND new_peer_edge_kind = 'requires' "
                    "AND (subject_id = $1 OR subject_id = $2)",
                    a,
                    b,
                )
            }
        assert reasons == {"because the analyst asked"}

    async def test_set_edge_annotation_records_reason(
        self,
        integ_store: Store,
    ) -> None:
        """A ``set_edge_annotation``'s ``reason`` reaches both audit rows."""
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(
            from_id=a, to_id=b, edge_kind="requires", actor="user"
        )
        await integ_store.set_edge_annotation(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            note="n",
            actor="user",
            reason="annotate-reason",
        )
        async with integ_store.engine.acquire() as conn:
            reasons = {
                r["reason"]
                for r in await conn.fetch(
                    "SELECT reason FROM change_log "
                    "WHERE kind = 'edge_annotation_changed' "
                    "AND (subject_id = $1 OR subject_id = $2)",
                    a,
                    b,
                )
            }
        assert reasons == {"annotate-reason"}

    async def test_edge_input_validation_raises_validation_error(
        self,
        integ_store: Store,
    ) -> None:
        """Pure-input edge errors raise ``ValidationError`` (HTTP 422), not 409.

        Priority-on-a-non-priority-kind and a self-loop are malformed requests
        knowable without consulting the graph -- RFC 9110 422, not the 409
        ``Conflict`` reserved for clashes with existing state (cycle).
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        # ``supersedes`` cannot carry priority -> input-invalid -> ValidationError.
        with pytest.raises(ValidationError, match="cannot carry priority"):
            await integ_store.add_edge(
                from_id=a, to_id=b, edge_kind="supersedes", priority=0, actor="u"
            )
        # A self-loop is input-invalid -> ValidationError.
        with pytest.raises(ValidationError, match="self-loop"):
            await integ_store.add_edge(
                from_id=a, to_id=a, edge_kind="requires", actor="u"
            )

    async def test_edge_cycle_stays_conflict_error(
        self,
        integ_store: Store,
    ) -> None:
        """A cycle is a STATE conflict (409), not input-invalid (422).

        The edge is well-formed; it fails only because of edges already in the
        graph. That is RFC 9110 409 Conflict, kept distinct from the 422 input
        errors above.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        await integ_store.add_edge(from_id=a, to_id=b, edge_kind="requires", actor="u")
        with pytest.raises(ConflictError, match="would create a cycle"):
            await integ_store.add_edge(
                from_id=b, to_id=a, edge_kind="requires", actor="u"
            )

    async def test_edge_create_is_an_upsert_like_every_other_set(
        self,
        integ_store: Store,
    ) -> None:
        """INVARIANT: an edge ``add`` is an upsert, exactly like ``label add``.

        This is the safeguard against re-introducing an "edge already exists"
        error. Every set in the system is idempotent: ``label add X`` on an
        already-labelled row no-ops, ``status to S`` overwrites, ``note to X``
        sets. Edge creation MUST behave the same -- a repeat never raises, a
        bare repeat is a no-op, an annotated repeat applies the annotation. If
        a future change makes edge-create raise on an existing edge, this test
        fails and names the principle it violated.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        # First create: a change is emitted, created flag set.
        _, created1 = await integ_store.add_edge(
            from_id=a, to_id=b, edge_kind="requires", actor="u"
        )
        assert created1 is True
        # Bare repeat: idempotent no-op, NO raise (the load-bearing assertion).
        change2, created2 = await integ_store.add_edge(
            from_id=a, to_id=b, edge_kind="requires", actor="u"
        )
        assert (change2, created2) == (None, False)
        # Annotated repeat: applies the annotation, NO raise.
        change3, created3 = await integ_store.add_edge(
            from_id=a, to_id=b, edge_kind="requires", actor="u", note="n"
        )
        assert change3 is not None
        assert created3 is False

    async def test_add_subscriber_is_atomic_and_idempotent(
        self,
        integ_store: Store,
    ) -> None:
        """Concurrent ``add_subscriber`` calls converge to the right set.

        Regression: the previous CLI Watch composed read-modify-write
        client-side, so two concurrent ``trax watch`` calls each read
        the same pre-edit list and the second POST clobbered the first.
        The server-side primitive uses ``FOR UPDATE`` and is idempotent.

        ``add_subscriber`` also supports ``subscriber != author``: an
        admin / router can subscribe someone else without round-trips
        through the racy whole-list edit path.
        """
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i")
        )
        # Parallel self-subscribes by three distinct actors.
        await asyncio.gather(
            integ_store.add_subscriber(issue_id, "alice", actor="alice"),
            integ_store.add_subscriber(issue_id, "bob", actor="bob"),
            integ_store.add_subscriber(issue_id, "carol", actor="carol"),
        )
        # Re-subscribing alice is a no-op (idempotent).
        await integ_store.add_subscriber(issue_id, "alice", actor="alice")
        # An admin subscribes dave on dave's behalf -- the
        # arbitrary-actor capability the whole-list rejection map
        # promises.
        await integ_store.add_subscriber(issue_id, "dave", actor="admin")
        issue = cast(Issue, await integ_store.get_inquiry(issue_id))
        assert issue.subscribers is not None
        assert sorted(issue.subscribers) == ["alice", "bob", "carol", "dave"]
        # Remove is atomic too and accepts a non-self subscriber.
        await integ_store.remove_subscriber(issue_id, "bob", actor="admin")
        issue = cast(Issue, await integ_store.get_inquiry(issue_id))
        assert issue.subscribers is not None
        assert sorted(issue.subscribers) == ["alice", "carol", "dave"]

    async def test_list_kind_filters_apply_before_limit(
        self,
        integ_store: Store,
    ) -> None:
        """Filters must bound the result set, not the pre-filter scan.

        Regression for Issue#256: ``list_kind(filters=...)`` previously
        post-filtered a server-truncated batch, so a needle past the
        ``limit`` recency window was unreachable. Here ``needle`` is
        the first-inserted (oldest) Issue and ``limit=5`` is well
        below the 30 newer Issues that follow; the contract is that
        every match comes back regardless of where it sits in the
        natural recency order.
        """
        needle_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="needle in the haystack",
                labels=["target"],
            ),
        )
        for i in range(30):
            await integ_store.submit_issue(
                SubmitIssue(account="tester@example.com", title=f"noise {i}")
            )

        rows = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="title", op="re", value="needle"),),
            limit=5,
        )
        assert [r.id for r in rows] == [needle_id], (
            f"expected only the needle past the LIMIT window; got {[r.id for r in rows]}"
        )

        label_rows = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="labels", op="is", value="target"),),
            limit=5,
        )
        assert [r.id for r in label_rows] == [needle_id]

    async def test_unset_optional_columns_store_null_and_isnull_finds_them(
        self,
        integ_store: Store,
    ) -> None:
        """An unset optional column stores SQL NULL, and ``isnull`` finds it.

        Regression for the nullable-columns migration: ``owner`` / ``labels``
        were ``NOT NULL DEFAULT '' / '{}'``, so an unset value stored an empty
        sentinel that ``isnull`` could never see and ``notnull`` always matched.
        With the columns nullable, an unspecified owner is genuinely NULL (not
        actor-stamped), ``owner isnull`` returns it, and ``owner notnull``
        excludes it. The same holds for the ``labels`` array column.
        """
        unowned = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="no owner here")
        )
        owned = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="has an owner",
                owner="dana",
                labels=["tagged"],
            ),
        )

        # The unset owner is stored as SQL NULL, not the actor or an empty string.
        async with integ_store.engine.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT owner FROM inquiries WHERE id = $1", unowned
            )
        assert stored is None

        isnull = await integ_store.list_kind(
            "Issue", filters=(Filter(field="owner", op="isnull", value=""),)
        )
        assert unowned in {r.id for r in isnull}
        assert owned not in {r.id for r in isnull}

        notnull = await integ_store.list_kind(
            "Issue", filters=(Filter(field="owner", op="notnull", value=""),)
        )
        assert owned in {r.id for r in notnull}
        assert unowned not in {r.id for r in notnull}

        # The array column behaves identically: no labels -> NULL -> isnull.
        labels_isnull = await integ_store.list_kind(
            "Issue", filters=(Filter(field="labels", op="isnull", value=""),)
        )
        assert unowned in {r.id for r in labels_isnull}
        assert owned not in {r.id for r in labels_isnull}

    async def test_clearing_nullable_columns_stores_null_not_sentinel(
        self,
        integ_store: Store,
    ) -> None:
        """Clearing a nullable column stores SQL NULL, not the empty sentinel.

        Unset is one encoding -- NULL -- at insert *and* on edit. Clearing
        labels (set_labels(None)) or blanking a scalar (set_description(""))
        previously stored '{}' / '' so ``isnull`` could not see the cleared
        row. Both clear paths now collapse to NULL.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com", title="s", description="d", labels=["x"]
            ),
        )
        await integ_store.set_labels(rid, None, actor="u")
        await integ_store.set_description(rid, "", actor="u")
        await integ_store.set_owner(rid, "", actor="u")

        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT owner, description, labels FROM inquiries WHERE id = $1", rid
            )
        assert row is not None
        assert row["labels"] is None
        assert row["description"] is None
        assert row["owner"] is None

        # isnull now matches the cleared row on every column.
        for field in ("labels", "description", "owner"):
            rows = await integ_store.list_kind(
                "Issue", filters=(Filter(field=field, op="isnull", value=""),)
            )
            assert rid in {r.id for r in rows}, field

    async def test_blank_only_list_edit_storage_and_audit_agree(
        self,
        integ_store: Store,
    ) -> None:
        """A blank-only list edit collapses post-normalize: storage and audit agree.

        ``set_labels(["", "  "])`` normalizes to ``()`` -- the empty result is
        absence, so storage is NULL. Setting it on an already-unset row is a
        no-op (no phantom change row), and any emitted audit must record NULL,
        never the ``()`` sentinel that contradicts storage.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s")
        )
        # Already-unset labels; a blank-only set is a no-op.
        change_id = await integ_store.set_labels(rid, ["", "  "], actor="u")
        assert change_id is None
        async with integ_store.engine.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT labels FROM inquiries WHERE id = $1", rid
            )
        assert stored is None

        # From a populated state, a blank-only set clears to NULL with a
        # NULL-recording audit (not ()).
        await integ_store.set_labels(rid, ["keep"], actor="u")
        change_id = await integ_store.set_labels(rid, ["", "  "], actor="u")
        assert change_id is not None
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow("SELECT labels FROM inquiries WHERE id = $1", rid)
            new_labels = await conn.fetchval(
                "SELECT new_labels FROM change_log WHERE id = $1", change_id
            )
        assert row is not None
        assert row["labels"] is None
        assert new_labels is None

    async def test_set_issue_kind_blank_only_raises_clean_conflict(
        self,
        integ_store: Store,
    ) -> None:
        """Emptying a min_items column via the setter raises ``ConflictError``.

        ``issue_kind`` normalizes a blank-only input to ``()``; the DB
        ``cardinality >= 1`` CHECK rejects it. ``_set_field`` wraps that as a
        clean ``ConflictError`` (a 4xx), not a raw asyncpg 500.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s", issue_kind=["bug"]),
        )
        with pytest.raises(ConflictError, match="check constraint"):
            # Deliberately blank-only input: it canonicalizes to (), exercising
            # the normalize-to-empty path against the min_items CHECK.
            await integ_store.set_issue_kind(
                rid, cast(list[Issue.Kind], ["", "  "]), actor="u"
            )

    async def test_set_source_blank_is_consistent_no_op(
        self,
        integ_store: Store,
    ) -> None:
        """``set_source("")`` on an unset source is a no-op, not a phantom change.

        The compare and audit must use the collapsed value, matching storage:
        blanking an already-NULL source changes nothing.
        """
        pid = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p")
        )
        change_id = await integ_store.set_source(pid, "  ", actor="u")
        assert change_id is None
        async with integ_store.engine.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT paper_source FROM inquiries WHERE id = $1", pid
            )
        assert stored is None

    async def test_insert_inquiry_collapses_blank_only_labels(
        self,
        integ_store: Store,
    ) -> None:
        """A direct ``insert_inquiry`` with blank-only labels stores NULL.

        The blank-collapse lives at the insert boundary, so a programmatic
        caller bypassing the route's wire validation can't store a
        whitespace label.
        """
        rid = uuid.uuid4()
        async with integ_store.engine.acquire() as conn, conn.transaction():
            await insert_inquiry(
                conn,
                rid,
                "Issue",
                values={
                    "title": "s",
                    "account": "tester@example.com",
                    "labels": ["", "  "],
                },
            )
            stored = await conn.fetchval(
                "SELECT labels FROM inquiries WHERE id = $1", rid
            )
        assert stored is None

    async def test_edit_from_null_records_null_old_side_in_audit(
        self,
        integ_store: Store,
    ) -> None:
        """An edit from an unset (NULL) column logs NULL on the audit old side.

        The change-log distinguishes NULL ("event did not touch / value was
        absent") from () ("explicitly cleared"). Editing labels from unset
        must record ``old_labels IS NULL``, not ``'{}'``.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s")
        )
        change_id = await integ_store.set_labels(rid, ["x"], actor="u")
        assert change_id is not None
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT old_labels, new_labels FROM change_log WHERE id = $1",
                change_id,
            )
        assert row is not None
        assert row["old_labels"] is None
        assert list(row["new_labels"]) == ["x"]

    async def test_removing_last_list_item_stores_null(
        self,
        integ_store: Store,
    ) -> None:
        """Removing the last label stores SQL NULL, not the empty sentinel.

        The list add/remove path must honor the same "unset is NULL" rule as
        the whole-list clear: an empty result is absence, so ``labels isnull``
        finds the row after its last label is removed.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s", labels=["only"]),
        )
        await integ_store.remove_label(rid, "only", actor="u")
        async with integ_store.engine.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT labels FROM inquiries WHERE id = $1", rid
            )
        assert stored is None
        rows = await integ_store.list_kind(
            "Issue", filters=(Filter(field="labels", op="isnull", value=""),)
        )
        assert rid in {r.id for r in rows}

    async def test_min_items_column_is_exempt_from_null_collapse(
        self,
        integ_store: Store,
    ) -> None:
        """A ``min_items`` column empties to a CHECK error, never NULL.

        ``labels`` (min_items 0) collapses to NULL when emptied, but
        ``issue_kind`` (min_items 1) must reach the DB CHECK and raise --
        the null-collapse rule does not weaken the non-empty invariant.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s", issue_kind=["bug"]),
        )
        with pytest.raises(ConflictError, match="check constraint"):
            await integ_store.remove_issue_kind(rid, "bug", actor="u")
        kept = cast(Issue, await integ_store.get_inquiry(rid))
        assert kept.issue_kind == ("bug",)

    async def test_submit_empty_optionals_store_null(
        self,
        integ_store: Store,
    ) -> None:
        """Submitting empty/blank optionals stores NULL, not the sentinel.

        The insert path shares the storage rule: an empty owner/description
        or all-blank labels is absence -> NULL, so a row created with empty
        values is indistinguishable from one created with them omitted.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="s",
                owner="",
                description="",
                labels=["", "  "],
            ),
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT owner, description, labels FROM inquiries WHERE id = $1",
                rid,
            )
        assert row is not None
        assert row["owner"] is None
        assert row["description"] is None
        assert row["labels"] is None

    async def test_set_source_empty_stores_null(
        self,
        integ_store: Store,
    ) -> None:
        """``set_source('')`` clears to NULL, like every other nullable setter."""
        rid = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="http://x"),
        )
        await integ_store.set_source(rid, "", actor="u")
        async with integ_store.engine.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT paper_source FROM inquiries WHERE id = $1", rid
            )
        assert stored is None

    async def test_blanking_scalar_with_whitespace_stores_null(
        self,
        integ_store: Store,
    ) -> None:
        """A whitespace-only scalar edit collapses to NULL, matching migration 003."""
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s", owner="dana"),
        )
        await integ_store.set_owner(rid, "   ", actor="u")
        async with integ_store.engine.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT owner FROM inquiries WHERE id = $1", rid
            )
        assert stored is None

    async def test_list_kind_filter_agent_cost_matches_cost_bearing_row(
        self,
        integ_store: Store,
    ) -> None:
        """``agent-cost gt 0`` must match a row with a positive cost.

        Cost lives at ``marginal_cost_agent_usd`` on the asyncpg row
        and ``marginal_cost.agent_usd`` on the JSON-tagged view.
        :func:`_filter_value` handles both shapes; this test pins
        that the wire round-trip flows through the store correctly.
        """
        priced_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="costs money"),
        )
        await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="free")
        )
        await integ_store.add_cost(
            priced_id,
            Cost(agent_usd=0.5),
            actor="alice",
        )

        rows = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="agent-cost", op="gt", value="0.1"),),
        )
        assert [r.id for r in rows] == [priced_id], [
            (r.id, r.marginal_cost) for r in rows
        ]
        # Same row excluded under a tighter lower bound -- proves the
        # comparison reads the actual cost, not a constant.
        rows = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="agent-cost", op="gt", value="5"),),
        )
        assert [r.id for r in rows] == []

    async def test_list_kind_route_wire_shape_round_trips_against_real_store(
        self,
        integ_store: Store,
    ) -> None:
        """End-to-end: CLI wire bytes -> FastAPI route -> Postgres.

        The unit tests cover each layer in isolation; this test wires
        the *exact* JSON-per-filter query string the trax CLI client
        emits through the real FastAPI route against the real
        Postgres-backed store. It catches every category of bug that
        hides between layers: wire shape disagreement (httpx
        serializer vs FastAPI parser), missing query param plumbing,
        JSON quoting / URL escaping edge cases, and the exact bug
        Issue#256 reports -- a matching row past the default recency
        window dropped on the floor.

        The needle is seeded *first* (oldest by created timestamp)
        so the default ``ORDER BY created DESC`` puts it past the
        ``limit=5`` window after the 50 noise rows.
        """
        needle_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="needle:with:colons parser",
                labels=["target"],
                issue_kind=["bug"],
            ),
        )
        for i in range(50):
            await integ_store.submit_issue(
                SubmitIssue(account="tester@example.com", title=f"noise {i}")
            )

        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.include_router(query_module.router)
        identity = make_test_identity()

        async def _identity_override() -> object:
            return identity

        app.dependency_overrides[current_user] = _identity_override

        def _filter_param(field: str, op: str, value: str) -> str:
            # Bit-for-bit the same JSON shape
            # ``trax.client.list_kind`` serializes. If the CLI ever
            # changes its serialization, this string would diverge
            # and this test would fail -- which is what we want (any
            # wire-format drift surfaces here, not in production).
            return json.dumps(
                {"field": field, "op": op, "value": value},
                separators=(",", ":"),
            )

        # ASGI transport keeps the route on the same event loop as
        # the integ_store. ``TestClient`` would spin a worker thread,
        # which is illegal for the shared asyncpg connection.
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                # 1. regex filter against title: the original bug.
                r = await http.get(
                    "/api/inquiries",
                    params=[
                        ("kind", "Issue"),
                        ("limit", "5"),
                        ("filter", _filter_param("title", "re", "parser")),
                    ],
                )
                assert r.status_code == 200, r.text
                assert [row["id"] for row in r.json()] == [str(needle_id)], r.json()

                # 2. list-shaped equality (``labels is target``):
                # must compare against array membership, not the
                # stringified array.
                r = await http.get(
                    "/api/inquiries",
                    params=[
                        ("kind", "Issue"),
                        ("limit", "5"),
                        ("filter", _filter_param("labels", "is", "target")),
                    ],
                )
                assert r.status_code == 200, r.text
                assert [row["id"] for row in r.json()] == [str(needle_id)]

                # 3. value containing URL-sensitive characters (``:``)
                # round-trips through httpx percent-encoding and
                # FastAPI decoding without escape damage.
                r = await http.get(
                    "/api/inquiries",
                    params=[
                        ("kind", "Issue"),
                        ("limit", "5"),
                        (
                            "filter",
                            _filter_param("title", "re", "needle:with:colons"),
                        ),
                    ],
                )
                assert r.status_code == 200, r.text
                assert [row["id"] for row in r.json()] == [str(needle_id)]

                # 4. ``kind`` is a CLI alias for the ``issue_kind``
                # payload column. Reading the discriminator (always
                # ``"Issue"`` for an Issue row) would silently match
                # nothing for any Issue.Kind literal, so the server
                # must consult the canonical payload-field mapping.
                # Regression for first-pass reviewer finding F1.
                r = await http.get(
                    "/api/inquiries",
                    params=[
                        ("kind", "Issue"),
                        ("limit", "5"),
                        ("filter", _filter_param("kind", "is", "bug")),
                    ],
                )
                assert r.status_code == 200, r.text
                assert [row["id"] for row in r.json()] == [str(needle_id)]

                # 5. control: same endpoint without filters returns the
                # default top-5 by recency; the needle is the oldest
                # row so it must be absent. Proves the recency window
                # really would have hidden the needle (Issue#256).
                r = await http.get(
                    "/api/inquiries", params={"kind": "Issue", "limit": "5"}
                )
                assert r.status_code == 200, r.text
                ids = [row["id"] for row in r.json()]
                assert str(needle_id) not in ids, ids

                # 6. disjoint ``seq_range`` union: the needle is seq 1
                # (seeded first); a union of ``1..1`` and a high open
                # interval must return the needle plus only the
                # high-seq tail, proving the OR-of-intervals lowers to
                # one indexed query that respects every interval.
                r = await http.get(
                    "/api/inquiries",
                    params=[
                        ("kind", "Issue"),
                        ("limit", "1000"),
                        ("seq_range", "1..1"),
                        ("seq_range", "40.."),
                    ],
                )
                assert r.status_code == 200, r.text
                seqs = sorted(row["seq"] for row in r.json())
                assert seqs[0] == 1
                assert all(s == 1 or s >= 40 for s in seqs)
                assert 2 not in seqs
                assert 39 not in seqs

                # 7. malformed seq_range is a 400, not a silent ignore.
                r = await http.get(
                    "/api/inquiries",
                    params=[("kind", "Issue"), ("seq_range", "foo..5")],
                )
                assert r.status_code == 400, r.text

                # 8. malformed filter is a 400, not a silent ignore --
                # silent-ignore is exactly how the pre-fix server
                # behaves with the new CLI in flight.
                r = await http.get(
                    "/api/inquiries",
                    params=[("kind", "Issue"), ("filter", "not-json")],
                )
                assert r.status_code == 400, r.text
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_web_read_routes_round_trip_against_real_store(
        self,
        integ_store: Store,
    ) -> None:
        """Every web read route example.sh hits runs its real SQL on Postgres.

        The web read routes (``_row_to_dict``, ``_edges_for`` /
        ``_PEER_COLUMNS``, ``_snapshot_to_dict``, the search ILIKE, the
        recent-changes snapshot flatten) hand-write SQL that names
        kind-specific columns -- e.g. the peer join selects
        ``belief_judgement``. The unit ``web_test`` mocks rows, so a wrong
        column name there never executes against the real schema and slips
        through -- only ``example.sh`` caught the ``_PEER_COLUMNS``
        ``t.judgement`` 500. This covers /get, /search, and
        /recent_changes -- the three example.sh exercises -- so the same
        bug class can't recur unseen. Seed a Paper that ``proves`` a Belief
        (citations store Artifact -> claim), then edit the Belief so a
        ``belief_judgement`` change row exists for the recent-changes flatten.
        """
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="x10 overfits",
                judgement="proven",
                confidence=0.9,
            ),
        )
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="TRM paper",
                source="arXiv:2501.00001",
            ),
        )
        # proves is stored Artifact(citer) -> claim: the Paper proves the Belief.
        await integ_store.add_edge(
            from_id=paper_id, to_id=belief_id, edge_kind="proves", actor="u"
        )

        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        web.attach(app)
        identity = make_test_identity()

        async def _identity_override() -> object:
            return identity

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                # The Paper detail: own fields surface bare, and the
                # outbound ``proves`` edge joins the Belief peer (the Paper is
                # the citing artifact, the from-side).
                r = await http.get(f"/api/web/get/{paper_id}")
                assert r.status_code == 200, r.text
                paper_body = r.json()
                assert paper_body["self"]["source"] == "arXiv:2501.00001"
                proves = paper_body["edges"]["proves"]
                assert [p["id"] for p in proves] == [str(belief_id)]
                assert proves[0]["judgement"] == "proven"

                # The Belief detail: the inbound backlink runs the same peer
                # join in the reverse direction (the Belief is the cited claim,
                # the to-side), projecting the Paper peer.
                r = await http.get(f"/api/web/get/{belief_id}")
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["self"]["judgement"] == "proven"
                assert body["self"]["confidence"] == 0.9
                backlink = body["backlinks"]["proves"]
                assert [b["id"] for b in backlink] == [str(paper_id)]

                # /search: cross-kind ILIKE over title/description, the
                # ``trax search`` path. Runs raw SQL against inquiries.
                r = await http.get("/api/web/search", params={"q": "overfits"})
                assert r.status_code == 200, r.text
                assert str(belief_id) in [row["id"] for row in r.json()]

                # An edit so a kind-specific change row exists, then
                # /recent_changes flattens its old_*/new_* snapshot
                # columns (``belief_judgement``) -- the ``trax recent``
                # path, exercising _snapshot_to_dict on a real prefixed
                # mirror column.
                await integ_store.set_judgement(
                    belief_id, "disproven", actor="reviewer"
                )
                r = await http.get("/api/web/recent_changes", params={"limit": "20"})
                assert r.status_code == 200, r.text
                changes = r.json()
                judged = [c for c in changes if c["kind"] == "belief_judgement"]
                assert judged, "judgement change should appear in recent"
                # The cross-kind audit feed keys snapshot fields by their
                # flat storage name, so it's belief_judgement, not bare.
                assert judged[0]["new"]["belief_judgement"] == "disproven"
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_web_search_treats_percent_as_literal(
        self,
        integ_store: Store,
    ) -> None:
        """A bare ``%`` token matches a literal percent, not every row.

        ``_build_term_clause`` escapes ILIKE wildcards in a user token, so
        ``q=%`` is a substring search for a literal ``%`` rather than a
        match-everything pattern (TRK-SRV-001).
        """
        literal_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="literal percent % here"),
        )
        await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="ordinary issue")
        )

        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        web.attach(app)

        async def _identity_override() -> object:
            return make_test_identity()

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                r = await http.get("/api/web/search", params={"q": "%"})
                assert r.status_code == 200, r.text
                ids = [row["id"] for row in r.json()]
                # Only the literal-percent row matches; the wildcard does not
                # leak into a match-all.
                assert ids == [str(literal_id)]
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_concurrent_edge_label_adds_do_not_clobber(
        self,
        integ_store: Store,
    ) -> None:
        """Two concurrent edge-label adds must both survive (TAPI-002).

        ``_mutate_edge_label`` read the labels outside the write
        transaction (``get_edge`` on its own connection) then overwrote
        via ``set_edge_annotation``, so interleaved adds lost-update one
        another. The fix locks the edge row ``FOR UPDATE`` for the whole
        read-modify-write, mirroring the inquiry list path.
        """
        src = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="src")
        )
        dst = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="dst")
        )
        await integ_store.add_edge(
            from_id=src, to_id=dst, edge_kind="requires", actor="u"
        )
        await asyncio.gather(
            integ_store.add_edge_label(
                from_id=src,
                to_id=dst,
                edge_kind="requires",
                label="a",
                actor="u",
            ),
            integ_store.add_edge_label(
                from_id=src,
                to_id=dst,
                edge_kind="requires",
                label="b",
                actor="u",
            ),
        )
        edge = await integ_store.get_edge(from_id=src, to_id=dst, edge_kind="requires")
        assert edge is not None
        assert set(edge.labels or ()) == {"a", "b"}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestIntegrationAuth:
    """End-to-end auth v2 Phase 1 paths against a real Postgres.

    Mocks can't catch FK / CHECK mismatches between :mod:`auth` and the
    schema -- e.g. an ``added_by NOT NULL`` constraint silently making
    ``bootstrap_admin`` 500. The integration pass exercises the real
    INSERTs so future schema drift surfaces here.
    """

    async def test_create_token_and_authenticate(self, integ_store: Store) -> None:
        user_id = uuid.uuid4()
        async with integ_store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, $3, 'admin', 'active')",
                user_id,
                "alice@example.com",
                "Alice",
            )
            _key_id, secret, _prefix, _role = await create_api_key(
                conn, user_id=user_id, name="laptop", ceiling="admin"
            )
        # The dependency must round-trip the secret straight back to
        # the same principal.
        request = MagicMock()
        request.headers = {"Authorization": f"Bearer {secret}"}
        request.app.state.engine = integ_store.engine
        request.app.state.store = integ_store
        # MagicMock auto-creates ``app.state.config`` as a Mock whose
        # attribute access is truthy; pin to a real Config so
        # ``current_user`` reads ``auth_disabled=False`` and exercises
        # the bearer path.
        request.app.state.config = Config()
        identity = await current_user(request)
        assert identity.user_id == user_id
        assert identity.email == "alice@example.com"
        assert identity.role == "admin"
        # ``last_used_at`` actually persisted.
        async with integ_store.engine.acquire() as conn:
            last_used = await conn.fetchval(
                "SELECT last_used_at FROM api_keys WHERE user_id = $1", user_id
            )
        assert last_used is not None

    async def test_revoked_token_is_rejected(self, integ_store: Store) -> None:
        user_id = uuid.uuid4()
        async with integ_store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, $3, 'writer', 'active')",
                user_id,
                "bob@example.com",
                "Bob",
            )
            key_id, secret, _prefix, _role = await create_api_key(
                conn, user_id=user_id, name="ci", ceiling="admin"
            )
            revoked = await revoke_api_key(conn, key_id=key_id, user_id=user_id)
            assert revoked is True
        request = MagicMock()
        request.headers = {"Authorization": f"Bearer {secret}"}
        request.app.state.engine = integ_store.engine
        request.app.state.store = integ_store
        request.app.state.config = Config()  # see test_create_token rationale.
        with pytest.raises(HTTPException) as exc_info:
            await current_user(request)
        assert exc_info.value.status_code == 401

    async def test_bootstrap_admin_seeds_with_null_added_by(
        self,
        integ_store: Store,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``allowlist.added_by`` must accept NULL so bootstrap can self-seed.

        Catches a future schema drift to ``NOT NULL`` that would break
        the first-deploy seed. Also exercises the user-row + api_key
        side-effects + token-file write that the bootstrap path now
        performs end-to-end against real Postgres.
        """
        token_file = tmp_path / "bootstrap_token"
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "admin@example.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(token_file))
        async with integ_store.engine.acquire() as conn:
            await bootstrap_admin(conn)
            # A second call must be a no-op (idempotent rerun semantics).
            await bootstrap_admin(conn)
            rows = await conn.fetch(
                "SELECT email_or_pattern, role, added_by FROM allowlist"
            )
            user_rows = await conn.fetch(
                "SELECT email, role, status FROM users WHERE email = $1",
                "admin@example.com",
            )
            key_rows = await conn.fetch(
                "SELECT name FROM api_keys WHERE user_id = "
                "(SELECT id FROM users WHERE email = $1)",
                "admin@example.com",
            )
        assert len(rows) == 1
        assert rows[0]["email_or_pattern"] == "admin@example.com"
        assert rows[0]["role"] == "admin"
        assert rows[0]["added_by"] is None
        # The user row + a single api_key were also created -- the
        # idempotency gate prevented duplicates on the second call.
        assert len(user_rows) == 1
        assert user_rows[0]["role"] == "admin"
        assert user_rows[0]["status"] == "active"
        assert len(key_rows) == 1
        assert key_rows[0]["name"] == "bootstrap"
        # Token file landed (mode 0600, prefixed plaintext secret).
        assert token_file.exists()
        assert (token_file.stat().st_mode & 0o777) == 0o600
        assert token_file.read_text().strip().startswith("trax_")

    async def test_delete_user_cascades_keys_and_nulls_change_log(
        self,
        integ_store: Store,
    ) -> None:
        """Hard-delete cascades ``api_keys`` and NULLs ``change_log.api_key_id``.

        End-to-end cover for the admin "Remove" flow. Asserts the
        schema-level FK behaviour the unit tests can't observe: keys
        with this user as ``user_id`` are gone (``ON DELETE CASCADE``)
        and any audit row that referenced one of those keys keeps its
        actor / kind / subject but loses the per-key link
        (``ON DELETE SET NULL``).
        """
        user_id = uuid.uuid4()
        async with integ_store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, $3, 'writer', 'active')",
                user_id,
                "doomed@example.com",
                "Doomed",
            )
            key_id, _secret, _prefix, _role = await create_api_key(
                conn, user_id=user_id, name="laptop", ceiling="admin"
            )
            # An audit row stamped with this key id -- the kind the
            # bearer-auth path emits in production.
            change_id = uuid.uuid4()
            subject_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO change_log (id, api_key_id, actor, subject_id, "
                "subject_kind, kind) "
                "VALUES ($1, $2, $3, $4, 'Issue', 'created')",
                change_id,
                key_id,
                "doomed@example.com",
                subject_id,
            )
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)
            # Keys gone (CASCADE on api_keys.user_id).
            remaining_keys = await conn.fetchval(
                "SELECT count(*) FROM api_keys WHERE user_id = $1", user_id
            )
            # Audit row survives with api_key_id nulled (SET NULL on
            # change_log.api_key_id).
            change_row = await conn.fetchrow(
                "SELECT api_key_id, actor FROM change_log WHERE id = $1",
                change_id,
            )
        assert remaining_keys == 0
        assert change_row is not None
        assert change_row["api_key_id"] is None
        assert change_row["actor"] == "doomed@example.com"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestClientChangeIdReplay:
    """End-to-end replay via ``set_client_change_id`` -> ``emit_change``.

    The trax client and SPA send ``Idempotency-Key`` so a retried mutation
    collides on ``change_log.id`` and the server returns the original
    outcome rather than double-applying the change.
    """

    async def test_same_id_same_operation_returns_original_outcome(
        self,
        integ_store: Store,
    ) -> None:
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i")
        )
        client_id = uuid.uuid4()
        # First edit: client_id becomes change_log.id.
        set_client_change_id(client_id)
        await integ_store.set_priority(issue_id, 1, actor="user")
        async with integ_store.engine.acquire() as conn:
            rows_before = await conn.fetch(
                "SELECT id FROM change_log WHERE subject_id = $1 AND kind = $2",
                issue_id,
                "issue_priority",
            )
        assert len(rows_before) == 1
        assert rows_before[0]["id"] == client_id

        # Retry with same client_id: middleware would set it again. The
        # second call must collide on the PK, the savepoint rolls back
        # the cost UPDATE, and emit_change returns without writing a
        # second change_log row or double-charging cost.
        set_client_change_id(client_id)
        await integ_store.set_priority(issue_id, 1, actor="user")
        async with integ_store.engine.acquire() as conn:
            rows_after = await conn.fetch(
                "SELECT id FROM change_log WHERE subject_id = $1 AND kind = $2",
                issue_id,
                "issue_priority",
            )
        assert len(rows_after) == 1, "retry must replay, not double-write to change_log"

    async def test_same_id_different_operation_raises_conflict(
        self,
        integ_store: Store,
    ) -> None:
        issue_a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a")
        )
        issue_b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b")
        )
        client_id = uuid.uuid4()

        set_client_change_id(client_id)
        await integ_store.set_priority(issue_a, 1, actor="user")

        # Same client_id, different subject -> the row in change_log is
        # for issue_a/'priority' but the new request would write
        # one for issue_b. ConflictError is the right answer (client bug
        # or attempted UUID-reuse, not a legitimate retry).
        set_client_change_id(client_id)
        with pytest.raises(ConflictError, match="idempotency_key"):
            await integ_store.set_priority(issue_b, 1, actor="user")

    async def test_header_only_submit_retry_replays_not_500(
        self,
        integ_store: Store,
    ) -> None:
        """A header-only submit (slot set, no body key) replays on retry.

        The ``Idempotency-Key`` header lands in the change-id slot via
        ``set_client_change_id`` with ``SubmitIssue.idempotency_key`` unset.
        The submit path used to ``assert req.idempotency_key is not None`` in
        the collision-recovery branch, so a retried header-only submit hit
        the assert -> 500. Now the effective key (body field OR slot) drives
        both the replay probe and the recovery, so the retry returns the same
        id (REV-OPUS-01).
        """
        key = new_uuid()
        # First header-only submit: slot set, no body idempotency_key.
        set_client_change_id(key)
        first = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="header-only")
        )
        # Retry with the same header key, still no body key. Must replay the
        # original row, not assert/500.
        set_client_change_id(key)
        second = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="header-only")
        )
        assert first == second
        assert first != key  # server-minted
        async with integ_store.engine.acquire() as conn:
            inquiry_count = await conn.fetchval(
                "SELECT COUNT(*) FROM inquiries WHERE id = $1", first
            )
            change_count = await conn.fetchval(
                "SELECT COUNT(*) FROM change_log WHERE subject_id = $1", first
            )
        assert inquiry_count == 1
        assert change_count == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestFirstEdgeInfersProduced:
    """The first edge between two vertices infers ``younger produced_by older``.

    The definition of provenance (see ``Inquiry.produced_by``): on the first
    edge between a pair, the older vertex produced the younger, stamped as a
    ``produced_by`` edge child -> parent (from=younger, to=older).
    Birth-coincident creates are the special case; a late link between two
    pre-existing nodes is the same rule. Verified end to end against a real Store.
    """

    @staticmethod
    async def _set_created(store: Store, target_id: uuid.UUID, when: datetime) -> None:
        """Pin a row's ``created`` so older/younger ordering is deterministic."""
        async with store.engine.acquire() as conn:
            await conn.execute(
                "UPDATE inquiries SET created = $2 WHERE id = $1", target_id, when
            )

    async def _two_issues(self, store: Store) -> tuple[uuid.UUID, uuid.UUID]:
        """Create an older and a younger Issue with pinned, distinct created."""
        older = await store.submit_issue(
            SubmitIssue(account="tester@example.com", title="older")
        )
        younger = await store.submit_issue(
            SubmitIssue(account="tester@example.com", title="younger")
        )
        await self._set_created(store, older, datetime(2020, 1, 1, tzinfo=UTC))
        await self._set_created(store, younger, datetime(2020, 1, 2, tzinfo=UTC))
        return older, younger

    async def _two_papers(self, store: Store) -> tuple[uuid.UUID, uuid.UUID]:
        """Create an older (cited) and younger (citing) Paper, pinned created."""
        older = await store.submit_paper(
            SubmitPaper(
                account="tester@example.com", title="cited", source="arXiv:2001.00001"
            )
        )
        younger = await store.submit_paper(
            SubmitPaper(
                account="tester@example.com", title="citing", source="arXiv:2401.00001"
            )
        )
        await self._set_created(store, older, datetime(2020, 1, 1, tzinfo=UTC))
        await self._set_created(store, younger, datetime(2024, 1, 2, tzinfo=UTC))
        return older, younger

    async def test_first_requires_edge_infers_produced_older_to_younger(
        self, integ_store: Store
    ) -> None:
        older, younger = await self._two_issues(integ_store)
        # The younger requires the older (stored requirer=younger -> prerequisite=
        # older). This is the pair's first edge, so the older produced the
        # younger -- regardless of the requires edge's own direction.
        await integ_store.add_edge(
            from_id=younger, to_id=older, edge_kind="requires", actor="alice"
        )
        older_row = cast(Issue, await integ_store.get_inquiry(older))
        younger_row = cast(Issue, await integ_store.get_inquiry(younger))
        assert younger_row.id in {e.id for e in older_row.produces}
        assert older_row.id in {e.id for e in younger_row.produced_by}

    async def test_reverse_direction_edge_still_infers_older_produced_younger(
        self, integ_store: Store
    ) -> None:
        older, younger = await self._two_issues(integ_store)
        # Edge written older -> younger (older requires younger). Provenance still
        # follows age, not edge direction: older produced younger.
        await integ_store.add_edge(
            from_id=older, to_id=younger, edge_kind="requires", actor="alice"
        )
        older_row = cast(Issue, await integ_store.get_inquiry(older))
        younger_row = cast(Issue, await integ_store.get_inquiry(younger))
        assert younger in {e.id for e in older_row.produces}
        assert older in {e.id for e in younger_row.produced_by}

    async def test_inferred_produced_target_can_be_issue(
        self, integ_store: Store
    ) -> None:
        # The widened produced_by edge (Inquiry -> Inquiry) lets an Issue be the
        # produced row, which the pre-widen schema forbade.
        older, younger = await self._two_issues(integ_store)
        await integ_store.add_edge(
            from_id=older, to_id=younger, edge_kind="narrows", actor="alice"
        )
        older_row = cast(Issue, await integ_store.get_inquiry(older))
        assert younger in {e.id for e in older_row.produces}
        assert "Issue" in {e.kind for e in older_row.produces}

    async def test_second_edge_between_pair_does_not_restamp(
        self, integ_store: Store
    ) -> None:
        older, younger = await self._two_issues(integ_store)
        await integ_store.add_edge(
            from_id=younger, to_id=older, edge_kind="requires", actor="alice"
        )
        # A second edge between the same pair must not add another produced_by edge.
        await integ_store.add_edge(
            from_id=older, to_id=younger, edge_kind="narrows", actor="alice"
        )
        async with integ_store.engine.acquire() as conn:
            produced = await conn.fetchval(
                "SELECT count(*) FROM edges WHERE edge_kind = 'produced_by' "
                "AND ((from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1))",
                older,
                younger,
            )
        assert produced == 1

    async def test_explicit_produced_by_edge_does_not_self_infer(
        self, integ_store: Store
    ) -> None:
        older, younger = await self._two_issues(integ_store)
        # Recording provenance directly (younger produced_by older) must not
        # trigger a second inferred edge.
        await integ_store.add_edge(
            from_id=younger, to_id=older, edge_kind="produced_by", actor="alice"
        )
        async with integ_store.engine.acquire() as conn:
            produced = await conn.fetchval(
                "SELECT count(*) FROM edges WHERE edge_kind = 'produced_by' "
                "AND ((from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1))",
                older,
                younger,
            )
        assert produced == 1

    async def test_birth_coincident_inline_create_infers_produced(
        self, integ_store: Store
    ) -> None:
        # The motivating case: anchor exists, a new row is born as an edge target
        # in one atomic batch. The anchor (older) produced the newborn (younger).
        anchor = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="anchor")
        )
        ids = await integ_store.submit_batch(
            [SubmitIssue(account="tester@example.com", title="newborn")],
            edges=[
                BatchEdge(
                    from_id=anchor,
                    to_index=0,
                    edge_kind="narrows",
                )
            ],
        )
        newborn = ids[0]
        anchor_row = cast(Issue, await integ_store.get_inquiry(anchor))
        assert newborn in {e.id for e in anchor_row.produces}

    async def test_supersedes_infers_produced_by(self, integ_store: Store) -> None:
        # ``younger supersedes older`` is consistent with the universal rule:
        # both point younger -> older, so the first edge between the pair infers
        # ``younger produced_by older`` (from = younger, to = older) like any
        # other kind. Supersession is not a separate exception.
        older, younger = await self._two_issues(integ_store)
        await integ_store.add_edge(
            from_id=younger, to_id=older, edge_kind="supersedes", actor="alice"
        )
        async with integ_store.engine.acquire() as conn:
            produced = await conn.fetchrow(
                "SELECT from_id, to_id FROM edges WHERE edge_kind = 'produced_by' "
                "AND ((from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1))",
                older,
                younger,
            )
        assert produced is not None
        # produced_by stored child -> parent: from = younger (produced), to = older.
        assert (produced["from_id"], produced["to_id"]) == (younger, older)

    async def test_cites_paper_round_trips_into_projection(
        self, integ_store: Store
    ) -> None:
        # A cites_paper edge (citing younger -> cited older) fills the citing
        # paper's forward ``cites`` and the cited paper's inverse ``cited_by``,
        # carrying the edge note (no valence -- historical citation).
        cited, citing = await self._two_papers(integ_store)
        await integ_store.add_edge(
            from_id=citing,
            to_id=cited,
            edge_kind="cites_paper",
            note="see section 3",
            actor="alice",
        )
        citing_row = cast(Paper, await integ_store.get_inquiry(citing))
        cited_row = cast(Paper, await integ_store.get_inquiry(cited))
        assert cited in {e.id for e in citing_row.cites}
        assert {e.note for e in citing_row.cites} == {"see section 3"}
        assert citing in {e.id for e in cited_row.cited_by}

    async def test_lone_cites_paper_infers_no_produced_by(
        self, integ_store: Store
    ) -> None:
        # Provenance-neutrality: a historical citation is NOT a provenance claim.
        # cites_paper is absent from PRODUCED_INFERENCE_PRECEDENCE, so a pair whose
        # only edge is cites_paper gets NO inferred produced_by (a 2024 paper
        # citing a 2020 paper must not be recorded as "produced by" it).
        cited, citing = await self._two_papers(integ_store)
        await integ_store.add_edge(
            from_id=citing, to_id=cited, edge_kind="cites_paper", actor="alice"
        )
        async with integ_store.engine.acquire() as conn:
            produced = await conn.fetchval(
                "SELECT count(*) FROM edges WHERE edge_kind = 'produced_by' "
                "AND ((from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1))",
                cited,
                citing,
            )
        assert produced == 0
        # And the projection confirms neither side gained a provenance edge.
        citing_row = cast(Paper, await integ_store.get_inquiry(citing))
        assert citing_row.produced_by == ()
        assert citing_row.produces == ()

    async def _paper_and_belief(self, store: Store) -> tuple[uuid.UUID, uuid.UUID]:
        """Create an older Belief (claim) and a younger Paper (evidence)."""
        belief = await store.submit_belief(
            SubmitBelief(account="tester@example.com", title="claim")
        )
        paper = await store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="evidence",
                source="arXiv:2401.00002",
            )
        )
        await self._set_created(store, belief, datetime(2020, 1, 1, tzinfo=UTC))
        await self._set_created(store, paper, datetime(2024, 1, 2, tzinfo=UTC))
        return belief, paper

    @pytest.mark.parametrize("edge_kind", ["favors", "proves"])
    async def test_lone_citation_of_belief_infers_no_produced_by(
        self, integ_store: Store, edge_kind: Edge.Kind
    ) -> None:
        # Epistemic-neutrality: a Paper favoring/proving a Belief is a CITATION,
        # not a provenance claim. proves/favors are absent from
        # PRODUCED_INFERENCE_PRECEDENCE, so a pair whose only edge is such a
        # citation gets NO inferred produced_by. Regression: these kinds were once
        # ranked in PRECEDENCE, which stamped one bogus "Belief produced_by Paper"
        # parent per citing paper -- a cited claim would accrue a fake producer for
        # every piece of evidence marshalled to support it.
        belief, paper = await self._paper_and_belief(integ_store)
        await integ_store.add_edge(
            from_id=paper, to_id=belief, edge_kind=edge_kind, actor="alice"
        )
        async with integ_store.engine.acquire() as conn:
            produced = await conn.fetchval(
                "SELECT count(*) FROM edges WHERE edge_kind = 'produced_by' "
                "AND ((from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1))",
                belief,
                paper,
            )
        assert produced == 0
        # The Belief gained no provenance parent; the Paper cites it via the
        # epistemic edge, which is the only relationship stored.
        belief_row = cast(Belief, await integ_store.get_inquiry(belief))
        assert belief_row.produced_by == ()
        assert belief_row.produces == ()

    async def test_cites_paper_allows_mutual_citation(self, integ_store: Store) -> None:
        # A historical bibliography is an EXTERNAL fact we record, not a DAG we
        # own: two papers can legitimately cite each other (companion papers,
        # errata, cross-version references). cites_paper is acyclicity-exempt, so
        # ``A cites B`` then ``B cites A`` both persist rather than raising the
        # per-kind cycle ConflictError that DAG kinds (narrows/requires/...) get.
        a, b = await self._two_papers(integ_store)
        await integ_store.add_edge(
            from_id=a, to_id=b, edge_kind="cites_paper", actor="alice"
        )
        await integ_store.add_edge(
            from_id=b, to_id=a, edge_kind="cites_paper", actor="alice"
        )
        a_row = cast(Paper, await integ_store.get_inquiry(a))
        b_row = cast(Paper, await integ_store.get_inquiry(b))
        assert b in {e.id for e in a_row.cites}
        assert a in {e.id for e in b_row.cites}
        # Acyclicity-exemption does NOT relax the self-loop bar: a paper still
        # cannot cite itself (from_id <> to_id, enforced before the policy skip).
        with pytest.raises(ValidationError):
            await integ_store.add_edge(
                from_id=a, to_id=a, edge_kind="cites_paper", actor="alice"
            )

    async def test_precedence_requires_outranks_supersedes_in_one_batch(
        self, integ_store: Store
    ) -> None:
        # A batch links the same pair with supersedes FIRST then requires.
        # Inference is universal -- either kind alone would infer produced_by --
        # so the pair infers it regardless of arrival order; precedence only
        # picks which kind labels the audit reason.
        older, younger = await self._two_issues(integ_store)
        await integ_store.submit_batch(
            [],
            edges=[
                BatchEdge(from_id=younger, to_id=older, edge_kind="supersedes"),
                BatchEdge(from_id=younger, to_id=older, edge_kind="requires"),
            ],
        )
        older_row = cast(Issue, await integ_store.get_inquiry(older))
        assert younger in {e.id for e in older_row.produces}

    async def test_inferred_produced_by_audit_is_chained_to_trigger(
        self, integ_store: Store
    ) -> None:
        # The inferred produced_by audit chains to the edge that triggered it via
        # caused_by, and carries the stable inferred-provenance reason prefix.
        # The inferred edge is stored child -> parent (from=younger, to=older), so
        # the younger row is the audit subject.
        older, younger = await self._two_issues(integ_store)
        await integ_store.add_edge(
            from_id=younger, to_id=older, edge_kind="requires", actor="alice"
        )
        async with integ_store.engine.acquire() as conn:
            trigger = await conn.fetchval(
                "SELECT id FROM change_log WHERE kind = 'edge_added' "
                "AND new_peer_edge_kind = 'requires' AND subject_id = $1",
                younger,
            )
            inferred = await conn.fetchrow(
                "SELECT caused_by, reason FROM change_log WHERE kind = 'edge_added' "
                "AND new_peer_edge_kind = 'produced_by' AND subject_id = $1 "
                "AND new_peer_id = $2",
                younger,
                older,
            )
        assert inferred is not None
        assert inferred["caused_by"] == trigger
        assert inferred["reason"].startswith(INFERRED_PROVENANCE_REASON)

    async def test_inferred_produced_by_creation_does_not_cascade(
        self, integ_store: Store
    ) -> None:
        # cascade=False on the inferred edge: its own creation emits no
        # dependency_changed. Any dependency_changed in this run must be caused by
        # the user-driven requires edge, never by the inferred produced_by edge.
        older, younger = await self._two_issues(integ_store)
        await integ_store.add_edge(
            from_id=younger, to_id=older, edge_kind="requires", actor="alice"
        )
        async with integ_store.engine.acquire() as conn:
            inferred_id = await conn.fetchval(
                "SELECT id FROM change_log WHERE kind = 'edge_added' "
                "AND new_peer_edge_kind = 'produced_by' AND subject_id = $1 "
                "AND new_peer_id = $2",
                younger,
                older,
            )
            caused_by_inferred = await conn.fetchval(
                "SELECT count(*) FROM change_log WHERE kind = 'dependency_changed' "
                "AND caused_by = $1",
                inferred_id,
            )
        assert caused_by_inferred == 0

    async def test_lone_proves_of_belief_infers_no_produced_by(
        self, integ_store: Store
    ) -> None:
        # A ``proves`` edge is a CITATION (Artifact -> claim), not provenance: a
        # WebSearch proving a Belief was NOT produced by that Belief -- the search
        # is independent evidence that later bears on the claim. proves is
        # provenance-neutral, so a lone proves between them infers no produced_by.
        # (A Belief genuinely producing a search it spawned is recorded by an
        # explicit produced_by edge, not inferred from a citation.)
        belief = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="claim")
        )
        search = await integ_store.submit_websearch(
            SubmitWebSearch(account="tester@example.com", title="search", query="q")
        )
        await self._set_created(integ_store, belief, datetime(2020, 1, 1, tzinfo=UTC))
        await self._set_created(integ_store, search, datetime(2020, 1, 2, tzinfo=UTC))
        # proves stores Artifact -> {Belief, Experiment}: the WebSearch (citing
        # artifact) points up to the older Belief it bears on.
        await integ_store.add_edge(
            from_id=search, to_id=belief, edge_kind="proves", actor="alice"
        )
        belief_row = cast(Belief, await integ_store.get_inquiry(belief))
        assert belief_row.produces == ()
        assert belief_row.produced_by == ()


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
