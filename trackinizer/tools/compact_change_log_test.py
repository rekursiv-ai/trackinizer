"""Unit tests for offline ``change_log`` compaction helpers and CLI."""

from __future__ import annotations

from typing import cast, override
from unittest.mock import AsyncMock

import argparse
import io
import sys

import asyncpg
import pytest

from trackinizer.conftest import make_conn
from trackinizer.lib.postgres import Conn
from trackinizer.tools import compact_change_log


def _sample_schema() -> compact_change_log.TableSchema:
    return compact_change_log.TableSchema(
        schema="public",
        owner="trackinizer",
        current_user="trackinizer",
        reloptions=("autovacuum_vacuum_scale_factor=0.05",),
        primary_key=compact_change_log.ConstraintSpec(
            name="change_log_pkey",
            contype="p",
            definition="PRIMARY KEY (id)",
            is_self_ref=False,
        ),
        unique_constraints=(),
        foreign_keys=(
            compact_change_log.ConstraintSpec(
                name="change_log_caused_by_fkey",
                contype="f",
                definition="FOREIGN KEY (caused_by) REFERENCES change_log(id)",
                is_self_ref=True,
            ),
            compact_change_log.ConstraintSpec(
                name="change_log_api_key_id_fkey",
                contype="f",
                definition="FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE SET NULL",
                is_self_ref=False,
            ),
        ),
        secondary_indexes=(
            compact_change_log.IndexSpec(
                name="idx_change_log_created",
                definition="CREATE INDEX idx_change_log_created ON public.change_log USING btree (created)",
            ),
            compact_change_log.IndexSpec(
                name="idx_change_log_subscribed_created_id",
                definition=(
                    "CREATE INDEX idx_change_log_subscribed_created_id "
                    "ON public.change_log USING btree (created, id) "
                    "WHERE (subscribers_snapshot <> '{}'::text[])"
                ),
            ),
        ),
        has_explicit_acl=True,
        grants=(
            compact_change_log.GrantSpec(
                grantee="reader_role",
                privilege="SELECT",
                is_grantable=False,
            ),
        ),
    )


def _sample_plan(
    *,
    blockers: tuple[str, ...] = (),
    active_other_connections: int = 0,
) -> compact_change_log.CompactionPlan:
    return compact_change_log.CompactionPlan(
        table_schema=_sample_schema(),
        total_rows=1000,
        prune_rows=970,
        keep_rows=30,
        reparent_rows=4,
        total_bytes=100_000_000,
        heap_bytes=60_000_000,
        index_bytes=40_000_000,
        estimated_new_bytes=3_000_000,
        other_connections=active_other_connections,
        active_other_connections=active_other_connections,
        blockers=blockers,
    )


def test_quote_ident_escapes_embedded_double_quotes() -> None:
    assert compact_change_log.quote_ident("change_log") == '"change_log"'
    assert compact_change_log.quote_ident('a"b') == '"a""b"'


def test_staged_name_respects_postgres_63_byte_limit_without_collisions() -> None:
    used: set[str] = set()
    long_a = "idx_" + ("a" * 57) + "_1"
    long_b = "idx_" + ("a" * 57) + "_2"
    staged_a = compact_change_log.staged_name(long_a, "__new", used)
    staged_b = compact_change_log.staged_name(long_b, "__new", used)
    assert len(staged_a.encode("utf-8")) <= 63
    assert len(staged_b.encode("utf-8")) <= 63
    assert staged_a != staged_b
    assert staged_a.endswith("__new")
    assert staged_b.endswith("__new")


