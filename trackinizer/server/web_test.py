"""Tests for trackinizer web helpers and routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, get_args
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import json
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import asyncpg
import pytest

from trackinizer.conftest import FakeEngine, make_conn, new_uuid
from trackinizer.server import web
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.server.route_iter import (
    iter_routes,
    registered_paths,
)
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.wire_sessions import FeedEvent


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
            [(None, "hello"), ("description", "re.*")]
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
    def test_row_to_dict_includes_kind_specific_values(self) -> None:
        target_id = new_uuid()
        out = web._row_to_dict(
            cast(
                Any,
                _inquiry_row(
                    id=target_id,
                    kind="WebSearch",
                    issue_priority=None,
                    websearch_query="q",
                    websearch_provider="google",
                ),
            )
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
                Any,
                _inquiry_row(
                    kind="Paper",
                    paper_google_scholar_cluster_id="vexaDfEelKEJ",
                    paper_google_scholar_cites_id="4727085927710188680",
                ),
            )
        )
        assert out["google_scholar_cluster_id"] == "vexaDfEelKEJ"
        assert out["google_scholar_cites_id"] == "4727085927710188680"

    def test_row_to_dict_omits_google_scholar_handles_when_absent(self) -> None:
        out = web._row_to_dict(cast(Any, _inquiry_row(kind="Paper")))
        assert "google_scholar_cluster_id" not in out
        assert "google_scholar_cites_id" not in out

    def test_row_to_dict_surfaces_experiment_config(self) -> None:
        cfg = {"lr": 3e-4, "batch": 32, "nested": {"warmup": 100}}
        out = web._row_to_dict(
            cast(
                Any,
                _inquiry_row(kind="Experiment", experiment_config=cfg),
            )
        )
        # JSONB is decoded to a dict by the codec; surfaced verbatim, not
        # ISO-formatted or stringified.
        assert out["config"] == cfg

    def test_row_to_dict_omits_config_when_absent(self) -> None:
        out = web._row_to_dict(
            cast(Any, _inquiry_row(kind="Experiment", experiment_config=None))
        )
        assert "config" not in out

    def test_row_to_dict_surfaces_agentsession_fields(self) -> None:
        started = datetime(2026, 5, 18, 9, tzinfo=UTC)
        out = web._row_to_dict(
            cast(
                Any,
                _inquiry_row(
                    kind="AgentSession",
                    agentsession_cli="claude",
                    agentsession_cli_session_id="abc-123",
                    agentsession_started=started,
                    agentsession_ended=None,
                    agentsession_rooms=["sear", "lab"],
                ),
            )
        )
        assert out["cli"] == "claude"
        assert out["cli_session_id"] == "abc-123"
        # TIMESTAMPTZ columns are ISO-formatted, not raw datetimes.
        assert out["started"] == started.isoformat()
        # A live session (ended IS NULL) omits the key entirely.
        assert "ended" not in out
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
        out = web._change_to_dict(cast(Any, row))
        assert out["api_key_id"] == str(api_key_id)
        assert out["principal"] == "api@example.com"
        assert out["caused_by"] == str(peer_id)
        assert cast(dict[str, object], out["old"])["labels"] == ["a"]
        assert cast(dict[str, object], out["new"])["experiment_codechanges"] == [
            str(old_codechanges)
        ]
        assert cast(dict[str, object], out["new"])["peer_id"] == str(peer_id)
        assert cast(dict[str, object], out["new"])["marginal_cost"] == {
            "agent_usd": 2.0,
            "resource_usd": 3.0,
        }

    def test_peer_ref_and_isoformat(self) -> None:
        peer_id = new_uuid()
        row = {
            "peer_kind": "Belief",
            "peer_seq": 4,
            "peer_title": None,
            "peer_status": "active",
            "peer_judgement": "proven",
        }
        assert web._peer_ref(cast(Any, row), peer_id) == {
            "id": str(peer_id),
            "kind": "Belief",
            "seq": 4,
            "title": "",
            "status": "active",
            "judgement": "proven",
        }
        assert web._isoformat(datetime(2026, 5, 18, tzinfo=UTC)).startswith("2026")
        assert web._isoformat("x") == "x"


class TestTimestampAssets:
    def test_browser_timestamp_formatters_use_local_time(self) -> None:
        root = Path(__file__).resolve().parent / "assets"
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
                    }
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
                    }
                ],
            ]
        )
        edges = await web._edges_for(cast(Any, conn), target_id, direction="outbound")
        backlinks = await web._edges_for(
            cast(Any, conn), target_id, direction="inbound"
        )
        edge = cast(list[dict[str, object]], edges["narrows"])[0]
        assert edge["priority"] == 10
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
        assert set(legend["node_kinds"]) == set(get_args(Inquiry.InquiryKind.__value__))
        assert set(legend["edge_kinds"]) == set(get_args(Edge.Kind.__value__))


class TestRoutes:
    @pytest.mark.asyncio
    async def test_web_search_empty_and_with_kind(self) -> None:
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        assert (
            await web.web_search(cast(Any, request), q="   ", identity=_TEST_IDENTITY)
            == []
        )

        engine.conn.fetch = AsyncMock(return_value=[_inquiry_row()])
        rows = await web.web_search(
            cast(Any, request),
            q="hello",
            identity=_TEST_IDENTITY,
            kind="Issue",
            limit=3,
        )
        assert rows[0]["title"] == "title"
        assert engine.conn.fetch.await_args is not None
        sql, *params = engine.conn.fetch.await_args.args
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
            cast(Any, request),
            q="title:^(a+)+$",
            identity=_TEST_IDENTITY,
        )
        executed = [c.args[0] for c in engine.conn.execute.call_args_list]
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
                    "invalid regular expression: invalid embedded option"
                ),
                [],
            ]
        )
        with pytest.raises(HTTPException) as caught:
            await web.web_search(
                cast(Any, request), q="title:(?P<n>a)", identity=_TEST_IDENTITY
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
            ]
        )
        with pytest.raises(asyncpg.PostgresSyntaxError):
            await web.web_search(
                cast(Any, request), q="title:^a", identity=_TEST_IDENTITY
            )

    @pytest.mark.asyncio
    async def test_recent_lookup_and_get_routes(self) -> None:
        target_id = new_uuid()
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        engine.conn.fetch = AsyncMock(
            return_value=[_change_row(principal="api@example.com")]
        )
        recent = await web.web_recent_changes(
            cast(Any, request), identity=_TEST_IDENTITY, limit=1
        )
        assert recent[0]["actor"] == "alice"
        assert recent[0]["principal"] == "api@example.com"
        assert "LEFT JOIN api_keys" in engine.conn.fetch.call_args.args[0]

        engine.conn.fetchval = AsyncMock(return_value="Issue")
        assert await web.web_lookup(
            target_id, cast(Any, request), identity=_TEST_IDENTITY
        ) == {
            "kind": "Issue",
            "id": str(target_id),
        }
        engine.conn.fetchval = AsyncMock(return_value=None)
        with pytest.raises(HTTPException):
            await web.web_lookup(target_id, cast(Any, request), identity=_TEST_IDENTITY)

        engine.conn.fetchrow = AsyncMock(return_value=_inquiry_row(id=target_id))
        engine.conn.fetch = AsyncMock(side_effect=[[], [], [_change_row()]])
        detail = await web.web_get(
            target_id, cast(Any, request), identity=_TEST_IDENTITY
        )
        assert cast(dict[str, object], detail["self"])["id"] == str(target_id)
        assert detail["edges"] == {}
        assert detail["backlinks"] == {}
        assert len(cast(list[object], detail["changes"])) == 1
        engine.conn.fetchrow = AsyncMock(return_value=None)
        with pytest.raises(HTTPException):
            await web.web_get(target_id, cast(Any, request), identity=_TEST_IDENTITY)

    @pytest.mark.asyncio
    async def test_web_graph_returns_nodes_and_edges(self) -> None:
        # The graph endpoint is the whole-graph aggregate the SPA paints from:
        # every inquiry as a typed node (oldest first, so a replay animation
        # adds them in creation order) plus every edge as a typed directed
        # link. Nodes carry only the light projection the graph view needs
        # (id, kind, seq, title, created), not the full per-kind detail.
        early = datetime(2026, 5, 18, 8, tzinfo=UTC)
        late = datetime(2026, 5, 18, 9, tzinfo=UTC)
        root_id, child_id = new_uuid(), new_uuid()
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
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
                [
                    {
                        "from_id": child_id,
                        "to_id": root_id,
                        "edge_kind": "favors",
                        "valence": 0.4,
                    }
                ],
            ]
        )
        # limit=0 is the unbounded whole-graph path (two fetches: nodes, edges).
        graph = await web.web_graph(
            cast(Any, request), identity=_TEST_IDENTITY, limit=0
        )
        nodes = cast(list[dict[str, object]], graph["nodes"])
        edges = cast(list[dict[str, object]], graph["edges"])
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
            }
        ]
        # Nodes must be ordered by ``created`` ascending so the replay
        # animation lands them in the order they were authored.
        node_sql = engine.conn.fetch.call_args_list[0].args[0]
        assert "ORDER BY created" in node_sql

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
                    }
                ],
            ]
        )
        graph = await web.web_graph(
            cast(Any, request), identity=_TEST_IDENTITY, limit=0
        )
        assert cast(list[dict[str, object]], graph["edges"]) == [
            {"from_id": str(a), "to_id": str(b), "edge_kind": "narrows"}
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
                # 2. edges touching the recent node -> reaches the old node
                [
                    {
                        "from_id": recent_id,
                        "to_id": old_id,
                        "edge_kind": "proves",
                        "valence": 0.5,
                    }
                ],
                # 3. full rows for the closed set (old + recent), created ASC
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
            ]
        )
        graph = await web.web_graph(
            cast(Any, request), identity=_TEST_IDENTITY, limit=1
        )
        nodes = cast(list[dict[str, object]], graph["nodes"])
        # The older referenced Paper is pulled in alongside the recent Belief.
        assert {n["id"] for n in nodes} == {str(old_id), str(recent_id)}
        # The recent-id query carried the limit; the edge query closed the set.
        recent_sql = engine.conn.fetch.call_args_list[0].args[0]
        assert "ORDER BY created DESC" in recent_sql
        assert "LIMIT" in recent_sql
        edge_sql = engine.conn.fetch.call_args_list[1].args[0]
        assert "from_id = ANY" in edge_sql
        assert "to_id = ANY" in edge_sql

    @pytest.mark.asyncio
    async def test_web_graph_rejects_bad_limit(self) -> None:
        engine = FakeEngine()
        store = _Store(engine=engine)
        request = _request(store, engine)
        for bad in (-1, 99_999):
            with pytest.raises(HTTPException):
                await web.web_graph(
                    cast(Any, request), identity=_TEST_IDENTITY, limit=bad
                )

    @pytest.mark.asyncio
    async def test_subscribe_streams_sse(self) -> None:
        engine = FakeEngine()
        subject_id = new_uuid()
        engine.listen_messages = [json.dumps({"id": str(subject_id)})]
        request = _request(object(), engine)
        response = await web.web_subscribe(cast(Any, request), identity=_TEST_IDENTITY)
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
        self, tmp_path: Path
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
        response = await web.web_subscribe(cast(Any, request), identity=_TEST_IDENTITY)
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
        resp = await web.web_feed(cast(Any, request), identity=_TEST_IDENTITY)
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
            cast(Any, request),
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
        resp = await web.web_feed(cast(Any, request), identity=_TEST_IDENTITY)
        assert resp.next_after is None
        assert resp.events == []

    @pytest.mark.asyncio
    async def test_partial_cursor_is_rejected(self) -> None:
        store = AsyncMock()
        request = _request(store, FakeEngine())
        # All three cursor parts must be given together.
        with pytest.raises(HTTPException):
            await web.web_feed(
                cast(Any, request),
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
                    cast(Any, request), identity=_TEST_IDENTITY, limit=bad
                )


class TestRouteBounds:
    @staticmethod
    def _client(app: FastAPI) -> TestClient:
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
        assert "quot" in r.json()["detail"]

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
        assert "empty" in r.json()["detail"]


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
    """Build a fresh FastAPI with the Phase 4 HTML pages attached.

    Args:
      tmp_path: Test-supplied temp dir holding stub HTML files. Each
        page is written with a recognizable body so assertions can tell
        them apart even though the routes serve raw files.
      with_session: When true, the app gets a stub ``Config`` whose
        ``session_secret`` is set; unauthed requests are then redirected
        to ``/auth/login_page`` instead of served as-is.

    """
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
        self, tmp_path: Path
    ) -> None:
        app = _build_pages_app(tmp_path, with_session=False)
        _install_identity(app, None)
        client = TestClient(app)
        r = client.get("/admin")
        assert r.status_code == 403
        assert r.json()["detail"] == "admin role required"

    def test_admin_403_for_non_admin(self, tmp_path: Path) -> None:
        app = _build_pages_app(tmp_path)
        _install_identity(app, _viewer_identity())
        client = TestClient(app)
        r = client.get("/admin")
        assert r.status_code == 403
        assert "admin" in r.json()["detail"]

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


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
