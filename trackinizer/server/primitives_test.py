"""Tests for edge-graph primitives (insert, cycle, kind-lookup, validation)."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import asyncio

import pytest

from trackinizer.conftest import make_conn, new_uuid
from trackinizer.lib.postgres import Conn
from trackinizer.server.primitives import (
    _reject_edge_cycle,
    insert_edge,
    insert_inquiry,
    lookup_kind,
    lookup_kinds,
    validate_edge_priority,
    validate_edge_valence,
    validate_list_references,
)
from trackinizer.types.edges import Edge
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from trackinizer.types.inquiries import CITATION_VALENCE_DEFAULT


class TestInsertInquiryGuards:
    """``insert_inquiry`` re-enforces the safety the enumerated signature gave.

    The derived ``values`` Mapping would otherwise silently drop a typo'd key
    (INS-01) and let a missing required column reach the DB as a raw NOT NULL
    500 (INS-02). Both must raise a ``ValueError`` BEFORE any SQL runs.
    """

    @pytest.mark.asyncio
    async def test_unknown_column_raises_before_sql(self) -> None:
        conn = make_conn()
        with pytest.raises(ValueError, match="unknown column"):
            await insert_inquiry(
                cast(Conn, conn),
                new_uuid(),
                "Paper",
                values={
                    "title": "p",
                    "account": "a@b.c",
                    "paper_soruce": "doi:10.1/x",  # codespell:ignore -- deliberate typo
                },
            )
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_required_column_raises_before_sql(self) -> None:
        conn = make_conn()
        # ``account`` is required; omitting it must raise here, not 500 at the DB.
        with pytest.raises(ValueError, match="missing required column"):
            await insert_inquiry(
                cast(Conn, conn),
                new_uuid(),
                "Issue",
                values={"title": "x"},
            )
        conn.execute.assert_not_called()


class TestPureFunctions:
    @pytest.mark.asyncio
    async def testlookup_kinds_batches_one_query(self) -> None:
        a, b = new_uuid(), new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(
            return_value=[
                {"id": a, "kind": "WebResult"},
                {"id": b, "kind": "Paper"},
            ]
        )
        kinds = await lookup_kinds(cast(Conn, conn), [a, b])
        assert kinds == {a: "WebResult", b: "Paper"}
        assert conn.fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_validate_codechanges_rejects_missing_target(self) -> None:
        missing = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])
        with pytest.raises(ConflictError, match="not found"):
            await validate_list_references(
                cast(Conn, conn), [missing], column="experiment_codechanges"
            )

    @pytest.mark.asyncio
    async def test_lookup_kind_unknown_id_raises_not_found(self) -> None:
        # Not-found is a 404, not a 409: every not-found mutation path must
        # raise NotFoundError so add_edge/add_cost on an unknown id 404.
        conn = make_conn()
        conn.fetchval.return_value = None
        with pytest.raises(NotFoundError, match="not found"):
            await lookup_kind(cast(Conn, conn), new_uuid())

    @pytest.mark.asyncio
    async def test_validate_list_references_missing_raises_not_found(self) -> None:
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])
        with pytest.raises(NotFoundError, match="not found"):
            await validate_list_references(
                cast(Conn, conn), [new_uuid()], column="experiment_codechanges"
            )

    @pytest.mark.asyncio
    async def test_validate_codechanges_rejects_wrong_kind(self) -> None:
        """Bare-UUID list elements (no declared kind) work identically:
        the metadata-driven validator dispatches on element shape.
        """
        good_id = new_uuid()
        bad_id = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(
            return_value=[
                {"id": good_id, "kind": "CodeChange"},
                {"id": bad_id, "kind": "Issue"},
            ]
        )
        await validate_list_references(
            cast(Conn, conn), [good_id], column="experiment_codechanges"
        )
        with pytest.raises(ConflictError, match="Issue"):
            await validate_list_references(
                cast(Conn, conn), [bad_id], column="experiment_codechanges"
            )

    @pytest.mark.asyncio
    async def test_validate_locks_referenced_rows_for_share(self) -> None:
        """The validator's ``lookup_kinds`` call uses ``FOR SHARE`` so
        the referenced rows cannot be purged between the kind check and
        the UPDATE that commits the new list. This is the write-time
        substitute for an FK on JSONB / ``UUID[]`` columns.
        """
        rid = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(
            return_value=[{"id": rid, "kind": "CodeChange"}],
        )
        await validate_list_references(
            cast(Conn, conn), [rid], column="experiment_codechanges"
        )
        (sql, _ids), _kwargs = conn.fetch.call_args
        assert "FOR SHARE" in sql

    @pytest.mark.asyncio
    async def testinsert_edge_rejects_self_loop_and_cycle(
        self,
    ) -> None:
        conn = make_conn()
        target_id = new_uuid()
        # ``insert_edge`` looks up the to-kind before checking self-loops.
        conn.fetchval.return_value = "Issue"
        # Self-loop is input-invalid -> 422 ValidationError.
        with pytest.raises(ValidationError, match="self-loop"):
            await insert_edge(
                conn,
                from_id=target_id,
                from_kind="Issue",
                to_id=target_id,
                edge_kind="narrows",
            )
        conn.fetchval.side_effect = ["Issue", True]
        # A cycle is a STATE conflict -> 409 ConflictError.
        with pytest.raises(ConflictError, match="cycle"):
            await insert_edge(
                conn,
                from_id=new_uuid(),
                from_kind="Issue",
                to_id=new_uuid(),
                edge_kind="narrows",
            )


class TestCLIHelpers:
    def test_edge_priority_is_restricted_to_issue_edges(self) -> None:
        validate_edge_priority("narrows", 10)
        validate_edge_priority("requires", 10)
        validate_edge_priority("proves", None)
        with pytest.raises(ValidationError, match="proves"):
            validate_edge_priority("proves", 10)

    def test_edge_valence_is_restricted_to_citation_edges(self) -> None:
        # ``validate_edge_valence`` is the single storage-boundary guard: it
        # returns the NORMALIZED valence to store. A citation keeps its value;
        # a structural edge stores NULL.
        assert validate_edge_valence("proves", 0.8) == 0.8
        assert validate_edge_valence("favors", -0.5) == -0.5
        assert validate_edge_valence("requires", None) is None
        # A valence on any structural edge is a caller error (4xx, not a raw
        # DB CHECK violation).
        with pytest.raises(ValidationError, match="requires"):
            validate_edge_valence("requires", 0.5)

    def test_edge_valence_defaults_null_citation_to_mild_support(self) -> None:
        # A citation is NEVER stored NULL: an unset valence defaults to the
        # citation default, closing the "citation valence never unset" contract
        # at the single boundary regardless of entry path.
        assert validate_edge_valence("proves", None) == CITATION_VALENCE_DEFAULT
        assert validate_edge_valence("favors", None) == CITATION_VALENCE_DEFAULT

    def test_edge_valence_rejects_out_of_range(self) -> None:
        # The range bound that the create-body Pydantic Field enforces must hold
        # at the storage boundary too, so the per-field PUT route gets a clean
        # 4xx instead of an opaque DB CHECK 409.
        for bad in (1.5, -1.5, float("nan"), float("inf")):
            with pytest.raises(ValidationError, match="valence"):
                validate_edge_valence("proves", bad)

    def test_edge_valence_rejects_unknown_edge_kind(self) -> None:
        # A bogus edge_kind must be rejected by the guard, not silently accepted
        # because it is neither a citation nor a structural kind.
        with pytest.raises(ValidationError, match="edge kind"):
            validate_edge_valence(cast(Edge.Kind, "bogus"), 0.5)

    def test_edge_priority_rejects_unknown_edge_kind(self) -> None:
        with pytest.raises(ValidationError, match="edge kind"):
            validate_edge_priority(cast(Edge.Kind, "bogus"), 5)

    def test_reject_edge_cycle_self_loop(self) -> None:
        """Self-loop is rejected outright before any DB walk."""
        conn = make_conn()
        target = new_uuid()
        with pytest.raises(ValidationError, match="self-loop"):
            asyncio.run(
                _reject_edge_cycle(
                    conn,
                    from_id=target,
                    to_id=target,
                    edge_kind="narrows",
                )
            )


class TestCoverageStoreReadsAndEdits:
    @pytest.mark.asyncio
    async def testinsert_edge_supersedes_allows_distinct_edges(self) -> None:
        conn = make_conn()
        conn.fetchval.side_effect = ["Issue", False, new_uuid()]
        inserted, to_kind = await insert_edge(
            conn,
            from_id=new_uuid(),
            from_kind="Issue",
            to_id=new_uuid(),
            edge_kind="supersedes",
        )
        assert inserted
        assert to_kind == "Issue"


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