def test_rewrite_index_def_targets_staged_table_and_index_name() -> None:
    src = (
        "CREATE INDEX idx_change_log_subscribed_created_id "
        "ON public.change_log USING btree (created, id) "
        "WHERE (subscribers_snapshot <> '{}'::text[])"
    )
    rewritten = compact_change_log.rewrite_index_def(
        src,
        schema="public",
        old_table="change_log",
        new_table="change_log_new",
        old_index="idx_change_log_subscribed_created_id",
        new_index="idx_change_log_subscribed_created_id__new",
    )
    assert rewritten == (
        'CREATE INDEX "idx_change_log_subscribed_created_id__new" '
        'ON "public"."change_log_new" USING btree (created, id) '
        "WHERE (subscribers_snapshot <> '{}'::text[])"
    )


def test_rewrite_index_def_preserves_unique_keyword_and_rejects_mismatch() -> None:
    src = 'CREATE UNIQUE INDEX "idx_u" ON "public"."change_log" USING btree (id)'
    rewritten = compact_change_log.rewrite_index_def(
        src,
        schema="public",
        old_table="change_log",
        new_table="change_log_new",
        old_index="idx_u",
        new_index="idx_u__new",
    )
    assert rewritten == (
        'CREATE UNIQUE INDEX "idx_u__new" ON "public"."change_log_new" USING btree (id)'
    )
    with pytest.raises(ValueError, match="Unexpected index definition"):
        compact_change_log.rewrite_index_def(
            "CREATE INDEX other ON public.other USING btree (id)",
            schema="public",
            old_table="change_log",
            new_table="change_log_new",
            old_index="idx_u",
            new_index="idx_u__new",
        )


@pytest.mark.parametrize(
    "definition",
    [
        "FOREIGN KEY (caused_by) REFERENCES change_log(id)",
        "FOREIGN KEY (caused_by) REFERENCES public.change_log(id)",
        'FOREIGN KEY (caused_by) REFERENCES "public"."change_log"(id)',
    ],
)
def test_rewrite_self_fk_def_targets_new_table(definition: str) -> None:
    rewritten = compact_change_log.rewrite_self_fk_def(
        definition,
        schema="public",
        old_table="change_log",
        new_table="change_log_new",
    )
    assert (
        rewritten == 'FOREIGN KEY (caused_by) REFERENCES "public"."change_log_new"(id)'
    )


def test_rewrite_self_fk_def_rejects_unexpected_definition() -> None:
    with pytest.raises(ValueError, match="Unexpected self-referencing FK"):
        compact_change_log.rewrite_self_fk_def(
            "FOREIGN KEY (api_key_id) REFERENCES api_keys(id)",
            schema="public",
            old_table="change_log",
            new_table="change_log_new",
        )


@pytest.mark.parametrize("valid", ["512MB", "2GB", "65536kB", "1TB"])
def test_validate_maintenance_work_mem_accepts_postgres_units(valid: str) -> None:
    assert compact_change_log.validate_maintenance_work_mem(valid) == valid


@pytest.mark.parametrize("invalid", ["", "2G", "100MB; DROP TABLE change_log", "0MB"])
def test_validate_maintenance_work_mem_rejects_bad_values(invalid: str) -> None:
    with pytest.raises(ValueError, match="maintenance_work_mem"):
        compact_change_log.validate_maintenance_work_mem(invalid)


def test_format_plan_reports_counts_sizes_and_reparenting() -> None:
    rendered = compact_change_log.format_plan(_sample_plan())
    assert "total_rows: 1,000" in rendered
    assert "prune_rows: 970 (97.0%)" in rendered
    assert "keep_rows: 30" in rendered
    assert "reparent_rows: 4" in rendered
    assert "change_log_caused_by_fkey" in rendered
    assert "idx_change_log_subscribed_created_id" in rendered
    assert "--apply" in rendered


