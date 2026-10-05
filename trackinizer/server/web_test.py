"""Tests for trackinizer web helpers and routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast, override
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import json
import os
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg

    from trackinizer.lib.postgres import PGliteEngine
else:
    from wrapt import lazy_import

    asyncpg = lazy_import("asyncpg")

import pytest
import pytest_asyncio

from trackinizer.conftest import FakeEngine, make_conn, make_store, new_uuid
from trackinizer.lib.custom_json import convert, parse
from trackinizer.lib.postgres import Conn
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server import web
from trackinizer.server.api import auth_routes
from trackinizer.server.api.query import get_inquiry_route
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.server.config import Config
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.route_iter import (
    iter_routes,
    registered_paths,
)
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_feed import WHOLE_FEED, FeedScope
from trackinizer.types.columns import column_specs, storage_name
from trackinizer.types.inquiries import INQUIRY_CLASSES, KIND_TO_CLASS, Inquiry
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
from trackinizer.wire.wire_sessions import (
    FeedBucket,
    FeedEvent,
    FeedFacetsResponse,
    FeedHistogramResponse,
)


_CWD: Final = Path(__file__).resolve().parent


# Tests in this module call the FastAPI route functions directly (not
# via TestClient), so the ``Depends(require_role(...))`` resolution does
# not run; the test must pass an :class:`AuthIdentity` explicitly to
# satisfy the parameter.
_TEST_IDENTITY = AuthIdentity(
    user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
    api_key_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
    email="webtest@example.com",
    role="viewer",
)

# The relations ``GET /api/inquiries/{id}`` projects onto the row. The web detail
# serves them as ``edges`` / ``backlinks`` instead, with each peer's title and status.
_EDGE_RELATIONS: Final = frozenset(
    {
        "supersedes",
        "superseded_by",
        "produces",
        "produced_by",
        "narrows",
        "narrowed_by",
        "requires",
        "required_by",
        "proves",
        "proved_by",
        "favors",
        "favored_by",
        "cites",
        "cited_by",
    },
)


@dataclass(slots=True, kw_only=True)
class _State:
    store: object
    engine: object


@dataclass(slots=True, kw_only=True)
class _App:
    state: _State


@dataclass(slots=True, kw_only=True)
class _Request:
    app: _App


def _request(store: object, engine: object) -> _Request:
    return _Request(app=_App(state=_State(store=store, engine=engine)))


@dataclass(slots=True, kw_only=True)
class _Store:
    engine: FakeEngine


def _inquiry_row(**overrides: object) -> dict[str, object]:
    now = datetime(2026, 5, 18, tzinfo=UTC)
    row: dict[str, object] = {
        "id": new_uuid(),
        "kind": "Issue",
        "seq": 1,
        "owner": "alice",
        "account": "alice@example.com",
        "status": "active",
        "title": "title",
        "description": "description",
        "labels": ["x"],
        "subscribers": ["bob"],
        "marginal_cost_agent_usd": 1.5,
        "marginal_cost_resource_usd": 2.5,
        "created": now,
        "modified": now,
        "belief_judgement": None,
        "issue_priority": "high",
        "experiment_outcome": None,
        "paper_abstract": None,
        "paper_authors": None,
        "paper_publication_type": None,
        "paper_venue": None,
        "paper_subvenue": None,
        "paper_publish_date": None,
        "paper_source": None,
        "paper_google_scholar_cluster_id": None,
        "paper_google_scholar_cites_id": None,
        "codechange_sha": None,
        "webresult_url": None,
        "websearch_query": None,
        "websearch_provider": None,
        "experiment_codechanges": None,
    }
    row.update(overrides)
    return row


def _null_row(kind: Inquiry.InquiryKind) -> dict[str, object]:
    """Return a ``SELECT *`` row of ``kind`` with every nullable column NULL."""
    now = datetime(2026, 5, 18, tzinfo=UTC)
    row: dict[str, object] = {
        "id": new_uuid(),
        "kind": kind,
        "seq": 1,
        "account": "alice@example.com",
        "status": "active",
        "title": "title",
        "marginal_cost_agent_usd": 0.0,
        "marginal_cost_resource_usd": 0.0,
        "created": now,
        "modified": now,
    }
    for cls in INQUIRY_CLASSES:
        for name, spec in column_specs(cls).items():
            if spec.flatten is None:
                row.setdefault(storage_name(name, spec), None)
    return row


def _change_row(**overrides: object) -> dict[str, object]:
    now = datetime(2026, 5, 18, tzinfo=UTC)
    row: dict[str, object] = {
        "id": new_uuid(),
        "created": now,
        "actor": "alice",
        "api_key_id": None,
        "subject_id": new_uuid(),
        "subject_kind": "Issue",
        "kind": "title",
        "caused_by": None,
        "reason": "why",
    }
    for prefix in ("old_", "new_"):
        for name in web._SNAPSHOT_COLUMNS:
            if name == "marginal_cost":
                row[prefix + "marginal_cost_agent_usd"] = 0.0
                row[prefix + "marginal_cost_resource_usd"] = 0.0
            else:
                row[prefix + name] = None
    row.update(overrides)
    return row


class TestQueryHelpers:
    def test_parse_query_tokenizes_quotes_and_fields(self) -> None:
        assert web._parse_query('hello title:"exact phrase" :kept') == [
            (None, "hello"),
            ("title", "exact phrase"),
            (None, ":kept"),
        ]

    def test_parse_query_raises_on_unterminated_quote(self) -> None:
        with pytest.raises(ValueError, match="quot"):
            web._parse_query('"unterminated')

    def test_parse_query_keeps_regex_backslashes(self) -> None:
        # Shell escaping ate the backslash outside quotes, so ``title:\d+``
        # reached Postgres as ``d+`` (R30).
        assert web._parse_query(r'title:\d+ description:"\bx\b" a\b') == [
            ("title", r"\d+"),
            ("description", r"\bx\b"),
            (None, r"a\b"),
        ]

    def test_parse_query_reads_apostrophes_and_hashes_literally(self) -> None:
        # An apostrophe opened a shell quote that never closed, so ``don't``
        # was a 400 (R30). Only ``"`` groups a phrase.
        assert web._parse_query("""don't title:"it's here" Issue#12""") == [
            (None, "don't"),
            ("title", "it's here"),
            (None, "Issue#12"),
        ]

    def test_parse_query_rejects_empty_field_value(self) -> None:
        # ``title:`` with no pattern is almost certainly a user typo;
        # silently dropping the prefix and ILIKE-matching ``%title:%``
        # masks intent. Surface a ValueError so the route returns 400.
        with pytest.raises(ValueError, match="empty"):
            web._parse_query("title:")
        with pytest.raises(ValueError, match="empty"):
            web._parse_query("description:")

    def test_build_term_clause(self) -> None:
        clause, params = web._build_term_clause(
            [(None, "hello"), ("description", "re.*")],
        )
        # Bare tokens ILIKE with an ESCAPE clause (so user ``%``/``_`` are
        # literal); field-qualified tokens use the regex operator unchanged.
        assert (
            clause == "(title ILIKE $1 ESCAPE '\\' OR description ILIKE $1 "
            "ESCAPE '\\') AND description ~* $2"
        )
        assert params == ["%hello%", "re.*"]

    def test_build_term_clause_rejects_invalid_regex(self) -> None:
        with pytest.raises(ValueError, match="invalid regex"):
            web._build_term_clause([("title", "[unclosed")])

    def test_bare_token_escapes_ilike_wildcards(self) -> None:
        # A bare token's ``%`` / ``_`` are ILIKE wildcards; without escaping,
        # ``q=%`` matches every row (TRK-SRV-001). The bound param must carry
        # the token's wildcards escaped, and the clause must declare ESCAPE.
        clause, params = web._build_term_clause([(None, "50% _x")])
        assert "ESCAPE" in clause
        # ``%`` and ``_`` in the user token are escaped to literals; the
        # surrounding ``%...%`` (the substring match) stays wild.
        assert params == [r"%50\% \_x%"]

    def test_bare_token_escapes_the_escape_char(self) -> None:
        # A literal backslash in the token must itself be escaped, or it
        # would consume the following char under ESCAPE semantics.
        _clause, params = web._build_term_clause([(None, r"a\b")])
        assert params == [r"%a\\b%"]


