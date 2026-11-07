"""End-to-end integration suite (Postgres-backed; session-scoped engine; marked)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import json
import uuid

from fastapi import FastAPI, HTTPException, Request
from treekle import from_plain

import asyncpg
import httpx2
import pytest

from trackinizer.server import web
from trackinizer.server.access_policy import DefaultPolicy
from trackinizer.server.api import (
    metrics_routes,
    query,
)
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    make_test_identity,
)
from trackinizer.server.auth import (
    BOOTSTRAP_ADMIN_ENV,
    BOOTSTRAP_TOKEN_FILE_ENV,
    bootstrap_admin,
    create_api_key,
    current_user,
    revoke_api_key,
)
from trackinizer.server.config import Config
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.store.store_test_support import (
    fetch_value,
    seed_active_user,
)
from trackinizer.wire.bodies import (
    SubmitAgentSession,
    SubmitBelief,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
)


if TYPE_CHECKING:
    from pathlib import Path

    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestIntegrationEndToEnd:
    """End-to-end cascade behaviour against a session-scoped Postgres.

    The shared ``integ_engine`` fixture amortises pool/codec setup; each
    test pays only ``integ_store``'s restore of the session's build.
    """

    async def test_metrics_db_rejects_negative_step(self, integ_store: Store) -> None:
        """The ``step >= 0`` DB CHECK backstops the wire ``ge=0`` guard.

        Mirrors ``session_records.idx``'s CHECK: a direct-SQL writer that
        bypasses the wire cannot persist a negative step.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
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
            SubmitExperiment(account="tester@example.com", title="run"),
        )
        # Blank means what the wire's ``str.strip`` means -- every code point
        # ``str.isspace`` admits -- or a tab key stores here and 500s the read.
        blanks = [c for c in map(chr, range(0x110000)) if c.isspace()]
        insert = (
            "INSERT INTO experiment_metrics "
            "(experiment_id, key, step, value) VALUES ($1, $2, $3, 0.5)"
        )
        async with integ_store.engine.acquire() as conn:
            for step, bad_key in enumerate(["", "x" * 513, "".join(blanks), *blanks]):
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(insert, eid, bad_key, step)
            await conn.execute(insert, eid, "　val/acc\t", 0)

    async def test_metrics_db_rejects_non_finite_value(
        self,
        integ_store: Store,
    ) -> None:
        """The finiteness DB CHECK backstops the wire ``allow_inf_nan=False``.

        The wire rejects NaN/±Inf and ``read_metrics`` reconstructs the value,
        so a stored non-finite value would 500 the read (NaN/±Inf are valid
        float8 but not valid JSON numbers). The CHECK stops a direct-SQL /
        bulk-load writer from persisting one -- completing the backstop set
        alongside the ``step >= 0`` and ``kind = 'scalar'`` CHECKs.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
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
        the ``session_records.kind`` CHECK.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
        )
        async with integ_store.engine.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO experiment_metrics "
                    "(experiment_id, key, step, value, kind) "
                    "VALUES ($1, 'loss', 0, 0.5, 'histogram')",
                    eid,
                )

    async def test_metrics_and_config_round_trip_through_http(
        self,
        integ_store: Store,
    ) -> None:
        """End-to-end: log metrics + read them + read config over HTTP.

        Proves the full chain the SPA depends on: ``POST/GET
        /api/experiments/{id}/metrics`` (the metrics routes) and the ``config``
        surfacing in ``GET /api/web/get/{id}`` (the detail view). Config rides on
        the submit; metrics are logged then read back in (key, step) order.
        """
        cfg: dict[str, object] = {"lr": 3e-4, "batch": 32}
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run", config=cfg),
        )
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        app.include_router(metrics_routes.router)
        web.attach(app)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None)

        await seed_active_user(integ_store, TEST_USER_EMAIL)
        app.dependency_overrides[current_user] = _identity
        app.dependency_overrides[web.optional_identity] = _identity
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                logged = await http.post(
                    f"/api/experiments/{eid}/metrics",
                    json={
                        "points": [
                            {"key": "loss", "step": 0, "value": 0.9},
                            {"key": "loss", "step": 1, "value": 0.5},
                            {"key": "acc", "step": 0, "value": 0.4},
                        ],
                    },
                )
                assert logged.status_code == 200, logged.text
                assert from_plain(logged.json(), dict[str, object]) == {
                    "logged": 3,
                    "skipped": 0,
                }

                metrics_response = await http.get(
                    f"/api/experiments/{eid}/metrics",
                )
                assert metrics_response.status_code == 200, metrics_response.text
                pts = from_plain(
                    from_plain(metrics_response.json(), dict[str, object])["points"],
                    list[object],
                )
                assert [
                    (
                        from_plain(from_plain(p, dict[str, object])["key"], str),
                        from_plain(from_plain(p, dict[str, object])["step"], int),
                        from_plain(from_plain(p, dict[str, object])["value"], float),
                    )
                    for p in pts
                ] == [
                    ("acc", 0, 0.4),
                    ("loss", 0, 0.9),
                    ("loss", 1, 0.5),
                ]

                # ``config`` surfaces on the SPA detail view verbatim.
                detail = await http.get(f"/api/web/get/{eid}")
                assert detail.status_code == 200, detail.text
                detail_body = from_plain(detail.json(), dict[str, object])
                assert (
                    from_plain(detail_body["self"], dict[str, object])["config"] == cfg
                )

                # An over-cap batch is a clean 422 at the boundary (not a 500 or
                # a memory-pinning mega-INSERT), and writes nothing.
                over = {
                    "points": [
                        {"key": "loss", "step": i, "value": 1.0} for i in range(10_001)
                    ],
                }
                over_resp = await http.post(
                    f"/api/experiments/{eid}/metrics",
                    json=over,
                )
                assert over_resp.status_code == 422, over_resp.text
                still = await http.get(f"/api/experiments/{eid}/metrics")
                assert (
                    len(
                        from_plain(
                            from_plain(still.json(), dict[str, object])["points"],
                            list[object],
                        ),
                    )
                    == 3
                )  # Unchanged.
        finally:
            app.dependency_overrides.pop(current_user, None)
            app.dependency_overrides.pop(web.optional_identity, None)

    async def test_web_get_surfaces_agentsession_fields(
        self,
        integ_store: Store,
    ) -> None:
        """``/api/web/get`` projects an AgentSession's kind-specific columns.

        Proves the Phase-1a web read path against the real schema: the
        ``agentsession_*`` columns surface as bare ``cli`` / ``cli_session_id``
        / ``started`` keys, ISO-formatted timestamps, and a live session
        (``ended IS NULL``) reads ``ended: null``.
        """
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="view",
                cli="claude",
                cli_session_id="sess-9",
            ),
            audience="open",
        )

        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        web.attach(app)

        async def _identity_override() -> object:
            return make_test_identity()

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                r = await http.get(f"/api/web/get/{sid}")
                assert r.status_code == 200, r.text
                self_view = from_plain(
                    from_plain(r.json(), dict[str, object])["self"],
                    dict[str, object],
                )
                assert from_plain(self_view["kind"], str) == "AgentSession"
                assert self_view["cli"] == "claude"
                assert self_view["cli_session_id"] == "sess-9"
                # A live session has not ended.
                assert self_view["ended"] is None
        finally:
            app.dependency_overrides.pop(current_user, None)

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
            stored = await fetch_value(
                conn,
                "SELECT labels FROM inquiries WHERE id = $1",
                rid,
            )
        assert stored is None

    async def test_list_kind_route_wire_shape_round_trips_against_real_store(
        self,
        integ_store: Store,
    ) -> None:
        """End-to-end: CLI wire bytes -> FastAPI route -> Postgres.

        The unit tests cover each layer in isolation; this test wires
        the *exact* JSON-per-filter query string the trax CLI client
        emits through the real FastAPI route against the real
        Postgres-backed store. It catches every category of bug that
        hides between layers: wire shape disagreement (httpx2
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
                SubmitIssue(account="tester@example.com", title=f"noise {i}"),
            )

        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.include_router(query.router)
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
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
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
                assert [from_plain(row["id"], str) for row in _json_objects(r)] == [
                    str(needle_id),
                ], r.json()

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
                assert [from_plain(row["id"], str) for row in _json_objects(r)] == [
                    str(needle_id),
                ]

                # 3. value containing URL-sensitive characters (``:``)
                # round-trips through httpx2 percent-encoding and
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
                assert [from_plain(row["id"], str) for row in _json_objects(r)] == [
                    str(needle_id),
                ]

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
                assert [from_plain(row["id"], str) for row in _json_objects(r)] == [
                    str(needle_id),
                ]

                # 5. control: same endpoint without filters returns the
                # default top-5 by recency; the needle is the oldest
                # row so it must be absent. Proves the recency window
                # really would have hidden the needle (Issue#256).
                r = await http.get(
                    "/api/inquiries",
                    params={"kind": "Issue", "limit": "5"},
                )
                assert r.status_code == 200, r.text
                ids = [from_plain(row["id"], str) for row in _json_objects(r)]
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
                seqs = sorted(from_plain(row["seq"], int) for row in _json_objects(r))
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

        The web read routes (``_row_to_dict``, ``_grouped_edges`` /
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
        # Proves is stored Artifact(citer) -> claim: the Paper proves the Belief.
        await integ_store.add_edge(
            from_id=paper_id,
            to_id=belief_id,
            edge_kind="proves",
            actor="u",
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
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                # The Paper detail: own fields surface bare, and the
                # outbound ``proves`` edge joins the Belief peer (the Paper is
                # the citing artifact, the from-side).
                r = await http.get(f"/api/web/get/{paper_id}")
                assert r.status_code == 200, r.text
                paper_body = from_plain(r.json(), dict[str, object])
                assert (
                    from_plain(
                        from_plain(paper_body["self"], dict[str, object])["source"],
                        str,
                    )
                    == "arXiv:2501.00001"
                )
                proves = from_plain(
                    from_plain(paper_body["edges"], dict[str, object])["proves"],
                    list[object],
                )
                assert [
                    from_plain(from_plain(p, dict[str, object])["id"], str)
                    for p in proves
                ] == [
                    str(belief_id),
                ]
                assert (
                    from_plain(
                        from_plain(proves[0], dict[str, object])["judgement"],
                        str,
                    )
                    == "proven"
                )

                # The Belief detail: the inbound backlink runs the same peer
                # join in the reverse direction (the Belief is the cited claim,
                # the to-side), projecting the Paper peer.
                r = await http.get(f"/api/web/get/{belief_id}")
                assert r.status_code == 200, r.text
                body = from_plain(r.json(), dict[str, object])
                assert (
                    from_plain(
                        from_plain(body["self"], dict[str, object])["judgement"],
                        str,
                    )
                    == "proven"
                )
                assert (
                    from_plain(
                        from_plain(body["self"], dict[str, object])["confidence"],
                        float,
                    )
                    == 0.9
                )
                backlink = from_plain(
                    from_plain(body["backlinks"], dict[str, object])["proves"],
                    list[object],
                )
                assert [
                    from_plain(from_plain(b, dict[str, object])["id"], str)
                    for b in backlink
                ] == [str(paper_id)]

                # /search: cross-kind ILIKE over title/description, the
                # ``trax search`` path. Runs raw SQL against inquiries.
                r = await http.get("/api/web/search", params={"q": "overfits"})
                assert r.status_code == 200, r.text
                assert str(belief_id) in [
                    from_plain(row["id"], str) for row in _json_objects(r)
                ]

                # An edit so a kind-specific change row exists, then
                # /recent_changes flattens its old_*/new_* snapshot
                # columns (``belief_judgement``) -- the ``trax recent``
                # path, exercising _snapshot_to_dict on a real prefixed
                # mirror column.
                await integ_store.set_judgement(
                    belief_id,
                    "disproven",
                    actor="reviewer",
                )
                r = await http.get("/api/web/recent_changes", params={"limit": "20"})
                assert r.status_code == 200, r.text
                changes = _json_objects(r)
                judged = [
                    c
                    for c in changes
                    if from_plain(c["kind"], str) == "belief_judgement"
                ]
                assert judged, "judgement change should appear in recent"
                # The cross-kind audit feed keys snapshot fields by their
                # flat storage name, so it's belief_judgement, not bare.
                assert (
                    from_plain(
                        from_plain(judged[0]["new"], dict[str, object])[
                            "belief_judgement"
                        ],
                        str,
                    )
                    == "disproven"
                )
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
            SubmitIssue(account="tester@example.com", title="ordinary issue"),
        )

        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        web.attach(app)

        async def _identity_override() -> object:
            return make_test_identity()

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                r = await http.get("/api/web/search", params={"q": "%"})
                assert r.status_code == 200, r.text
                ids = [from_plain(row["id"], str) for row in _json_objects(r)]
                # Only the literal-percent row matches; the wildcard does not
                # leak into a match-all.
                assert ids == [str(literal_id)]
        finally:
            app.dependency_overrides.pop(current_user, None)


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
                conn,
                user_id=user_id,
                name="laptop",
                ceiling="admin",
            )
        # The dependency must round-trip the secret straight back to
        # the same principal.
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        request = Request(
            {
                "type": "http",
                "app": app,
                "headers": [(b"authorization", f"Bearer {secret}".encode())],
            },
        )
        identity = await current_user(request)
        assert identity.user_id == user_id
        assert identity.email == "alice@example.com"
        assert identity.role == "admin"
        # ``last_used_at`` actually persisted.
        async with integ_store.engine.acquire() as conn:
            last_used = await fetch_value(
                conn,
                "SELECT last_used_at FROM api_keys WHERE user_id = $1",
                user_id,
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
                conn,
                user_id=user_id,
                name="ci",
                ceiling="admin",
            )
            revoked = await revoke_api_key(conn, key_id=key_id, user_id=user_id)
            assert revoked is True
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        request = Request(
            {
                "type": "http",
                "app": app,
                "headers": [(b"authorization", f"Bearer {secret}".encode())],
            },
        )
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
            await bootstrap_admin(conn, policy=DefaultPolicy())
            # A second call must be a no-op (idempotent rerun semantics).
            await bootstrap_admin(conn, policy=DefaultPolicy())
            rows = await conn.fetch(
                "SELECT email_or_pattern, role, added_by FROM allowlist",
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
                conn,
                user_id=user_id,
                name="laptop",
                ceiling="admin",
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
            remaining_keys = await fetch_value(
                conn,
                "SELECT count(*) FROM api_keys WHERE user_id = $1",
                user_id,
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


def _json_objects(response: httpx2.Response) -> list[dict[str, object]]:
    """Narrow an HTTP JSON array of objects at the response boundary."""
    return [
        from_plain(item, dict[str, object])
        for item in cast(list[object], response.json())
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