@pytest.mark.asyncio
async def test_compact_refuses_when_blockers_or_active_connections_present() -> None:
    conn = make_conn()
    with pytest.raises(compact_change_log.CompactionError, match="existing table"):
        await compact_change_log.compact_change_log(
            cast(Conn, conn),
            _sample_plan(blockers=("existing table public.change_log_old",)),
        )
    conn.execute.assert_not_awaited()

    with pytest.raises(compact_change_log.CompactionError, match="active"):
        await compact_change_log.compact_change_log(
            cast(Conn, conn),
            _sample_plan(active_other_connections=2),
        )
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_executes_reparent_fks_indexes_and_atomic_swap() -> None:
    conn = make_conn()

    def _exec_reply(sql: str, *args: object) -> str:
        del args
        return "UPDATE 4" if sql.lstrip().startswith("WITH RECURSIVE walk") else "OK"

    conn.execute.side_effect = _exec_reply
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {
        "live_keep": 30,
        "live_prune": 970,
        "new_total": 30,
        "new_pruned_leak": 0,
        "new_dangling_fk": 0,
    }
    conn.fetchval.return_value = 3_100_000

    result = await compact_change_log.compact_change_log(
        cast(Conn, conn),
        _sample_plan(),
        maintenance_work_mem="1GB",
    )

    assert result == compact_change_log.CompactionResult(
        kept_rows=30,
        pruned_rows=970,
        reparented_rows=4,
        old_bytes=100_000_000,
        new_bytes=3_100_000,
        backup_table="public.change_log_old",
    )
    sqls = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert sqls[:3] == [
        "BEGIN",
        "SET LOCAL statement_timeout = 0",
        "SELECT set_config('search_path', $1, true)",
    ]
    # The bare table names below resolve in the schema inspection found.
    assert conn.execute.call_args_list[2].args[1:] == ('"public"',)
    assert "SET LOCAL maintenance_work_mem = '1GB'" in sqls
    assert "LOCK TABLE change_log IN ACCESS EXCLUSIVE MODE NOWAIT" in sqls
    assert any(
        s.startswith("CREATE TABLE change_log_new (LIKE change_log ") for s in sqls
    )
    assert any("WITH RECURSIVE walk" in s for s in sqls)
    assert any('VALIDATE CONSTRAINT "change_log_caused_by_fkey"' in s for s in sqls)
    assert any('VALIDATE CONSTRAINT "change_log_api_key_id_fkey"' in s for s in sqls)
    old_idx = sqls.index("ALTER TABLE change_log RENAME TO change_log_old")
    new_idx = sqls.index("ALTER TABLE change_log_new RENAME TO change_log")
    commit_idx = sqls.index("COMMIT")
    assert old_idx < new_idx < commit_idx
    assert sqls.index('ANALYZE "public".change_log') > commit_idx


@pytest.mark.asyncio
async def test_compact_rolls_back_on_verification_mismatch() -> None:
    conn = make_conn()
    conn.fetchrow = AsyncMock(
        return_value={
            "live_keep": 30,
            "live_prune": 970,
            "new_total": 29,
            "new_pruned_leak": 0,
            "new_dangling_fk": 0,
        },
    )
    with pytest.raises(compact_change_log.CompactionError, match="row count mismatch"):
        await compact_change_log.compact_change_log(cast(Conn, conn), _sample_plan())
    sqls = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert "ROLLBACK" in sqls
    assert "COMMIT" not in sqls


def test_add_arguments_defaults_to_dry_run() -> None:
    parser = argparse.ArgumentParser()
    compact_change_log._add_arguments(parser)
    flags = cast(compact_change_log.Flags, parser.parse_args([]))
    assert flags.dsn == ""
    assert flags.apply is False
    assert flags.yes is False
    assert flags.maintenance_work_mem == ""
    assert flags.allow_active_connections is False


def test_confirm_apply_requires_yes_when_non_interactive() -> None:
    assert (
        compact_change_log.confirm_apply(yes=True, stdin=io.StringIO(""), is_tty=False)
        is True
    )
    with pytest.raises(compact_change_log.CompactionError, match="--yes"):
        compact_change_log.confirm_apply(yes=False, stdin=io.StringIO(""), is_tty=False)
    assert (
        compact_change_log.confirm_apply(
            yes=False,
            stdin=io.StringIO("compact\n"),
            is_tty=True,
        )
        is True
    )
    assert (
        compact_change_log.confirm_apply(
            yes=False,
            stdin=io.StringIO("no\n"),
            is_tty=True,
        )
        is False
    )