class TestSerialization:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", sorted(KIND_TO_CLASS))
    async def test_self_is_the_inquiry_route_less_its_edges(
        self,
        kind: Inquiry.InquiryKind,
    ) -> None:
        # ``/api/web/get`` hand-listed its columns, turned NULL owner and
        # description into ``""`` and NULL lists into ``[]``, and dropped unset
        # kind fields, so the two reads of one row disagreed (R17). ``self``
        # must be what ``GET /api/inquiries/{id}`` serves, less the relations
        # that ``edges`` and ``backlinks`` carry instead.
        row = _null_row(kind)
        target_id = cast(uuid.UUID, row["id"])
        store, engine = make_store()
        engine.conn.fetchrow = AsyncMock(return_value=row)
        engine.conn.fetch = AsyncMock(return_value=[])
        request = cast(Request, _request(store, engine))
        canonical = await get_inquiry_route(
            target_id,
            request,
            identity=_TEST_IDENTITY,
        )
        detail = await web.web_get(target_id, request, identity=_TEST_IDENTITY)
        assert detail["self"] == {
            key: value for key, value in canonical.items() if key not in _EDGE_RELATIONS
        }

    def test_row_to_dict_includes_kind_specific_values(self) -> None:
        target_id = new_uuid()
        out = web._row_to_dict(
            cast(
                "asyncpg.Record",
                _inquiry_row(
                    id=target_id,
                    kind="WebSearch",
                    issue_priority=None,
                    websearch_query="q",
                    websearch_provider="google",
                ),
            ),
        )
        assert out["id"] == str(target_id)
        assert out["labels"] == ["x"]
        assert out["marginal_cost"] == {"agent_usd": 1.5, "resource_usd": 2.5}
        assert out["query"] == "q"
        assert out["provider"] == "google"
        assert "priority" not in out
        # ``account`` is a required base field; the detail/SPA view must
        # surface it like ``owner`` (regression: it was omitted, so the
        # ``trax issue <seq>`` detail rendered a blank account).
        assert out["account"] == "alice@example.com"
        assert out["owner"] == "alice"

    def test_row_to_dict_surfaces_paper_google_scholar_handles(self) -> None:
        # Regression: the Scholar handles were stored + emitted by the list-json
        # path but NOT by _row_to_dict, so the trax field-getter and the SPA
        # detail view rendered them empty despite being populated.
        out = web._row_to_dict(
            cast(
                "asyncpg.Record",
                _inquiry_row(
                    kind="Paper",
                    paper_google_scholar_cluster_id="vexaDfEelKEJ",
                    paper_google_scholar_cites_id="4727085927710188680",
                ),
            ),
        )
        assert out["google_scholar_cluster_id"] == "vexaDfEelKEJ"
        assert out["google_scholar_cites_id"] == "4727085927710188680"

    def test_row_to_dict_surfaces_experiment_config(self) -> None:
        cfg = {"lr": 3e-4, "batch": 32, "nested": {"warmup": 100}}
        out = web._row_to_dict(
            cast(
                "asyncpg.Record",
                _inquiry_row(kind="Experiment", experiment_config=cfg),
            ),
        )
        # JSONB is decoded to a dict by the codec; surfaced verbatim, not
        # ISO-formatted or stringified.
        assert out["config"] == cfg

    def test_row_to_dict_surfaces_agentsession_fields(self) -> None:
        started = datetime(2026, 5, 18, 9, tzinfo=UTC)
        out = web._row_to_dict(
            cast(
                "asyncpg.Record",
                _inquiry_row(
                    kind="AgentSession",
                    agentsession_cli="claude",
                    agentsession_cli_session_id="abc-123",
                    agentsession_started=started,
                    agentsession_ended=None,
                    agentsession_rooms=["sear", "lab"],
                ),
            ),
        )
        assert out["cli"] == "claude"
        assert out["cli_session_id"] == "abc-123"
        # TIMESTAMPTZ columns are ISO-formatted, not raw datetimes.
        assert out["started"] == started.isoformat()
        # A live session has not ended: NULL, as ``/api/inquiries`` says it.
        assert out["ended"] is None
        # Rooms surface as a plain list for the SPA addressing UI.
        assert out["rooms"] == ["sear", "lab"]
        json.dumps(out)

    def test_change_to_dict_and_snapshot_conversion(self) -> None:
        peer_id = new_uuid()
        old_codechanges = new_uuid()
        api_key_id = new_uuid()
        row = _change_row(
            api_key_id=api_key_id,
            principal="api@example.com",
            caused_by=peer_id,
            old_labels=["a"],
            new_experiment_codechanges=[old_codechanges],
            new_peer_id=peer_id,
            new_marginal_cost_agent_usd=2,
            new_marginal_cost_resource_usd=3,
        )
        out = web._change_to_dict(cast("asyncpg.Record", row))
        assert out["api_key_id"] == str(api_key_id)
        assert out["principal"] == "api@example.com"
        assert out["caused_by"] == str(peer_id)
        assert cast(dict[str, object], out["old"])["labels"] == ["a"]
        assert cast(dict[str, object], out["new"])["experiment_codechanges"] == [
            str(old_codechanges),
        ]
        assert cast(dict[str, object], out["new"])["peer_id"] == str(peer_id)
        assert cast(dict[str, object], out["new"])["marginal_cost"] == {
            "agent_usd": 2.0,
            "resource_usd": 3.0,
        }

    def test_peer_ref_and_isoformat(self) -> None:
        peer_id = new_uuid()
        created = datetime(2026, 5, 18, tzinfo=UTC)
        row = {
            "peer_kind": "Belief",
            "peer_seq": 4,
            "peer_title": None,
            "peer_status": "active",
            "peer_judgement": "proven",
            "peer_created": created,
            "peer_priority": None,
        }
        assert web._peer_ref(cast("asyncpg.Record", row), peer_id) == {
            "id": str(peer_id),
            "kind": "Belief",
            "seq": 4,
            "title": "",
            "status": "active",
            "judgement": "proven",
            "peer_created": created.isoformat(),
        }
        issue = {
            **row,
            "peer_kind": "Issue",
            "peer_judgement": None,
            "peer_priority": 0,
        }
        # Priority 0 is the most urgent, not an absent one.
        assert (
            web._peer_ref(cast("asyncpg.Record", issue), peer_id)["peer_priority"] == 0
        )
        assert web._isoformat(datetime(2026, 5, 18, tzinfo=UTC)).startswith("2026")
        assert web._isoformat("x") == "x"


class TestEdgeHelpers:
    @pytest.mark.asyncio
    async def test_edges_and_backlinks_group_refs(self) -> None:
        target_id = new_uuid()
        peer_id = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(
            side_effect=[
                [
                    {
                        "to_id": peer_id,
                        "edge_kind": "narrows",
                        "priority": 10,
                        "note": "edge note",
                        "valence": 0.5,
                        "labels": ["edge-label"],
                        "peer_kind": "Issue",
                        "peer_seq": 2,
                        "peer_title": "peer",
                        "peer_status": "active",
                        "peer_judgement": None,
                        "peer_created": datetime(2026, 5, 18, tzinfo=UTC),
                        "peer_priority": 20,
                    },
                ],
                [
                    {
                        "from_id": peer_id,
                        "edge_kind": "requires",
                        "priority": None,
                        "note": "",
                        "valence": None,
                        "labels": [],
                        "peer_kind": "Issue",
                        "peer_seq": 3,
                        "peer_title": "back",
                        "peer_status": "complete",
                        "peer_judgement": None,
                        "peer_created": datetime(2026, 5, 19, tzinfo=UTC),
                        "peer_priority": None,
                    },
                ],
            ],
        )
        edges = await web._edges_for(cast(Conn, conn), target_id, direction="outbound")
        backlinks = await web._edges_for(
            cast(Conn, conn),
            target_id,
            direction="inbound",
        )
        edge = cast(list[dict[str, object]], edges["narrows"])[0]
        # The edge's priority and the peer's own ride side by side.
        assert (edge["priority"], edge["peer_priority"]) == (10, 20)
        assert edge["note"] == "edge note"
        assert edge["valence"] == 0.5
        assert edge["labels"] == ["edge-label"]
        assert cast(list[dict[str, object]], backlinks["requires"])[0]["seq"] == 3


