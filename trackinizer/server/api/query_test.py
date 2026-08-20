"""Tests for inquiry read / cost / lookup / change-log / stream routes.

Houses inquiry delete too: ``DELETE /api/inquiries/{id}`` is defined in
``api/query.py`` alongside the read-side handlers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import json
import logging
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient as FastAPITestClient
from hypothesis import (
    given,
    settings,
    strategies as st,
)

import httpx
import pytest

from trackinizer.conftest import (
    FakeEngine,
    make_store,
    new_uuid,
    queue_field_rows,
    set_field_row,
)
from trackinizer.lib.custom_json import dict_val, float_val, int_val, str_val
from trackinizer.server import web
from trackinizer.server.api import query
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    answer_account_active,
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.types.inquiries import Inquiry


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.server.store.core import Store
from trackinizer.wire.routes import MAX_LIST_LIMIT


class TestRoutes:
    def test_purge_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"kind": "Issue"})
        engine.conn.fetch.return_value = []
        r = client.request(
            "DELETE",
            f"/api/inquiries/{new_uuid()}",
            json={"actor": "user", "reason": ""},
        )
        assert r.status_code == 200

    def test_purge_rejects_an_owned_inquiry(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"kind": "Issue", "owner": "worker-1"})

        response = client.request(
            "DELETE",
            f"/api/inquiries/{new_uuid()}",
            json={"actor": "other-worker", "reason": ""},
        )

        assert response.status_code == 409
        assert "release its owner" in response.json()["detail"]
        assert not any(
            "DELETE FROM inquiries" in call.args[0]
            for call in engine.conn.execute.call_args_list
        )

    def test_purge_unknown_id_is_404(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # The row lookup returns no row, so the purge hits the not-found
        # path: a DELETE on an id that never existed is 404, not 409.
        client, _store, engine = route_client
        set_field_row(engine.conn, None)
        r = client.request(
            "DELETE",
            f"/api/inquiries/{new_uuid()}",
            json={"actor": "user", "reason": ""},
        )
        assert r.status_code == 404
        assert r.json()["code"] == "not_found"

    def test_list_kind_route_rejects_bad_bounds(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        r = client.get("/api/inquiries", params={"kind": "Issue", "limit": 0})
        assert r.status_code == 400
        r = client.get("/api/inquiries", params={"kind": "Issue", "offset": -1})
        assert r.status_code == 400
        r = client.get("/api/inquiries", params={"kind": "Issue", "limit": 5000})
        assert r.status_code == 400
        r = client.get("/api/inquiries", params={"kind": "Issue", "seq_range": "0.."})
        assert r.status_code == 400
        r = client.get(
            "/api/inquiries", params={"kind": "Issue", "seq_range": "foo..5"}
        )
        assert r.status_code == 400
        r = client.get("/api/inquiries", params={"kind": "Issue", "seq_range": ".."})
        assert r.status_code == 400

    def test_list_kind_route_forwards_seq_ranges_to_store(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Repeated ``seq_range=A..B`` params reach ``store.list_kind`` as a union.

        Each occurrence is one inclusive interval; the route forwards the
        ordered union so the store can OR them into a single indexed query.
        """
        client, store, _engine = route_client
        with patch.object(store, "list_kind", new_callable=AsyncMock) as mock:
            mock.return_value = []
            r = client.get(
                "/api/inquiries",
                params=[
                    ("kind", "Issue"),
                    ("seq_range", "222..260"),
                    ("seq_range", "279.."),
                ],
            )
        assert r.status_code == 200, r.text
        assert mock.await_args is not None
        forwarded = mock.await_args.kwargs.get("seq_ranges")
        assert forwarded is not None
        assert [(r.start, r.stop) for r in forwarded] == [(222, 260), (279, None)]

    def test_list_kind_route_forwards_filters_to_store(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Repeated ``filter=<json>`` query params reach ``store.list_kind``.

        The route must parse each occurrence as a ``{field, op, value}``
        triple and hand them through unchanged; the store is the
        authoritative filter evaluator. Asserting the call kwargs
        catches any regression where the route drops filters or
        misorders them.
        """
        client, store, _engine = route_client
        with patch.object(store, "list_kind", new_callable=AsyncMock) as mock:
            mock.return_value = []
            r = client.get(
                "/api/inquiries",
                params=[
                    ("kind", "Issue"),
                    ("filter", '{"field":"title","op":"re","value":"foo"}'),
                    ("filter", '{"field":"owner","op":"nre","value":"Dan"}'),
                    ("filter", '{"field":"priority","op":"gt","value":"5"}'),
                ],
            )
        assert r.status_code == 200, r.text
        assert mock.await_count == 1
        assert mock.await_args is not None
        forwarded = mock.await_args.kwargs.get("filters")
        assert forwarded is not None
        triples = [(f.field, f.op, f.value) for f in forwarded]
        # The route canonicalizes filter fields to their flat storage
        # column before forwarding: priority -> issue_priority, so the
        # store filters the real (prefixed) inquiries rows. ``nre`` rides
        # through like any other op.
        assert triples == [
            ("title", "re", "foo"),
            ("owner", "nre", "Dan"),
            ("issue_priority", "gt", "5"),
        ]

    def test_list_kind_route_logs_correlated_query_stage(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, store, _engine = route_client
        with (
            patch.object(store, "list_kind", new_callable=AsyncMock) as mock,
            caplog.at_level(logging.INFO),
        ):
            mock.return_value = []
            response = client.get(
                "/api/inquiries",
                params=[
                    ("kind", "Experiment"),
                    ("filter", '{"field":"labels","op":"is","value":"ready"}'),
                    ("filter", '{"field":"owner","op":"isnull"}'),
                ],
            )

        assert response.status_code == 200
        request_id = response.headers["X-Request-ID"]
        record = next(
            record
            for record in caplog.records
            if getattr(record, "event", "") == "trackinizer_query_completed"
        )
        fields = dict_val(record.__dict__)
        assert str_val(fields.get("request_id")) == request_id
        assert str_val(fields.get("kind")) == "Experiment"
        assert int_val(fields.get("filter_count"), 0) == 2
        assert int_val(fields.get("returned_rows"), -1) == 0
        assert float_val(fields.get("duration_sec"), -1) >= 0

    def test_list_kind_route_rejects_isnull_on_not_null_column(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """The route 400s ``isnull`` / ``notnull`` on a NOT-NULL column.

        A presence test on a never-NULL column (id, status, cost axes,
        identity columns) is always-empty / always-all -- a silent wrong
        answer. The route rejects it instead of validating it.
        """
        client, _store, _engine = route_client
        for field, op in (
            ("id", "isnull"),
            ("status", "isnull"),
            ("title", "notnull"),
            ("marginal_cost_agent_usd", "isnull"),
        ):
            r = client.get(
                "/api/inquiries",
                params=[
                    ("kind", "Issue"),
                    ("filter", f'{{"field":"{field}","op":"{op}"}}'),
                ],
            )
            assert r.status_code == 400, f"{field} {op}: {r.text}"

    def test_list_kind_route_filters_agentsession(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """AgentSession filters validate against its columns, not 500.

        AgentSession was absent from the server filter-kind map, so a
        filtered request raised ``KeyError`` deep in ``_filter_columns_for``
        (a 500). It is now a first-class filterable kind: a base-column
        filter is accepted (200) and a kind-invalid field is a clean 400.
        """
        client, store, _engine = route_client
        with patch.object(store, "list_kind", new_callable=AsyncMock) as mock:
            mock.return_value = []
            ok = client.get(
                "/api/inquiries",
                params=[
                    ("kind", "AgentSession"),
                    ("filter", '{"field":"owner","op":"isnull"}'),
                ],
            )
        assert ok.status_code == 200, ok.text
        bad = client.get(
            "/api/inquiries",
            params=[
                ("kind", "AgentSession"),
                ("filter", '{"field":"judgement","op":"is","value":"proven"}'),
            ],
        )
        assert bad.status_code == 400, bad.text

    def test_list_kind_route_rejects_malformed_filter(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """The route must reject every well-known wire-shape mistake.

        Silent-accept of an unknown field would let ``ne`` against a
        missing column match every row -- the ``str(None) != "x"``
        trap. Silent-accept of bad regex would surface as a 500 from
        deep inside the store. Each case here pins one validation.
        """
        client, _store, _engine = route_client
        # Non-JSON body.
        r = client.get(
            "/api/inquiries", params=[("kind", "Issue"), ("filter", "not-json")]
        )
        assert r.status_code == 400, r.text
        # Unknown op.
        r = client.get(
            "/api/inquiries",
            params=[
                ("kind", "Issue"),
                ("filter", '{"field":"title","op":"bogus","value":"v"}'),
            ],
        )
        assert r.status_code == 400, r.text
        # Unknown field for this kind.
        r = client.get(
            "/api/inquiries",
            params=[
                ("kind", "Issue"),
                ("filter", '{"field":"bogus","op":"is","value":"x"}'),
            ],
        )
        assert r.status_code == 400, r.text
        # Field valid for some kind but not this one (``judgement``
        # is a Belief field, not an Issue field).
        r = client.get(
            "/api/inquiries",
            params=[
                ("kind", "Issue"),
                ("filter", '{"field":"judgement","op":"is","value":"proven"}'),
            ],
        )
        assert r.status_code == 400, r.text
        # Invalid regex syntax must surface at the route, not as a
        # 500 from deep inside ``store.list_kind`` -- for both the regex
        # op and its negation.
        for regex_op in ("re", "nre"):
            r = client.get(
                "/api/inquiries",
                params=[
                    ("kind", "Issue"),
                    ("filter", f'{{"field":"title","op":"{regex_op}","value":"["}}'),
                ],
            )
            assert r.status_code == 400, r.text

    def test_list_kind_route_rejects_overlong_filter_value(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """An over-long filter ``value`` is a 400, not a per-request ReDoS risk.

        ``re.compile(value)`` runs per request on a ``re`` / ``nre`` filter; a
        long pathological pattern enables catastrophic backtracking on the
        validation compile. The wire caps ``value`` length, so the route
        rejects the oversized payload before compiling it.
        """
        client, _store, _engine = route_client
        huge = "a" * 10_000
        r = client.get(
            "/api/inquiries",
            params=[
                ("kind", "Issue"),
                ("filter", json.dumps({"field": "title", "op": "re", "value": huge})),
            ],
        )
        assert r.status_code == 400, r.text

    def test_list_kind_route_rejects_unhashable_op_as_400(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A non-string ``op`` (JSON list) is a 400, never a 500.

        The valueless-op check runs before the type guard, so it must not
        attempt set membership on an unhashable value.
        """
        client, _store, _engine = route_client
        r = client.get(
            "/api/inquiries",
            params=[
                ("kind", "Issue"),
                ("filter", '{"field":"owner","op":["isnull"],"value":"x"}'),
            ],
        )
        assert r.status_code == 400, r.text

    def test_list_kind_route_accepts_null_ops_without_value(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """``isnull`` / ``notnull`` need no value; the route forwards ``value=''``.

        Unlike every other op, the null-presence ops carry no operand. The
        route accepts a filter object with the ``value`` key absent (or
        empty) and hands a ``Filter`` with ``value=''`` to the store.
        """
        client, store, _engine = route_client
        with patch.object(store, "list_kind", new_callable=AsyncMock) as mock:
            mock.return_value = []
            r = client.get(
                "/api/inquiries",
                params=[
                    ("kind", "Issue"),
                    ("filter", '{"field":"issue_kind","op":"isnull"}'),
                    ("filter", '{"field":"owner","op":"notnull"}'),
                ],
            )
        assert r.status_code == 200, r.text
        assert mock.await_args is not None
        forwarded = mock.await_args.kwargs.get("filters")
        assert forwarded is not None
        triples = [(f.field, f.op, f.value) for f in forwarded]
        assert triples == [
            ("issue_kind", "isnull", ""),
            ("owner", "notnull", ""),
        ]

    def test_list_kind_route_rejects_a_value_on_a_presence_op(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A presence op with an operand is a question the route cannot answer.

        ``{"op": "isnull", "value": "Dan"}`` reads as "owner is Dan" and was
        silently answered as "owner is null" -- the operand dropped without a
        word to the caller.
        """
        client, _store, _engine = route_client
        r = client.get(
            "/api/inquiries",
            params=[
                ("kind", "Issue"),
                ("filter", '{"field":"owner","op":"isnull","value":"Dan"}'),
            ],
        )
        assert r.status_code == 400, r.text
        assert "takes no value" in r.text

    @pytest.mark.parametrize("param", ["filter", "seq_range"])
    def test_repeated_query_params_are_length_capped(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        param: str,
    ) -> None:
        # ``kind`` was capped and these two were not, so one request could
        # still force an unbounded number of JSON parses, regex compiles, and
        # ``OR`` disjuncts before any store-level limit applied. The operand
        # is deliberately unparseable: the cap must reject on COUNT before
        # anything decodes it, so a malformed value still yields 422 rather
        # than a per-filter 400.
        client, _store, _engine = route_client
        params: tuple[tuple[str, str], ...] = (
            ("kind", "Issue"),
            *((param, "x") for _ in range(MAX_LIST_LIMIT + 1)),
        )
        r = client.get("/api/inquiries", params=params)
        assert r.status_code == 422, r.text

    def test_change_log_route_rejects_bad_limit(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        r = client.get(
            "/api/change_log",
            params={
                "since": "2026-01-01T00:00:00Z",
                "limit": 5000,
            },
        )
        assert r.status_code == 400

    def test_repeated_kind_runs_one_query_per_distinct_kind(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # Only nine kinds exist, so a repeated ``kind`` param can only re-run
        # a query whose answer is already in hand. Measured before the dedup:
        # 200 copies of ``kind=Issue`` returned 9,348,201 bytes in 1.876s
        # against 46,742 bytes in 0.050s for one copy -- a 200x amplification
        # available to any viewer.
        client, _store, engine = route_client
        engine.conn.fetch.return_value = []
        params = "&".join(["kind=Issue"] * 50)
        r = client.get(f"/api/inquiries?{params}")
        assert r.status_code == 200, r.text
        # One row fetch for the single distinct kind. The edge bulk-fetch
        # short-circuits on an empty row set, so 50 repeats that dedup to one
        # kind issue exactly one query; without the dedup this was 50.
        assert engine.conn.fetch.call_count == 1

    def test_kind_list_is_length_capped(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # Dedup bounds the work but not the parse: the cap is what keeps an
        # arbitrarily long query string from being decoded at all.
        client, _store, _engine = route_client
        params = "&".join(["kind=Issue"] * (MAX_LIST_LIMIT + 1))
        r = client.get(f"/api/inquiries?{params}")
        assert r.status_code == 422

    def test_lookup_route_rejects_oversize_list(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # The cap is a typed ``Field(max_length=...)`` on the body, so an
        # oversize list is rejected at validation (422) before the ROUTE BODY
        # runs. It bounds the lookup, not the read -- the byte bound is
        # ``BodyLimitMiddleware`` (see ``body_limit_test``), because FastAPI
        # buffers and decodes the whole request before this cap is consulted.
        client, _store, _engine = route_client
        big = [str(new_uuid()) for _ in range(1001)]
        r = client.post("/api/inquiries/lookup", json=big)
        assert r.status_code == 422

    def test_lookup_route_batches_kind_resolution(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        a, b = new_uuid(), new_uuid()
        engine.conn.fetch = AsyncMock(
            return_value=[
                {"id": a, "kind": "WebResult"},
                {"id": b, "kind": "Paper"},
            ]
        )
        r = client.post("/api/inquiries/lookup", json=[str(a), str(b)])
        assert r.status_code == 200
        # Response names found ids by kind and lists the missing ones
        # (REV-OPUS-12), so a caller learns which ids were unknown.
        assert r.json() == {
            "found": {str(a): "WebResult", str(b): "Paper"},
            "missing": [],
        }
        assert engine.conn.fetch.await_count == 1

    def test_lookup_route_reports_missing_ids(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # A mix of known + unknown ids: the unknown id is named in
        # ``missing`` rather than silently dropped (REV-OPUS-12).
        client, _store, engine = route_client
        good, bad = new_uuid(), new_uuid()
        engine.conn.fetch = AsyncMock(
            return_value=[{"id": good, "kind": "Issue"}],
        )
        r = client.post("/api/inquiries/lookup", json=[str(good), str(bad)])
        assert r.status_code == 200
        body = r.json()
        assert body["found"] == {str(good): "Issue"}
        assert body["missing"] == [str(bad)]

    def test_subscribe_streams_every_change_id(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """SSE relays every NOTIFY payload as ``{"id": "<uuid>"}``.

        Matches the Auth section of ``docs/design.md``: viewer-gated reads, no
        per-subscriber filter; localhost-only deployment is the
        access boundary.
        """
        client, _store, engine = route_client
        first = new_uuid()
        second = new_uuid()
        engine.listen_messages = [
            json.dumps({"id": str(first)}),
            json.dumps({"id": str(second)}),
        ]
        with client.stream("GET", "/api/change_log/stream") as r:
            body = b"".join(r.iter_bytes())
        assert f'data: {{"id": "{first}"}}'.encode() in body
        assert f'data: {{"id": "{second}"}}'.encode() in body

    def test_subscribe_routes_share_wire_shape(self) -> None:
        """``/api/change_log/stream`` and ``/api/web/subscribe`` emit identical bytes.

        Both relay the same ``NOTIFY_CHANNEL`` payload through the
        shared :func:`notify.iter_sse_events` generator; divergence
        would force consumers to maintain two parsers.
        """
        identity = AuthIdentity(
            user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
            api_key_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
            email="test@example.com",
            role="viewer",
        )

        async def _identity_override() -> AuthIdentity:
            return identity

        def _build(path: str) -> bytes:
            engine = FakeEngine()
            engine.listen_messages = [json.dumps({"id": str(subject_id)})]
            app = FastAPI()
            app.state.engine = engine
            app.state.store = AsyncMock()
            if path.startswith("/api/web/"):
                web.attach(app)
            else:
                app.include_router(query.router)
            app.dependency_overrides[current_user] = _identity_override
            with FastAPITestClient(app).stream("GET", path) as r:
                return b"".join(r.iter_bytes())

        subject_id = new_uuid()
        assert _build("/api/change_log/stream") == _build("/api/web/subscribe")


class TestCoverageRoutesAndCli:
    def _row(
        self, target_id: uuid.UUID, *, kind: Inquiry.InquiryKind = "Issue"
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        row: dict[str, Any] = {
            "id": target_id,
            "kind": kind,
            "seq": 1,
            "owner": "alice",
            "account": "alice",
            "status": "active",
            "title": "title",
            "description": "",
            "labels": [],
            "subscribers": [],
            "created": now,
            "modified": now,
            "marginal_cost_agent_usd": 0.0,
            "marginal_cost_resource_usd": 0.0,
            "priority": "medium" if kind == "Issue" else None,
        }
        return row

    def test_read_routes(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        target_id = new_uuid()
        queue_field_rows(
            engine.conn,
            # GET /api/inquiries/{id}: get_inquiry fetchrow.
            self._row(target_id),
            # GET /api/inquiries/{kind}/{seq}: seq-lookup then get_inquiry.
            {"id": target_id},
            self._row(target_id),
            # GET /api/inquiries/next_issue: next_issue fetchrow.
            self._row(target_id),
        )
        engine.conn.fetch.side_effect = [
            # /api/inquiries/{id}: fetch_edges (outbound + inbound).
            [],
            [],
            # /api/inquiries/{kind}/{seq}: fetch_edges after inner get_inquiry.
            [],
            [],
            # /api/inquiries?kind=: list-select then fetch_edges_bulk.
            [self._row(target_id)],
            [],
            [],
            # /api/inquiries/next_issue: fetch_edges outbound + inbound.
            [],
            [],
        ]
        assert client.get(f"/api/inquiries/{target_id}").json()["kind"] == "Issue"
        assert client.get("/api/inquiries/Issue/1").json()["id"] == str(target_id)
        assert (
            client.get("/api/inquiries", params={"kind": "Issue"}).json()[0]["kind"]
            == "Issue"
        )
        assert client.get("/api/inquiries/next_issue").json()["kind"] == "Issue"

    def test_misc_routes(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        target_id = new_uuid()
        # cost_for now does an existence check via fetchval first.
        engine.conn.fetchval.return_value = 1
        set_field_row(engine.conn, {"agent_usd": 1.0, "resource_usd": 2.0})
        assert client.get(f"/api/inquiries/{target_id}/cost").json()["agent_usd"] == 1.0
        engine.conn.fetch.side_effect = [
            # proves_belief: main select + bulk outbound + bulk inbound.
            [self._row(target_id, kind="Experiment")],
            [],
            [],
            # change_log: list_changes rows.
            [],
        ]
        assert (
            client.get(f"/api/inquiries/{target_id}/proves_belief").json()[0]["kind"]
            == "Experiment"
        )
        assert (
            client.get(
                "/api/change_log",
                params={"since": datetime.now(UTC).isoformat()},
            ).json()
            == []
        )


class TestMissingResourceIs404:
    """A read addressing a specific id that does not exist returns 404.

    Mirrors ``get_change`` / ``get_edge`` so the API is consistent: a
    missing by-id resource is 404, not a 200 with a null body (API-08/24).
    ``next_issue`` is exempt -- a null there means "no actionable issue", a
    valid empty-queue answer, not a missing resource.
    """

    def test_get_inquiry_unknown_id_is_404(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, None)
        r = client.get(f"/api/inquiries/{new_uuid()}")
        assert r.status_code == 404

    def test_by_seq_unknown_is_404(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, None)
        r = client.get("/api/inquiries/Issue/999")
        assert r.status_code == 404

    def test_cost_unknown_id_is_404(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        # ``cost_for`` existence probe returns no row.
        engine.conn.fetchval.return_value = None
        r = client.get(f"/api/inquiries/{new_uuid()}/cost")
        assert r.status_code == 404


# -- Property: the list endpoint never 500s on malformed query params ----------
# The route parses ``seq_range`` / ``filter`` / ``limit`` / ``offset`` BEFORE the
# engine runs; a malformed value must surface as a clean 4xx, never a 500 leaking
# a traceback (the HTTP analog of the parser no-leak property). ``list_kind`` is
# stubbed to ``[]`` so the engine is not the variable -- only param parsing is.

_QUERY_VALUES = st.sampled_from(
    [
        "1..5",
        "a..b",
        "..",
        "1..",
        "..9",
        "1.2.3",
        "-1..2",
        "0",
        "abc",
        "{}",
        "notjson",
        '{"field":"title","op":"is","value":"x"}',
        '{"bad":1}',
        "-5",
        "999999999999",
        "Issue",
        "Belief",
        "NotAKind",
        "",
    ]
)


@settings(max_examples=100, deadline=None)
@given(
    seq_ranges=st.lists(_QUERY_VALUES, max_size=3),
    filters=st.lists(_QUERY_VALUES, max_size=3),
    limit=st.one_of(st.integers(min_value=-10, max_value=2000), st.just("x")),
    offset=st.one_of(st.integers(min_value=-10, max_value=10), st.just("y")),
)
def test_list_endpoint_never_500s_on_bad_params(
    seq_ranges: list[str],
    filters: list[str],
    limit: object,
    offset: object,
) -> None:
    store, engine = make_store()
    answer_account_active(engine)
    monkeypatch_list = AsyncMock(return_value=[])
    prev_engine = getattr(app.state, "engine", None)
    prev_store = getattr(app.state, "store", None)
    app.state.engine = engine
    app.state.store = store
    install_identity(make_test_identity())
    store.list_kind = monkeypatch_list
    try:
        client = FastAPITestClient(app)
        params: list[tuple[str, str]] = [("kind", "Issue")]
        params += [("seq_range", v) for v in seq_ranges]
        params += [("filter", v) for v in filters]
        params.append(("limit", str(limit)))
        params.append(("offset", str(offset)))
        # httpx.QueryParams is the typed carrier the TestClient stub accepts and
        # preserves the repeated keys (seq_range/filter) a bare list encodes.
        r = client.get("/api/inquiries", params=httpx.QueryParams(params))
        assert r.status_code != 500, f"500 on params {params!r}: {r.text[:200]}"
    finally:
        clear_identity_override()
        if prev_engine is None:
            app.state.__dict__.pop("engine", None)
        else:
            app.state.engine = prev_engine
        if prev_store is None:
            app.state.__dict__.pop("store", None)
        else:
            app.state.store = prev_store


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