def test_format_bytes_scales_units() -> None:
    assert compact_change_log.format_bytes(512) == "512 B"
    assert compact_change_log.format_bytes(1023) == "1023 B"
    assert compact_change_log.format_bytes(1024) == "1.00 KiB"
    assert compact_change_log.format_bytes(2048) == "2.00 KiB"
    assert compact_change_log.format_bytes(5 * 1024**2) == "5.00 MiB"
    assert compact_change_log.format_bytes(3 * 1024**3) == "3.00 GiB"
    assert compact_change_log.format_bytes(2 * 1024**4) == "2.00 TiB"


def test_staged_name_rejects_oversized_suffix_and_breaks_hash_ties() -> None:
    with pytest.raises(ValueError, match="identifier limit"):
        compact_change_log.staged_name("a", "_" * 63, set())
    used: set[str] = set()
    long_name = "idx_" + ("x" * 60)
    first = compact_change_log.staged_name(long_name, "__new", used)
    second = compact_change_log.staged_name(long_name, "__new", used)
    assert first != second
    assert len(second.encode("utf-8")) <= 63


@pytest.mark.asyncio
async def test_inspect_change_log_raises_when_table_missing_or_without_pk() -> None:
    conn = make_conn()
    conn.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(compact_change_log.CompactionError, match="does not exist"):
        await compact_change_log.inspect_change_log(cast(Conn, conn))

    conn2 = make_conn()
    conn2.fetchrow = AsyncMock(
        side_effect=[
            {"oid": 42, "nspname": "public"},
            {
                "owner": "trackinizer",
                "current_user_name": "trackinizer",
                "reloptions": None,
                "relrowsecurity": False,
                "has_explicit_acl": False,
            },
        ],
    )
    with pytest.raises(compact_change_log.CompactionError, match="no PRIMARY KEY"):
        await compact_change_log.inspect_change_log(cast(Conn, conn2))


@pytest.mark.asyncio
async def test_inspect_counts_a_session_it_cannot_read_as_an_active_client() -> None:
    conn = make_conn()
    rows: dict[str, dict[str, object]] = {
        "to_regclass('change_log')": {"oid": 42, "nspname": "public"},
        "pg_class c WHERE": {
            "owner": "trackinizer",
            "current_user_name": "operator",
            "reloptions": None,
            "relrowsecurity": False,
            "has_explicit_acl": False,
        },
        "pg_total_relation_size": {"total_bytes": 0, "heap_bytes": 0, "index_bytes": 0},
        "AS total_rows": {"total_rows": 0, "prune_rows": 0, "keep_rows": 0},
    }
    fetched: dict[str, list[dict[str, object]]] = {
        "pg_stat_activity": [
            # The server's session, as a role without pg_read_all_stats sees
            # another role's: its backend type and state read NULL.
            {"backend_type": None, "state": None},
            {"backend_type": "client backend", "state": "idle"},
            {"backend_type": "client backend", "state": "idle in transaction"},
            {"backend_type": "autovacuum worker", "state": None},
        ],
        "contype IN": [
            {
                "conname": "change_log_pkey",
                "contype": "p",
                "definition": "PRIMARY KEY (id)",
                "is_self_ref": False,
            },
        ],
    }

    async def fetchrow(sql: str, *args: object) -> dict[str, object]:
        del args
        return next(row for marker, row in rows.items() if marker in sql)

    async def fetch(sql: str, *args: object) -> list[dict[str, object]]:
        del args
        none: list[dict[str, object]] = []
        return next((found for marker, found in fetched.items() if marker in sql), none)

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchval = AsyncMock(side_effect=[None, None, 0])

    plan = await compact_change_log.inspect_change_log(cast(Conn, conn))

    assert (plan.other_connections, plan.active_other_connections) == (3, 2)
    with pytest.raises(compact_change_log.CompactionError, match="2 active"):
        await compact_change_log.compact_change_log(cast(Conn, conn), plan)


