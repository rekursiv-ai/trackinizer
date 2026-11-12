"""Tests for ``Store`` field edits -- set_*/add_*/remove_* setters and cost."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Protocol, cast
from unittest.mock import AsyncMock
from uuid import UUID

import asyncio

import pytest

from trackinizer.conftest import (
    RecordingPolicy,
    executed_sql,
    make_conn,
    make_store,
    new_uuid,
    queue_field_rows,
    set_field_row,
)
from trackinizer.lib.postgres import Conn
from trackinizer.server.access_policy import Resource
from trackinizer.server.setter_dispatch import COLUMN_SPECS
from trackinizer.server.store.change_id_slot import set_client_change_id
from trackinizer.types.columns import storage_name
from trackinizer.types.cost import Cost
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.wire.routes import InquiryFieldRoute, inquiry_field_routes


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from trackinizer.mock_test_support import MockCall, MockConnection
    from trackinizer.server.store.core import Store
    from trackinizer.types.inquiries import Inquiry


class TestStatusEdits:
    @pytest.mark.asyncio
    async def test_set_status_no_op_when_value_matches(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"status": "active", "kind": "Issue"})
        store, _engine = make_store(conn)
        await store.set_status(new_uuid(), "active", actor="user")
        sqls = executed_sql(conn)
        assert not any(s.startswith("UPDATE inquiries SET status") for s in sqls)

    @pytest.mark.asyncio
    async def test_set_status_emits_change_when_value_differs(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"status": "active", "kind": "Issue"})
        store, _engine = make_store(conn)
        await store.set_status(new_uuid(), "complete", actor="user")
        sqls = executed_sql(conn)
        assert any("UPDATE inquiries SET status" in s for s in sqls)
        assert any("INSERT INTO change_log" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_set_status_missing_inquiry_raises(self) -> None:
        conn = make_conn()
        set_field_row(conn, None)
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError):
            await store.set_status(new_uuid(), "complete", actor="user")


class TestTransitionStatus:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("given", [True, False])
    async def test_moves_the_status_and_audits_both_sides(self, *, given: bool) -> None:
        """The row read under its lock moves; the audit row keeps the key and the reason."""
        conn = make_conn()
        set_field_row(conn, {"status": "active", "kind": "Issue"})
        store, _engine = make_store(conn)
        target, key = new_uuid(), new_uuid()
        change = (
            await store.transition_status(
                target,
                expected_from="active",
                to="stopped",
                api_key_id=key,
                actor="meter",
                reason="budget_stop",
            )
            if given
            else await store.transition_status(
                target,
                expected_from="active",
                to="stopped",
                actor="meter",
            )
        )
        read = conn.fetchrow.call_args_list[0]
        update = next(
            c for c in conn.execute.call_args_list if "UPDATE inquiries SET" in _sql(c)
        )
        audit = next(
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO change_log" in _sql(c)
        )
        columns = _sql(audit).split("(", 1)[1].split(")", 1)[0].split(", ")
        logged = dict(zip(columns, audit.args[1:], strict=True))
        assert read.args == (
            "SELECT status, kind FROM inquiries WHERE id = $1 FOR UPDATE",
            target,
        )
        assert "SET status" in _sql(update)
        assert update.args[1:3] == ("stopped", target)
        assert {
            name: logged[name]
            for name in (
                "id",
                "api_key_id",
                "actor",
                "subject_id",
                "subject_kind",
                "kind",
                "reason",
                "old_status",
                "new_status",
            )
        } == {
            "id": change,
            "api_key_id": key if given else None,
            "actor": "meter",
            "subject_id": target,
            "subject_kind": "Issue",
            "kind": "status",
            "reason": "budget_stop" if given else "",
            "old_status": "active",
            "new_status": "stopped",
        }

    @pytest.mark.asyncio
    async def test_refuses_a_status_other_than_the_expected_one(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"status": "complete", "kind": "Issue"})
        store, _engine = make_store(conn)
        with pytest.raises(
            ConflictError,
            match=r"^status transition rejected: expected 'active', found 'complete'$",
        ):
            await store.transition_status(
                new_uuid(),
                expected_from="active",
                to="stopped",
                actor="meter",
            )
        assert not any("UPDATE inquiries SET" in s for s in executed_sql(conn))

    @pytest.mark.asyncio
    async def test_the_status_it_already_has_changes_nothing(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"status": "stopped", "kind": "Issue"})
        store, _engine = make_store(conn)
        change = await store.transition_status(
            new_uuid(),
            expected_from="stopped",
            to="stopped",
            actor="meter",
        )
        sqls = executed_sql(conn)
        assert change is None
        assert not any("UPDATE inquiries SET" in s for s in sqls)
        assert not any("INSERT INTO change_log" in s for s in sqls)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("committed_before_the_read", [True, False])
    async def test_a_retry_replays_its_committed_change(
        self,
        *,
        committed_before_the_read: bool,
    ) -> None:
        """A retry's key returns its change, found before or after the status read."""
        conn = make_conn()
        target, key = new_uuid(), new_uuid()
        committed = {"actor": "meter", "subject_id": target, "kind": "status"}
        row = {"status": "active", "kind": "Issue"}
        queue_field_rows(
            conn,
            *((committed,) if committed_before_the_read else (None, row, committed)),
        )
        store, _engine = make_store(conn)
        set_client_change_id(key)
        try:
            change = await store.transition_status(
                target,
                expected_from="active",
                to="stopped",
                actor="meter",
            )
        finally:
            set_client_change_id(None)
        assert change == key
        assert not any("UPDATE inquiries SET" in s for s in executed_sql(conn))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "same_subject",
        [True, False],
        ids=["other-field", "other-subject"],
    )
    async def test_a_key_used_for_another_operation_is_refused(
        self,
        *,
        same_subject: bool,
    ) -> None:
        conn = make_conn()
        target = new_uuid()
        queue_field_rows(
            conn,
            {
                "actor": "meter",
                "subject_id": target if same_subject else new_uuid(),
                "kind": "title" if same_subject else "status",
            },
        )
        store, _engine = make_store(conn)
        set_client_change_id(new_uuid())
        try:
            with pytest.raises(ConflictError, match="already used"):
                await store.transition_status(
                    target,
                    expected_from="active",
                    to="stopped",
                    actor="meter",
                )
        finally:
            set_client_change_id(None)


