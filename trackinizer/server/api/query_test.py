"""Tests for inquiry read / cost / lookup / change-log / stream routes.

Houses inquiry delete too: ``DELETE /api/inquiries/{id}`` is defined in
``api/query.py`` alongside the read-side handlers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import json
import logging
import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient as FastAPITestClient
from hypothesis import (
    given,
    settings,
    strategies as st,
)

import httpx2
import pytest

from trackinizer.conftest import (
    FakeEngine,
    make_store,
    new_uuid,
    queue_field_rows,
    set_field_row,
)
from trackinizer.lib.codec import from_plain, loads
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
from trackinizer.types.change_log import Change, Snapshot
from trackinizer.types.inquiries import KIND_TO_CLASS
from trackinizer.wire.bodies import (
    SubmitAgentSession,
    SubmitArtifact,
    SubmitBelief,
    SubmitCodeChange,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
    SubmitWebResult,
    SubmitWebSearch,
)
from trackinizer.wire.filters import Filter
from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.seq_ranges import SeqRange


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from fastapi.testclient import TestClient

    from trackinizer.server.store.core import Store
    from trackinizer.types.edges import Edge
    from trackinizer.types.inquiries import Inquiry


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
        body = from_plain(response.json(), dict[str, object])
        assert "release its owner" in from_plain(body["detail"], str)
        assert not any(
            isinstance(call.args[0], str) and "DELETE FROM inquiries" in call.args[0]
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
        body = from_plain(r.json(), dict[str, object])
        assert body["code"] == "not_found"

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
            "/api/inquiries",
            params={"kind": "Issue", "seq_range": "foo..5"},
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
        assert isinstance(forwarded, Sequence)
        ranges: list[SeqRange] = []
        for item in forwarded:
            assert isinstance(item, SeqRange)
            ranges.append(item)
        assert len(ranges) == len(forwarded)
        assert [(r.start, r.stop) for r in ranges] == [(222, 260), (279, None)]

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
        assert isinstance(forwarded, Sequence)
        filters: list[Filter] = []
        for item in forwarded:
            assert isinstance(item, Filter)
            filters.append(item)
        assert len(filters) == len(forwarded)
        triples = [(f.field, f.op, f.value) for f in filters]
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
        fields = from_plain(record.__dict__, dict[str, object])
        assert from_plain(fields.get("request_id"), str) == request_id
        assert from_plain(fields.get("kind"), str) == "Experiment"
        assert from_plain(fields.get("filter_count"), int, default=0) == 2
        assert from_plain(fields.get("returned_rows"), int, default=-1) == 0
        assert from_plain(fields.get("duration_sec"), float, default=-1) >= 0

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
            "/api/inquiries",
            params=[("kind", "Issue"), ("filter", "not-json")],
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
        assert isinstance(forwarded, Sequence)
        filters: list[Filter] = []
        for item in forwarded:
            assert isinstance(item, Filter)
            filters.append(item)
        assert len(filters) == len(forwarded)
        triples = [(f.field, f.op, f.value) for f in filters]
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

    def test_list_kind_route_rejects_a_field_no_row_carries(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A name no kind's rows carry is a typo; another kind's field is not.

        Dropping an unknown name without a word would hand the caller rows
        missing a key it believes it asked for.
        """
        client, store, _engine = route_client
        with patch.object(store, "list_kind", new_callable=AsyncMock) as mock:
            mock.return_value = []
            bad = client.get(
                "/api/inquiries",
                params=[("kind", "Issue"), ("fields", "id"), ("fields", "bogus")],
            )
            # ``narrows`` is an Issue field: absent from a Belief row, not unknown.
            ok = client.get(
                "/api/inquiries",
                params=[("kind", "Belief"), ("fields", "narrows")],
            )
        assert bad.status_code == 400, bad.text
        assert "'bogus'" in bad.text
        assert ok.status_code == 200, ok.text

    @pytest.mark.parametrize("param", ["filter", "seq_range", "fields"])
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

    @pytest.mark.parametrize(
        "zone",
        ["Pacific/Kiritimati", "America/Los_Angeles", "Europe/Berlin"],
    )
    def test_change_log_route_reads_a_naive_since_as_utc(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        zone: str,
    ) -> None:
        """Read a naive ``since`` as UTC whatever the server's local zone is."""
        client, store, _engine = route_client
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        try:
            with patch.object(store, "list_changes", new_callable=AsyncMock) as mock:
                mock.return_value = []
                client.get("/api/change_log", params={"since": "2024-12-10T00:00:00"})
        finally:
            monkeypatch.undo()
            time.tzset()
        since = cast("datetime", mock.call_args.kwargs["since"])
        assert since.tzinfo is not None
        assert since == datetime(2024, 12, 10, tzinfo=UTC)

    def test_change_log_route_sends_brief_rows_only_when_asked(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # A brief snapshot keeps its set keys alone, its text cut to 32
        # characters; the rest of the row is as a whole one sends it.
        client, store, _engine = route_client
        change = Change(
            kind="description",
            subject_id=new_uuid(),
            subject_kind="Issue",
            old=Snapshot(description="d" * 100),
            new=Snapshot(description="e" * 100),
        )
        with patch.object(store, "list_changes", new_callable=AsyncMock) as mock:
            mock.return_value = [change]
            (whole,) = from_plain(
                client.get("/api/change_log").json(),
                list[dict[str, object]],
            )
            (brief,) = from_plain(
                client.get("/api/change_log", params={"brief": "true"}).json(),
                list[dict[str, object]],
            )
        assert from_plain(whole["new"], dict[str, object])["description"] == "e" * 100
        assert from_plain(whole["new"], dict[str, object])["title"] is None
        assert brief["old"] == {"description": "d" * 32}
        assert brief["new"] == {"description": "e" * 32}
        assert {k: v for k, v in brief.items() if k not in {"old", "new"}} == {
            k: v for k, v in whole.items() if k not in {"old", "new"}
        }

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
            ],
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
        body = from_plain(loads(r.content), dict[str, object])
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
        self,
        target_id: uuid.UUID,
        *,
        kind: Inquiry.InquiryKind = "Issue",
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        row: dict[str, object] = {
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
        assert (
            from_plain(
                client.get(f"/api/inquiries/{target_id}").json(),
                dict[str, object],
            )["kind"]
            == "Issue"
        )
        assert from_plain(
            client.get("/api/inquiries/Issue/1").json(),
            dict[str, object],
        )["id"] == str(target_id)
        assert (
            from_plain(
                client.get("/api/inquiries", params={"kind": "Issue"}).json(),
                list[dict[str, object]],
            )[0]["kind"]
            == "Issue"
        )
        assert (
            from_plain(
                client.get("/api/inquiries/next_issue").json(),
                dict[str, object],
            )["kind"]
            == "Issue"
        )

    @pytest.mark.parametrize(
        ("fields", "reads"),
        [
            # No relation named: the row read alone.
            (("id", "title", "judgement"), 1),
            # A relation named: the row read, then the edges each way.
            (("id", "narrows"), 3),
        ],
    )
    def test_list_returns_only_the_named_fields(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        fields: tuple[str, ...],
        reads: int,
    ) -> None:
        """``fields`` names the keys each row carries; another kind's are absent.

        A 50-row page measured 315 KB on production rows, 94% of it keys a list
        never reads: descriptions, validation and every edge list. The edges
        cost two reads per page, so they are read only for a named relation.
        """
        client, _store, engine = route_client
        engine.conn.fetch.side_effect = _rows_then_no_edges(self._row(new_uuid()))
        r = client.get(
            "/api/inquiries",
            params=[("kind", "Issue"), *(("fields", name) for name in fields)],
        )
        assert r.status_code == 200, r.text
        (row,) = from_plain(r.json(), list[dict[str, object]])
        # ``judgement`` is a Belief field, so an Issue row has no such key.
        assert set(row) == set(fields) - {"judgement"}
        assert engine.conn.fetch.await_count == reads

    def test_misc_routes(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        target_id = new_uuid()
        # cost_for now does an existence check via fetchval first.
        engine.conn.fetchval.return_value = 1
        set_field_row(engine.conn, {"agent_usd": 1.0, "resource_usd": 2.0})
        assert (
            from_plain(
                client.get(f"/api/inquiries/{target_id}/cost").json(),
                dict[str, object],
            )["agent_usd"]
            == 1.0
        )
        engine.conn.fetch.side_effect = [
            # proves_belief: main select + bulk outbound + bulk inbound.
            [self._row(target_id, kind="Experiment")],
            [],
            [],
            # change_log: list_changes rows.
            [],
        ]
        assert (
            from_plain(
                client.get(f"/api/inquiries/{target_id}/proves_belief").json(),
                list[dict[str, object]],
            )[0]["kind"]
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

    def test_confidence_unknown_id_is_404(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        # ``confidence_for`` kind probe returns no row (missing id).
        engine.conn.fetchval.return_value = None
        r = client.get(f"/api/inquiries/{new_uuid()}/confidence")
        assert r.status_code == 404

    def test_confidence_no_evidence_is_neutral(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        engine.conn.fetchval.return_value = "Belief"  # Claimable kind probe.
        engine.conn.fetch.return_value = []  # No proving edges.
        r = client.get(f"/api/inquiries/{new_uuid()}/confidence")
        assert r.status_code == 200
        assert from_plain(r.json(), dict[str, object])["confidence"] == 0.5

    def test_authority_unknown_id_is_404(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, None)
        r = client.get(f"/api/inquiries/{new_uuid()}/authority")
        assert r.status_code == 404

    def test_authority_returns_non_null_scores(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(
            engine.conn,
            {
                "proves_authority": 0.42,
                "favors_authority": None,
                "cited_by_authority": None,
                "issue_authority": None,
            },
        )
        r = client.get(f"/api/inquiries/{new_uuid()}/authority")
        assert r.status_code == 200
        assert from_plain(r.json(), dict[str, object]) == {"proves_authority": 0.42}


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
    ],
)


@settings(max_examples=100, deadline=None)
@given(
    seq_ranges=st.lists(_QUERY_VALUES, max_size=3),
    filters=st.lists(_QUERY_VALUES, max_size=3),
    limit=st.one_of(st.integers(min_value=-10, max_value=2000), st.just("x")),
    offset=st.one_of(st.integers(min_value=-10, max_value=10), st.just("y")),
)
@pytest.mark.compute_large_fixture
def test_list_endpoint_never_500s_on_bad_params(
    seq_ranges: list[str],
    filters: list[str],
    limit: object,
    offset: object,
) -> None:
    store, engine = make_store()
    answer_account_active(engine)
    prev_engine = getattr(app.state, "engine", None)
    prev_store = getattr(app.state, "store", None)
    app.state.engine = engine
    app.state.store = store
    install_identity(make_test_identity())
    try:
        with patch.object(store, "list_kind", new=AsyncMock(return_value=[])):
            client = FastAPITestClient(app)
            params: list[tuple[str, str]] = [("kind", "Issue")]
            params += [("seq_range", v) for v in seq_ranges]
            params += [("filter", v) for v in filters]
            params.append(("limit", str(limit)))
            params.append(("offset", str(offset)))
            # httpx2.QueryParams is the typed carrier the TestClient stub accepts
            # and preserves the repeated keys (seq_range/filter) a bare list
            # encodes.
            r = client.get("/api/inquiries", params=httpx2.QueryParams(params))
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


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_fields_keep_the_named_values_on_a_real_engine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Each named key keeps the full row's value, and no other key comes back.

    Every key a row of any kind carries is a name ``fields`` takes, relations
    included, so naming all of a row's keys returns the whole row.
    """
    http, store = pglite_route_client
    account = "alice@example.com"
    parent, child, artifact, belief, *_ = await store.submit_batch(
        [
            SubmitIssue(account=account, title="parent", priority=10),
            SubmitIssue(account=account, title="child", description="long"),
            SubmitArtifact(account=account, title="evidence"),
            SubmitBelief(account=account, title="claim", judgement="unproven"),
            SubmitExperiment(account=account, title="run"),
            SubmitPaper(account=account, title="paper", authors=["a"]),
            SubmitCodeChange(account=account, title="commit"),
            SubmitWebResult(account=account, title="page"),
            SubmitWebSearch(account=account, title="query"),
            SubmitAgentSession(account=account, title="session"),
        ],
    )
    edges: tuple[tuple[uuid.UUID, Edge.Kind, uuid.UUID], ...] = (
        (child, "narrows", parent),
        (artifact, "proves", belief),
    )
    for from_id, edge_kind, to_id in edges:
        await store.add_edge(
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            actor="alice",
        )
    kinds = [("kind", kind) for kind in sorted(KIND_TO_CLASS)]
    full = from_plain(
        (await http.get("/api/inquiries", params=kinds)).json(),
        list[dict[str, object]],
    )
    assert len(full) == 10
    for row in full:
        seq = f"{row['seq']}..{row['seq']}"
        named = await http.get(
            "/api/inquiries",
            params=[
                ("kind", from_plain(row["kind"], str)),
                ("seq_range", seq),
                *(("fields", key) for key in row),
            ],
        )
        assert from_plain(named.json(), list[dict[str, object]]) == [row], row["kind"]
    subset = ("id", "title", "priority", "judgement", "proved_by", "narrows")
    named = await http.get(
        "/api/inquiries",
        params=[*kinds, *(("fields", name) for name in subset)],
    )
    assert named.status_code == 200, named.text
    rows = from_plain(named.json(), list[dict[str, object]])
    assert rows == [{k: v for k, v in row.items() if k in subset} for row in full]
    # The relations are real when named: the edges were read.
    assert any(row.get("narrows") for row in rows)
    assert any(row.get("proved_by") for row in rows)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_brief_changes_drop_unset_keys_and_cut_text_on_a_real_engine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """``brief`` drops each snapshot's unset keys and cuts its free text short.

    Activity's Edits tab only asks whether a description is set, yet a page sent
    both sides of every edit whole: 5.2 MB for 50 description edits on
    production. Ids, statuses and the other values stay whole, so an edge pair
    still matches on its peers.
    """
    http, store = pglite_route_client
    account = "alice@example.com"
    first, second = "First draft. " * 20, "Second draft. " * 20
    parent = await store.submit_issue(
        SubmitIssue(
            account=account,
            title="A parent whose title runs on past the cut",
            description=first,
        ),
    )
    child = await store.submit_issue(SubmitIssue(account=account, title="child"))
    await store.set_description(parent, second, actor="alice")
    await store.set_status(parent, "complete", actor="alice")
    await store.add_edge(
        from_id=child,
        to_id=parent,
        edge_kind="narrows",
        actor="alice",
    )
    full = from_plain(
        (await http.get("/api/change_log")).json(),
        list[dict[str, object]],
    )
    brief = await http.get("/api/change_log", params={"brief": "true"})
    assert brief.status_code == 200, brief.text
    rows = from_plain(brief.json(), list[dict[str, object]])
    assert rows == [_briefed(row) for row in full]
    (edit,) = (row for row in rows if row["kind"] == "description")
    assert from_plain(edit["old"], dict[str, object])["description"] == first[:32]
    assert from_plain(edit["new"], dict[str, object])["description"] == second[:32]
    peers = {
        from_plain(row["new"], dict[str, object]).get("peer_id")
        for row in rows
        if row["kind"] == "edge_added"
    }
    assert {str(child), str(parent)} <= peers


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_change_log_takes_several_kinds_before_its_limit_on_a_real_engine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A repeated ``kind`` keeps changes of any of them, filtered before ``limit``.

    An Activity tab shows several change kinds, and one request per kind would
    break its live budget of a few requests a second. Newer changes of other
    kinds must not crowd the page out.
    """
    http, store = pglite_route_client
    issue = await store.submit_issue(
        SubmitIssue(account="alice@example.com", title="t0"),
    )
    await store.set_status(issue, "complete", actor="alice")
    await store.set_title(issue, "t1", actor="alice")
    for step in range(3):
        await store.set_description(issue, f"d{step}", actor="alice")

    async def kinds(*query: tuple[str, str]) -> list[object]:
        r = await http.get("/api/change_log", params=query)
        assert r.status_code == 200, r.text
        return [row["kind"] for row in from_plain(r.json(), list[dict[str, object]])]

    many = await kinds(("kind", "status"), ("kind", "title"), ("limit", "2"))
    assert many == ["title", "status"]
    assert await kinds(("kind", "status")) == ["status"]
    r = await http.get("/api/change_log", params=[("kind", "status"), ("kind", "x")])
    assert r.status_code == 422, r.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_narrows_filters_roots_and_children_on_a_real_engine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """``narrows`` filters on an Issue's parents, which live in ``edges``.

    A list of root goals is ``narrows isnull``; one parent's children is
    ``narrows is <id>``. Both answered "unknown filter field" before.
    """
    http, store = pglite_route_client
    account = "alice@example.com"
    root, mid, leaf, _ = await store.submit_batch(
        [SubmitIssue(account=account, title=t) for t in ("root", "mid", "leaf", "x")],
    )
    for child, parent in ((mid, root), (leaf, mid)):
        await store.add_edge(
            from_id=child,
            to_id=parent,
            edge_kind="narrows",
            actor="alice",
        )
    assert await _titles_where(http, "isnull") == ["root", "x"]
    assert await _titles_where(http, "notnull") == ["leaf", "mid"]
    assert await _titles_where(http, "is", str(root)) == ["mid"]
    assert await _titles_where(http, "ne", str(root)) == ["leaf", "root", "x"]
    # Text no id equals matches nothing, rather than failing a uuid cast.
    assert await _titles_where(http, "is", "not-an-id") == []
    for kind, clause, status in (
        # No SQL orders or matches a set of parents; the store refuses the op,
        # as it does on any column without one.
        ("Issue", {"field": "narrows", "op": "re", "value": "x"}, 422),
        ("Issue", {"field": "narrows", "op": "gt", "value": "x"}, 422),
        # Only an Issue narrows.
        ("Belief", {"field": "narrows", "op": "isnull"}, 400),
    ):
        r = await http.get(
            "/api/inquiries",
            params=[("kind", kind), ("filter", json.dumps(clause))],
        )
        assert r.status_code == status, (kind, clause, r.text)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_ancestors_walk_up_to_the_roots_on_a_real_engine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """``ancestors=narrows`` adds each row's ancestry, capped, cycles included."""
    http, store = pglite_route_client
    await _check_ancestry(http, store)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_ancestors_walk_up_to_the_roots_on_postgres(integ_store: Store) -> None:
    """The same walk on Postgres, whose recursive CTE PGlite only mirrors."""
    served = FastAPI()
    served.include_router(query.router)
    served.state.store = integ_store
    served.dependency_overrides[current_user] = _viewer
    transport = httpx2.ASGITransport(app=served, raise_app_exceptions=False)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as http:
        await _check_ancestry(http, integ_store)


async def _check_ancestry(http: httpx2.AsyncClient, store: Store) -> None:
    """Assert the ancestry of rows shaped to hit each rule of the walk."""
    account = "alice@example.com"
    names = (
        *("g", "p1", "p2", "r"),
        *(f"c{i}" for i in range(11)),
        *("a", "b", "r2", "x", "y"),
        *("w", "gp"),
        *(f"q{i}" for i in range(201)),
    )
    made = await store.submit_batch(
        [SubmitIssue(account=account, title=name) for name in names],
    )
    n = dict(zip(names, made, strict=True))
    # Through the store, so the inferred ``produced_by`` edges exist too and the
    # walk is seen to follow ``narrows`` alone.
    for child, parent in (("p1", "g"), ("p2", "g"), ("r", "p1"), ("r", "p2")):
        await store.add_edge(
            from_id=n[child],
            to_id=n[parent],
            edge_kind="narrows",
            actor="alice",
        )
    # Written directly, as legacy rows hold them: the store refuses a cycle, and
    # 202 edges through it would run its dependency cascade 202 times.
    await _insert_narrows(
        store,
        [
            *((f"c{i}", f"c{i + 1}") for i in range(10)),
            ("a", "b"),
            ("b", "a"),
            ("r2", "x"),
            ("x", "y"),
            ("y", "x"),
            *(("w", f"q{i}") for i in range(201)),
            ("q0", "gp"),
        ],
        n,
    )
    # Nearest first; a level in id order; every parent of a multi-parent row.
    p1, p2 = sorted(("p1", "p2"), key=lambda name: str(n[name]))
    assert await _ancestry(http, n, "r") == [
        (p1, ["r"]),
        (p2, ["r"]),
        ("g", [p1, p2]),
    ]
    assert await _ancestry(http, n, "g") == []
    # At most 8 levels up.
    assert await _ancestry(http, n, "c0") == [
        (f"c{i}", [f"c{i - 1}"]) for i in range(1, 9)
    ]
    # A cycle ends: a row is never its own ancestor, and each ancestor shows once.
    assert await _ancestry(http, n, "a") == [("b", ["a"])]
    assert await _ancestry(http, n, "r2") == [("x", ["r2", "y"]), ("y", ["x"])]
    # At most 200 ancestors, the nearest kept.
    capped = await _ancestry(http, n, "w")
    assert len(capped) == 200
    assert "gp" not in {title for title, _ in capped}
    # Without the param, a row is as it was.
    plain = await http.get("/api/inquiries", params={"kind": "Issue", "limit": 5})
    assert all(
        "ancestors" not in row
        for row in from_plain(plain.json(), list[dict[str, object]])
    )


def _briefed(row: Mapping[str, object]) -> dict[str, object]:
    """Return ``row`` as ``brief`` sends it: snapshots without nulls, text cut."""
    out = dict(row)
    for side in ("old", "new"):
        out[side] = {
            key: value[:32]
            if key in {"title", "description"} and isinstance(value, str)
            else value
            for key, value in from_plain(row[side], dict[str, object]).items()
            if value is not None
        }
    return out


def _rows_then_no_edges(row: dict[str, object]) -> Callable[..., list[object]]:
    """Return a ``fetch`` answering the row read with ``row``, an edge read with none."""

    def fetch(sql: str, *args: object) -> list[object]:
        del args
        return [row] if "FROM inquiries" in sql else []

    return fetch


async def _titles_where(
    http: httpx2.AsyncClient,
    op: str,
    value: str = "",
) -> list[str]:
    """Return the sorted titles of the Issues the ``narrows <op> <value>`` filter keeps."""
    clause = {"field": "narrows", "op": op} | ({"value": value} if value else {})
    r = await http.get(
        "/api/inquiries",
        params=[("kind", "Issue"), ("filter", json.dumps(clause))],
    )
    assert r.status_code == 200, r.text
    return sorted(
        from_plain(row["title"], str)
        for row in from_plain(r.json(), list[dict[str, object]])
    )


async def _ancestry(
    http: httpx2.AsyncClient,
    ids: Mapping[str, uuid.UUID],
    name: str,
) -> list[tuple[str, list[str]]]:
    """Return row ``name``'s ancestors as ``(title, child titles)``, as sent."""
    by_id = json.dumps({"field": "id", "op": "is", "value": str(ids[name])})
    r = await http.get(
        "/api/inquiries",
        params=[
            ("kind", "Issue"),
            ("filter", by_id),
            ("fields", "id"),
            ("ancestors", "narrows"),
        ],
    )
    assert r.status_code == 200, r.text
    (row,) = from_plain(r.json(), list[dict[str, object]])
    names = {made: title for title, made in ids.items()}
    out: list[tuple[str, list[str]]] = []
    for entry in from_plain(row.get("ancestors"), list[dict[str, object]], default=[]):
        assert set(entry) == {"id", "kind", "seq", "title", "status", "child_ids"}
        assert entry["kind"] == "Issue"
        title = names[uuid.UUID(from_plain(entry["id"], str))]
        assert entry["title"] == title
        children = from_plain(entry["child_ids"], list[str])
        out.append((title, [names[uuid.UUID(child)] for child in children]))
    return out


async def _insert_narrows(
    store: Store,
    pairs: Sequence[tuple[str, str]],
    ids: Mapping[str, uuid.UUID],
) -> None:
    """Insert ``narrows`` edges child -> parent straight into ``edges``."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind) "
            "SELECT c, 'Issue', p, 'Issue', 'narrows' "
            "FROM unnest($1::uuid[], $2::uuid[]) AS t(c, p)",
            [ids[child] for child, _ in pairs],
            [ids[parent] for _, parent in pairs],
        )


async def _viewer() -> AuthIdentity:
    return make_test_identity(role="viewer")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
