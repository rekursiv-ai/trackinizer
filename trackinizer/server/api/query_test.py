"""Tests for inquiry read / cost / lookup / change-log / stream routes.

Houses inquiry delete too: ``DELETE /api/inquiries/{id}`` is defined in
``api/query.py`` alongside the read-side handlers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import asyncio
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
from treekle import from_plain, json_loads, to_plain

import httpx2
import pytest

from trackinizer.conftest import (
    FakeEngine,
    make_store,
    new_uuid,
    queue_field_rows,
    set_field_row,
)
from trackinizer.server import web
from trackinizer.server.api import conftest, edit, query
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    answer_account_active,
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.audience import SessionReader
from trackinizer.server.auth import AuthIdentity, Role, current_user
from trackinizer.server.values import vetted_sql
from trackinizer.types.change_log import Change, Snapshot
from trackinizer.types.inquiries import KIND_TO_CLASS, Inquiry
from trackinizer.wire.filters import Filter
from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.seq_ranges import SeqRange


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from fastapi.testclient import TestClient

    from trackinizer.server.store.core import Store


@pytest.fixture(scope="module", autouse=True)
def _query_app() -> Iterator[None]:
    scoped_app = FastAPI()
    scoped_app.user_middleware = app.user_middleware.copy()
    scoped_app.exception_handlers = app.exception_handlers.copy()
    scoped_app.include_router(query.router)
    scoped_app.include_router(edit.router)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(conftest, "app", scoped_app)
        monkeypatch.setattr(f"{__name__}.app", scoped_app)
        yield


class TestRoutes:
    def test_purge_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"kind": "Issue", "account": TEST_USER_EMAIL})
        engine.conn.fetch.return_value = list[dict[str, object]]()
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
        set_field_row(
            engine.conn,
            {"kind": "Issue", "owner": "worker-1", "account": TEST_USER_EMAIL},
        )

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
            mock.return_value = list[Inquiry]()
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
            mock.return_value = list[Inquiry]()
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

    def test_list_kind_route_lists_for_the_reader_its_caller_is(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, store, _engine = route_client
        asked: list[tuple[frozenset[str], str]] = []
        with patch.object(store, "list_kind", new_callable=AsyncMock) as mock:
            mock.return_value = list[Inquiry]()
            for audiences in (frozenset({"open"}), frozenset({"open", "staff"})):
                install_identity(
                    replace(make_test_identity(), read_audiences=audiences),
                )
                assert client.get("/api/inquiries", params={"kind": "Issue"}).is_success
                assert mock.await_args is not None
                reader = cast(object, mock.await_args.kwargs["reader"])
                assert isinstance(reader, SessionReader)
                asked.append((reader.audiences, reader.email))

        assert asked == [
            (frozenset({"open"}), TEST_USER_EMAIL),
            (frozenset({"open", "staff"}), TEST_USER_EMAIL),
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
            mock.return_value = list[Inquiry]()
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
        filtered request raised ``KeyError`` deep in ``_filter_columns``
        (a 500). It is now a first-class filterable kind: a base-column
        filter is accepted (200) and a kind-invalid field is a clean 400.
        """
        client, store, _engine = route_client
        with patch.object(store, "list_kind", new_callable=AsyncMock) as mock:
            mock.return_value = list[Inquiry]()
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
        assert json_loads(r.content, dict[str, str])["detail"].startswith(
            "filter is not valid JSON: ",
        )
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

    @pytest.mark.parametrize("raw", ['["title"]', '"title"', "3"])
    def test_list_kind_route_rejects_a_filter_that_is_not_an_object(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        raw: str,
    ) -> None:
        """JSON that parses to anything but an object is the caller's 400."""
        client, _store, _engine = route_client
        r = client.get("/api/inquiries", params=[("kind", "Issue"), ("filter", raw)])
        assert r.status_code == 400, r.text
        assert json_loads(r.content, dict[str, str]) == {
            "detail": "filter must be a JSON object",
        }

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
            mock.return_value = list[Inquiry]()
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
            mock.return_value = list[Inquiry]()
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
                mock.return_value = list[Inquiry]()
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
        engine.conn.fetch.return_value = list[dict[str, object]]()
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
        body = json_loads(r.content, dict[str, object])
        assert body["found"] == {str(good): "Issue"}
        assert body["missing"] == [str(bad)]

    def test_subscribe_streams_every_change_id(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """SSE relays every NOTIFY payload as ``{"id": "<uuid>"}``.

        Viewer-gated reads, and every change reaches every viewer; the one
        exception, a ping about a session's transcript, is the next test's.
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

    def test_subscribe_holds_back_a_hidden_transcripts_ping_from_a_viewer(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        # The database says the viewer reads the session, once they hold its audience.
        engine.conn.fetchval = AsyncMock(return_value=True)
        hidden = new_uuid()
        shown = new_uuid()
        engine.listen_messages = [
            json.dumps({"id": str(hidden), "audience": "staff"}),
            json.dumps({"id": str(shown), "audience": "open"}),
        ]

        def streamed() -> str:
            with client.stream("GET", "/api/change_log/stream") as r:
                return b"".join(r.iter_bytes()).decode()

        install_identity(make_test_identity())
        opened = streamed()
        install_identity(
            replace(make_test_identity(), read_audiences=frozenset({"open", "staff"})),
        )
        staffed = streamed()

        assert (str(hidden) in opened, str(shown) in opened) == (False, True)
        assert (str(hidden) in staffed, str(shown) in staffed) == (True, True)

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
            list[dict[str, object]](),
            list[dict[str, object]](),
            # /api/inquiries/{kind}/{seq}: fetch_edges after inner get_inquiry.
            list[dict[str, object]](),
            list[dict[str, object]](),
            # /api/inquiries?kind=: list-select then fetch_edges_bulk.
            [self._row(target_id)],
            list[dict[str, object]](),
            list[dict[str, object]](),
            # /api/inquiries/next_issue: fetch_edges outbound + inbound.
            list[dict[str, object]](),
            list[dict[str, object]](),
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
        # inquiry_cost now does an existence check via fetchval first.
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
            list[dict[str, object]](),
            list[dict[str, object]](),
            # change_log: list_changes rows.
            list[dict[str, object]](),
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
        # ``inquiry_cost`` existence probe returns no row.
        engine.conn.fetchval.return_value = None
        r = client.get(f"/api/inquiries/{new_uuid()}/cost")
        assert r.status_code == 404

    def test_confidence_unknown_id_is_404(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        # ``derived_confidence`` kind probe returns no row (missing id).
        engine.conn.fetchval.return_value = None
        r = client.get(f"/api/inquiries/{new_uuid()}/confidence")
        assert r.status_code == 404

    def test_confidence_no_evidence_is_neutral(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        engine.conn.fetchval.return_value = "Belief"  # Claimable kind probe.
        engine.conn.fetch.return_value = list[dict[str, object]]()  # No proving edges.
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

    def test_change_unknown_or_hidden_is_404(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, store, _engine = route_client
        change_id = new_uuid()
        with patch.object(store, "get_change", AsyncMock(return_value=None)) as get:
            r = client.get(f"/api/change_log/{change_id}")
        assert r.status_code == 404
        assert from_plain(r.json(), dict[str, str])["detail"] == "change not found"
        assert get.await_args is not None
        assert get.await_args.args == (change_id,)

    def test_a_visible_change_is_returned(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, store, _engine = route_client
        change = Change(
            kind="description",
            subject_id=new_uuid(),
            subject_kind="Issue",
            old=Snapshot(description="d"),
            new=Snapshot(description="e"),
        )
        with patch.object(store, "get_change", AsyncMock(return_value=change)):
            r = client.get(f"/api/change_log/{change.id}")
        assert r.status_code == 200, r.text
        body = from_plain(r.json(), dict[str, object])
        assert body["id"] == str(change.id)
        assert body["subject_id"] == str(change.subject_id)


class TestClaimNextIssue:
    def test_a_claim_without_an_idempotency_key_is_refused_unclaimed(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, store, _engine = route_client
        with patch.object(store, "claim_next_issue", AsyncMock()) as claim:
            r = client.post("/api/inquiries/next_issue", json={"owner": "worker"})
        assert r.status_code == 400
        assert "Idempotency-Key" in from_plain(r.json(), dict[str, str])["detail"]
        claim.assert_not_awaited()

    def test_a_keyed_claim_names_the_owner_and_the_caller_as_actor(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, store, _engine = route_client
        with patch.object(
            store,
            "claim_next_issue",
            AsyncMock(return_value=None),
        ) as claim:
            r = client.post(
                "/api/inquiries/next_issue",
                json={"owner": "worker"},
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
        assert r.status_code == 200, r.text
        assert r.json() is None
        assert claim.await_args is not None
        assert claim.await_args.kwargs["owner"] == "worker"
        assert claim.await_args.kwargs["actor"] == TEST_USER_EMAIL


@pytest.mark.asyncio
async def test_a_cancelled_list_query_is_logged_cancelled_and_reraised(
    route_client: tuple[TestClient, Store, FakeEngine],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _client, store, engine = route_client
    # Without the app's ``BaseHTTPMiddleware``, which turns a cancelled body into
    # "No response returned".
    bare = FastAPI()
    bare.include_router(query.router)
    bare.state.store, bare.state.engine = store, engine
    bare.dependency_overrides.update(app.dependency_overrides)
    transport = httpx2.ASGITransport(app=bare)
    with (
        patch.object(
            store,
            "list_kind",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        caplog.at_level(logging.INFO),
    ):
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            with pytest.raises(asyncio.CancelledError):
                await http.get("/api/inquiries", params={"kind": "Issue"})

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", "") == "trackinizer_query_completed"
    )
    fields = from_plain(record.__dict__, dict[str, object])
    assert from_plain(fields.get("outcome"), str) == "cancelled"
    assert from_plain(fields.get("error_type"), str) == "CancelledError"


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


@pytest.mark.parametrize("kind", sorted(KIND_TO_CLASS))
def test_fields_keep_the_named_values_through_the_real_store(
    route_client: tuple[TestClient, Store, FakeEngine],
    kind: Inquiry.InquiryKind,
) -> None:
    client, _store, engine = route_client
    target, parent, evidence = new_uuid(), new_uuid(), new_uuid()
    row = TestCoverageRoutesAndCli()._row(target, kind=kind)
    outbound = (
        [
            {
                "from_id": target,
                "to_id": parent,
                "to_kind": "Issue",
                "edge_kind": "narrows",
                "priority": 10,
                "note": "parent",
                "valence": None,
                "labels": ["context"],
            },
        ]
        if kind == "Issue"
        else []
    )
    inbound = (
        [
            {
                "to_id": target,
                "from_id": evidence,
                "from_kind": "Artifact",
                "edge_kind": "proves",
                "priority": None,
                "note": "evidence",
                "valence": 0.75,
                "labels": ["proof"],
            },
        ]
        if kind == "Belief"
        else []
    )
    engine.conn.fetch.side_effect = [[row], outbound, inbound] * 3
    whole = client.get("/api/inquiries", params={"kind": kind})
    assert whole.status_code == 200, whole.text
    [full] = json_loads(whole.content, list[dict[str, object]])
    named = client.get(
        "/api/inquiries",
        params=[
            ("kind", kind),
            ("seq_range", "1..1"),
            *(("fields", key) for key in full),
        ],
    )
    assert named.status_code == 200, named.text
    assert json_loads(named.content, list[dict[str, object]]) == [full]
    subset = ("id", "title", "priority", "judgement", "proved_by", "narrows")
    named = client.get(
        "/api/inquiries",
        params=[("kind", kind), *(("fields", key) for key in subset)],
    )
    assert named.status_code == 200, named.text
    assert json_loads(named.content, list[dict[str, object]]) == [
        {key: value for key, value in full.items() if key in subset},
    ]
    if kind == "Issue":
        assert full["narrows"] == [
            {
                "id": str(parent),
                "kind": "Issue",
                "note": "parent",
                "labels": ["context"],
                "priority": 10,
            },
        ]
    if kind == "Belief":
        assert full["proved_by"] == [
            {
                "id": str(evidence),
                "kind": "Artifact",
                "note": "evidence",
                "labels": ["proof"],
                "valence": 0.75,
            },
        ]
    sql, *params = engine.conn.fetch.call_args_list[3].args
    assert sql == (
        "SELECT * FROM inquiries WHERE kind = $1 AND ((seq >= $2 AND seq <= $3)) "
        "ORDER BY created DESC, id DESC LIMIT $4 OFFSET $5"
    )
    assert params == [kind, 1, 1, 50, 0]


@pytest.mark.parametrize(
    ("email", "role", "status"),
    [
        ("bob@example.com", "writer", 403),
        ("Ada@Example.com", "writer", 200),
        ("root@example.com", "admin", 200),
    ],
)
def test_a_row_is_purged_by_its_account_or_an_admin_and_no_other_writer(
    route_client: tuple[TestClient, Store, FakeEngine],
    email: str,
    role: Role,
    status: int,
) -> None:
    client, _store, engine = route_client
    target = new_uuid()
    set_field_row(engine.conn, {"kind": "Issue", "account": "ada@example.com"})
    engine.conn.fetch.return_value = []
    install_identity(make_test_identity(api_key_id=None, email=email, role=role))
    response = client.request("DELETE", f"/api/inquiries/{target}", json={})
    assert response.status_code == status, response.text
    deletes = [
        call.args
        for call in engine.conn.execute.call_args_list
        if str(call.args[0]).startswith("DELETE FROM inquiries")
    ]
    if status == 403:
        assert (
            json_loads(response.content, dict[str, str])["detail"]
            == "A row is purged by its account or an admin"
        )
        assert deletes == []
    else:
        assert deletes == [("DELETE FROM inquiries WHERE id = $1", target)]
        assert json_loads(response.content, dict[str, object])["id"] == str(target)


def test_a_writer_cannot_take_a_row_by_its_account_and_then_purge_it(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _store, engine = route_client
    target = new_uuid()
    ada, bob = "ada@example.com", "bob@example.com"
    set_field_row(engine.conn, {"kind": "Issue", "account": ada})
    engine.conn.fetch.return_value = []
    install_identity(make_test_identity(api_key_id=None, email=bob))
    take = client.put(f"/api/inquiries/{target}/account", json={"value": bob})
    purge = client.request("DELETE", f"/api/inquiries/{target}", json={})
    assert (take.status_code, purge.status_code) == (403, 403)
    assert not any(
        "UPDATE inquiries" in str(call.args[0])
        or "DELETE FROM inquiries" in str(call.args[0])
        for call in engine.conn.execute.call_args_list
    )
    identities: tuple[tuple[str, Role], ...] = (
        (ada, "writer"),
        ("root@example.com", "admin"),
    )
    for email, role in identities:
        install_identity(make_test_identity(api_key_id=None, email=email, role=role))
        response = client.put(f"/api/inquiries/{target}/account", json={"value": bob})
        assert response.status_code == 200, response.text
    updates = [
        call.args
        for call in engine.conn.execute.call_args_list
        if str(call.args[0]).startswith("UPDATE inquiries SET account")
    ]
    assert len(updates) == 2
    assert all(call[1:3] == (bob, target) for call in updates)


def test_brief_changes_drop_unset_keys_and_cut_text_through_the_real_store(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _store, engine = route_client
    parent, child = new_uuid(), new_uuid()
    first, second = "First draft. " * 20, "Second draft. " * 20
    base = {
        "created": datetime(2026, 1, 1, tzinfo=UTC),
        "actor": "alice",
        "reason": "",
        "subject_kind": "Issue",
        "subject_id": parent,
    }
    engine.conn.fetch.return_value = [
        {
            **base,
            "id": new_uuid(),
            "kind": "description",
            "old_description": first,
            "new_description": second,
        },
        {
            **base,
            "id": new_uuid(),
            "kind": "status",
            "old_status": "active",
            "new_status": "complete",
        },
        {
            **base,
            "id": new_uuid(),
            "kind": "edge_added",
            "new_peer_id": child,
            "new_peer_kind": "Issue",
            "new_peer_edge_kind": "narrows",
        },
        {
            **base,
            "id": new_uuid(),
            "subject_id": child,
            "kind": "edge_added",
            "new_peer_id": parent,
            "new_peer_kind": "Issue",
            "new_peer_edge_kind": "narrows",
        },
    ]
    whole = client.get("/api/change_log")
    brief = client.get("/api/change_log", params={"brief": "true"})
    assert (whole.status_code, brief.status_code) == (200, 200)
    full = json_loads(whole.content, list[dict[str, object]])
    rows = json_loads(brief.content, list[dict[str, object]])
    assert rows == [_briefed(row) for row in full]
    assert rows[0]["old"] == {"description": first[:32]}
    assert rows[0]["new"] == {"description": second[:32]}
    assert {
        from_plain(row["new"], dict[str, object]).get("peer_id")
        for row in rows
        if row["kind"] == "edge_added"
    } == {str(child), str(parent)}
    assert engine.conn.fetch.call_args_list[0].args == (
        (
            "SELECT * FROM change_log "
            "WHERE (change_log.kind NOT IN ('locked', 'unlocked')) "
            "ORDER BY created DESC, id DESC LIMIT $1"
        ),
        200,
    )


def test_change_log_takes_several_kinds_before_its_limit_through_the_real_store(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _store, engine = route_client
    base = {
        "created": datetime(2026, 1, 1, tzinfo=UTC),
        "actor": "alice",
        "reason": "",
        "subject_kind": "Issue",
        "subject_id": new_uuid(),
    }
    title = {
        **base,
        "id": new_uuid(),
        "kind": "title",
        "old_title": "t0",
        "new_title": "t1",
    }
    status = {
        **base,
        "id": new_uuid(),
        "kind": "status",
        "old_status": "active",
        "new_status": "complete",
    }
    engine.conn.fetch.side_effect = [[title, status], [status]]
    many = client.get(
        "/api/change_log",
        params=[("kind", "status"), ("kind", "title"), ("limit", "2")],
    )
    one = client.get("/api/change_log", params={"kind": "status"})
    invalid = client.get("/api/change_log", params=[("kind", "status"), ("kind", "x")])
    assert (many.status_code, one.status_code, invalid.status_code) == (200, 200, 422)
    assert [
        row["kind"] for row in json_loads(many.content, list[dict[str, object]])
    ] == ["title", "status"]
    assert [
        row["kind"] for row in json_loads(one.content, list[dict[str, object]])
    ] == ["status"]
    by_kind = (
        "SELECT * FROM change_log "
        "WHERE (change_log.kind NOT IN ('locked', 'unlocked')) "
        "AND kind = ANY($1::text[]) ORDER BY created DESC, id DESC LIMIT $2"
    )
    assert [call.args for call in engine.conn.fetch.call_args_list] == [
        (by_kind, ["status", "title"], 2),
        (by_kind, ["status"], 200),
    ]


@pytest.mark.parametrize(
    ("op", "value", "predicate", "bindings"),
    [
        (
            "isnull",
            "",
            "NOT EXISTS (SELECT 1 FROM edges e WHERE e.from_id = inquiries.id AND e.edge_kind = 'narrows')",
            ["Issue", 50, 0],
        ),
        (
            "notnull",
            "",
            "EXISTS (SELECT 1 FROM edges e WHERE e.from_id = inquiries.id AND e.edge_kind = 'narrows')",
            ["Issue", 50, 0],
        ),
        (
            "is",
            "parent-id",
            "EXISTS (SELECT 1 FROM edges e WHERE e.from_id = inquiries.id AND e.edge_kind = 'narrows' AND e.to_id::text = $VALUE)",
            ["Issue", "parent-id", 50, 0],
        ),
        (
            "ne",
            "parent-id",
            "NOT EXISTS (SELECT 1 FROM edges e WHERE e.from_id = inquiries.id AND e.edge_kind = 'narrows' AND e.to_id::text = $VALUE)",
            ["Issue", "parent-id", 50, 0],
        ),
        (
            "is",
            "not-an-id",
            "EXISTS (SELECT 1 FROM edges e WHERE e.from_id = inquiries.id AND e.edge_kind = 'narrows' AND e.to_id::text = $VALUE)",
            ["Issue", "not-an-id", 50, 0],
        ),
    ],
)
def test_narrows_filters_lower_at_the_real_store_sql_boundary(
    route_client: tuple[TestClient, Store, FakeEngine],
    op: str,
    value: str,
    predicate: str,
    bindings: list[object],
) -> None:
    client, _store, engine = route_client
    engine.conn.fetch.return_value = []
    clause = {"field": "narrows", "op": op} | ({"value": value} if value else {})
    response = client.get(
        "/api/inquiries",
        params=[("kind", "Issue"), ("filter", json.dumps(to_plain(clause)))],
    )
    assert response.status_code == 200, response.text
    assert response.json() == []
    count = len(bindings)
    engine.conn.fetch.assert_awaited_once_with(
        vetted_sql(
            "SELECT * FROM inquiries WHERE kind = $1 AND ",
            predicate.replace("$VALUE", "$2"),
            " ORDER BY created DESC, id DESC LIMIT $",
            str(count - 1),
            " OFFSET $",
            str(count),
        ),
        *bindings,
    )


@pytest.mark.parametrize(
    ("kind", "clause", "status"),
    [
        ("Issue", {"field": "narrows", "op": "re", "value": "x"}, 422),
        ("Issue", {"field": "narrows", "op": "gt", "value": "x"}, 422),
        ("Belief", {"field": "narrows", "op": "isnull"}, 400),
    ],
)
def test_narrows_filters_refuse_unsupported_ops_before_sql(
    route_client: tuple[TestClient, Store, FakeEngine],
    kind: str,
    clause: dict[str, str],
    status: int,
) -> None:
    client, _store, engine = route_client
    response = client.get(
        "/api/inquiries",
        params=[("kind", kind), ("filter", json.dumps(to_plain(clause)))],
    )
    assert response.status_code == status, response.text
    engine.conn.fetch.assert_not_awaited()


@pytest.mark.parametrize(
    ("pairs", "expected"),
    [
        ([(1, 3), (3, 4), (1, 2), (2, 4)], [(2, [1]), (3, [1]), (4, [2, 3])]),
        ([], []),
        ([(1, 2), (2, 1)], [(2, [1])]),
        ([(1, 2), (2, 3), (3, 2)], [(2, [1, 3]), (3, [2])]),
        ([(i, i + 1) for i in range(1, 9)], [(i, [i - 1]) for i in range(2, 10)]),
        ([(1, i) for i in range(2, 202)], [(i, [1]) for i in range(2, 202)]),
    ],
)
def test_ancestors_materialize_real_store_rows_and_bind_exact_recursive_bounds(
    route_client: tuple[TestClient, Store, FakeEngine],
    pairs: list[tuple[int, int]],
    expected: list[tuple[int, list[int]]],
) -> None:
    client, _store, engine = route_client
    target = uuid.UUID(int=1)
    row = TestCoverageRoutesAndCli()._row(target)
    found = [
        {
            "child": uuid.UUID(int=child),
            "id": uuid.UUID(int=parent),
            "kind": "Issue",
            "seq": parent,
            "title": f"node-{parent}",
            "status": "active",
        }
        for child, parent in pairs
    ]
    engine.conn.fetch.side_effect = [[row], found, [row]]
    response = client.get(
        "/api/inquiries",
        params=[("kind", "Issue"), ("fields", "id"), ("ancestors", "narrows")],
    )
    assert response.status_code == 200, response.text
    assert json_loads(response.content, list[dict[str, object]]) == [
        {
            "id": str(target),
            "ancestors": [
                {
                    "id": str(uuid.UUID(int=parent)),
                    "kind": "Issue",
                    "seq": parent,
                    "title": f"node-{parent}",
                    "status": "active",
                    "child_ids": [str(uuid.UUID(int=child)) for child in children],
                }
                for parent, children in expected
            ],
        },
    ]
    bound: list[object] = []
    readable = ""
    recursive = vetted_sql(
        "WITH RECURSIVE up(child, parent, depth) AS ("
        "    SELECT e.from_id, e.to_id, 1 FROM edges e "
        "    WHERE e.edge_kind = 'narrows' AND e.from_id = ANY($1::uuid[])",
        readable,
        " "
        "    UNION "
        "    SELECT e.from_id, e.to_id, up.depth + 1 FROM edges e "
        "    JOIN up ON e.from_id = up.parent "
        "    WHERE e.edge_kind = 'narrows' AND up.depth < $2",
        readable,
        "), kept AS ("
        "    SELECT parent FROM up GROUP BY parent "
        "    ORDER BY min(depth), parent LIMIT $3"
        ") SELECT DISTINCT up.child, t.id, t.kind, t.seq, t.title, t.status "
        "FROM up JOIN kept ON kept.parent = up.parent "
        "JOIN inquiries t ON t.id = up.parent",
    )
    assert engine.conn.fetch.call_args_list[1].args == (
        recursive,
        [target],
        8,
        200,
        *bound,
    )
    plain = client.get("/api/inquiries", params=[("kind", "Issue"), ("fields", "id")])
    assert plain.status_code == 200
    assert json_loads(plain.content, list[dict[str, object]]) == [{"id": str(target)}]
    assert engine.conn.fetch.await_count == 3


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
        # An edge read names inquiries too, in its peer's read filter.
        return [] if "FROM edges WHERE" in sql else [row]

    return fetch


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