class TestCLIHelpers:
    def test_set_title_rejects_empty(self) -> None:
        """Direct Store callers can't blank a required field."""
        store, _engine = make_store()
        with pytest.raises(ConflictError, match="cannot be empty"):
            asyncio.run(store.set_title(new_uuid(), "", actor="u"))

    def test_set_issue_kind_rejects_empty(self) -> None:
        store, _engine = make_store()
        with pytest.raises(ConflictError, match="at least one entry"):
            asyncio.run(store.set_issue_kind(new_uuid(), [], actor="u"))

    def test_set_account_rejects_blank(self) -> None:
        """A required column can't be blanked to whitespace by a direct caller."""
        store, _engine = make_store()
        with pytest.raises(ConflictError, match="account cannot be empty"):
            asyncio.run(store.set_account(new_uuid(), "   ", actor="u"))


class TestCoverageStoreEdits:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "column", "old", "new", "kind"),
        [
            ("set_title", "title", "old", "new", "Issue"),
            ("set_description", "description", "old", "new", "Issue"),
            ("set_owner", "owner", "old", "new", "Issue"),
            ("set_description", "description", "old", "", "Issue"),
            ("set_owner", "owner", "old", "", "Issue"),
            ("set_priority", "issue_priority", 30, 10, "Issue"),
            ("set_outcome", "experiment_outcome", "old", "new", "Experiment"),
            ("set_query", "websearch_query", "old", "new", "WebSearch"),
            ("set_provider", "websearch_provider", "old", "new", "WebSearch"),
        ],
    )
    async def test_scalar_edit_methods(
        self,
        method: str,
        column: str,
        old: object,
        new: object,
        kind: Inquiry.InquiryKind,
    ) -> None:
        conn = make_conn()
        set_field_row(conn, {column: old, "kind": kind})
        store, _engine = make_store(conn)
        await getattr(store, method)(new_uuid(), new, actor="alice")
        sqls = executed_sql(conn)
        assert any(sql.startswith("UPDATE inquiries SET") for sql in sqls)
        assert any("INSERT INTO change_log" in sql for sql in sqls)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "column", "old", "new", "stored", "kind"),
        [
            # Nullable columns clear to SQL NULL, not "" / [] (TAPI-007):
            # unset is one encoding (NULL) so ``isnull`` sees cleared rows.
            ("set_description", "description", "old", None, None, "Issue"),
            ("set_owner", "owner", "old", None, None, "Issue"),
            ("set_labels", "labels", ["old"], None, None, "Issue"),
            # Blanking a nullable scalar with "" also collapses to NULL.
            ("set_description", "description", "old", "", None, "Issue"),
            ("set_labels", "labels", ["old"], [], None, "Issue"),
            ("set_judgement", "belief_judgement", "proven", None, None, "Belief"),
        ],
    )
    async def test_null_edit_storage_contracts(
        self,
        *,
        method: str,
        column: str,
        old: object,
        new: object,
        stored: object,
        kind: Inquiry.InquiryKind,
    ) -> None:
        conn = make_conn()
        set_field_row(conn, {column: old, "kind": kind})
        store, _engine = make_store(conn)
        await getattr(store, method)(new_uuid(), new, actor="alice")
        update = next(
            c for c in conn.execute.call_args_list if "UPDATE inquiries SET" in _sql(c)
        )
        assert update.args[1] == stored

    @pytest.mark.asyncio
    async def test_atomic_list_mutation_canonicalizes_item(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"labels": [], "kind": "Issue"})
        store, _engine = make_store(conn)
        await store.add_label(new_uuid(), "  x  ", actor="alice")
        update = next(
            c for c in conn.execute.call_args_list if "UPDATE inquiries SET" in _sql(c)
        )
        assert update.args[1] == ["x"]

    @pytest.mark.asyncio
    async def test_atomic_subscriber_rejects_blank_item(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"subscribers": [], "kind": "Issue"})
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError, match="non-empty"):
            await store.add_subscriber(new_uuid(), "   ", actor="alice")
        assert not any("UPDATE inquiries SET" in s for s in executed_sql(conn))

    @pytest.mark.asyncio
    async def test_set_source_updates_paper_source(self) -> None:
        """``set_source`` writes the single self-describing ``paper_source``."""
        conn = make_conn()
        set_field_row(conn, {"paper_source": "old", "kind": "Paper"})
        store, _engine = make_store(conn)
        await store.set_source(new_uuid(), "arXiv:2405.16391", actor="alice")
        update_columns = [
            s for s in executed_sql(conn) if s.startswith("UPDATE inquiries SET")
        ]
        assert any("SET paper_source =" in s for s in update_columns)

    @pytest.mark.asyncio
    async def test_set_venue_updates_paper_venue(self) -> None:
        """``set_venue`` writes the free-text ``paper_venue`` column."""
        conn = make_conn()
        set_field_row(conn, {"paper_venue": "NeurIPS", "kind": "Paper"})
        store, _engine = make_store(conn)
        await store.set_venue(new_uuid(), "KDD", actor="alice")
        update_columns = [
            s for s in executed_sql(conn) if s.startswith("UPDATE inquiries SET")
        ]
        assert any("SET paper_venue =" in s for s in update_columns)

    @pytest.mark.asyncio
    async def test_set_publication_type_updates_column(self) -> None:
        """``set_publication_type`` writes ``paper_publication_type``."""
        conn = make_conn()
        set_field_row(conn, {"paper_publication_type": "misc", "kind": "Paper"})
        store, _engine = make_store(conn)
        await store.set_publication_type(new_uuid(), "article", actor="alice")
        update_columns = [
            s for s in executed_sql(conn) if s.startswith("UPDATE inquiries SET")
        ]
        assert any("SET paper_publication_type =" in s for s in update_columns)

    @pytest.mark.asyncio
    async def test_set_authors_no_op_when_value_matches(self) -> None:
        """Re-setting an identical byline is a no-op (no phantom audit row).

        PG returns a ``list`` for the ``TEXT[]`` column while the wire delivers
        a ``tuple`` (``Paper.authors`` is ``tuple[str, ...]``); without the
        ``paper_authors`` RUNTIME_HOOKS the dedup compares ``list != tuple`` and
        every identical PUT writes a change + audit row. Both sides must
        normalize to ``tuple`` so an unchanged byline writes nothing.
        """
        conn = make_conn()
        set_field_row(conn, {"paper_authors": ["Ada", "Grace"], "kind": "Paper"})
        store, _engine = make_store(conn)
        await store.set_authors(new_uuid(), ("Ada", "Grace"), actor="alice")
        sqls = executed_sql(conn)
        assert not any("UPDATE inquiries SET paper_authors" in s for s in sqls)
        assert not any("INSERT INTO change_log" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_set_authors_emits_change_when_byline_differs(self) -> None:
        """A genuinely changed byline still updates and audits (order matters)."""
        conn = make_conn()
        set_field_row(conn, {"paper_authors": ["Ada", "Grace"], "kind": "Paper"})
        store, _engine = make_store(conn)
        await store.set_authors(new_uuid(), ("Grace", "Ada"), actor="alice")
        sqls = executed_sql(conn)
        assert any("UPDATE inquiries SET paper_authors" in s for s in sqls)
        assert any("INSERT INTO change_log" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_add_author_strips_per_element(self) -> None:
        # Each author is stripped on write ("Smith " stored as "Smith"). A
        # byline add ALWAYS appends (duplicates are significant), so adding
        # "Smith" to a stored "Smith " yields two byline entries.
        conn = make_conn()
        set_field_row(conn, {"paper_authors": ["Smith "], "kind": "Paper"})
        store, _engine = make_store(conn)
        await store.add_author(new_uuid(), "Smith", actor="alice")
        update = next(
            c
            for c in conn.execute.call_args_list
            if c.args and "UPDATE inquiries SET paper_authors" in _sql(c)
        )
        assert update.args[1] == ("Smith", "Smith")

    @pytest.mark.asyncio
    async def test_add_author_always_appends_existing(self) -> None:
        # A byline add of an already-present author appends a second entry
        # (not a set no-op) -- two distinct contributors can share a surname.
        conn = make_conn()
        set_field_row(conn, {"paper_authors": ["Smith"], "kind": "Paper"})
        store, _engine = make_store(conn)
        result = await store.add_author(new_uuid(), "Smith", actor="alice")
        assert result is not None
        update = next(
            c
            for c in conn.execute.call_args_list
            if c.args and "UPDATE inquiries SET paper_authors" in _sql(c)
        )
        assert update.args[1] == ("Smith", "Smith")

    @pytest.mark.asyncio
    async def test_remove_author_drops_first_match_only(self) -> None:
        # A byline remove drops ONE occurrence (the first), not every match:
        # set arithmetic would delete both Smiths.
        conn = make_conn()
        set_field_row(
            conn,
            {"paper_authors": ["Smith", "Jones", "Smith"], "kind": "Paper"},
        )
        store, _engine = make_store(conn)
        await store.remove_author(new_uuid(), "Smith", actor="alice")
        update = next(
            c
            for c in conn.execute.call_args_list
            if c.args and "UPDATE inquiries SET paper_authors" in _sql(c)
        )
        assert update.args[1] == ("Jones", "Smith")

    @pytest.mark.asyncio
    async def test_add_label_still_dedups_non_byline(self) -> None:
        # A non-byline list (labels) keeps canonical-set semantics: adding an
        # already-present label is a no-op, no regression from the byline path.
        conn = make_conn()
        set_field_row(conn, {"labels": ["bug"], "kind": "Issue"})
        store, _engine = make_store(conn)
        result = await store.add_label(new_uuid(), "bug", actor="alice")
        assert result is None
        sqls = executed_sql(conn)
        assert not any("UPDATE inquiries SET labels" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_set_authors_strips_per_element_preserving_order_and_dups(
        self,
    ) -> None:
        conn = make_conn()
        set_field_row(conn, {"paper_authors": ["x"], "kind": "Paper"})
        store, _engine = make_store(conn)
        await store.set_authors(
            new_uuid(),
            (" Smith ", "Jones", "Smith"),
            actor="alice",
        )
        update = next(
            c
            for c in conn.execute.call_args_list
            if c.args and "UPDATE inquiries SET paper_authors" in _sql(c)
        )
        # Each element stripped; order and the Smith duplicate both kept.
        # paper_authors has no list-encode hook, so the tuple is passed
        # through to asyncpg (which accepts a tuple for TEXT[]).
        assert update.args[1] == ("Smith", "Jones", "Smith")

    @pytest.mark.asyncio
    async def test_judgement_confidence_and_status_are_author_edits(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        set_field_row(conn, {"belief_judgement": "unproven", "kind": "Belief"})
        await store.set_judgement(new_uuid(), "proven", actor="alice")
        set_field_row(conn, {"belief_confidence": 0.5, "kind": "Belief"})
        await store.set_confidence(new_uuid(), 0.75, actor="alice")
        set_field_row(conn, {"status": "active", "kind": "Experiment"})
        await store.set_status(new_uuid(), "complete", actor="alice")
        sqls = executed_sql(conn)
        assert any("UPDATE inquiries SET belief_judgement" in sql for sql in sqls)
        assert any("UPDATE inquiries SET belief_confidence" in sql for sql in sqls)
        assert not any("librarian" in sql for sql in sqls)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "column", "old", "new", "kind"),
        [
            ("set_labels", "labels", ["a"], ["b"], "WebSearch"),
            ("set_subscribers", "subscribers", ["a"], ["b"], "WebSearch"),
            (
                "set_codechanges",
                "experiment_codechanges",
                [new_uuid()],
                [new_uuid()],
                "Experiment",
            ),
        ],
    )
    async def test_sequence_edit_methods(
        self,
        method: str,
        column: str,
        old: list[object],
        new: list[object],
        kind: Inquiry.InquiryKind,
    ) -> None:
        conn = make_conn()
        if method == "set_codechanges":
            # set_codechanges validates each UUID via lookup_kinds.
            target_uuid = new[0]
            conn.fetch.side_effect = [
                [{"id": target_uuid, "kind": "CodeChange"}],
                list[dict[str, object]](),
            ]
        set_field_row(conn, {column: old, "kind": kind})
        store, _engine = make_store(conn)
        await getattr(store, method)(new_uuid(), new, actor="alice")
        sqls = executed_sql(conn)
        assert any(sql.startswith("UPDATE inquiries SET") for sql in sqls)
        assert any("INSERT INTO change_log" in sql for sql in sqls)


class TestAddCostFloor:
    """``add_cost`` rejects deltas that would push a running cost negative.

    The ``Cost`` dataclass enforces nonnegative components on
    construction; reading back a negative ``marginal_cost`` row raises
    inside ``inquiry_cost``. ``add_cost`` must refuse the delta upstream so
    the read path stays valid.
    """

    @pytest.mark.asyncio
    async def test_negative_delta_against_zero_balance_raises(self) -> None:
        conn = make_conn()
        # ``add_cost`` first resolves the subject's kind via
        # ``lookup_kind``, then runs the cost ``UPDATE ... RETURNING``
        # CTE statement that fuses the floor-guarded UPDATE with the
        # presence probe in a single snapshot.
        conn.fetchval.return_value = "Issue"

        async def fetchrow(sql: str, *args: object) -> object:
            if "marginal_cost_agent_usd" in sql and "RETURNING" in sql:
                # Mirror Postgres: the modifying CTE's WHERE rejects
                # rows whose new total would be negative; the outer
                # SELECT LEFT JOINs the probe against the empty update
                # CTE so the cost columns come back as NULLs while the
                # ``existing_id`` reports the row present. Starting
                # balance is zero, so any negative delta misses the
                # predicate and produces this floor-refused shape.
                agent = float(cast(float, args[0]))
                resource = float(cast(float, args[1]))
                subject_id = args[2]
                if agent < 0 or resource < 0:
                    return {
                        "existing_id": subject_id,
                        "old_agent": None,
                        "old_resource": None,
                        "new_agent": None,
                        "new_resource": None,
                        "current_subscribers": None,
                    }
                return {
                    "existing_id": subject_id,
                    "old_agent": 0.0,
                    "old_resource": 0.0,
                    "new_agent": agent,
                    "new_resource": resource,
                    "current_subscribers": list[str](),
                }
            return None

        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError, match="negative"):
            await store.add_cost(
                new_uuid(),
                Cost(agent_usd=-1.0),
                actor="alice",
            )

    @pytest.mark.asyncio
    async def test_negative_delta_does_not_emit_change_log(self) -> None:
        conn = make_conn()
        conn.fetchval.return_value = "Issue"

        async def fetchrow(sql: str, *args: object) -> object:
            if "marginal_cost_agent_usd" in sql and "RETURNING" in sql:
                agent = float(cast(float, args[0]))
                resource = float(cast(float, args[1]))
                subject_id = args[2]
                if agent < 0 or resource < 0:
                    return {
                        "existing_id": subject_id,
                        "old_agent": None,
                        "old_resource": None,
                        "new_agent": None,
                        "new_resource": None,
                        "current_subscribers": None,
                    }
                return {
                    "existing_id": subject_id,
                    "old_agent": 0.0,
                    "old_resource": 0.0,
                    "new_agent": agent,
                    "new_resource": resource,
                    "current_subscribers": list[str](),
                }
            return None

        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError):
            await store.add_cost(
                new_uuid(),
                Cost(resource_usd=-0.5),
                actor="alice",
            )
        sqls = executed_sql(conn)
        # Transaction rolled back: no audit row leaks.
        assert not any("INSERT INTO change_log" in sql for sql in sqls)
        assert any(sql == "ROLLBACK" for sql in sqls)