class TestRoutes:
    @pytest.mark.asyncio
    async def test_web_search_empty_and_with_kind(self) -> None:
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        assert (
            await web.web_search(
                cast(Request, request),
                q="   ",
                identity=_TEST_IDENTITY,
            )
            == []
        )

        engine.conn.fetch = AsyncMock(return_value=[_inquiry_row()])
        rows = await web.web_search(
            cast(Request, request),
            q="hello",
            identity=_TEST_IDENTITY,
            kind="Issue",
            limit=3,
        )
        assert rows[0]["title"] == "title"
        assert engine.conn.fetch.await_args is not None
        sql, *params = engine.conn.fetch.await_args.args
        assert isinstance(sql, str)
        assert "kind = $2" in sql
        assert params == ["%hello%", "Issue", 3]

    @pytest.mark.asyncio
    async def test_web_search_bounds_db_cost_with_statement_timeout(self) -> None:
        # A field-scoped ``~*`` regex runs POSIX-side in Postgres, where a
        # pathological pattern can backtrack catastrophically and pin the
        # connection. Python ``re.compile`` only checks syntax, not DB cost.
        # The search must cap each query with ``SET LOCAL statement_timeout``
        # (which requires the query to run inside a transaction) so a viewer
        # cannot DoS the cluster with one expensive regex.
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        engine.conn.fetch = AsyncMock(return_value=[_inquiry_row()])
        await web.web_search(
            cast(Request, request),
            q="title:^(a+)+$",
            identity=_TEST_IDENTITY,
        )
        executed = [
            s
            for c in engine.conn.execute.call_args_list
            for s in c.args[:1]
            if isinstance(s, str)
        ]
        timeouts = [s for s in executed if "statement_timeout" in s.lower()]
        assert timeouts, (
            "search must SET LOCAL statement_timeout to bound per-query DB cost"
        )
        assert any("SET LOCAL" in s for s in timeouts), (
            "the timeout must be SET LOCAL so it is scoped to the search tx"
        )
        # SET LOCAL only takes effect inside a transaction, so the search must
        # open one (BEGIN ... COMMIT) around the bounded query.
        assert "BEGIN" in executed, "SET LOCAL requires the query to run in a tx"

    @pytest.mark.asyncio
    async def test_web_search_reports_an_invalid_pattern_as_400(self) -> None:
        # Postgres answers an invalid regex with SQLSTATE 2201B
        # (``InvalidRegularExpressionError``, a ``DataError``) -- NOT the
        # 42601 ``PostgresSyntaxError`` the phrase "syntax error" suggests.
        # The two classes are unrelated, so a guard written against the
        # plausible one never fires and the caller gets a 500.
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        # Only the SEARCH query fails. ``tx`` rolls back through ``fetch`` as
        # well, so a blanket ``side_effect`` would break the cleanup too and
        # bury the 400 under the rollback's own error.
        engine.conn.fetch = AsyncMock(
            side_effect=[
                asyncpg.InvalidRegularExpressionError(
                    "invalid regular expression: invalid embedded option",
                ),
                [],
            ],
        )
        with pytest.raises(HTTPException) as caught:
            await web.web_search(
                cast(Request, request),
                q="title:(?P<n>a)",
                identity=_TEST_IDENTITY,
            )
        assert caught.value.status_code == 400

    @pytest.mark.asyncio
    async def test_web_search_lets_a_server_fault_through(self) -> None:
        # A generated-SQL defect is our bug. Relabelling it 400 would hide a
        # server fault behind a client error.
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        engine.conn.fetch = AsyncMock(
            side_effect=[
                asyncpg.PostgresSyntaxError('syntax error at or near "FROM"'),
                [],
            ],
        )
        with pytest.raises(asyncpg.PostgresSyntaxError):
            await web.web_search(
                cast(Request, request),
                q="title:^a",
                identity=_TEST_IDENTITY,
            )

    @pytest.mark.asyncio
    async def test_recent_lookup_and_get_routes(self) -> None:
        target_id = new_uuid()
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        engine.conn.fetch = AsyncMock(
            return_value=[_change_row(principal="api@example.com")],
        )
        recent = await web.web_recent_changes(
            cast(Request, request),
            identity=_TEST_IDENTITY,
            limit=1,
        )
        assert recent[0]["actor"] == "alice"
        assert recent[0]["principal"] == "api@example.com"
        sql = engine.conn.fetch.call_args.args[0]
        assert isinstance(sql, str)
        assert "LEFT JOIN api_keys" in sql

        engine.conn.fetchval = AsyncMock(return_value="Issue")
        assert await web.web_lookup(
            target_id,
            cast(Request, request),
            identity=_TEST_IDENTITY,
        ) == {
            "kind": "Issue",
            "id": str(target_id),
        }
        engine.conn.fetchval = AsyncMock(return_value=None)
        with pytest.raises(HTTPException):
            await web.web_lookup(
                target_id,
                cast(Request, request),
                identity=_TEST_IDENTITY,
            )

        engine.conn.fetchrow = AsyncMock(return_value=_inquiry_row(id=target_id))
        engine.conn.fetch = AsyncMock(side_effect=[[], [], [_change_row()]])
        detail = await web.web_get(
            target_id,
            cast(Request, request),
            identity=_TEST_IDENTITY,
        )
        assert cast(dict[str, object], detail["self"])["id"] == str(target_id)
        assert detail["edges"] == {}
        assert detail["backlinks"] == {}
        assert len(cast(list[object], detail["changes"])) == 1
        engine.conn.fetchrow = AsyncMock(return_value=None)
        with pytest.raises(HTTPException):
            await web.web_get(
                target_id,
                cast(Request, request),
                identity=_TEST_IDENTITY,
            )

    @pytest.mark.asyncio
    async def test_web_get_breaks_change_time_ties_by_id(self) -> None:
        # Changes can share ``created``; ordering by it alone left their order,
        # and which of them made the 50-row page, to the planner (R21).
        target_id = new_uuid()
        engine = FakeEngine()
        request = _request(_Store(engine=engine), engine)
        engine.conn.fetchrow = AsyncMock(return_value=_inquiry_row(id=target_id))
        engine.conn.fetch = AsyncMock(return_value=[])
        await web.web_get(target_id, cast(Request, request), identity=_TEST_IDENTITY)
        changes_sql = engine.conn.fetch.call_args_list[-1].args[0]
        assert isinstance(changes_sql, str)
        assert "ORDER BY c.created DESC, c.id DESC LIMIT 50" in changes_sql

    @pytest.mark.asyncio
    async def test_web_graph_returns_nodes_and_edges(self) -> None:
        # The graph endpoint is the aggregate the SPA paints from: each inquiry
        # as a typed node (oldest first, so a replay animation adds them in
        # creation order) plus each edge as a typed directed link. Nodes carry
        # only the light projection the graph view needs (id, kind, seq, title,
        # created), not the full per-kind detail.
        early = datetime(2026, 5, 18, 8, tzinfo=UTC)
        late = datetime(2026, 5, 18, 9, tzinfo=UTC)
        root_id, child_id = new_uuid(), new_uuid()
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        edge_rows = [
            {
                "from_id": child_id,
                "to_id": root_id,
                "edge_kind": "favors",
                "valence": 0.4,
            },
        ]
        engine.conn.fetch = AsyncMock(
            side_effect=[
                [
                    {
                        "id": root_id,
                        "kind": "Belief",
                        "seq": 1,
                        "title": "root",
                        "status": "active",
                        "created": early,
                        "belief_judgement": "proven",
                        "belief_confidence": 0.8,
                    },
                    {
                        "id": child_id,
                        "kind": "WebSearch",
                        "seq": 2,
                        "title": "child",
                        "status": "complete",
                        "created": late,
                        "belief_judgement": None,
                        "belief_confidence": None,
                    },
                ],
                edge_rows,
            ],
        )
        graph = await web.web_graph(
            cast(Request, request),
            identity=_TEST_IDENTITY,
            limit=2,
        )
        nodes = cast(list[dict[str, object]], graph["nodes"])
        edges = graph["edges"]
        assert [n["id"] for n in nodes] == [str(root_id), str(child_id)]
        assert nodes[0] == {
            "id": str(root_id),
            "kind": "Belief",
            "seq": 1,
            "title": "root",
            "status": "active",
            "created": early.isoformat(),
            "judgement": "proven",
            "confidence": 0.8,
        }
        # A non-Belief node omits the belief-only fields entirely.
        assert "judgement" not in nodes[1]
        assert "confidence" not in nodes[1]
        assert edges == [
            {
                "from_id": str(child_id),
                "to_id": str(root_id),
                "edge_kind": "favors",
                "valence": 0.4,
            },
        ]
        # Nodes must be ordered by ``created`` ascending so the replay
        # animation lands them in the order they were authored.
        node_sql = engine.conn.fetch.call_args_list[0].args[0]
        assert isinstance(node_sql, str)
        assert node_sql.endswith("ORDER BY created ASC, id ASC")
        # Edges are read for exactly the returned nodes.
        assert engine.conn.fetch.call_args_list[1].args[1] == [root_id, child_id]

    @pytest.mark.asyncio
    async def test_web_graph_omits_null_valence(self) -> None:
        # ``valence`` is non-NULL only on ``proves`` / ``favors`` citations; a
        # structural edge stores NULL there and the node must omit the key
        # rather than carry a null the SPA would have to special-case.
        a, b = new_uuid(), new_uuid()
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        engine.conn.fetch = AsyncMock(
            side_effect=[
                [],
                [
                    {
                        "from_id": a,
                        "to_id": b,
                        "edge_kind": "narrows",
                        "valence": None,
                    },
                ],
            ],
        )
        graph = await web.web_graph(
            cast(Request, request),
            identity=_TEST_IDENTITY,
            limit=1,
        )
        assert graph["edges"] == [
            {"from_id": str(a), "to_id": str(b), "edge_kind": "narrows"},
        ]

    @pytest.mark.asyncio
    async def test_web_graph_takes_any_limit_from_1(self) -> None:
        # ``limit=0`` served the whole graph to any viewer (S11), so a limit
        # below 1 is refused. Above it there is no top: the graph view lets a
        # person type any count, and the answer is still at most ``limit`` nodes.
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        engine.conn.fetch = AsyncMock(return_value=[])
        for bad in (-1, 0):
            with pytest.raises(HTTPException) as caught:
                await web.web_graph(
                    cast(Request, request),
                    identity=_TEST_IDENTITY,
                    limit=bad,
                )
            assert caught.value.status_code == 400
        for limit in (1, 5_001, 1_000_000):
            graph = await web.web_graph(
                cast(Request, request),
                identity=_TEST_IDENTITY,
                limit=limit,
            )
            assert graph == {"nodes": [], "edges": []}
            assert engine.conn.fetch.call_args_list[-2].args[1] == limit

    @pytest.mark.asyncio
    async def test_subscribe_streams_sse(self) -> None:
        engine = FakeEngine()
        subject_id = new_uuid()
        engine.listen_messages = [json.dumps({"id": str(subject_id)})]
        request = _request(object(), engine)
        response = await web.web_subscribe(
            cast(Request, request),
            identity=_TEST_IDENTITY,
        )
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            assert isinstance(chunk, bytes)
            chunks.append(chunk)
        # Wire shape is ``{"id": ...}`` JSON -- the SPA's onmessage does
        # ``JSON.parse(e.data).id``, so a bare uuid silently breaks it.
        assert b"".join(chunks) == (
            f': open\n\ndata: {{"id": "{subject_id}"}}\n\n'.encode()
        )

    def test_without_the_app_the_old_paths_serve_nothing(self, tmp_path: Path) -> None:
        # The old pages are gone. Without ``--app-dir`` there is no ``/app/`` to
        # send their paths to, so each answers 404, even where an old build's
        # files still lie in the assets directory; the read API stays.
        for name in ("index", "graph", "console", "me", "admin"):
            (tmp_path / f"{name}.html").write_text(name)
        (tmp_path / "static").mkdir()
        (tmp_path / "static" / "app.js").write_text("// old")
        app = FastAPI()
        web.attach(app, assets_dir=tmp_path)
        client = TestClient(app, follow_redirects=False)
        for path in ("/", "/me", "/admin", "/graph", "/console", "/static/app.js"):
            assert client.get(path).status_code == 404, path
        paths = registered_paths(app)
        for path in ("/api/web/search", "/api/web/graph", "/api/web/feed"):
            assert path in paths, path

    def test_static_dir_serves_files_written_after_deploy(
        self,
        tmp_path: Path,
    ) -> None:
        # The runtime --static-dir serves files written after deploy from a
        # path the source tree never sees (the demo's report.html link).
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        (runtime / "report.html").write_text("<html>report</html>")
        app = FastAPI()
        web.attach(app, assets_dir=tmp_path, static_dir=runtime)
        client = TestClient(app)
        response = client.get("/static/report.html")
        assert response.status_code == 200
        assert response.text == "<html>report</html>"

    def test_attach_is_idempotent(self, tmp_path: Path) -> None:
        # ``server.py`` calls ``attach`` on the module-global app; a second
        # call (re-import, test reuse, future hot-reload) must not duplicate
        # routes (TRK-SRV-002). Exactly one ``/api/web/search`` route.
        app = FastAPI()
        web.attach(app, assets_dir=tmp_path)
        web.attach(app, assets_dir=tmp_path)
        search_routes = [
            path for path, _ in iter_routes(app) if path == "/api/web/search"
        ]
        assert len(search_routes) == 1

    @pytest.mark.asyncio
    async def test_subscribe_skips_undecodable_payloads(self) -> None:
        engine = FakeEngine()
        good_id = new_uuid()
        engine.listen_messages = [
            "not-json",
            json.dumps({"no_id_field": True}),
            json.dumps({"id": str(good_id)}),
        ]
        request = _request(object(), engine)
        response = await web.web_subscribe(
            cast(Request, request),
            identity=_TEST_IDENTITY,
        )
        chunks = [chunk async for chunk in response.body_iterator]
        # Only the well-formed payload survives.
        assert chunks == [b": open\n\n", f'data: {{"id": "{good_id}"}}\n\n'.encode()]


