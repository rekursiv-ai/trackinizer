"""Tests for ``Store`` field edits -- set_*/add_*/remove_* setters and cost."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import asyncio
import uuid

import pytest

from trackinizer.conftest import (
    executed_sql,
    make_conn,
    make_store,
    new_uuid,
    set_field_row,
)
from trackinizer.types.cost import Cost
from trackinizer.types.errors import ConflictError, NotFoundError
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
            c
            for c in conn.execute.call_args_list
            if "UPDATE inquiries SET" in c.args[0]
        )
        assert update.args[1] == stored

    @pytest.mark.asyncio
    async def test_atomic_list_mutation_canonicalizes_item(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"labels": [], "kind": "Issue"})
        store, _engine = make_store(conn)
        await store.add_label(new_uuid(), "  x  ", actor="alice")
        update = next(
            c
            for c in conn.execute.call_args_list
            if "UPDATE inquiries SET" in c.args[0]
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
            if c.args and "UPDATE inquiries SET paper_authors" in c.args[0]
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
            if c.args and "UPDATE inquiries SET paper_authors" in c.args[0]
        )
        assert update.args[1] == ("Smith", "Smith")

    @pytest.mark.asyncio
    async def test_remove_author_drops_first_match_only(self) -> None:
        # A byline remove drops ONE occurrence (the first), not every match:
        # set arithmetic would delete both Smiths.
        conn = make_conn()
        set_field_row(
            conn, {"paper_authors": ["Smith", "Jones", "Smith"], "kind": "Paper"}
        )
        store, _engine = make_store(conn)
        await store.remove_author(new_uuid(), "Smith", actor="alice")
        update = next(
            c
            for c in conn.execute.call_args_list
            if c.args and "UPDATE inquiries SET paper_authors" in c.args[0]
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
            new_uuid(), (" Smith ", "Jones", "Smith"), actor="alice"
        )
        update = next(
            c
            for c in conn.execute.call_args_list
            if c.args and "UPDATE inquiries SET paper_authors" in c.args[0]
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
            target_uuid = cast(uuid.UUID, new[0])
            conn.fetch.side_effect = [
                [{"id": target_uuid, "kind": "CodeChange"}],
                [],
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
    inside ``cost_for``. ``add_cost`` must refuse the delta upstream so
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

        async def fetchrow(sql: str, *args: Any) -> Any:
            if "marginal_cost_agent_usd" in sql and "RETURNING" in sql:
                # Mirror Postgres: the modifying CTE's WHERE rejects
                # rows whose new total would be negative; the outer
                # SELECT LEFT JOINs the probe against the empty update
                # CTE so the cost columns come back as NULLs while the
                # ``existing_id`` reports the row present. Starting
                # balance is zero, so any negative delta misses the
                # predicate and produces this floor-refused shape.
                agent = float(args[0])
                resource = float(args[1])
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
                    "current_subscribers": [],
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

        async def fetchrow(sql: str, *args: Any) -> Any:
            if "marginal_cost_agent_usd" in sql and "RETURNING" in sql:
                agent = float(args[0])
                resource = float(args[1])
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
                    "current_subscribers": [],
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
        await store._update_field(conn, new_uuid(), "codechange_sha", "new_sha")
        await store._update_field(conn, new_uuid(), "webresult_url", "https://b")
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
            if c.args and "UPDATE inquiries SET paper_source" in c.args[0]
        )
        # "   " is absence: it must store SQL NULL, mirroring the submit
        # boundary's whitespace->None coercion, not the raw "   ".
        assert update.args[1] is None


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


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