class TestArtifactLocatorFields:
    """``CodeChange.sha`` and ``WebResult.url`` are editable artifact fields."""

    @pytest.mark.asyncio
    async def test_set_sha_updates_field(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"codechange_sha": "old", "kind": "CodeChange"})
        store, _engine = make_store(conn)
        await store.set_sha(new_uuid(), "new", actor="alice")
        sqls = executed_sql(conn)
        assert any(
            sql.startswith("UPDATE inquiries SET codechange_sha") for sql in sqls
        )
        assert any("INSERT INTO change_log" in sql for sql in sqls)

    @pytest.mark.asyncio
    async def test_set_url_updates_field(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"webresult_url": "https://a", "kind": "WebResult"})
        store, _engine = make_store(conn)
        await store.set_url(new_uuid(), "https://b", actor="alice")
        sqls = executed_sql(conn)
        assert any(sql.startswith("UPDATE inquiries SET webresult_url") for sql in sqls)
        assert any("INSERT INTO change_log" in sql for sql in sqls)

    @pytest.mark.asyncio
    async def test_update_field_allows_artifact_locator_columns(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        await store._update_field(
            cast(Conn, conn),
            new_uuid(),
            "codechange_sha",
            "new_sha",
        )
        await store._update_field(
            cast(Conn, conn),
            new_uuid(),
            "webresult_url",
            "https://b",
        )
        sqls = executed_sql(conn)
        assert any(
            sql.startswith("UPDATE inquiries SET codechange_sha") for sql in sqls
        )
        assert any(sql.startswith("UPDATE inquiries SET webresult_url") for sql in sqls)


class TestSetSourceWhitespace:
    """``set_source`` coerces a whitespace-only value to NULL, not literal."""

    @pytest.mark.asyncio
    async def test_whitespace_source_stores_null(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"paper_source": "arXiv:1", "kind": "Paper"})
        store, _engine = make_store(conn)
        await store.set_source(new_uuid(), "   ", actor="alice")
        update = next(
            c
            for c in conn.execute.call_args_list
            if c.args and "UPDATE inquiries SET paper_source" in _sql(c)
        )
        # "   " is absence: it must store SQL NULL, mirroring the submit
        # boundary's whitespace->None coercion, not the raw "   ".
        assert update.args[1] is None