class TestFeedRoute:
    @pytest.mark.asyncio
    async def test_next_after_is_last_event_composite_cursor(self) -> None:
        early = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        late = datetime(2026, 6, 1, 12, 5, tzinfo=UTC)
        last_session = new_uuid()
        events = [
            FeedEvent(
                session_id=new_uuid(),
                actor="scientist",
                rooms=["sear"],
                cli="codex",
                seq=0,
                kind="UserMessage",
                created=early,
            ),
            FeedEvent(
                session_id=last_session,
                actor="eng",
                seq=3,
                kind="AssistantMessage",
                created=late,
            ),
        ]
        store = AsyncMock()
        store.read_feed = AsyncMock(return_value=events)
        request = _request(store, FakeEngine())
        resp = await web.web_feed(cast(Request, request), identity=_TEST_IDENTITY)
        # next_after is the full composite key of the newest event, so a same-
        # ``created`` tie split across the page boundary is not skipped.
        assert resp.next_after is not None
        assert resp.next_after.created == late
        assert resp.next_after.session_id == last_session
        assert resp.next_after.seq == 3
        assert [e.actor for e in resp.events] == ["scientist", "eng"]

    @pytest.mark.asyncio
    async def test_empty_feed_next_after_echoes_supplied_cursor(self) -> None:
        created = datetime(2026, 6, 1, tzinfo=UTC)
        session_id = new_uuid()
        store = AsyncMock()
        store.read_feed = AsyncMock(return_value=[])
        request = _request(store, FakeEngine())
        resp = await web.web_feed(
            cast(Request, request),
            identity=_TEST_IDENTITY,
            after_created=created,
            after_session=session_id,
            after_seq=5,
        )
        # An empty page echoes the supplied cursor (does not rewind the tail).
        assert resp.next_after is not None
        assert resp.next_after.created == created
        assert resp.next_after.session_id == session_id
        assert resp.next_after.seq == 5
        assert resp.events == []

    @pytest.mark.asyncio
    async def test_empty_feed_no_cursor_is_none(self) -> None:
        store = AsyncMock()
        store.read_feed = AsyncMock(return_value=[])
        request = _request(store, FakeEngine())
        resp = await web.web_feed(cast(Request, request), identity=_TEST_IDENTITY)
        assert resp.next_after is None
        assert resp.events == []

    @pytest.mark.asyncio
    async def test_partial_cursor_is_rejected(self) -> None:
        store = AsyncMock()
        request = _request(store, FakeEngine())
        # All three cursor parts must be given together.
        with pytest.raises(HTTPException):
            await web.web_feed(
                cast(Request, request),
                identity=_TEST_IDENTITY,
                after_created=datetime(2026, 6, 1, tzinfo=UTC),
            )

    @pytest.mark.asyncio
    async def test_rejects_bad_limit(self) -> None:
        store = AsyncMock()
        request = _request(store, FakeEngine())
        for bad in (0, 5000):
            with pytest.raises(HTTPException):
                await web.web_feed(
                    cast(Request, request),
                    identity=_TEST_IDENTITY,
                    limit=bad,
                )


class TestFeedReads:
    """The feed, its facets and its histogram take one set of repeatable filters."""

    @classmethod
    def _client(cls, store: AsyncMock) -> TestClient:
        app = FastAPI()
        app.state.engine = FakeEngine()
        app.state.store = store
        web.attach(app)
        app.dependency_overrides[current_user] = _viewer
        return TestClient(app)

    def test_each_filter_repeats_and_one_room_or_actor_still_filters(self) -> None:
        store = AsyncMock()
        store.read_feed = AsyncMock(return_value=[])
        client = self._client(store)

        def scope_of(*params: tuple[str, str]) -> object:
            response = client.get("/api/web/feed", params=params)
            assert response.status_code == 200, response.text
            return store.read_feed.call_args.kwargs["scope"]

        assert scope_of(
            ("actor", "a"),
            ("actor", "b"),
            ("room", "lab"),
            ("cli", "codex"),
            ("kind", "ToolCall"),
            ("kind", "UserMessage"),
        ) == FeedScope(
            actors=("a", "b"),
            rooms=("lab",),
            clis=("codex",),
            kinds=("ToolCall", "UserMessage"),
        )
        assert scope_of(("room", "lab"), ("actor", "eng")) == FeedScope(
            actors=("eng",),
            rooms=("lab",),
        )
        assert scope_of() == WHOLE_FEED

    def test_the_feed_keeps_only_conversation_when_asked(self) -> None:
        store = AsyncMock()
        store.read_feed = AsyncMock(return_value=[])
        client = self._client(store)

        for params, conversation in (({}, False), ({"conversation": "true"}, True)):
            response = client.get("/api/web/feed", params=params)
            assert response.status_code == 200, response.text
            assert store.read_feed.call_args.kwargs["conversation"] is conversation

    def test_facets_count_a_window_under_the_filters(self) -> None:
        store = AsyncMock()
        store.read_feed_facets = AsyncMock(
            return_value=FeedFacetsResponse(actors=[], rooms=[], kinds=[]),
        )
        client = self._client(store)
        window = (("since", "2026-10-01T00:00:00Z"), ("until", "2026-10-02T00:00:00Z"))

        response = client.get(
            "/api/web/feed/facets",
            params=(*window, ("cli", "codex")),
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"actors": [], "rooms": [], "kinds": []}
        assert store.read_feed_facets.call_args.kwargs == {
            "since": datetime(2026, 10, 1, tzinfo=UTC),
            "until": datetime(2026, 10, 2, tzinfo=UTC),
            "scope": FeedScope(clis=("codex",)),
        }
        backwards = (("since", window[1][1]), ("until", window[0][1]))
        assert client.get("/api/web/feed/facets", params=backwards).status_code == 400

    def test_a_histogram_has_2_to_1000_buckets_and_120_unless_asked(self) -> None:
        store = self._histogram_store()
        client = self._client(store)

        response = client.get("/api/web/feed/histogram", params={"kind": "ToolCall"})

        assert response.status_code == 200, response.text
        assert convert(response.json(), dict[str, object])["counts"] == [
            {"start": "2026-10-01T00:00:00Z", "count": 3},
        ]
        kwargs = store.read_feed_histogram.call_args.kwargs
        assert {name: kwargs[name] for name in kwargs if name != "earliest"} == {
            "since": None,
            "until": None,
            "buckets": 120,
            "scope": FeedScope(kinds=("ToolCall",)),
        }
        for buckets, status in ((1, 400), (2, 200), (1_000, 200), (1_001, 400)):
            response = client.get(
                "/api/web/feed/histogram",
                params={"buckets": buckets},
            )
            assert response.status_code == status, buckets
        day = timedelta(days=1)
        backwards = {
            "since": (datetime.now(UTC) - day).isoformat(),
            "until": (datetime.now(UTC) - 2 * day).isoformat(),
        }
        response = client.get("/api/web/feed/histogram", params=backwards)
        assert response.status_code == 400

    def test_a_histogram_counts_at_most_the_last_week(self) -> None:
        """An earlier ``since`` starts a week back; an ``until`` before that is 400."""
        store = self._histogram_store()
        client = self._client(store)
        week = timedelta(days=7)
        month_ago = (datetime.now(UTC) - 4 * week).isoformat()

        before = datetime.now(UTC)
        response = client.get("/api/web/feed/histogram", params={"since": month_ago})
        after = datetime.now(UTC)

        assert response.status_code == 200, response.text
        earliest = store.read_feed_histogram.call_args.kwargs["earliest"]
        assert isinstance(earliest, datetime)
        assert before - week <= earliest <= after - week
        for days, status in ((8, 400), (6, 200)):
            until = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            response = client.get(
                "/api/web/feed/histogram",
                params={"since": month_ago, "until": until},
            )
            assert response.status_code == status, days

    @classmethod
    def _histogram_store(cls) -> AsyncMock:
        """Return a store whose histogram read answers one bucket."""
        store = AsyncMock()
        store.read_feed_histogram = AsyncMock(
            return_value=FeedHistogramResponse(
                start=datetime(2026, 10, 1, tzinfo=UTC),
                end=datetime(2026, 10, 2, tzinfo=UTC),
                bucket_seconds=86_400,
                counts=[FeedBucket(start=datetime(2026, 10, 1, tzinfo=UTC), count=3)],
            ),
        )
        return store


