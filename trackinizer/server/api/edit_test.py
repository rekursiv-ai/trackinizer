"""Tests for ``PUT`` / ``PATCH`` / ``DELETE /api/inquiries/{id}/{field}`` routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import dataclasses

from fastapi import HTTPException

import pytest

from trackinizer.conftest import make_store, new_uuid, set_field_row
from trackinizer.server.api.edit import _run_compare_and_set
from trackinizer.server.auth import AuthIdentity
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.bodies import FieldSet
from trackinizer.wire.routes import (
    inquiry_field_path,
    inquiry_field_routes,
)


def test_compare_and_set_rejects_unwired_column() -> None:
    """A CAS route whose column is neither status nor judgement raises, not a
    silent route to ``transition_judgement`` (a wrong-field write).

    Drift defense: a future ``compare_and_set=True`` column with no named
    transition branch in ``_run_compare_and_set`` must fail loudly. Clones a
    real CAS route and renames its column to one with no branch.
    """
    cas_route = next(r for r in inquiry_field_routes() if r.compare_and_set)
    bogus = dataclasses.replace(cas_route, column="confidence")
    body: FieldSet[object] = FieldSet(value="x", mode="cas", expected="y")
    store, _engine = make_store()
    identity = AuthIdentity(
        user_id=new_uuid(), api_key_id=None, email="u@x", role="writer"
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_run_compare_and_set(bogus, new_uuid(), body, store, identity))
    assert exc.value.status_code == 500
    assert "compare-and-set" in exc.value.detail


def test_no_polymorphic_results_route_remains() -> None:
    """No field route has column ``results`` after WebSearch.results was dropped.

    The edit PATCH path used to special-case the polymorphic ``results`` column
    (a ``(uuid, kind)`` pair). That column is gone (findings are ``produces``
    edges now), so the special-case is dead; this pins that no such route can
    reappear and silently revive the two-arg contract.
    """
    assert not [r for r in inquiry_field_routes() if r.column == "results"]


def test_no_agentsession_ended_field_route() -> None:
    """``agentsession_ended`` has no generic field route.

    ``ended`` is stamped only by ``Store.end_session`` (together with
    ``status='complete'``); a standalone ``PUT .../ended`` would desync the
    AgentSession lifecycle CHECK, so the spec marks it ``route_editable=False``
    and no route is generated.
    """
    assert not [r for r in inquiry_field_routes() if r.column == "agentsession_ended"]


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


class TestRoutes:
    def test_edit_status_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"status": "active", "kind": "Issue"})
        r = client.put(
            f"/api/inquiries/{new_uuid()}/status",
            json={"value": "complete", "actor": "alice"},
        )
        assert r.status_code == 200

    def test_set_account_route_repoints_to_active_user(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"account": "old@example.com", "kind": "Issue"})
        r = client.put(
            f"/api/inquiries/{new_uuid()}/account",
            json={"value": "new@example.com", "actor": "alice"},
        )
        assert r.status_code == 200, r.text

    def test_set_account_route_rejects_inactive_user(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"account": "old@example.com", "kind": "Issue"})

        # Make the account-active probe answer "no active row"; the route's
        # gate must 422 before any field write.
        async def fetchval(sql: str, *args: object) -> object:
            del sql, args
            return None

        engine.conn.fetchval.side_effect = fetchval
        r = client.put(
            f"/api/inquiries/{new_uuid()}/account",
            json={"value": "ghost@example.com", "actor": "alice"},
        )
        assert r.status_code == 422, r.text
        assert "not an active user" in r.json()["detail"]

    def test_account_has_no_delete_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """``account`` is required, so it exposes no clear-to-NULL DELETE."""
        client, _store, _engine = route_client
        r = client.delete(f"/api/inquiries/{new_uuid()}/account")
        assert r.status_code == 405

    def test_add_subscriber_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"subscribers": [], "kind": "Issue"})
        r = client.patch(
            f"/api/inquiries/{new_uuid()}/subscribers",
            json={"op": "add", "value": "bob", "actor": "alice"},
        )
        assert r.status_code == 200

    def test_remove_subscriber_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"subscribers": ["bob"], "kind": "Issue"})
        r = client.patch(
            f"/api/inquiries/{new_uuid()}/subscribers",
            json={"op": "sub", "value": "bob", "actor": "alice"},
        )
        assert r.status_code == 200

    def test_add_remove_label_routes(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"labels": [], "kind": "Issue"})
        target = new_uuid()
        r = client.patch(
            f"/api/inquiries/{target}/labels",
            json={"op": "add", "value": "x", "actor": "u"},
        )
        assert r.status_code == 200
        set_field_row(engine.conn, {"labels": ["x"], "kind": "Issue"})
        r = client.patch(
            f"/api/inquiries/{target}/labels",
            json={"op": "sub", "value": "x", "actor": "u"},
        )
        assert r.status_code == 200

    def test_add_label_empty_string(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"labels": [], "kind": "Issue"})
        r = client.patch(
            f"/api/inquiries/{new_uuid()}/labels",
            json={"op": "add", "value": "", "actor": "u"},
        )
        # ``FieldOp.value`` is generic JSON with no min_length, so the wire
        # layer accepts ``""``; emptiness is now enforced by the Store
        # (``_mutate_list_field`` normalizes and rejects the empty element),
        # which surfaces as a 409 ConflictError -- not the old Pydantic 422.
        assert r.status_code == 409
        assert "must be non-empty" in r.json()["detail"]

    def test_add_issue_kind_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"issue_kind": ["task"], "kind": "Issue"})
        r = client.patch(
            f"/api/issue/{new_uuid()}/issue_kind",
            json={"op": "add", "value": "bug", "actor": "u"},
        )
        assert r.status_code == 200

    def test_add_codechange_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        target = new_uuid()
        codechange = new_uuid()
        set_field_row(
            engine.conn,
            {"experiment_codechanges": [], "kind": "Experiment"},
        )
        # ``add_codechange`` validates the target via lookup_kinds
        # (one fetch). After it, the emit_change cascade re-walks
        # ``edges`` -- side_effect lets the validator return the kind
        # row first, then the cascade walk returns no edges.
        engine.conn.fetch.side_effect = [
            [{"id": codechange, "kind": "CodeChange"}],
            [],
        ]
        r = client.patch(
            f"/api/experiment/{target}/codechanges",
            json={"op": "add", "value": str(codechange), "actor": "u"},
        )
        assert r.status_code == 200

    def test_remove_codechange_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        target = new_uuid()
        codechange = new_uuid()
        set_field_row(
            engine.conn,
            {"experiment_codechanges": [codechange], "kind": "Experiment"},
        )
        r = client.patch(
            f"/api/experiment/{target}/codechanges",
            json={"op": "sub", "value": str(codechange), "actor": "u"},
        )
        assert r.status_code == 200

    def test_add_cost_route_accepts_signed_delta(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        engine.conn.fetchval.return_value = "Issue"
        # Negative delta -> op=sub with a positive value.
        r = client.patch(
            f"/api/inquiries/{new_uuid()}/marginal_cost_agent_usd",
            json={"op": "sub", "value": 0.5, "actor": "alice", "reason": "correction"},
        )
        assert r.status_code == 200

    def test_transition_status_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"status": "active", "kind": "Issue"})
        r = client.put(
            f"/api/inquiries/{new_uuid()}/status",
            json={
                "value": "complete",
                "mode": "cas",
                "expected": "active",
                "actor": "alice",
                "reason": "ship it",
            },
        )
        assert r.status_code == 200

    def test_transition_status_rejects_stale_expectation(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"status": "complete", "kind": "Issue"})
        r = client.put(
            f"/api/inquiries/{new_uuid()}/status",
            json={
                "value": "complete",
                "mode": "cas",
                "expected": "active",
                "actor": "alice",
            },
        )
        assert r.status_code == 409
        assert "expected 'active'" in r.json()["detail"]

    def test_misspelled_guard_field_is_422_not_blind_write(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # A typo'd compare-and-set guard (``expected``) must 422, never
        # silently degrade to a blind overwrite of a stale value
        # (REV-OPUS-04). ``extra="forbid"`` rejects the unknown field.
        client, _store, engine = route_client
        set_field_row(engine.conn, {"status": "complete", "kind": "Issue"})
        r = client.put(
            f"/api/inquiries/{new_uuid()}/status",
            json={
                "value": "complete",
                "expcted": "active",  # codespell:ignore expcted -- deliberate typo of 'expected'
                "actor": "alice",
            },
        )
        assert r.status_code == 422
        # And no UPDATE was issued -- the typo never reached the store.
        sqls = [c.args[0] for c in engine.conn.execute.call_args_list]
        assert not any("UPDATE inquiries SET status" in s for s in sqls)

    def test_cas_mode_without_expected_is_422(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(engine.conn, {"status": "active", "kind": "Issue"})
        r = client.put(
            f"/api/inquiries/{new_uuid()}/status",
            json={"value": "complete", "mode": "cas", "actor": "alice"},
        )
        assert r.status_code == 422

    def test_expected_without_cas_mode_is_422(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # A bare ``expected`` (no ``mode='cas'``) is rejected so the intent
        # can't be ambiguous -- the caller must opt into compare-and-set.
        client, _store, engine = route_client
        set_field_row(engine.conn, {"status": "active", "kind": "Issue"})
        r = client.put(
            f"/api/inquiries/{new_uuid()}/status",
            json={"value": "complete", "expected": "active", "actor": "alice"},
        )
        assert r.status_code == 422

    def test_blind_set_still_works_without_mode(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # The default mode is ``set``: a plain value PUT (no mode, no
        # expected) is a blind overwrite, unchanged.
        client, _store, engine = route_client
        set_field_row(engine.conn, {"status": "active", "kind": "Issue"})
        r = client.put(
            f"/api/inquiries/{new_uuid()}/status",
            json={"value": "complete", "actor": "alice"},
        )
        assert r.status_code == 200

    def test_edit_venue_route(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(
            engine.conn,
            {"paper_venue": "NeurIPS", "kind": "Paper"},
        )
        r = client.put(
            f"/api/paper/{new_uuid()}/venue",
            json={"value": "KDD", "actor": "u"},
        )
        assert r.status_code == 200

    def test_edit_publication_type_rejects_unknown(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A value outside the closed ``Paper.PublicationType`` set is a 422."""
        client, _store, engine = route_client
        set_field_row(
            engine.conn,
            {"paper_publication_type": "misc", "kind": "Paper"},
        )
        r = client.put(
            f"/api/paper/{new_uuid()}/publication_type",
            json={"value": "PUNCHCARD", "actor": "u"},
        )
        assert r.status_code == 422

    def test_delete_description_clears_to_null(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """DELETE on a string column clears to SQL NULL, not ``""``.

        Regression TAPI-007: ``set_description`` / ``set_owner`` coerced
        ``None`` -> ``""``, contradicting the wire contract
        (``wire/routes.py`` DELETE = "clear to NULL").
        """
        client, _store, engine = route_client
        set_field_row(engine.conn, {"description": "old", "kind": "Issue"})
        r = client.request(
            "DELETE",
            f"/api/inquiries/{new_uuid()}/description",
            json={"actor": "u"},
        )
        assert r.status_code == 200
        update = next(
            c
            for c in engine.conn.execute.call_args_list
            if "UPDATE inquiries SET description" in c.args[0]
        )
        assert update.args[1] is None

    def test_edit_venue_route_clears_null(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        set_field_row(
            engine.conn,
            {"paper_venue": "NeurIPS", "kind": "Paper"},
        )
        r = client.request(
            "DELETE",
            f"/api/paper/{new_uuid()}/venue",
            json={"actor": "u"},
        )
        assert r.status_code == 200
        update = next(
            c
            for c in engine.conn.execute.call_args_list
            if "UPDATE inquiries SET paper_venue" in c.args[0]
        )
        assert update.args[1] is None


class TestCoverageRoutesAndCli:
    def test_submit_and_edit_routes_more_kinds(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        submit_payloads: dict[str, dict[str, object]] = {
            "artifact": {"title": "a"},
            "experiment": {"title": "e", "codechanges": []},
            "paper": {"title": "p"},
            "belief": {"title": "c"},
            "issue": {"title": "i"},
            "webresult": {"title": "w", "url": "https://x"},
            "websearch": {"title": "s", "query": "q"},
        }
        for token, payload in submit_payloads.items():
            assert (
                client.post(f"/api/inquiries/{token}", json=payload).status_code == 201
            )
        # field -> (column, old, new, expected_status); PUT overwrites.
        edit_values: dict[str, tuple[str, object, object, int]] = {
            "title": ("title", "old", "new", 200),
            "description": ("description", "old", "new", 200),
            "owner": ("owner", "old", "new", 200),
            "status": ("status", "active", "complete", 200),
            "judgement": ("belief_judgement", "unproven", "proven", 200),
            "confidence": ("belief_confidence", 0.5, 0.75, 200),
            "priority": ("issue_priority", 30, 10, 200),
            "outcome": ("experiment_outcome", "old", "new", 200),
            # source is scheme-validated (<scheme>:<rest>), so use schemed values.
            "source": ("paper_source", "doi:10.1/old", "doi:10.1/new", 200),
            "query": ("websearch_query", "old", "new", 200),
            "provider": ("websearch_provider", "old", "new", 200),
            "sha": ("codechange_sha", "old", "new", 200),
            "url": ("webresult_url", "old", "new", 200),
        }
        # Each editable column has its allowed kinds; mock accordingly so
        # the kind-validation in _read_field doesn't reject the edit.
        kind_for_route: dict[str, Inquiry.InquiryKind] = {
            "title": "Issue",
            "description": "Issue",
            "owner": "Issue",
            "status": "Issue",
            "judgement": "Belief",
            "confidence": "Belief",
            "priority": "Issue",
            "outcome": "Experiment",
            "source": "Paper",
            "query": "WebSearch",
            "provider": "WebSearch",
            "sha": "CodeChange",
            "url": "WebResult",
        }
        for field, (column, old, new, expected_status) in edit_values.items():
            set_field_row(
                engine.conn,
                {column: old, "kind": kind_for_route[field]},
            )
            path = inquiry_field_path(field).format(target_id=new_uuid())
            assert (
                client.put(
                    path,
                    json={"value": new, "actor": "u"},
                ).status_code
                == expected_status
            )
        # List add via PATCH op=add for labels / subscribers.
        for field in ("labels", "subscribers"):
            set_field_row(engine.conn, {field: [], "kind": "Issue"})
            assert (
                client.patch(
                    f"/api/inquiries/{new_uuid()}/{field}",
                    json={"op": "add", "value": "x", "actor": "u"},
                ).status_code
                == 200
            )
        set_field_row(
            engine.conn,
            {"paper_source": "doi:10.1/old", "kind": "Paper"},
        )
        # source is scheme-validated (<scheme>:<rest>); a schemed value passes.
        assert (
            client.put(
                f"/api/paper/{new_uuid()}/source",
                json={"value": "doi:10.1/new", "actor": "u"},
            ).status_code
            == 200
        )


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