class TestSettersForwardToThePipeline:
    """Each field route's ``set_<field>`` hands its arguments to ``_set_field``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "route",
        # ``set_title`` re-embeds in its own transaction, and a cost axis writes
        # through ``set_cost_axis``; every other field route's setter is a pipeline's.
        [
            pytest.param(route, id=route.column)
            for route in inquiry_field_routes()
            if route.set_method not in {None, "set_title"}
        ],
    )
    async def test_a_setter_forwards_its_row_value_and_audit_unchanged(
        self,
        route: InquiryFieldRoute,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert route.set_method is not None
        store, _engine = make_store()
        spy = _SetFieldSpy()
        monkeypatch.setattr(store, "_set_field", spy)
        setter = cast(_Setter, getattr(store, route.set_method))
        target_id, api_key_id = new_uuid(), new_uuid()
        # A well-formed source and a non-blank account pass the setters that check.
        value = "doi:10.1/x"

        returned = [
            await setter(target_id, value, api_key_id=api_key_id, actor="alice"),
            await setter(
                target_id,
                value,
                api_key_id=api_key_id,
                actor="alice",
                reason="why",
            ),
        ]

        assert returned == [spy.change_id, spy.change_id]
        assert [
            (call.target_id, call.value, call.api_key_id, call.actor, call.reason)
            for call in spy.calls
        ] == [
            (target_id, value, api_key_id, "alice", ""),
            (target_id, value, api_key_id, "alice", "why"),
        ]
        column = spy.calls[0].column
        assert storage_name(route.column, COLUMN_SPECS[column]) == column


class TestAddCostZeroDelta:
    """``add_cost`` short-circuits a zero delta, matching ``set_cost_axis``."""

    @pytest.mark.asyncio
    async def test_zero_delta_returns_none_writes_no_change(self) -> None:
        conn = make_conn()
        conn.fetchval.return_value = "Issue"
        store, _engine = make_store(conn)
        change_id = await store.add_cost(new_uuid(), Cost(), actor="alice")
        assert change_id is None
        sqls = executed_sql(conn)
        assert not any("INSERT INTO change_log" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_zero_delta_unknown_id_raises_not_found(self) -> None:
        # The zero-guard must run AFTER the id lookup, so add_cost on an
        # unknown id still 404s (matching set_cost_axis) rather than
        # silently returning None.
        conn = make_conn()
        conn.fetchval.return_value = None
        store, _engine = make_store(conn)
        with pytest.raises(NotFoundError, match="not found"):
            await store.add_cost(new_uuid(), Cost(), actor="alice")


class TestReplayProbes:
    """A keyed retry replays its committed change, and only that change."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("existing", "outcome"),
        [
            ({"actor": "alice", "kind": "issue_priority"}, "replay"),
            ({"actor": "bob", "kind": "issue_priority"}, ConflictError),
            ({"actor": "alice", "kind": "labels"}, ConflictError),
        ],
        ids=["same-edit", "other-actor", "other-kind"],
    )
    async def test_a_field_key_names_its_one_edit(
        self,
        existing: dict[str, object],
        outcome: str | type[Exception],
    ) -> None:
        target, key = new_uuid(), new_uuid()
        conn = make_conn()
        set_field_row(conn, {**existing, "subject_id": target})
        store, _engine = make_store(conn)
        set_client_change_id(key)
        try:
            if outcome == "replay":
                assert await _replayed(store, conn, target) == key
            else:
                assert isinstance(outcome, type)
                with pytest.raises(outcome, match="different operation"):
                    await _replayed(store, conn, target)
        finally:
            set_client_change_id(None)

    @pytest.mark.asyncio
    async def test_a_sibling_that_consumed_the_key_first_replays_nothing(self) -> None:
        """Gather siblings share one key; only the one that consumes it replays."""
        target, key = new_uuid(), new_uuid()
        conn = make_conn()
        row: dict[str, object] = {
            "actor": "alice",
            "kind": "issue_priority",
            "subject_id": target,
        }
        conn.fetchrow = AsyncMock(side_effect=partial(_consumed_then, row))
        store, _engine = make_store(conn)
        set_client_change_id(key)

        assert await _replayed(store, conn, target) is None
        set_client_change_id(key)
        async with store.engine.acquire() as acquired:
            claimed = await store._replay_claim(
                acquired,
                owner="w",
                actor="alice",
                principal=None,
            )
        set_client_change_id(None)

        assert claimed is None