class TestRouteBounds:
    @classmethod
    def _client(cls, app: FastAPI) -> TestClient:
        """Wire a viewer-role override so the route reaches its own checks."""

        async def _identity() -> AuthIdentity:
            return _TEST_IDENTITY

        app.dependency_overrides[current_user] = _identity
        return TestClient(app)

    def test_search_rejects_bad_limit(self) -> None:
        engine = FakeEngine()
        store = AsyncMock()
        app = FastAPI()
        app.state.engine = engine
        app.state.store = store
        web.attach(app)
        c = self._client(app)
        r = c.get("/api/web/search", params={"q": "x", "limit": 0})
        assert r.status_code == 400
        r = c.get("/api/web/search", params={"q": "x", "limit": 5000})
        assert r.status_code == 400

    def test_recent_changes_rejects_bad_limit(self) -> None:
        engine = FakeEngine()
        store = AsyncMock()
        app = FastAPI()
        app.state.engine = engine
        app.state.store = store
        web.attach(app)
        c = self._client(app)
        r = c.get("/api/web/recent_changes", params={"limit": 5000})
        assert r.status_code == 400

    def test_search_rejects_invalid_regex(self) -> None:
        engine = FakeEngine()
        store = AsyncMock()
        app = FastAPI()
        app.state.engine = engine
        app.state.store = store
        web.attach(app)
        c = self._client(app)
        # ``[unclosed`` is a bad regex; ``_build_term_clause`` rejects
        # it via Python ``re.compile`` before Postgres ever sees it.
        r = c.get("/api/web/search", params={"q": "title:[unclosed"})
        assert r.status_code == 400

    def test_search_rejects_unterminated_quote(self) -> None:
        engine = FakeEngine()
        store = AsyncMock()
        app = FastAPI()
        app.state.engine = engine
        app.state.store = store
        web.attach(app)
        c = self._client(app)
        # Unbalanced quote: ``shlex.split`` raises; route surfaces 400
        # instead of silently falling back to ``str.split`` which would
        # return wrong matches.
        r = c.get("/api/web/search", params={"q": 'title:"unclosed'})
        assert r.status_code == 400
        body = convert(r.json(), dict[str, object])
        detail = body["detail"]
        assert isinstance(detail, str)
        assert "quot" in detail

    def test_search_rejects_empty_field_value(self) -> None:
        engine = FakeEngine()
        store = AsyncMock()
        app = FastAPI()
        app.state.engine = engine
        app.state.store = store
        web.attach(app)
        c = self._client(app)
        # ``title:`` (no pattern) used to silently degrade to a bare
        # token search for ``%title:%``; now it is a 400.
        r = c.get("/api/web/search", params={"q": "title:"})
        assert r.status_code == 400
        body = convert(r.json(), dict[str, object])
        detail = body["detail"]
        assert isinstance(detail, str)
        assert "empty" in detail

    def test_search_returns_only_the_named_fields(self) -> None:
        # Each hit is a whole row, description included; the palette shows a
        # handful of its keys. A name no row carries is a 400, and another
        # kind's field is absent, as on ``GET /api/inquiries``.
        engine = FakeEngine()
        engine.conn.fetch = AsyncMock(return_value=[_inquiry_row()])
        app = FastAPI()
        app.state.engine = engine
        app.state.store = _Store(engine=engine)
        web.attach(app)
        c = self._client(app)
        query = [("q", "title"), ("kind", "Issue")]
        r = c.get(
            "/api/web/search",
            params=[
                *query,
                ("fields", "id"),
                ("fields", "title"),
                ("fields", "judgement"),
            ],
        )
        assert r.status_code == 200, r.text
        assert [set(hit) for hit in convert(r.json(), list[dict[str, object]])] == [
            {"id", "title"},
        ]
        r = c.get("/api/web/search", params=[*query, ("fields", "bogus")])
        assert r.status_code == 400, r.text
        assert "'bogus'" in r.text
        r = c.get(
            "/api/web/search",
            params=[*query, *(("fields", "id") for _ in range(1_001))],
        )
        assert r.status_code == 422, r.text


# ---- The sign-in page -----------------------------------------------------


def _admin_identity() -> AuthIdentity:
    return AuthIdentity(
        user_id=uuid.UUID("44444444-4444-4444-4444-444444444444"),
        api_key_id=None,
        email="admin@example.com",
        role="admin",
    )


def _viewer_identity() -> AuthIdentity:
    return AuthIdentity(
        user_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
        api_key_id=None,
        email="viewer@example.com",
        role="viewer",
    )


def _install_identity(app: FastAPI, identity: AuthIdentity | None) -> None:
    """Override ``optional_identity`` so the page routes see ``identity``."""

    async def _override() -> AuthIdentity | None:
        return identity

    app.dependency_overrides[web.optional_identity] = _override


class TestLoginPage:
    """The sign-in page as it ships, served to a signed-out visitor."""

    def test_login_page_always_serves(self, tmp_path: Path) -> None:
        (tmp_path / "login.html").write_text("LOGIN-PAGE")
        app = FastAPI()
        web.attach(app, assets_dir=tmp_path)
        # No identity install; login page is the one route that must
        # never gate on auth -- otherwise users couldn't sign in.
        r = TestClient(app).get("/auth/login_page")
        assert r.status_code == 200
        assert r.text == "LOGIN-PAGE"

    def test_google_sign_in_waits_for_the_server_to_offer_it(self) -> None:
        # Without OAuth the button leads to a 503, or a 404 where OAuth is not
        # mounted. So it starts hidden, and the page shows it only when
        # ``/auth/login/ready`` answers 2xx: a script that never runs offers none.
        (google,) = (
            attrs
            for tag, attrs in _login_page_tags()
            if tag == "a" and (attrs.get("href") or "").startswith("/auth/login")
        )
        assert "hidden" in google

    def test_loads_nothing_a_signed_out_visitor_cannot(self) -> None:
        # The app's own files answer 401 to the visitor this page is for, and a
        # server without ``--app-dir`` has none, so the page carries its own look.
        urls = [
            value
            for _, attrs in _login_page_tags()
            for name, value in attrs.items()
            if name in {"href", "src"} and value
        ]
        assert urls
        assert all(url.startswith(("data:", "/auth/")) for url in urls), urls


# ---- A separately built web app at /app/ ----------------------------------


def _write_build(root: Path, *, marker: str) -> Path:
    """Write a minimal built web app (entry page plus one asset) at ``root``."""
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(f"INDEX-{marker}")
    (root / "assets" / "app.js").write_text(f"// {marker}")
    return root


def _build_pages_and_app(tmp_path: Path) -> FastAPI:
    """Build a FastAPI with the sign-in page and an app build."""
    assets = tmp_path / "pages"
    assets.mkdir()
    (assets / "login.html").write_text("LOGIN")
    app = FastAPI()
    build = _write_build(tmp_path / "b", marker="a")
    web.attach(app, assets_dir=assets, app_dir=build)
    return app


