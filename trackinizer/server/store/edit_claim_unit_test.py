"""Issue acquisition decisions with database and projection boundaries faked."""

from __future__ import annotations

from typing import TYPE_CHECKING, override
from unittest.mock import patch

import pytest

from trackinizer.conftest import (
    BEGIN,
    COMMIT,
    ExchangeEngine,
    RecordingPolicy,
    Says,
    executed_sql,
    make_conn,
    make_store,
    new_uuid,
    queue_field_rows,
)
from trackinizer.server.access_policy import Principal
from trackinizer.server.audience import filed_readable_sql
from trackinizer.server.sql_fragments import CLAIM_NEXT_ISSUE_SQL
from trackinizer.server.store import edit
from trackinizer.server.store.change_id_slot import (
    _peek_client_change_id,
    set_client_change_id,
)
from trackinizer.server.store.core import Store
from trackinizer.server.values import vetted_sql
from trackinizer.types.change_log import Snapshot
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.inquiries import Issue


if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from trackinizer.server.access_policy import ResourceKind


@pytest.fixture
def claim_key() -> Iterator[UUID]:
    """Install one request's key and clear its shared cursor afterward."""
    key = new_uuid()
    set_client_change_id(key)
    try:
        yield key
    finally:
        set_client_change_id(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["", " \t "])
async def test_blank_claimant_never_opens_a_transaction(owner: str) -> None:
    store, engine = make_store()
    with pytest.raises(ConflictError, match="owner cannot be empty"):
        await store.claim_next_issue(owner=owner, actor="dispatcher")
    assert engine.conn.mock_calls == []


@pytest.mark.asyncio
async def test_no_candidate_does_not_emit_an_owner_change(claim_key: UUID) -> None:
    conn = make_conn()
    queue_field_rows(conn, None, None)
    store, engine = make_store(conn)
    with patch.object(Store, "_emit_field_change") as emit:
        assert await store.claim_next_issue(owner="worker", actor="dispatcher") is None
    emit.assert_not_called()
    assert [call.args for call in conn.fetchrow.call_args_list] == [
        (
            (
                "SELECT subject_id, subject_kind, kind, actor, old_owner, new_owner "
                "FROM change_log WHERE id = $1"
            ),
            claim_key,
        ),
        (CLAIM_NEXT_ISSUE_SQL.format(readable=""), "worker"),
    ]
    assert executed_sql(conn) == [
        "BEGIN",
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        "COMMIT",
    ]
    assert _peek_client_change_id() == claim_key
    assert engine.notify_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [False, True])
async def test_new_claim_audits_attribution_and_projects_visible_edges(
    restricted: bool,
) -> None:
    conn = make_conn()
    target, api_key = new_uuid(), new_uuid()
    row = {"id": target, "kind": "Issue"}
    queue_field_rows(conn, row)
    store, _engine = make_store(conn)
    policy = AccountPolicy()
    store.access_policy = policy
    principal = (
        Principal(email="reader@example.com", role="writer") if restricted else None
    )
    issue = Issue(id=target, owner="worker")
    outbound = [{"to_id": new_uuid()}]
    inbound = [{"from_id": new_uuid()}]
    with (
        patch.object(Store, "_emit_field_change") as emit,
        patch.object(edit, "fetch_edges", return_value=(outbound, inbound)) as edges,
        patch.object(edit, "materialize", return_value=issue) as project,
    ):
        claimed = await store.claim_next_issue(
            owner="worker",
            api_key_id=api_key,
            actor="dispatcher",
            reason="next task",
            principal=principal,
        )
    assert claimed is issue
    gate, bound = _chat_gate(after=2, email="reader@example.com")
    readable = (
        vetted_sql(
            " AND (EXISTS (SELECT 1 FROM inquiries readable_inquiry WHERE "
            "readable_inquiry.id = issue.id AND ((readable_inquiry.account = $2) "
            "AND ",
            gate,
            ")))",
        )
        if restricted
        else ""
    )
    args = ("worker", principal.email, *bound) if principal is not None else ("worker",)
    conn.fetchrow.assert_awaited_once_with(
        CLAIM_NEXT_ISSUE_SQL.format(readable=readable),
        *args,
    )
    emit.assert_awaited_once_with(
        conn,
        target,
        "Issue",
        "owner",
        Snapshot(owner=None),
        new=Snapshot(owner="worker"),
        api_key_id=api_key,
        actor="dispatcher",
        reason="next task",
    )
    edges.assert_awaited_once_with(conn, target, policy=policy, principal=principal)
    project.assert_called_once_with(row, {target: outbound}, {target: inbound})
    assert executed_sql(conn) == ["BEGIN", "COMMIT"]