class TestNoOpAndRefusedEdits:
    @pytest.mark.asyncio
    async def test_an_unchanged_title_writes_nothing(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"title": "same", "kind": "Issue"})
        store, _engine = make_store(conn)

        assert await store.set_title(new_uuid(), "same", actor="alice") is None
        assert not [s for s in executed_sql(conn) if "UPDATE inquiries" in s]

    @pytest.mark.asyncio
    async def test_a_column_of_another_kind_is_refused(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"issue_priority": None, "kind": "Belief"})
        store, _engine = make_store(conn)

        with pytest.raises(ConflictError, match=r"is a Belief; issue_priority is only"):
            await store.set_priority(new_uuid(), 2, actor="alice")

    @pytest.mark.asyncio
    async def test_an_immutable_column_is_refused_at_both_write_paths(self) -> None:
        column = "agentsession_opened_by_api_key_id"
        conn = make_conn()
        store, _engine = make_store(conn)

        with pytest.raises(ConflictError, match="is immutable"):
            await store._set_field(
                new_uuid(),
                new_uuid(),
                column=column,
                api_key_id=None,
                actor="alice",
            )
        async with store.engine.acquire() as acquired:
            with pytest.raises(ConflictError, match="is immutable"):
                await store._update_field(acquired, new_uuid(), column, new_uuid())

        assert not [s for s in executed_sql(conn) if "UPDATE inquiries" in s]

    @pytest.mark.asyncio
    async def test_an_account_move_locks_and_validates_through_the_policy(
        self,
    ) -> None:
        conn = make_conn()
        set_field_row(conn, {"account": "old@example.com", "kind": "Issue"})
        conn.fetchval.return_value = 1
        store, _engine = make_store(conn)
        policy = RecordingPolicy()
        store.access_policy = policy
        target = new_uuid()

        await store.set_account(target, "new@example.com", actor="admin")

        assert [name for name, _ in policy.calls] == [
            "prepare_account_change",
            "validate",
        ]
        assert all(
            resource
            == Resource(kind="inquiry", id=str(target), account="new@example.com")
            for resource in (
                *policy.of("prepare_account_change"),
                *policy.of("validate"),
            )
        )

    @pytest.mark.asyncio
    async def test_a_blank_claimant_is_refused_before_any_read(self) -> None:
        store, engine = make_store()

        with pytest.raises(ConflictError, match="owner cannot be empty"):
            await store.claim_next_issue(owner="  ", actor="alice")

        assert engine.conn.mock_calls == []

    @pytest.mark.asyncio
    async def test_a_transition_to_where_it_stands_writes_nothing(self) -> None:
        owner_conn, status_conn, judged_conn = make_conn(), make_conn(), make_conn()
        set_field_row(owner_conn, {"owner": "w", "kind": "Issue"})
        set_field_row(status_conn, {"status": "active", "kind": "Issue"})
        set_field_row(judged_conn, {"belief_judgement": None, "kind": "Belief"})
        owners, _ = make_store(owner_conn)
        statuses, _ = make_store(status_conn)
        judgements, _ = make_store(judged_conn)
        target = new_uuid()

        assert (
            await owners.transition_owner(target, expected_from="w", to="w", actor="w")
            is None
        )
        assert (
            await statuses.transition_status(
                target,
                expected_from="active",
                to="active",
                actor="w",
            )
            is None
        )
        assert (
            await judgements.transition_judgement(
                target,
                expected_from=None,
                to=None,
                actor="w",
            )
            is None
        )
        for conn in (owner_conn, status_conn, judged_conn):
            assert not [s for s in executed_sql(conn) if "UPDATE inquiries" in s]

    @pytest.mark.asyncio
    async def test_a_judgement_transition_from_another_state_is_refused(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"belief_judgement": "proven", "kind": "Belief"})
        store, _engine = make_store(conn)

        with pytest.raises(ConflictError, match="expected None, found 'proven'"):
            await store.transition_judgement(
                new_uuid(),
                expected_from=None,
                to="disproven",
                actor="w",
            )

    @pytest.mark.asyncio
    async def test_removing_an_absent_label_writes_nothing(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"labels": ["a"], "kind": "Issue"})
        store, _engine = make_store(conn)

        assert await store.remove_label(new_uuid(), "b", actor="alice") is None
        assert not [s for s in executed_sql(conn) if "UPDATE inquiries" in s]