def _build_app_dir_app(tmp_path: Path, app_dir: Path) -> FastAPI:
    """Build a fresh FastAPI serving ``app_dir`` at ``/app/``, no sign-in page."""
    assets = tmp_path / "no-pages"
    assets.mkdir()
    app = FastAPI()
    web.attach(app, assets_dir=assets, app_dir=app_dir)
    return app


class TestAppDir:
    def test_signed_out_page_redirects_to_login_with_next(
        self,
        tmp_path: Path,
    ) -> None:
        # The login page's ``next`` is the request, query included, so a
        # signed-out visitor lands back on the app after signing in.
        app = _build_app_dir_app(tmp_path, _write_build(tmp_path / "b", marker="a"))
        _install_identity(app, None)
        client = TestClient(app, follow_redirects=False)
        for path in ("/app/", "/app/index.html?tab=x"):
            r = client.get(path)
            assert r.status_code == 302, path
            location = urlparse(r.headers["location"])
            assert location.path == "/auth/login_page"
            assert parse_qs(location.query)["next"] == [path]

    def test_signed_out_asset_is_401(self, tmp_path: Path) -> None:
        # A script or stylesheet cannot follow a redirect to the login page,
        # so it gets 401. A missing file answers the same, which keeps a
        # signed-out caller from probing what a build contains.
        app = _build_app_dir_app(tmp_path, _write_build(tmp_path / "b", marker="a"))
        _install_identity(app, None)
        client = TestClient(app, follow_redirects=False)
        for path in ("/app/assets/app.js", "/app/assets/missing.js"):
            r = client.get(path)
            assert r.status_code == 401, path
            assert "// a" not in r.text

    @pytest.mark.parametrize(
        ("config", "answers"),
        [
            pytest.param(
                Config(),
                (401, 302, "/auth/login_page?next=%2Fapp%2F", 401),
                id="default",
            ),
            pytest.param(
                Config(auth_disabled=True),
                (200, 200, None, 200),
                id="no-auth",
            ),
            # Google sign-in adds routes, not a rule: neither the API nor the app
            # reads its settings, so this stands for a server with it too.
            pytest.param(
                Config(session_secret=uuid.uuid4().hex),
                (401, 302, "/auth/login_page?next=%2Fapp%2F", 401),
                id="session-login",
            ),
        ],
    )
    def test_app_lets_in_whoever_the_api_does(
        self,
        tmp_path: Path,
        config: Config,
        answers: tuple[int, int, str | None, int],
    ) -> None:
        # The app's source is public, so ``/app/`` keeps nothing back on its own
        # account: an anonymous caller gets it where the API answers them and is
        # refused where the API refuses them, whatever the mode. A browser opens
        # the entry page, so its refusal is the sign-in page.
        app = _build_app_dir_app(tmp_path, _write_build(tmp_path / "b", marker="a"))
        app.state.config = config
        app.state.store, app.state.engine = make_store()
        app.include_router(auth_routes.router)
        client = TestClient(app, follow_redirects=False)
        page = client.get("/app/")
        assert (
            client.get("/api/me/profile").status_code,
            page.status_code,
            page.headers.get("location"),
            client.get("/app/assets/app.js").status_code,
        ) == answers

    def test_signed_in_viewer_loads_assets(self, tmp_path: Path) -> None:
        app = _build_app_dir_app(tmp_path, _write_build(tmp_path / "b", marker="a"))
        _install_identity(app, _viewer_identity())
        client = TestClient(app)
        r = client.get("/app/assets/app.js")
        assert r.status_code == 200
        assert r.text == "// a"

    def test_app_root_serves_index_html(self, tmp_path: Path) -> None:
        app = _build_app_dir_app(tmp_path, _write_build(tmp_path / "b", marker="a"))
        _install_identity(app, _viewer_identity())
        client = TestClient(app)
        for path in ("/app/", "/app/index.html", "/app"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert r.text == "INDEX-a"

    def test_missing_directory_answers_404_until_a_build_lands(
        self,
        tmp_path: Path,
    ) -> None:
        # A failed or pending build must never stop the server: a missing
        # directory answers 404, and a build that lands later is served without
        # a restart. The flag's wiring is tested in ``server_test.py``.
        build = tmp_path / "build"
        app = _build_app_dir_app(tmp_path, build)
        _install_identity(app, _viewer_identity())
        client = TestClient(app)
        assert client.get("/app/").status_code == 404
        _write_build(build, marker="late")
        r = client.get("/app/")
        assert r.status_code == 200
        assert r.text == "INDEX-late"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 directory")
    def test_unreadable_build_is_a_server_error_not_signed_out(
        self,
        tmp_path: Path,
    ) -> None:
        # Starlette answers 401 when it cannot read a file. For a signed-in
        # caller that would read as "signed out"; a deploy that wrote the build
        # with the wrong permissions is the server's fault.
        build = _write_build(tmp_path / "b", marker="a")
        (build / "assets").chmod(0o000)
        try:
            app = _build_app_dir_app(tmp_path, build)
            _install_identity(app, _viewer_identity())
            r = TestClient(app).get("/app/assets/app.js")
        finally:
            (build / "assets").chmod(0o755)
        assert r.status_code == 500
        assert r.headers.get("cache-control") == "private, no-cache"

    def test_swapped_symlink_serves_the_new_build_without_restart(
        self,
        tmp_path: Path,
    ) -> None:
        # A deploy points a ``current`` symlink at a new build with an atomic
        # rename; the next request must see it.
        old = _write_build(tmp_path / "builds" / "old", marker="old")
        new = _write_build(tmp_path / "builds" / "new", marker="new")
        current = tmp_path / "current"
        current.symlink_to(old)
        app = _build_app_dir_app(tmp_path, current)
        _install_identity(app, _viewer_identity())
        client = TestClient(app)
        assert client.get("/app/").text == "INDEX-old"
        staged = tmp_path / "current.next"
        staged.symlink_to(new)
        staged.replace(current)
        assert client.get("/app/").text == "INDEX-new"
        assert client.get("/app/assets/app.js").text == "// new"

    def test_path_traversal_is_refused(self, tmp_path: Path) -> None:
        build = _write_build(tmp_path / "b", marker="a")
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET")
        (build / "escape.txt").symlink_to(secret)
        app = _build_app_dir_app(tmp_path, build)
        _install_identity(app, _viewer_identity())
        client = TestClient(app)
        for path in (
            "/app/%2e%2e/secret.txt",
            "/app/..%2fsecret.txt",
            "/app/assets/..%2f..%2fsecret.txt",
            f"/app/{secret}",
            "/app/escape.txt",
        ):
            r = client.get(path)
            assert r.status_code == 404, path
            assert "SECRET" not in r.text

    def test_static_stays_public_beside_the_app(self, tmp_path: Path) -> None:
        static = tmp_path / "static"
        static.mkdir()
        (static / "report.html").write_text("REPORT")
        app = FastAPI()
        web.attach(
            app,
            assets_dir=tmp_path,
            static_dir=static,
            app_dir=_write_build(tmp_path / "b", marker="a"),
        )
        _install_identity(app, None)
        client = TestClient(app)
        r = client.get("/static/report.html")
        assert r.status_code == 200
        assert r.text == "REPORT"
        assert client.get("/app/assets/app.js").status_code == 401

    def test_every_response_forbids_shared_caches(self, tmp_path: Path) -> None:
        # A shared cache in front of the server (Cloudflare stores ``.js`` by
        # extension there, 404s included) must never store an /app/ response:
        # a signed-in user's copy would be served to anyone, and a cached 401 or
        # 404 would break the app for everyone. ``private`` keeps shared caches
        # out. A file under ``assets/`` is named by its content's hash, so the
        # browser keeps it for good and a repeat visit asks for none of them;
        # anything else is revalidated (``no-cache``), so a deploy's new entry
        # page is seen at once.
        build = _write_build(tmp_path / "b", marker="a")
        (build / "favicon.svg").write_text("<svg/>")
        app = _build_app_dir_app(tmp_path, build)
        immutable = "private, max-age=31536000, immutable"
        revalidated = "private, no-cache"
        cases: tuple[tuple[AuthIdentity | None, str, int, str], ...] = (
            (_viewer_identity(), "/app/", 200, revalidated),
            (_viewer_identity(), "/app/favicon.svg", 200, revalidated),
            (_viewer_identity(), "/app/assets/app.js", 200, immutable),
            (_viewer_identity(), "/app/assets/missing.js", 404, revalidated),
            (None, "/app/", 302, revalidated),
            (None, "/app/assets/app.js", 401, revalidated),
        )
        for identity, path, status, cache_control in cases:
            _install_identity(app, identity)
            r = TestClient(app, follow_redirects=False).get(path)
            assert r.status_code == status, path
            assert r.headers.get("cache-control") == cache_control, path
        # A 304's headers replace the stored ones, so a revalidated asset must
        # say ``immutable`` too, or one cached before this rule stays
        # ``no-cache`` for as long as its hash does not change.
        _install_identity(app, _viewer_identity())
        client = TestClient(app)
        etag = client.get("/app/assets/app.js").headers["etag"]
        r = client.get("/app/assets/app.js", headers={"if-none-match": etag})
        assert r.status_code == 304
        assert r.headers.get("cache-control") == immutable

    def test_app_is_off_unless_a_directory_is_given(self, tmp_path: Path) -> None:
        app = FastAPI()
        web.attach(app, assets_dir=tmp_path)
        assert not any(path.startswith("/app") for path in registered_paths(app))


# ---- The old UI's paths, once the app is served ---------------------------


# Each old page's path and where the app shows the same thing. Stored links point at
# ``/`` with a hash naming the view, which the browser keeps across a redirect whose
# ``Location`` has none, and the app's router reads v1's hashes; a ``Location`` that
# has a hash replaces the request's.
_OLD_PATHS: Final = (
    ("/", "/app/"),
    ("/me", "/app/#/settings"),
    ("/admin", "/app/#/admin"),
    ("/graph", "/app/#/graph"),
    ("/console", "/app/#/console"),
)


class TestOldPathsWithTheApp:
    @pytest.mark.parametrize(
        "identity",
        [None, _viewer_identity(), _admin_identity()],
        ids=["signed-out", "viewer", "admin"],
    )
    def test_each_old_path_redirects_into_the_app(
        self,
        tmp_path: Path,
        identity: AuthIdentity | None,
    ) -> None:
        # With the app served, each of the old UI's paths answers 302, signed in
        # or not, and ``/app/`` decides sign-in. 302 rather than 301, which a
        # browser keeps for good; ``no-cache`` as on ``/app/``.
        app = _build_pages_and_app(tmp_path)
        _install_identity(app, identity)
        client = TestClient(app, follow_redirects=False)
        for path, location in _OLD_PATHS:
            r = client.get(path)
            assert (r.status_code, r.headers.get("location")) == (302, location), path
            assert r.headers.get("cache-control") == "private, no-cache", path

    def test_legacy_links_reach_the_app_or_the_login_page(
        self,
        tmp_path: Path,
    ) -> None:
        # The server never sees a link's hash: ``#/ref/Issue/7`` stays in the
        # browser for the app's router. So each link walks to the app signed
        # in, and to the login page signed out, whose ``next`` is the app.
        app = _build_pages_and_app(tmp_path)
        links = (
            "/#/ref/Issue/7",
            f"/#/inquiry/{new_uuid()}",
            "/#/recent",
            "/#/search?q=x",
            "/me",
            "/admin",
            "/graph",
            "/console",
        )
        walks: tuple[
            tuple[AuthIdentity | None, tuple[str, dict[str, list[str]]], str],
            ...,
        ] = (
            (_viewer_identity(), ("/app/", {}), "INDEX-a"),
            (None, ("/auth/login_page", {"next": ["/app/"]}), "LOGIN"),
        )
        for identity, landing, page in walks:
            _install_identity(app, identity)
            client = TestClient(app)
            for link in links:
                r = client.get(link)
                assert r.status_code == 200, link
                assert (r.url.path, parse_qs(r.url.query.decode())) == landing, link
                assert r.text == page, link


# ---- Read routes on a real engine ----------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def pglite_store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    yield store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_self_is_the_inquiry_route_on_a_real_engine(pglite_store: Store) -> None:
    """Every kind, bare and filled, reads the same through both routes (R17).

    asyncpg decodes each column type its own way, so the agreement is checked
    here as well as against the mock rows.
    """
    account = "alice@example.com"
    started = datetime(2026, 5, 18, 9, tzinfo=UTC)
    ids = await pglite_store.submit_batch(
        [
            SubmitIssue(account=account, title="bare"),
            SubmitArtifact(account=account, title="bare"),
            SubmitExperiment(account=account, title="bare"),
            SubmitPaper(account=account, title="bare"),
            SubmitBelief(account=account, title="bare"),
            SubmitCodeChange(account=account, title="bare"),
            SubmitWebResult(account=account, title="bare"),
            SubmitWebSearch(account=account, title="bare"),
            SubmitAgentSession(account=account, title="bare"),
            SubmitIssue(
                account=account,
                title="filled",
                owner="bob",
                description="d",
                labels=["x"],
                issue_kind=["bug"],
                priority=10,
            ),
            SubmitExperiment(
                account=account,
                title="filled",
                outcome="ok",
                config={"lr": 0.1, "nested": {"warmup": 100}},
            ),
            SubmitPaper(
                account=account,
                title="filled",
                authors=["a", "b"],
                publish_date=started,
                source="doi:10.1/x",
            ),
            SubmitAgentSession(
                account=account,
                title="filled",
                cli="claude",
                started=started,
                rooms=["lab"],
            ),
        ],
    )
    request = cast(Request, _request(pglite_store, pglite_store.engine))
    for target_id in ids:
        canonical = await get_inquiry_route(
            target_id,
            request,
            identity=_TEST_IDENTITY,
        )
        detail = await web.web_get(target_id, request, identity=_TEST_IDENTITY)
        assert detail["self"] == {
            key: value for key, value in canonical.items() if key not in _EDGE_RELATIONS
        }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_peers_carry_their_own_created_and_priority_on_a_real_engine(
    pglite_store: Store,
) -> None:
    """Each peer carries its own ``created`` and priority beside the edge's.

    The timeline places a neighbour by when it was created, and a parent's list
    shows a child's own priority where the edge sets none (COLD-17). Neither
    reached the peer refs, so each cost a request per neighbour.
    """
    account = "alice@example.com"
    parent = await pglite_store.submit_issue(
        SubmitIssue(account=account, title="parent", priority=10),
    )
    child = await pglite_store.submit_issue(SubmitIssue(account=account, title="child"))
    await pglite_store.add_edge(
        from_id=child,
        to_id=parent,
        edge_kind="narrows",
        actor="alice",
    )
    request = cast(Request, _request(pglite_store, pglite_store.engine))
    parent_view = await web.web_get(parent, request, identity=_TEST_IDENTITY)
    child_view = await web.web_get(child, request, identity=_TEST_IDENTITY)
    (child_ref,) = convert(
        convert(parent_view["backlinks"], dict[str, object])["narrows"],
        list[object],
    )
    (parent_ref,) = convert(
        convert(child_view["edges"], dict[str, object])["narrows"],
        list[object],
    )
    child_ref, parent_ref = (
        convert(child_ref, dict[str, object]),
        convert(parent_ref, dict[str, object]),
    )
    assert (
        parent_ref["peer_created"]
        == convert(parent_view["self"], dict[str, object])["created"]
    )
    assert parent_ref["peer_priority"] == 10
    assert (
        child_ref["peer_created"]
        == convert(child_view["self"], dict[str, object])["created"]
    )
    # Neither the child nor the edge has a priority, so the ref carries neither.
    assert "peer_priority" not in child_ref
    assert "priority" not in child_ref


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_web_get_breaks_change_time_ties_by_id_on_a_real_engine(
    pglite_store: Store,
) -> None:
    """Changes stamped with one ``created`` come back in id order (R21)."""
    target_id = await pglite_store.submit_issue(
        SubmitIssue(account="alice@example.com", title="t0"),
    )
    for step in range(1, 5):
        await pglite_store.set_title(target_id, f"t{step}", actor="alice")
    async with pglite_store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE change_log SET created = $1 WHERE subject_id = $2",
            datetime(2026, 5, 18, tzinfo=UTC),
            target_id,
        )
    request = cast(Request, _request(pglite_store, pglite_store.engine))
    detail = await web.web_get(target_id, request, identity=_TEST_IDENTITY)
    change_ids = [
        convert(convert(change, dict[str, object])["id"], str)
        for change in convert(detail["changes"], list[object])
    ]
    assert len(change_ids) == 5
    assert change_ids == sorted(change_ids, reverse=True)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_search_keeps_backslashes_and_apostrophes_on_a_real_engine(
    pglite_store: Store,
) -> None:
    r"""``\d`` reaches Postgres as a digit class, and ``'`` is a letter (R30)."""
    for title in ("build 42 passes", "don't panic", "plain words"):
        await pglite_store.submit_issue(
            SubmitIssue(account="alice@example.com", title=title),
        )
    request = cast(Request, _request(pglite_store, pglite_store.engine))
    for q, titles in (
        (r"title:\d+", ["build 42 passes"]),
        (r'title:"\d+"', ["build 42 passes"]),
        ("don't", ["don't panic"]),
        ('title:"don\'t pan"', ["don't panic"]),
    ):
        rows = await web.web_search(request, q=q, identity=_TEST_IDENTITY)
        assert [row["title"] for row in rows] == titles, q


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_web_graph_returns_at_most_limit_nodes_on_a_real_engine(
    pglite_store: Store,
) -> None:
    """``limit`` bounds the nodes, neighbours included, and edges join them (R3-02).

    The newest ``limit`` nodes were closed over every neighbour, so a node linking
    to many returned them all: ``limit=1`` measured 5,002 nodes. Now the newest
    node comes first, followed by the older nodes it links to, until ``limit``.
    """
    account = "alice@example.com"
    titles = ("cited 0", "cited 1", "cited 2", "unlinked", "newest")
    ids = [
        await pglite_store.submit_issue(SubmitIssue(account=account, title=title))
        for title in titles
    ]
    c0, c1, c2, _, newest = ids
    # Links in both directions: ``c0`` points at ``newest``.
    for from_id, to_id in ((c0, newest), (newest, c1), (newest, c2)):
        await pglite_store.add_edge(
            from_id=from_id,
            to_id=to_id,
            edge_kind="requires",
            actor="alice",
        )
    # One ``created`` per node, in ``titles`` order, so no order below is a tie.
    async with pglite_store.engine.acquire() as conn:
        for minute, node in enumerate(ids):
            await conn.execute(
                "UPDATE inquiries SET created = $1 WHERE id = $2",
                datetime(2026, 5, 18, 0, minute, tzinfo=UTC),
                node,
            )
    request = cast(Request, _request(pglite_store, pglite_store.engine))
    # Each first link also stamps an inferred ``produced_by`` edge, so the
    # expected edges come from the whole graph.
    whole = _edge_ids(
        await web.web_graph(request, identity=_TEST_IDENTITY, limit=5_000),
    )
    assert (str(c0), str(newest)) in whole
    for limit, kept in (
        (1, [newest]),
        (2, [c2, newest]),
        # The three nodes ``newest`` links to outrank ``unlinked``, a newer node.
        (4, [c0, c1, c2, newest]),
        (5_000, ids),
    ):
        graph = await web.web_graph(request, identity=_TEST_IDENTITY, limit=limit)
        nodes = [
            convert(n, dict[str, object])["id"]
            for n in convert(graph["nodes"], list[object])
        ]
        assert nodes == [str(n) for n in kept], limit
        assert _edge_ids(graph) == {
            (a, b) for a, b in whole if a in nodes and b in nodes
        }, limit


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_web_graph_draws_a_focus_neighbourhood_on_a_real_engine(
    pglite_store: Store,
) -> None:
    """``focus`` keeps its nearest nodes, each with its ``hops``, and their edges.

    ``far -> near -> focus <- near_2 <- far_2``, and ``far -> far_2`` joins the two
    hop-2 nodes. ``limit=4`` keeps ``far_2``, the newer of them, and so drops both
    of ``far``'s edges.
    """
    account = "alice@example.com"
    titles = ("far", "near", "focus", "near 2", "far 2")
    ids = [
        await pglite_store.submit_issue(SubmitIssue(account=account, title=title))
        for title in titles
    ]
    far, near, focus, near_2, far_2 = ids
    for from_id, to_id in (
        (near, focus),
        (near_2, focus),
        (far, near),
        (far_2, near_2),
        (far, far_2),
    ):
        await pglite_store.add_edge(
            from_id=from_id,
            to_id=to_id,
            edge_kind="requires",
            actor="alice",
        )
    async with pglite_store.engine.acquire() as conn:
        for minute, node in enumerate(ids):
            await conn.execute(
                "UPDATE inquiries SET created = $1 WHERE id = $2",
                datetime(2026, 5, 18, 0, minute, tzinfo=UTC),
                node,
            )
    request = cast(Request, _request(pglite_store, pglite_store.engine))
    whole = _edge_ids(await web.web_graph(request, identity=_TEST_IDENTITY))
    assert (str(far), str(far_2)) in whole
    for limit, hops, kept in (
        (None, 2, {far: 2, near: 1, focus: 0, near_2: 1, far_2: 2}),
        (4, 2, {near: 1, focus: 0, near_2: 1, far_2: 2}),
        (None, 1, {near: 1, focus: 0, near_2: 1}),
    ):
        graph = await web.web_graph(
            request,
            identity=_TEST_IDENTITY,
            limit=limit,
            focus=focus,
            hops=hops,
        )
        nodes = [
            convert(n, dict[str, object]) for n in convert(graph["nodes"], list[object])
        ]
        # Oldest first, as without a focus.
        assert [(n["id"], n["hops"]) for n in nodes] == [
            (str(node), distance) for node, distance in kept.items()
        ], (limit, hops)
        kept_ids = {str(node) for node in kept}
        assert _edge_ids(graph) == {
            (a, b) for a, b in whole if a in kept_ids and b in kept_ids
        }, (limit, hops)
    for target, hops, status in (
        (uuid.uuid4(), 2, 404),
        (focus, 0, 400),
        (focus, 4, 400),
    ):
        with pytest.raises(HTTPException) as caught:
            await web.web_graph(
                request,
                identity=_TEST_IDENTITY,
                focus=target,
                hops=hops,
            )
        assert caught.value.status_code == status, (target, hops)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_web_graph_draws_60_nodes_round_a_focus_and_1000_without(
    pglite_store: Store,
) -> None:
    hub = await pglite_store.submit_issue(
        SubmitIssue(account="alice@example.com", title="hub"),
    )
    async with pglite_store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, account, title)"
            " SELECT gen_random_uuid(), 'Issue', 1000 + n, 'alice@example.com', 'leaf'"
            " FROM generate_series(1, 70) AS n",
        )
        await conn.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind)"
            " SELECT id, 'Issue', $1, 'Issue', 'narrows' FROM inquiries"
            " WHERE title = 'leaf'",
            hub,
        )
    request = cast(Request, _request(pglite_store, pglite_store.engine))
    around = await web.web_graph(request, identity=_TEST_IDENTITY, focus=hub)
    assert len(convert(around["nodes"], list[object])) == 60
    whole = convert(
        (await web.web_graph(request, identity=_TEST_IDENTITY))["nodes"],
        list[dict[str, object]],
    )
    assert len(whole) == 71
    # Without a focus there is no distance to give.
    assert not any("hops" in node for node in whole)