@pytest.mark.asyncio
async def test_retry_locks_before_replay_and_never_allocates_again(
    claim_key: UUID,
) -> None:
    issue = Issue(owner="worker")
    row = {"id": issue.id, "status": "active", "owner": "worker"}
    engine = ExchangeEngine(
        BEGIN,
        Says(
            method="execute",
            sql="SELECT pg_advisory_xact_lock(hashtext($1))",
            args=(f"claim_next_issue:{claim_key}",),
        ),
        Says(
            method="fetchrow",
            sql="SELECT subject_id, subject_kind, kind, actor, old_owner, new_owner "
            "FROM change_log WHERE id = $1",
            args=(claim_key,),
            answer=_claim_audit(issue.id),
        ),
        Says(
            method="fetchrow",
            sql="SELECT * FROM inquiries WHERE id = $1",
            args=(issue.id,),
            answer=row,
        ),
        COMMIT,
    )
    store, store_engine = make_store(engine.conn)
    with (
        patch.object(Store, "_emit_field_change") as emit,
        patch.object(edit, "fetch_edges", return_value=([], [])),
        patch.object(edit, "materialize", return_value=issue),
    ):
        assert await store.claim_next_issue(owner="worker", actor="dispatcher") is issue
    assert engine.done
    emit.assert_not_called()
    assert _peek_client_change_id() is None
    assert store_engine.notify_calls == []


@pytest.mark.asyncio
async def test_replay_without_a_key_does_not_query() -> None:
    store, engine = make_store()
    async with store.engine.acquire() as conn:
        assert (
            await store._replay_claim(
                conn,
                owner="worker",
                actor="dispatcher",
                principal=None,
            )
            is None
        )
    engine.conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_loses_to_a_sibling_consuming_the_shared_key(
    claim_key: UUID,
) -> None:
    conn = make_conn()
    audit = _claim_audit(new_uuid())

    async def sibling_consumes(sql: str, *args: object) -> dict[str, object]:
        del sql, args
        set_client_change_id(None)
        return audit

    conn.fetchrow.side_effect = sibling_consumes
    store, _engine = make_store(conn)
    async with store.engine.acquire() as acquired:
        assert (
            await store._replay_claim(
                acquired,
                owner="worker",
                actor="dispatcher",
                principal=None,
            )
            is None
        )
    conn.fetchrow.assert_awaited_once_with(
        "SELECT subject_id, subject_kind, kind, actor, old_owner, new_owner "
        "FROM change_log WHERE id = $1",
        claim_key,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "title"),
        ("subject_kind", "Belief"),
        ("actor", "other"),
        ("old_owner", "previous"),
        ("new_owner", "other"),
    ],
)
async def test_replay_refuses_each_mismatched_operation_component(
    claim_key: UUID,
    field: str,
    value: str,
) -> None:
    conn = make_conn()
    audit = _claim_audit(new_uuid())
    audit[field] = value
    queue_field_rows(conn, audit)
    store, _engine = make_store(conn)
    async with store.engine.acquire() as acquired:
        with pytest.raises(
            ConflictError,
            match=f"idempotency_key {claim_key} already used",
        ):
            await store._replay_claim(
                acquired,
                owner="worker",
                actor="dispatcher",
                principal=None,
            )
    assert conn.fetchrow.await_count == 1
    assert _peek_client_change_id() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "error", "diagnostic"),
    [
        (None, NotFoundError, "no longer exists"),
        (
            {"status": "complete", "owner": "worker"},
            ConflictError,
            "is no longer active and owned by 'worker'",
        ),
        (
            {"status": "active", "owner": None},
            ConflictError,
            "is no longer active and owned by 'worker'",
        ),
        (
            {"status": "active", "owner": "other"},
            ConflictError,
            "is no longer active and owned by 'worker'",
        ),
    ],
)
async def test_replay_reports_missing_or_changed_issue_without_substitution(
    claim_key: UUID,
    state: dict[str, object] | None,
    error: type[Exception],
    diagnostic: str,
) -> None:
    conn = make_conn()
    target = new_uuid()
    queue_field_rows(conn, _claim_audit(target), state)
    store, _engine = make_store(conn)
    async with store.engine.acquire() as acquired:
        with pytest.raises(error) as raised:
            await store._replay_claim(
                acquired,
                owner="worker",
                actor="dispatcher",
                principal=None,
            )
    assert str(raised.value) == f"claimed issue {target} {diagnostic}"
    assert [call.args for call in conn.fetchrow.call_args_list] == [
        (
            (
                "SELECT subject_id, subject_kind, kind, actor, old_owner, new_owner "
                "FROM change_log WHERE id = $1"
            ),
            claim_key,
        ),
        ("SELECT * FROM inquiries WHERE id = $1", target),
    ]
    assert _peek_client_change_id() is None