class TestListFieldForwarding:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("given", [False, True])
    @pytest.mark.parametrize(
        ("method", "column", "include"),
        [
            ("add_author", "paper_authors", True),
            ("remove_author", "paper_authors", False),
            ("add_subscriber", "subscribers", True),
            ("remove_subscriber", "subscribers", False),
            ("add_label", "labels", True),
            ("remove_label", "labels", False),
            ("add_issue_kind", "issue_kind", True),
            ("remove_issue_kind", "issue_kind", False),
            ("add_codechange", "experiment_codechanges", True),
            ("remove_codechange", "experiment_codechanges", False),
            ("add_room", "agentsession_rooms", True),
            ("remove_room", "agentsession_rooms", False),
        ],
    )
    async def test_each_list_edit_names_its_column_and_direction(
        self,
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        column: str,
        *,
        include: bool,
        given: bool,
    ) -> None:
        store, _engine = make_store()
        change = new_uuid()
        mutate = AsyncMock(return_value=change)
        monkeypatch.setattr(store, "_mutate_list_field", mutate)
        target, key = new_uuid(), new_uuid()
        item = new_uuid() if column == "experiment_codechanges" else "bug"

        edit = cast("Callable[..., Awaitable[UUID | None]]", getattr(store, method))
        returned = (
            await edit(target, item, api_key_id=key, actor="alice", reason="r")
            if given
            else await edit(target, item, actor="alice")
        )
        assert returned == change

        call = mutate.await_args
        assert call is not None
        assert call.args == (target, item)
        assert (call.kwargs["column"], call.kwargs["include"]) == (column, include)
        assert (
            call.kwargs["api_key_id"],
            call.kwargs["actor"],
            call.kwargs["reason"],
        ) == (key if given else None, "alice", "r" if given else "")


