"""Tests for trackinizer web helpers and routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast, get_args
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
from trackinizer.lib.custom_json import DictCodec, ListCodec, StrCodec
from trackinizer.lib.postgres import Conn
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server import web
from trackinizer.server.api.query import get_inquiry_route
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.route_iter import (
    iter_routes,
    registered_paths,
)
from trackinizer.server.store.core import Store
from trackinizer.types.columns import column_specs, storage_name
from trackinizer.types.edges import Edge
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
from trackinizer.wire.wire_sessions import FeedEvent


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


class TestTimestampAssets:
    def test_browser_timestamp_formatters_use_local_time(self) -> None:
        root = _CWD / "assets"
        for name in ("index.html", "admin.html", "me.html"):
            text = (root / name).read_text()
            assert "new Date(iso)" in text
            assert "getFullYear()" in text
            assert ".toISOString()" not in text
            assert 'replace("T", " ").substring(0, 19)' not in text


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


class TestGraphLegend:
    def test_legend_kinds_match_domain_enums(self) -> None:
        # The graph view colors nodes by inquiry kind and edges by edge kind.
        # ``graph_legend`` is the single source the SPA reads, pinned here
        # against the domain enums so a new Inquiry subclass or Edge.Kind
        # cannot silently leave the legend stale (the SPA would render an
        # uncolored node/edge with no key entry).
        legend = web.graph_legend()
        assert set(legend["node_kinds"]) == set(get_args(Inquiry.InquiryKind))
        assert set(legend["edge_kinds"]) == set(get_args(Edge.Kind))


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
                [{"id": child_id}, {"id": root_id}],
                edge_rows,
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
        node_sql = engine.conn.fetch.call_args_list[2].args[0]
        assert isinstance(node_sql, str)
        assert "ORDER BY created ASC" in node_sql

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
                [{"id": a}],
                [
                    {
                        "from_id": a,
                        "to_id": b,
                        "edge_kind": "narrows",
                        "valence": None,
                    },
                ],
                [],
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
    async def test_web_graph_limit_edge_closes(self) -> None:
        # The limited path keeps the recent N nodes AND edge-closes them: an
        # OLDER node referenced by an edge to a recent node is pulled back in,
        # so no edge dangles and a still-cited old node stays visible.
        recent_id, old_id = new_uuid(), new_uuid()
        early = datetime(2026, 5, 18, 8, tzinfo=UTC)
        late = datetime(2026, 5, 18, 9, tzinfo=UTC)
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        engine.conn.fetch = AsyncMock(
            side_effect=[
                # 1. recent ids (only the new node fits the limit)
                [{"id": recent_id}],
                # 2. edges touching the recent node -> reaches the old node.
                [
                    {
                        "from_id": recent_id,
                        "to_id": old_id,
                        "edge_kind": "proves",
                        "valence": 0.5,
                    },
                ],
                # 3. full rows for the closed set (old + recent), created ASC.
                [
                    {
                        "id": old_id,
                        "kind": "Paper",
                        "seq": 1,
                        "title": "foundational",
                        "status": "complete",
                        "created": early,
                        "belief_judgement": None,
                        "belief_confidence": None,
                    },
                    {
                        "id": recent_id,
                        "kind": "Belief",
                        "seq": 2,
                        "title": "new claim",
                        "status": "active",
                        "created": late,
                        "belief_judgement": "proven",
                        "belief_confidence": 0.9,
                    },
                ],
            ],
        )
        graph = await web.web_graph(
            cast(Request, request),
            identity=_TEST_IDENTITY,
            limit=1,
        )
        nodes = cast(list[dict[str, object]], graph["nodes"])
        # The older referenced Paper is pulled in alongside the recent Belief.
        assert {n["id"] for n in nodes} == {str(old_id), str(recent_id)}
        # The recent-id query carried the limit; the edge query closed the set.
        recent_sql = engine.conn.fetch.call_args_list[0].args[0]
        assert isinstance(recent_sql, str)
        assert "ORDER BY created DESC" in recent_sql
        assert "LIMIT" in recent_sql
        edge_sql = engine.conn.fetch.call_args_list[1].args[0]
        assert isinstance(edge_sql, str)
        assert "from_id = ANY" in edge_sql
        assert "to_id = ANY" in edge_sql

    @pytest.mark.asyncio
    async def test_web_graph_caps_limit_at_5000(self) -> None:
        # ``limit=0`` served the whole graph to any viewer: 92k nodes and 29 MB
        # in 8.8 s on production (S11). 5000 is the largest cap ``graph.html``
        # offers.
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        engine.conn.fetch = AsyncMock(return_value=[])
        for bad in (-1, 0, 5_001):
            with pytest.raises(HTTPException) as caught:
                await web.web_graph(
                    cast(Request, request),
                    identity=_TEST_IDENTITY,
                    limit=bad,
                )
            assert caught.value.status_code == 400
        graph = await web.web_graph(
            cast(Request, request),
            identity=_TEST_IDENTITY,
            limit=5_000,
        )
        assert graph == {"nodes": [], "edges": []}

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
        assert b"".join(chunks) == f'data: {{"id": "{subject_id}"}}\n\n'.encode()

    def test_attach_mounts_routes_static_and_index(self, tmp_path: Path) -> None:
        assets = tmp_path
        (assets / "static").mkdir()
        (assets / "index.html").write_text("hello")
        app = FastAPI()
        web.attach(app, assets_dir=assets)
        client = TestClient(app)
        assert client.get("/").text == "hello"
        assert "/api/web/search" in registered_paths(app)
        assert "/api/web/graph" in registered_paths(app)

    def test_attach_without_files_mounts_router_only(self, tmp_path: Path) -> None:
        app = FastAPI()
        web.attach(app, assets_dir=tmp_path)
        assert "/api/web/search" in registered_paths(app)

    def test_static_dir_overrides_the_bundled_static_mount(
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

    def test_static_dir_unset_keeps_bundled_assets_static(self, tmp_path: Path) -> None:
        # Backward compatible: with no override, /static serves assets/static.
        assets = tmp_path
        (assets / "static").mkdir()
        (assets / "static" / "app.js").write_text("// bundled")
        app = FastAPI()
        web.attach(app, assets_dir=assets)
        client = TestClient(app)
        response = client.get("/static/app.js")
        assert response.status_code == 200
        assert response.text == "// bundled"

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
        assert chunks == [f'data: {{"id": "{good_id}"}}\n\n'.encode()]


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
        body = DictCodec.coerce(r.json())
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
        body = DictCodec.coerce(r.json())
        detail = body["detail"]
        assert isinstance(detail, str)
        assert "empty" in detail


# ---- Phase 4 HTML pages --------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class _SessionStub:
    """Minimal stand-in for ``Config`` -- exposes the two attrs web.py reads."""

    session_secret: str | None = "test-secret"  # noqa: S105 -- test fixture.
    session_max_age_seconds: int = 600


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


def _build_pages_app(tmp_path: Path, *, with_session: bool = True) -> FastAPI:
    """Build a fresh FastAPI with the Phase 4 HTML pages attached."""
    (tmp_path / "index.html").write_text("INDEX")
    (tmp_path / "console.html").write_text("CONSOLE-PAGE")
    (tmp_path / "me.html").write_text("ME-PAGE")
    (tmp_path / "admin.html").write_text("ADMIN-PAGE")
    (tmp_path / "login.html").write_text("LOGIN-PAGE")
    app = FastAPI()
    if with_session:
        app.state.config = _SessionStub()
    web.attach(app, assets_dir=tmp_path)
    return app


def _install_identity(app: FastAPI, identity: AuthIdentity | None) -> None:
    """Override ``optional_identity`` so the page routes see ``identity``."""

    async def _override() -> AuthIdentity | None:
        return identity

    app.dependency_overrides[web.optional_identity] = _override


class TestPhase4Pages:
    def test_me_redirects_to_login_when_unauthed(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path)
        _install_identity(app, None)
        client = TestClient(app, follow_redirects=False)
        r = client.get("/me")
        assert r.status_code == 302
        query = parse_qs(urlparse(r.headers["location"]).query)
        assert query["next"] == ["/me"]

    def test_me_renders_for_authed_user(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path)
        _install_identity(app, _viewer_identity())
        client = TestClient(app)
        r = client.get("/me")
        assert r.status_code == 200, r.text
        # The HTML is served verbatim; the JS in the real page fetches
        # ``/api/me/profile`` to populate the user info, which is
        # separately covered in ``admin_routes_test.py``.
        assert r.text == "ME-PAGE"

    def test_admin_redirects_when_unauthed(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path)
        _install_identity(app, None)
        client = TestClient(app, follow_redirects=False)
        r = client.get("/admin")
        assert r.status_code == 302
        assert "/auth/login_page" in r.headers["location"]

    def test_admin_redirect_preserves_query_in_next(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path)
        _install_identity(app, None)
        client = TestClient(app, follow_redirects=False)
        r = client.get("/admin?foo=bar&x=y")
        assert r.status_code == 302
        query = parse_qs(urlparse(r.headers["location"]).query)
        assert query["next"] == ["/admin?foo=bar&x=y"]

    def test_admin_forbids_unauthed_without_session_config(
        self,
        tmp_path: Path,
    ) -> None:
        app = _build_pages_app(tmp_path, with_session=False)
        _install_identity(app, None)
        client = TestClient(app)
        r = client.get("/admin")
        assert r.status_code == 403
        body = DictCodec.coerce(r.json())
        assert body["detail"] == "admin role required"

    def test_admin_403_for_non_admin(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path)
        _install_identity(app, _viewer_identity())
        client = TestClient(app)
        r = client.get("/admin")
        assert r.status_code == 403
        body = DictCodec.coerce(r.json())
        detail = body["detail"]
        assert isinstance(detail, str)
        assert "admin" in detail

    def test_admin_renders_for_admin(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path)
        _install_identity(app, _admin_identity())
        client = TestClient(app)
        r = client.get("/admin")
        assert r.status_code == 200, r.text
        assert r.text == "ADMIN-PAGE"

    def test_login_page_always_serves(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path)
        # No identity install; login page is the one route that must
        # never gate on auth -- otherwise users couldn't sign in.
        client = TestClient(app)
        r = client.get("/auth/login_page")
        assert r.status_code == 200
        assert r.text == "LOGIN-PAGE"

    def test_index_redirects_when_session_configured(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path, with_session=True)
        _install_identity(app, None)
        client = TestClient(app, follow_redirects=False)
        r = client.get("/")
        assert r.status_code == 302
        assert "/auth/login_page" in r.headers["location"]

    def test_index_serves_when_session_not_configured(self, tmp_path: Path) -> None:
        # No ``app.state.config`` -- the deployment doesn't run OAuth,
        # so the page is served as-is. Otherwise the SPA would be
        # locked behind a login that doesn't exist.
        app = _build_pages_app(tmp_path, with_session=False)
        _install_identity(app, None)
        client = TestClient(app)
        r = client.get("/")
        assert r.status_code == 200
        assert r.text == "INDEX"

    def test_console_redirects_when_session_configured(self, tmp_path: Path) -> None:
        # The multi-agent console is auth-gated like the main SPA: an unauthed
        # request under a session-configured deploy is redirected to login.
        app = _build_pages_app(tmp_path, with_session=True)
        _install_identity(app, None)
        client = TestClient(app, follow_redirects=False)
        r = client.get("/console")
        assert r.status_code == 302
        assert "/auth/login_page" in r.headers["location"]

    def test_console_renders_for_authed_user(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path)
        _install_identity(app, _viewer_identity())
        client = TestClient(app)
        r = client.get("/console")
        assert r.status_code == 200, r.text
        assert r.text == "CONSOLE-PAGE"

    def test_console_serves_when_session_not_configured(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path, with_session=False)
        _install_identity(app, None)
        client = TestClient(app)
        r = client.get("/console")
        assert r.status_code == 200
        assert r.text == "CONSOLE-PAGE"


# ---- A separately built web app at /app/ ----------------------------------


def _write_build(root: Path, *, marker: str) -> Path:
    """Write a minimal built web app (entry page plus one asset) at ``root``."""
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(f"INDEX-{marker}")
    (root / "assets" / "app.js").write_text(f"// {marker}")
    return root


def _build_app_dir_app(
    tmp_path: Path,
    app_dir: Path,
    *,
    with_session: bool = True,
) -> FastAPI:
    """Build a fresh FastAPI serving ``app_dir`` at ``/app/`` and no old pages."""
    assets = tmp_path / "no-pages"
    assets.mkdir()
    app = FastAPI()
    if with_session:
        app.state.config = _SessionStub()
    web.attach(app, assets_dir=assets, app_dir=app_dir)
    return app


class TestAppDir:
    def test_signed_out_page_redirects_to_login_with_next(
        self,
        tmp_path: Path,
    ) -> None:
        # The entry page redirects exactly as ``/`` does, so a signed-out
        # visitor lands back on the app after signing in.
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

    def test_signed_out_page_is_401_without_session_login(
        self,
        tmp_path: Path,
    ) -> None:
        # The old pages are served to anyone when session login is not
        # configured. The app is private, so it is not.
        app = _build_app_dir_app(
            tmp_path,
            _write_build(tmp_path / "b", marker="a"),
            with_session=False,
        )
        _install_identity(app, None)
        client = TestClient(app, follow_redirects=False)
        r = client.get("/app/")
        assert r.status_code == 401
        assert "INDEX-a" not in r.text

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
        app.state.config = _SessionStub()
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
        # out; ``no-cache`` makes browsers revalidate, so a deploy's new entry
        # page is seen at once.
        app = _build_app_dir_app(tmp_path, _write_build(tmp_path / "b", marker="a"))
        cases: tuple[tuple[AuthIdentity | None, str, int], ...] = (
            (_viewer_identity(), "/app/", 200),
            (_viewer_identity(), "/app/assets/app.js", 200),
            (_viewer_identity(), "/app/assets/missing.js", 404),
            (None, "/app/", 302),
            (None, "/app/assets/app.js", 401),
        )
        for identity, path, status in cases:
            _install_identity(app, identity)
            r = TestClient(app, follow_redirects=False).get(path)
            assert r.status_code == status, path
            assert r.headers.get("cache-control") == "private, no-cache", path

    def test_app_is_off_unless_a_directory_is_given(self, tmp_path: Path) -> None:
        app = FastAPI()
        web.attach(app, assets_dir=tmp_path)
        assert not any(path.startswith("/app") for path in registered_paths(app))


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
    (child_ref,) = ListCodec.coerce(
        DictCodec.coerce(parent_view["backlinks"])["narrows"],
    )
    (parent_ref,) = ListCodec.coerce(DictCodec.coerce(child_view["edges"])["narrows"])
    child_ref, parent_ref = DictCodec.coerce(child_ref), DictCodec.coerce(parent_ref)
    assert (
        parent_ref["peer_created"] == DictCodec.coerce(parent_view["self"])["created"]
    )
    assert parent_ref["peer_priority"] == 10
    assert child_ref["peer_created"] == DictCodec.coerce(child_view["self"])["created"]
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
        StrCodec.coerce(DictCodec.coerce(change)["id"])
        for change in ListCodec.coerce(detail["changes"])
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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