@pytest.mark.asyncio
async def test_apply_table_attributes_restores_owner_and_public_grant_option() -> None:
    conn = make_conn()
    schema = compact_change_log.TableSchema(
        schema="public",
        owner="trackinizer",
        current_user="postgres",
        reloptions=("fillfactor=90",),
        primary_key=_sample_schema().primary_key,
        unique_constraints=(),
        foreign_keys=(),
        secondary_indexes=(),
        has_explicit_acl=True,
        grants=(
            compact_change_log.GrantSpec(
                grantee="trackinizer",
                privilege="SELECT",
                is_grantable=False,
            ),
            compact_change_log.GrantSpec(
                grantee=None,
                privilege="SELECT",
                is_grantable=False,
            ),
            compact_change_log.GrantSpec(
                grantee="ops",
                privilege="SELECT",
                is_grantable=True,
            ),
        ),
    )
    await compact_change_log._apply_table_attributes(cast(Conn, conn), ts=schema)
    sqls = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert "ALTER TABLE change_log_new SET (fillfactor=90)" in sqls
    assert 'ALTER TABLE change_log_new OWNER TO "trackinizer"' in sqls
    assert "REVOKE ALL ON TABLE change_log_new FROM PUBLIC" in sqls
    assert "GRANT SELECT ON TABLE change_log_new TO PUBLIC" in sqls
    assert 'GRANT SELECT ON TABLE change_log_new TO "ops" WITH GRANT OPTION' in sqls
    assert not any("GRANT " in s and 'TO "trackinizer"' in s for s in sqls)


@pytest.mark.asyncio
async def test_read_table_schema_captures_rls_uniques_and_explicit_grants() -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {
        "owner": "trackinizer",
        "current_user_name": "trackinizer",
        "reloptions": None,
        "relrowsecurity": True,
        "has_explicit_acl": True,
    }
    conn.fetch.side_effect = [
        [
            {
                "conname": "change_log_pkey",
                "contype": "p",
                "definition": "PRIMARY KEY (id)",
                "is_self_ref": False,
            },
            {
                "conname": "change_log_seq_key",
                "contype": "u",
                "definition": "UNIQUE (id)",
                "is_self_ref": False,
            },
        ],
        [],
        [
            {"grantee": None, "privilege_type": "SELECT", "is_grantable": False},
            {"grantee": "reader", "privilege_type": "SELECT", "is_grantable": True},
        ],
    ]
    blockers: list[str] = []
    schema = await compact_change_log._read_table_schema(
        cast(Conn, conn),
        table_oid=42,
        schema="public",
        blockers=blockers,
    )
    assert any("row-level security" in b for b in blockers)
    assert [c.name for c in schema.unique_constraints] == ["change_log_seq_key"]
    assert schema.grants == (
        compact_change_log.GrantSpec(
            grantee=None,
            privilege="SELECT",
            is_grantable=False,
        ),
        compact_change_log.GrantSpec(
            grantee="reader",
            privilege="SELECT",
            is_grantable=True,
        ),
    )