class TestSetCostAxis:
    @pytest.mark.asyncio
    async def test_an_unknown_row_is_not_found(self) -> None:
        conn = make_conn()
        set_field_row(conn, None)
        store, _engine = make_store(conn)

        with pytest.raises(NotFoundError, match="inquiry not found"):
            await store.set_cost_axis(new_uuid(), "agent_usd", 1.0, actor="alice")

    @pytest.mark.asyncio
    async def test_a_negative_total_is_refused_and_the_same_total_writes_nothing(
        self,
    ) -> None:
        conn = make_conn()
        set_field_row(conn, {"kind": "Issue", "current": 2.5})
        store, _engine = make_store(conn)

        with pytest.raises(ConflictError, match="agent_usd cannot be negative"):
            await store.set_cost_axis(new_uuid(), "agent_usd", -1.0, actor="alice")
        assert (
            await store.set_cost_axis(new_uuid(), "agent_usd", 2.5, actor="a") is None
        )
        assert not [s for s in executed_sql(conn) if "INSERT INTO change_log" in s]


async def _replayed(store: Store, conn: MockConnection, target: UUID) -> UUID | None:
    """Probe ``target``'s priority edit for a committed replay on ``conn``."""
    del conn
    async with store.engine.acquire() as acquired:
        return await store._replay_field_change(
            acquired,
            target,
            "issue_priority",
            actor="alice",
        )