class TestSubscribeProbe:
    """The stream probe sends frames on the caller's schedule, then ends cleanly."""

    @classmethod
    def _get(
        cls,
        **params: float | bool,
    ) -> tuple[int, dict[str, str], list[dict[str, object]]]:
        """Stream the probe; return its status, headers and parsed frames."""
        app = FastAPI()
        app.state.engine = FakeEngine()
        app.state.store = AsyncMock()
        web.attach(app)
        app.dependency_overrides[current_user] = _viewer
        with TestClient(app).stream(
            "GET",
            "/api/web/subscribe/probe",
            params=params,
        ) as r:
            body = b"".join(r.iter_bytes())
            status, headers = r.status_code, dict(r.headers)
        if status != 200:
            return status, headers, []
        frames = [
            parse(frame.removeprefix(b"data: "), dict[str, object])
            for frame in body.split(b"\n\n")[:-1]
        ]
        return status, headers, frames

    def test_frames_follow_the_schedule_then_the_stream_ends(self) -> None:
        status, headers, frames = self._get(
            first_after_sec=0.02,
            every_sec=0.02,
            for_sec=0.09,
        )
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert "no-transform" not in headers.get("cache-control", "")
        # Frames at 0.02, 0.04, 0.06 and 0.08 s; none at or after for_sec.
        assert [convert(f["seq"], int) for f in frames] == [0, 1, 2, 3]
        elapsed = [convert(f["t"], float) for f in frames]
        assert elapsed[0] >= 0.02
        assert elapsed == sorted(elapsed)
        assert elapsed[-1] < 0.09

    def test_one_frame_without_an_interval(self) -> None:
        # One byte, then silence until for_sec: the idle-cut experiment.
        _, _, frames = self._get(first_after_sec=0, for_sec=0.05)
        assert [convert(f["seq"], int) for f in frames] == [0]

    def test_no_bytes_when_the_first_is_due_after_the_end(self) -> None:
        # Headers only: the experiment for a proxy that holds them.
        status, _, frames = self._get(first_after_sec=0.05, for_sec=0.05)
        assert status == 200
        assert frames == []

    def test_forbid_transform_marks_the_response(self) -> None:
        _, headers, _ = self._get(for_sec=0.01, forbid_transform=True)
        assert "no-transform" in headers["cache-control"]

    @pytest.mark.parametrize(
        "params",
        [
            {"first_after_sec": -1},
            {"every_sec": -0.5},
            {"for_sec": 0},
            {"for_sec": 601},
            {"first_after_sec": float("nan")},
        ],
    )
    def test_out_of_range_schedules_are_refused(
        self,
        params: dict[str, float],
    ) -> None:
        status, _, _ = self._get(**params)
        assert status == 400


async def _viewer() -> AuthIdentity:
    return _TEST_IDENTITY


def _edge_ids(graph: web.WebView) -> set[tuple[object, object]]:
    """Return the ``(from_id, to_id)`` of every edge in a ``/graph`` response."""
    return {
        (edge["from_id"], edge["to_id"])
        for edge in (
            convert(e, dict[str, object]) for e in convert(graph["edges"], list[object])
        )
    }


def _login_page_tags() -> list[tuple[str, dict[str, str | None]]]:
    """Fetch the shipped login page; return its start tags and their attributes."""
    app = FastAPI()
    web.attach(app)
    response = TestClient(app).get("/auth/login_page")
    assert response.status_code == 200
    parser = _StartTags()
    parser.feed(response.text)
    return parser.tags


class _StartTags(HTMLParser):
    """Collect every start tag with its attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
