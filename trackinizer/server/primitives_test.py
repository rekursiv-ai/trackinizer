"""Tests for edge-graph primitives (insert, cycle, kind-lookup, validation)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock
from uuid import UUID

import asyncio

import pytest

from trackinizer.conftest import executed_sql, make_conn, new_uuid
from trackinizer.lib.postgres import Conn
from trackinizer.server.access_policy import DefaultPolicy, Principal
from trackinizer.server.primitives import (
    _bind_reference_validators,
    _reject_edge_cycle,
    infer_produced_endpoints,
    insert_edge,
    insert_inquiry,
    lookup_kind,
    lookup_kinds,
    validate_edge_priority,
    validate_edge_valence,
    validate_list_references,
)
from trackinizer.server.schema_gen import SEQ_FOR_KIND
from trackinizer.server.setter_dispatch import (
    COLUMN_SPECS,
    NO_HOOKS,
    RUNTIME_HOOKS,
    TargetValidator,
)
from trackinizer.types.columns import ColumnSpec
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from trackinizer.types.inquiries import CITATION_VALENCE_DEFAULT


if TYPE_CHECKING:
    from trackinizer.types.edges import Edge


_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext($1))"
_CYCLE_SQL = (
    "WITH RECURSIVE descendants(id) AS ("
    "    SELECT $1::uuid "
    "    UNION "
    "    SELECT e.to_id FROM edges e "
    "    JOIN descendants d ON e.from_id = d.id "
    "    WHERE e.edge_kind = $3"
    ") SELECT EXISTS (SELECT 1 FROM descendants WHERE id = $2)"
)
_INSERT_EDGE_SQL = (
    "INSERT INTO edges "
    "(from_id, from_kind, to_id, to_kind, edge_kind, priority, "
    "note, valence, labels) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
    "ON CONFLICT (from_id, to_id, edge_kind) DO NOTHING "
    "RETURNING from_id"
)
_PAIR_KINDS_SQL = (
    "SELECT DISTINCT edge_kind FROM edges "
    "WHERE (from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1)"
)
_CREATED_SQL = "SELECT id, created FROM inquiries WHERE id = ANY($1::uuid[])"


def _keep(value: object) -> object:
    return value


class TestEdgeSql:
    @pytest.mark.asyncio
    async def test_cycle_check_locks_and_walks_the_kinds_own_graph(self) -> None:
        from_id, to_id = new_uuid(), new_uuid()
        conn = make_conn()
        conn.fetchval.return_value = False
        await _reject_edge_cycle(
            cast(Conn, conn),
            from_id=from_id,
            to_id=to_id,
            edge_kind="supersedes",
        )
        conn.execute.assert_awaited_once_with(
            _LOCK_SQL,
            "trackinizer.edge_dag.supersedes",
        )
        conn.fetchval.assert_awaited_once_with(_CYCLE_SQL, to_id, from_id, "supersedes")

    @pytest.mark.asyncio
    async def test_insert_edge_writes_the_edge_row_exactly(self) -> None:
        from_id, to_id = new_uuid(), new_uuid()
        conn = make_conn()
        conn.fetchval.side_effect = ["Issue", False, from_id]
        inserted, to_kind = await insert_edge(
            cast(Conn, conn),
            from_id=from_id,
            from_kind="Issue",
            to_id=to_id,
            edge_kind="narrows",
            priority=3,
            note="hi",
            labels=("a",),
        )
        assert (inserted, to_kind) == (True, "Issue")
        assert conn.fetchval.await_args_list[0].args == (
            "SELECT kind FROM inquiries WHERE id = $1",
            to_id,
        )
        assert conn.fetchval.await_args_list[2].args == (
            _INSERT_EDGE_SQL,
            from_id,
            "Issue",
            to_id,
            "Issue",
            "narrows",
            3,
            "hi",
            None,
            ["a"],
        )

    @pytest.mark.asyncio
    async def test_insert_edge_defaults_an_empty_note_and_an_unset_citation(
        self,
    ) -> None:
        from_id, to_id = new_uuid(), new_uuid()
        conn = make_conn()
        conn.fetchval.side_effect = ["Issue", False, from_id]
        await insert_edge(
            cast(Conn, conn),
            from_id=from_id,
            from_kind="Issue",
            to_id=to_id,
            edge_kind="proves",
        )
        assert conn.fetchval.await_args_list[2].args == (
            _INSERT_EDGE_SQL,
            from_id,
            "Issue",
            to_id,
            "Issue",
            "proves",
            None,
            None,
            CITATION_VALENCE_DEFAULT,
            None,
        )

    @pytest.mark.asyncio
    async def test_insert_edge_stores_the_citation_valence_it_is_given(self) -> None:
        from_id, to_id = new_uuid(), new_uuid()
        conn = make_conn()
        conn.fetchval.side_effect = ["Issue", False, from_id]
        await insert_edge(
            cast(Conn, conn),
            from_id=from_id,
            from_kind="Issue",
            to_id=to_id,
            edge_kind="proves",
            valence=0.25,
        )
        assert conn.fetchval.await_args_list[2].args[8] == 0.25

    @pytest.mark.asyncio
    async def test_insert_edge_refuses_a_priority_on_a_structural_edge(self) -> None:
        conn = make_conn()
        conn.fetchval.return_value = "Issue"
        with pytest.raises(ValidationError, match="cannot carry priority"):
            await insert_edge(
                cast(Conn, conn),
                from_id=new_uuid(),
                from_kind="Issue",
                to_id=new_uuid(),
                edge_kind="proves",
                priority=3,
            )
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_inference_orders_the_pair_by_creation_time(self) -> None:
        younger, older = UUID(int=1), UUID(int=2)
        conn = make_conn()
        conn.fetch = AsyncMock(
            side_effect=[
                [{"edge_kind": "narrows"}],
                [
                    {"id": younger, "created": datetime(2026, 2, 1, tzinfo=UTC)},
                    {"id": older, "created": datetime(2026, 1, 1, tzinfo=UTC)},
                ],
            ],
        )
        assert await infer_produced_endpoints(
            cast(Conn, conn),
            from_id=younger,
            to_id=older,
        ) == (older, younger, "narrows")
        assert conn.fetch.await_args_list[0].args == (_PAIR_KINDS_SQL, younger, older)
        assert conn.fetch.await_args_list[1].args == (_CREATED_SQL, [younger, older])


async def _refused_column(validate: TargetValidator | None) -> str:
    """Return the column ``validate`` names when it refuses a reference not found."""
    assert validate is not None
    conn = make_conn()
    conn.fetch = AsyncMock(return_value=[])
    with pytest.raises(NotFoundError) as refused:
        await validate(cast(Conn, conn), (new_uuid(),))
    return str(refused.value).partition(" ")[0]


class TestReferenceBinding:
    @pytest.mark.asyncio
    async def test_binding_a_column_with_no_hook_yet_uses_the_empty_hooks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        column = "experiment_codechanges"
        monkeypatch.delitem(RUNTIME_HOOKS, column, raising=False)
        _bind_reference_validators()
        hooks = RUNTIME_HOOKS[column]
        assert hooks.normalize is NO_HOOKS.normalize
        assert await _refused_column(hooks.validate) == column

    @pytest.mark.asyncio
    async def test_binding_keeps_each_hook_and_names_its_column(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        column = "experiment_codechanges"
        monkeypatch.setitem(RUNTIME_HOOKS, column, replace(NO_HOOKS, normalize=_keep))
        _bind_reference_validators()
        hooks = RUNTIME_HOOKS[column]
        assert hooks.normalize is _keep
        assert await _refused_column(hooks.validate) == column


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
                    "paper_soruce": "doi:10.1/x",  # codespell:ignore -- deliberate typo.
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

    @pytest.mark.asyncio
    async def test_a_column_required_of_one_kind_is_asked_of_that_kind_only(
        self,
    ) -> None:
        conn = make_conn()
        values = {"title": "x", "account": "a@b.c"}

        with pytest.raises(ValueError, match="agentsession_audience"):
            await insert_inquiry(
                cast(Conn, conn),
                new_uuid(),
                "AgentSession",
                values=values,
            )
        conn.execute.assert_not_called()

        await insert_inquiry(cast(Conn, conn), new_uuid(), "Issue", values=values)
        conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_backfilled_creation_time_is_written_last(self) -> None:
        conn = make_conn()
        created = datetime(2020, 1, 2, tzinfo=UTC)
        await insert_inquiry(
            cast(Conn, conn),
            new_uuid(),
            "Issue",
            values={"title": "x", "account": "a@b.c", "created": created},
        )
        call = conn.execute.call_args
        assert call is not None
        sql = call.args[0]
        assert isinstance(sql, str)
        assert sql.partition(") VALUES")[0].endswith(", created")
        assert call.args[-1] == created

    @pytest.mark.asyncio
    async def test_an_empty_optional_value_is_stored_as_null(self) -> None:
        """An empty description is absence: NULL, never an empty-string sentinel."""
        conn = make_conn()
        await insert_inquiry(
            cast(Conn, conn),
            new_uuid(),
            "Issue",
            values={"title": "x", "account": "a@b.c", "description": "  "},
        )
        call = conn.execute.call_args
        assert call is not None
        sql = call.args[0]
        assert isinstance(sql, str)
        columns = [
            column.strip()
            for column in sql.partition("(")[2].partition(")")[0].split(",")
        ]
        assert call.args[1 + columns.index("description")] is None

    @pytest.mark.asyncio
    async def test_optional_text_is_null_only_when_empty(self) -> None:
        conn = make_conn()
        await insert_inquiry(
            cast(Conn, conn),
            new_uuid(),
            "Issue",
            values={
                "title": "",
                "account": "a@b.c",
                "description": "kept",
            },
        )
        call = conn.execute.call_args
        assert call is not None
        sql = call.args[0]
        assert isinstance(sql, str)
        columns = [
            column.strip()
            for column in sql.partition("(")[2].partition(")")[0].split(",")
        ]
        assert call.args[columns.index("title")] == ""
        assert call.args[columns.index("description")] == "kept"

    @pytest.mark.asyncio
    async def test_blank_text_is_null_and_labels_are_canonical(self) -> None:
        conn = make_conn()
        await insert_inquiry(
            cast(Conn, conn),
            new_uuid(),
            "Issue",
            values={
                "title": "x",
                "account": "a@b.c",
                "description": "  ",
                "labels": [" b ", "a", "", "a"],
            },
        )
        call = conn.execute.call_args
        assert call is not None
        sql = call.args[0]
        assert isinstance(sql, str)
        columns = [
            column.strip()
            for column in sql.partition("(")[2].partition(")")[0].split(",")
        ]
        assert call.args[columns.index("description")] is None
        assert call.args[columns.index("labels")] == ["b", "a"]

    @pytest.mark.asyncio
    async def test_each_value_binds_to_its_numbered_placeholder(self) -> None:
        conn = make_conn()
        row_id = new_uuid()
        await insert_inquiry(
            cast(Conn, conn),
            row_id,
            "Issue",
            status="complete",
            values={"title": "x", "account": "a@b.c"},
        )
        call = conn.execute.call_args
        assert call is not None
        sql = call.args[0]
        assert isinstance(sql, str)
        assert sql.startswith("INSERT INTO inquiries (id, kind, seq, status, ")
        columns = [
            column.strip()
            for column in sql.partition("(")[2].partition(")")[0].split(",")
        ]
        placeholders = ", ".join(f"${i}" for i in range(4, len(columns)))
        assert sql.partition(") VALUES (")[2] == (
            f"$1, $2, nextval('{SEQ_FOR_KIND['Issue']}'), COALESCE($3, 'active'), "
            f"{placeholders})"
        )
        assert call.args[1:4] == (row_id, "Issue", "complete")
        assert call.args[columns.index("title")] == "x"
        assert call.args[columns.index("account")] == "a@b.c"


class TestPureFunctions:
    @pytest.mark.asyncio
    async def testlookup_kinds_batches_one_query(self) -> None:
        a, b = new_uuid(), new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(
            return_value=[
                {"id": a, "kind": "WebResult"},
                {"id": b, "kind": "Paper"},
            ],
        )
        kinds = await lookup_kinds(cast(Conn, conn), [a, b])
        assert kinds == {a: "WebResult", b: "Paper"}
        assert conn.fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_lookup_kinds_of_no_ids_reads_nothing(self) -> None:
        conn = make_conn()
        assert await lookup_kinds(cast(Conn, conn), []) == {}
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_declared_kind_must_match_the_row(self) -> None:
        rid = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[{"id": rid, "kind": "CodeChange"}])
        await validate_list_references(
            cast(Conn, conn),
            [(rid, "CodeChange")],
            column="experiment_codechanges",
        )
        with pytest.raises(
            ConflictError,
            match="declared as Paper but is a CodeChange",
        ):
            await validate_list_references(
                cast(Conn, conn),
                [(rid, "Paper")],
                column="experiment_codechanges",
            )
        assert [call.args[1] for call in conn.fetch.call_args_list] == [[rid], [rid]]

    @pytest.mark.asyncio
    async def test_no_references_reads_nothing(self) -> None:
        conn = make_conn()
        await validate_list_references(
            cast(Conn, conn),
            [],
            column="experiment_codechanges",
        )
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_wrong_kind_names_every_permitted_kind(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(
            COLUMN_SPECS,
            "cites",
            ColumnSpec(sql_type="UUID[]", references=frozenset({"Paper", "WebResult"})),
        )
        rid = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[{"id": rid, "kind": "Issue"}])
        with pytest.raises(
            ConflictError,
            match=f"^cites target {rid} is a Issue; entries must be Paper or WebResult$",
        ):
            await validate_list_references(cast(Conn, conn), [rid], column="cites")

    @pytest.mark.asyncio
    async def test_a_reference_column_validates_through_its_runtime_hook(self) -> None:
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])
        validate = RUNTIME_HOOKS["experiment_codechanges"].validate
        assert validate is not None
        with pytest.raises(NotFoundError, match="experiment_codechanges target"):
            await validate(cast(Conn, conn), (new_uuid(),))

    @pytest.mark.asyncio
    async def test_a_column_without_references_is_a_caller_bug(self) -> None:
        conn = make_conn()
        with pytest.raises(AssertionError, match="'title' with no references"):
            await validate_list_references(
                cast(Conn, conn),
                [new_uuid()],
                column="title",
            )
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_validate_codechanges_rejects_missing_target(self) -> None:
        missing = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])
        with pytest.raises(ConflictError, match="not found"):
            await validate_list_references(
                cast(Conn, conn),
                [missing],
                column="experiment_codechanges",
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
    async def test_lookup_kind_reads_the_kind_of_its_one_id(self) -> None:
        target = new_uuid()
        conn = make_conn()
        conn.fetchval.return_value = "Paper"
        assert await lookup_kind(cast(Conn, conn), target) == "Paper"
        conn.fetchval.assert_awaited_once_with(
            "SELECT kind FROM inquiries WHERE id = $1",
            target,
        )

    @pytest.mark.asyncio
    async def test_lookup_kinds_reads_unlocked_rows_without_a_policy(self) -> None:
        target = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])
        await lookup_kinds(cast(Conn, conn), [target])
        conn.fetch.assert_awaited_once_with(
            "SELECT id, kind FROM inquiries WHERE id = ANY($1::uuid[])",
            [target],
        )

    @pytest.mark.asyncio
    async def test_lookup_kinds_locks_rows_only_when_asked(self) -> None:
        target = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])
        await lookup_kinds(cast(Conn, conn), [target], for_share=True)
        conn.fetch.assert_awaited_once_with(
            "SELECT id, kind FROM inquiries WHERE id = ANY($1::uuid[]) FOR SHARE",
            [target],
        )

    @pytest.mark.asyncio
    async def test_lookup_kinds_gates_rows_by_the_policy_and_principal(self) -> None:
        target = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])
        await lookup_kinds(
            cast(Conn, conn),
            [target],
            policy=DefaultPolicy(),
            principal=Principal(email="a@b.c", role="writer"),
        )
        sql, ids, *params = cast("tuple[object, ...]", conn.fetch.call_args.args)
        assert ids == [target]
        assert sql == "SELECT id, kind FROM inquiries WHERE id = ANY($1::uuid[])"
        assert params == []

    @pytest.mark.asyncio
    async def test_validate_list_references_missing_raises_not_found(self) -> None:
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])
        with pytest.raises(NotFoundError, match="not found"):
            await validate_list_references(
                cast(Conn, conn),
                [new_uuid()],
                column="experiment_codechanges",
            )

    @pytest.mark.asyncio
    async def test_validate_codechanges_rejects_wrong_kind(self) -> None:
        """Bare-UUID list elements (no declared kind) work identically.

        The metadata-driven validator dispatches on element shape.
        """
        good_id = new_uuid()
        bad_id = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(
            return_value=[
                {"id": good_id, "kind": "CodeChange"},
                {"id": bad_id, "kind": "Issue"},
            ],
        )
        await validate_list_references(
            cast(Conn, conn),
            [good_id],
            column="experiment_codechanges",
        )
        with pytest.raises(ConflictError, match="Issue"):
            await validate_list_references(
                cast(Conn, conn),
                [bad_id],
                column="experiment_codechanges",
            )

    @pytest.mark.asyncio
    async def test_validate_locks_referenced_rows_for_share(self) -> None:
        """The validator's ``lookup_kinds`` call uses ``FOR SHARE`` so the.

        Referenced rows cannot be purged between the kind check and the UPDATE that
        commits the new list. This is the write-time substitute for an FK on JSONB /
        ``UUID[]`` columns.
        """
        rid = new_uuid()
        conn = make_conn()
        conn.fetch.return_value = [{"id": rid, "kind": "CodeChange"}]
        await validate_list_references(
            cast(Conn, conn),
            [rid],
            column="experiment_codechanges",
        )
        recorded = conn.fetch.call_args
        assert recorded is not None
        sql = recorded.args[0]
        assert isinstance(sql, str)
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
                cast(Conn, conn),
                from_id=target_id,
                from_kind="Issue",
                to_id=target_id,
                edge_kind="narrows",
            )
        conn.fetchval.side_effect = ["Issue", True]
        # A cycle is a STATE conflict -> 409 ConflictError.
        with pytest.raises(ConflictError, match="cycle"):
            await insert_edge(
                cast(Conn, conn),
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

    def test_edge_valence_accepts_the_range_endpoints(self) -> None:
        assert validate_edge_valence("proves", -1.0) == -1.0
        assert validate_edge_valence("proves", 1.0) == 1.0

    def test_edge_valence_rejects_unknown_edge_kind(self) -> None:
        # A bogus edge_kind must be rejected by the guard, not silently accepted
        # because it is neither a citation nor a structural kind.
        with pytest.raises(ValidationError, match="edge kind"):
            validate_edge_valence("bogus", 0.5)  # ty: ignore[invalid-argument-type] -- negative test passes an intentionally invalid kind.  # pyright: ignore[reportArgumentType] -- negative test passes an intentionally invalid kind.  # pyrefly: ignore[bad-argument-type] -- negative test passes an intentionally invalid kind.

    def test_edge_priority_rejects_unknown_edge_kind(self) -> None:
        with pytest.raises(ValidationError, match="edge kind"):
            validate_edge_priority("bogus", 5)  # ty: ignore[invalid-argument-type] -- negative test passes an intentionally invalid kind.  # pyright: ignore[reportArgumentType] -- negative test passes an intentionally invalid kind.  # pyrefly: ignore[bad-argument-type] -- negative test passes an intentionally invalid kind.

    def test_reject_edge_cycle_self_loop(self) -> None:
        """Self-loop is rejected outright before any DB walk."""
        conn = make_conn()
        target = new_uuid()
        with pytest.raises(ValidationError, match="self-loop"):
            asyncio.run(
                _reject_edge_cycle(
                    cast(Conn, conn),
                    from_id=target,
                    to_id=target,
                    edge_kind="narrows",
                ),
            )

    @pytest.mark.asyncio
    async def test_reject_edge_cycle_refuses_an_unknown_kind(self) -> None:
        conn = make_conn()
        # Read from a list so the checkers see a plain ``str``, not a literal.
        unknown = ["likes"]
        with pytest.raises(ValidationError, match="unknown edge_kind 'likes'"):
            await _reject_edge_cycle(
                cast(Conn, conn),
                from_id=new_uuid(),
                to_id=new_uuid(),
                edge_kind=cast("Edge.Kind", unknown[0]),
            )
        assert executed_sql(conn) == []

    @pytest.mark.asyncio
    async def test_a_bibliography_may_cite_in_a_circle(self) -> None:
        conn = make_conn()
        await _reject_edge_cycle(
            cast(Conn, conn),
            from_id=new_uuid(),
            to_id=new_uuid(),
            edge_kind="cites_paper",
        )
        assert executed_sql(conn) == []


class TestInferProducedEndpoints:
    @pytest.mark.parametrize(
        "present",
        [[], ["cites_paper"], ["produced_by", "narrows"]],
    )
    @pytest.mark.asyncio
    async def test_no_inference_without_a_structural_edge_or_with_provenance(
        self,
        present: list[str],
    ) -> None:
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[{"edge_kind": k} for k in present])
        assert (
            await infer_produced_endpoints(
                cast(Conn, conn),
                from_id=new_uuid(),
                to_id=new_uuid(),
            )
            is None
        )
        assert conn.fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_the_older_end_produced_the_younger(self) -> None:
        older, younger = new_uuid(), new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(
            side_effect=[
                [{"edge_kind": "requires"}, {"edge_kind": "narrows"}],
                [
                    {"id": younger, "created": datetime(2026, 2, 1, tzinfo=UTC)},
                    {"id": older, "created": datetime(2026, 1, 1, tzinfo=UTC)},
                ],
            ],
        )
        assert await infer_produced_endpoints(
            cast(Conn, conn),
            from_id=younger,
            to_id=older,
        ) == (older, younger, "narrows")


class TestCoverageStoreReadsAndEdits:
    @pytest.mark.asyncio
    async def testinsert_edge_supersedes_allows_distinct_edges(self) -> None:
        conn = make_conn()
        conn.fetchval.side_effect = ["Issue", False, new_uuid()]
        inserted, to_kind = await insert_edge(
            cast(Conn, conn),
            from_id=new_uuid(),
            from_kind="Issue",
            to_id=new_uuid(),
            edge_kind="supersedes",
        )
        assert inserted
        assert to_kind == "Issue"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