async def _consumed_then(row: dict[str, object], sql: str, *args: object) -> object:
    """Answer ``row`` after a gather sibling has consumed the shared key."""
    del sql, args
    set_client_change_id(None)
    return row


def _sql(call: MockCall) -> str:
    """Narrow a recorded mock argument to the SQL statements under test."""
    sql = call.args[0]
    assert isinstance(sql, str)
    return sql


class _Setter(Protocol):
    """The signature every ``set_<field>`` shares."""

    async def __call__(
        self,
        target_id: UUID,
        value: object,
        /,
        *,
        api_key_id: UUID | None = None,
        actor: str,
        reason: str = "",
    ) -> UUID | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class _SetFieldCall:
    """One call ``_SetFieldSpy`` recorded."""

    target_id: UUID
    value: object
    column: str
    api_key_id: UUID | None
    actor: str
    reason: str


@dataclass(slots=True, kw_only=True)
class _SetFieldSpy:
    """Stands in for ``Store._set_field``: records each call, returns one change id."""

    change_id: UUID = field(default_factory=new_uuid)
    calls: list[_SetFieldCall] = field(default_factory=list[_SetFieldCall])

    async def __call__(
        self,
        target_id: UUID,
        value: object,
        *,
        column: str,
        api_key_id: UUID | None,
        actor: str,
        reason: str = "",
    ) -> UUID:
        """Record the call and return the spy's change id."""
        self.calls.append(
            _SetFieldCall(
                target_id=target_id,
                value=value,
                column=column,
                api_key_id=api_key_id,
                actor=actor,
                reason=reason,
            ),
        )
        return self.change_id


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