class _TtyStringIO(io.StringIO):
    @override
    def isatty(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_verify_before_swap_counts_both_tables_in_one_query() -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {
        "live_keep": 10,
        "live_prune": 5,
        "new_total": 10,
        "new_pruned_leak": 0,
        "new_dangling_fk": 0,
    }

    assert await compact_change_log._verify_before_swap(cast(Conn, conn)) == (10, 5)
    # One statement, so every count reads the same snapshot.
    conn.fetchrow.assert_awaited_once_with(
        "SELECT (SELECT COUNT(*) FROM change_log WHERE "
        "kind <> 'dependency_changed' OR subscribers_snapshot <> '{}') AS live_keep, "
        "(SELECT COUNT(*) FROM change_log WHERE "
        "kind = 'dependency_changed' AND subscribers_snapshot = '{}') AS live_prune, "
        "(SELECT COUNT(*) FROM change_log_new) AS new_total, "
        "(SELECT COUNT(*) FROM change_log_new WHERE "
        "kind = 'dependency_changed' AND subscribers_snapshot = '{}') "
        "AS new_pruned_leak, "
        "(SELECT COUNT(*) FROM change_log_new n "
        "LEFT JOIN change_log_new p ON p.id = n.caused_by "
        "WHERE n.caused_by IS NOT NULL AND p.id IS NULL) AS new_dangling_fk",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "pattern"),
    [
        (None, "failed to read staged table verification"),
        (
            {
                "live_keep": 10,
                "live_prune": 5,
                "new_total": 10,
                "new_pruned_leak": 1,
                "new_dangling_fk": 0,
            },
            "unsubscribed cascade",
        ),
        (
            {
                "live_keep": 10,
                "live_prune": 5,
                "new_total": 10,
                "new_pruned_leak": 0,
                "new_dangling_fk": 2,
            },
            "dangling caused_by",
        ),
    ],
)
async def test_verify_before_swap_rejects_broken_invariants(
    row: dict[str, int] | None,
    pattern: str,
) -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = row
    with pytest.raises(compact_change_log.CompactionError, match=pattern):
        await compact_change_log._verify_before_swap(cast(Conn, conn))


def test_main_dry_run_and_apply_cli_flows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = make_conn()
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(
        compact_change_log,
        "inspect_change_log",
        AsyncMock(return_value=_sample_plan()),
    )
    monkeypatch.setattr(
        compact_change_log,
        "compact_change_log",
        AsyncMock(
            return_value=compact_change_log.CompactionResult(
                kept_rows=30,
                pruned_rows=970,
                reparented_rows=4,
                old_bytes=100_000_000,
                new_bytes=3_000_000,
                backup_table="public.change_log_old",
            ),
        ),
    )

    monkeypatch.delenv("TRACKINIZER_DSN", raising=False)
    monkeypatch.setattr(sys, "argv", ["compact_change_log.py"])
    with pytest.raises(SystemExit):
        compact_change_log.main()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compact_change_log.py",
            "--dsn",
            "postgresql://example/db",
            "--maintenance-work-mem",
            "bad",
        ],
    )
    with pytest.raises(SystemExit):
        compact_change_log.main()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compact_change_log.py",
            "--dsn",
            "postgresql://example/db",
            "--maintenance-work-mem",
            "1GB",
        ],
    )
    assert compact_change_log.main() == 0
    assert "Dry-run complete" in capsys.readouterr().out

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compact_change_log.py",
            "--dsn",
            "postgresql://example/db",
            "--apply",
        ],
    )
    monkeypatch.setattr(sys, "stdin", _TtyStringIO("nope\n"))
    assert compact_change_log.main() == 1
    assert "Aborted" in capsys.readouterr().out

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compact_change_log.py",
            "--dsn",
            "postgresql://example/db",
            "--apply",
            "--yes",
        ],
    )
    assert compact_change_log.main() == 0
    out = capsys.readouterr().out
    assert "Compaction committed:" in out
    assert "DROP TABLE public.change_log_old;" in out

    monkeypatch.setattr(
        compact_change_log,
        "inspect_change_log",
        AsyncMock(
            return_value=_sample_plan(blockers=("existing table change_log_old",)),
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compact_change_log.py",
            "--dsn",
            "postgresql://example/db",
            "--apply",
            "--yes",
        ],
    )
    assert compact_change_log.main() == 1
    assert "ERROR:" in capsys.readouterr().err


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