@pytest.mark.asyncio
async def test_replay_filters_the_original_issue_and_its_edges(claim_key: UUID) -> None:
    conn = make_conn()
    issue = Issue(owner="worker")
    row = {"id": issue.id, "status": "active", "owner": "worker"}
    queue_field_rows(conn, _claim_audit(issue.id), row)
    store, _engine = make_store(conn)
    policy = AccountPolicy()
    store.access_policy = policy
    principal = Principal(email="reader@example.com", role="writer")
    outbound = [{"to_id": new_uuid()}]
    inbound = [{"from_id": new_uuid()}]
    with (
        patch.object(edit, "fetch_edges", return_value=(outbound, inbound)) as edges,
        patch.object(edit, "materialize", return_value=issue) as project,
    ):
        async with store.engine.acquire() as acquired:
            assert (
                await store._replay_claim(
                    acquired,
                    owner="worker",
                    actor="dispatcher",
                    principal=principal,
                )
                is issue
            )
    gate, bound = _chat_gate(after=2, email=principal.email)
    conn.fetchrow.assert_awaited_with(
        vetted_sql(
            "SELECT * FROM inquiries WHERE id = $1 AND (EXISTS (SELECT 1 FROM "
            "inquiries readable_inquiry WHERE readable_inquiry.id = inquiries.id "
            "AND ((readable_inquiry.account = $2) AND ",
            gate,
            ")))",
        ),
        issue.id,
        principal.email,
        *bound,
    )
    assert conn.fetchrow.call_args_list[0].args[1:] == (claim_key,)
    edges.assert_awaited_once_with(conn, issue.id, policy=policy, principal=principal)
    project.assert_called_once_with(row, {issue.id: outbound}, {issue.id: inbound})
    assert _peek_client_change_id() is None


def _chat_gate(*, after: int, email: str) -> tuple[str, list[object]]:
    """Return the chat test every gated read carries for ``email``, after ``after`` binds."""
    params: list[object] = [None] * after
    clause = filed_readable_sql("readable_inquiry", params=params, email=email)
    return clause, params[after:]


def _claim_audit(target: UUID) -> dict[str, object]:
    return {
        "subject_id": target,
        "subject_kind": "Issue",
        "kind": "owner",
        "actor": "dispatcher",
        "old_owner": None,
        "new_owner": "worker",
    }


class AccountPolicy(RecordingPolicy):
    """Bind a reader parameter after the query's pre-existing parameters."""

    @override
    def read_filter(
        self,
        kind: ResourceKind,
        alias: str,
        *,
        params: list[object],
        principal: Principal,
    ) -> str:
        assert kind == "inquiry"
        params.append(principal.email)
        return f"{alias}.account = ${len(params)}"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
