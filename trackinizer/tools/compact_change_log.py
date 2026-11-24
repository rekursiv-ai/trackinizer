#!/bin/sh
# ruff: noqa: EXE003, D300, D205, T201 -- Polyglot shell/Python CLI; prints status.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 -P "$0" "$@"
Compact Trackinizer's ``change_log`` table by pruning unsubscribed cascades.

Removes historical ``dependency_changed`` rows with an empty
``subscribers_snapshot`` (``kind = 'dependency_changed' AND
subscribers_snapshot = '{}'``) and reclaims heap + index disk space via an
atomic table swap rather than an in-place ``DELETE``.

Dry-run is the default: without ``--apply`` the tool runs read-only inspection
queries and prints row counts, estimated size savings, re-parenting counts,
and schema/safety checks.

Before running ``--apply``:
  1. Stop the Trackinizer server so no writes occur during compaction.
  2. Take a fresh database backup.
  3. Inspect the dry-run report, then run with ``--apply`` (and ``--yes`` for
     non-interactive use).
  4. Verify the compacted ``change_log`` table, restart the server, and drop
     ``change_log_old`` manually once satisfied.

Examples:
  ./compact_change_log.py --dsn "$TRACKINIZER_DSN"
  ./compact_change_log.py --dsn "$TRACKINIZER_DSN" --apply --yes
  ./compact_change_log.py --dsn "$TRACKINIZER_DSN" --apply --maintenance-work-mem 2GB

'''
# fmt: on

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, TextIO, cast

import argparse
import asyncio
import hashlib
import os
import re
import sys

from treekle import from_plain

from trackinizer.server.values import vetted_sql


if TYPE_CHECKING:
    import asyncpg

    from trackinizer.lib.postgres import Conn
else:
    from wrapt import lazy_import

    asyncpg = lazy_import("asyncpg")  # ~60ms; only _run_cli opens a live connection.


_PG_MAX_IDENT_BYTES: Final = 63
_MAINT_MEM_RE: Final = re.compile(r"^[1-9][0-9]*(?:kB|MB|GB|TB)$")
_PRUNE_WHERE: Final = "kind = 'dependency_changed' AND subscribers_snapshot = '{}'"
_KEEP_WHERE: Final = "kind <> 'dependency_changed' OR subscribers_snapshot <> '{}'"


class CompactionError(RuntimeError):
    """Raised when pre-flight checks, confirmation, or verification fail."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ConstraintSpec:
    """One PK, UNIQUE, or FOREIGN KEY constraint on ``change_log``."""

    name: str
    contype: str
    definition: str
    is_self_ref: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexSpec:
    """One non-constraint index definition read from ``pg_index``."""

    name: str
    definition: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GrantSpec:
    """One table-level privilege entry from ``aclexplode(relacl)``."""

    grantee: str | None
    privilege: str
    is_grantable: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class TableSchema:
    """Live catalog snapshot of ``change_log`` preserved across the swap."""

    schema: str
    owner: str
    current_user: str
    reloptions: tuple[str, ...]
    primary_key: ConstraintSpec
    unique_constraints: tuple[ConstraintSpec, ...]
    foreign_keys: tuple[ConstraintSpec, ...]
    secondary_indexes: tuple[IndexSpec, ...]
    has_explicit_acl: bool
    grants: tuple[GrantSpec, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactionPlan:
    """Read-only dry-run report produced by :func:`inspect_change_log`."""

    table_schema: TableSchema
    total_rows: int
    prune_rows: int
    keep_rows: int
    reparent_rows: int
    total_bytes: int
    heap_bytes: int
    index_bytes: int
    estimated_new_bytes: int
    other_connections: int
    active_other_connections: int
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactionResult:
    """Measured outcome returned by :func:`compact_change_log`."""

    kept_rows: int
    pruned_rows: int
    reparented_rows: int
    old_bytes: int
    new_bytes: int
    backup_table: str


class Flags(Protocol):
    """Parsed command-line flags."""

    dsn: str
    apply: bool
    yes: bool
    maintenance_work_mem: str
    allow_active_connections: bool


def quote_ident(name: str) -> str:
    """Quote a PostgreSQL identifier safely."""
    return '"' + name.replace('"', '""') + '"'


def staged_name(original: str, suffix: str, used: set[str]) -> str:
    """Return a unique <=63-byte PostgreSQL identifier ending in ``suffix``.

    Args:
      original: Source constraint or index name.
      suffix: Tag appended during staging or retirement (e.g. ``__new``).
      used: Mutable set of names already claimed in this namespace; updated.

    Returns:
      name: Unique identifier fitting within PostgreSQL's 63-byte limit.

    """
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= _PG_MAX_IDENT_BYTES:
        raise ValueError(f"suffix {suffix!r} exceeds PostgreSQL identifier limit")
    candidate = f"{original}{suffix}"
    if len(candidate.encode("utf-8")) <= _PG_MAX_IDENT_BYTES and candidate not in used:
        used.add(candidate)
        return candidate
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
    tail = f"_{digest}{suffix}"
    max_prefix_bytes = _PG_MAX_IDENT_BYTES - len(tail.encode("utf-8"))
    prefix = _truncate_utf8(original, max_prefix_bytes)
    candidate = f"{prefix}{tail}"
    counter = 1
    while candidate in used:
        extra = f"_{counter}{tail}"
        prefix = _truncate_utf8(
            original,
            _PG_MAX_IDENT_BYTES - len(extra.encode("utf-8")),
        )
        candidate = f"{prefix}{extra}"
        counter += 1
    used.add(candidate)
    return candidate


def rewrite_index_def(
    definition: str,
    *,
    schema: str,
    old_table: str,
    new_table: str,
    old_index: str,
    new_index: str,
) -> str:
    """Rewrite ``pg_get_indexdef`` to build ``new_index`` on ``new_table``.

    Args:
      definition: Exact ``CREATE [UNIQUE] INDEX ...`` DDL from PostgreSQL.
      schema: Owning schema name.
      old_table: Source table name.
      new_table: Staged replacement table name.
      old_index: Source index name.
      new_index: Staged replacement index name.

    Returns:
      rewritten: DDL creating ``new_index`` on ``schema.new_table``.

    Raises:
      ValueError: ``definition`` does not match the expected ``pg_get_indexdef``
        prefix for ``(schema, old_table, old_index)``.

    """
    pattern = re.compile(
        r"^CREATE (UNIQUE )?INDEX "
        + _ident_pattern(old_index)
        + r" ON "
        + _ident_pattern(schema)
        + r"\."
        + _ident_pattern(old_table)
        + r" ",
    )
    match = pattern.match(definition)
    if match is None:
        raise ValueError(
            f"Unexpected index definition for {old_index!r}: {definition!r}",
        )
    unique = match.group(1) or ""
    rest = definition[match.end() :]
    return (
        f"CREATE {unique}INDEX {quote_ident(new_index)} "
        f"ON {quote_ident(schema)}.{quote_ident(new_table)} {rest}"
    )


def rewrite_self_fk_def(
    definition: str,
    *,
    schema: str,
    old_table: str,
    new_table: str,
) -> str:
    """Rewrite a self-referencing FK definition to point at ``new_table``.

    Args:
      definition: Exact ``FOREIGN KEY ... REFERENCES ...`` DDL from PostgreSQL.
      schema: Owning schema name.
      old_table: Source table name.
      new_table: Staged replacement table name.

    Returns:
      rewritten: FK definition referencing ``schema.new_table``.

    Raises:
      ValueError: ``definition`` does not reference ``old_table``.

    """
    pattern = re.compile(
        r"\bREFERENCES (?:(?:"
        + _ident_pattern(schema)
        + r"\.)?"
        + _ident_pattern(old_table)
        + r")(?=\()",
    )
    replacement = f"REFERENCES {quote_ident(schema)}.{quote_ident(new_table)}"
    rewritten, count = pattern.subn(replacement, definition, count=1)
    if count != 1:
        raise ValueError(
            f"Unexpected self-referencing FK definition for {old_table!r}: "
            f"{definition!r}",
        )
    return rewritten


def validate_maintenance_work_mem(value: str) -> str:
    """Validate a PostgreSQL ``maintenance_work_mem`` setting like ``2GB``."""
    if not _MAINT_MEM_RE.match(value):
        raise ValueError(
            f"Invalid maintenance_work_mem {value!r}; expected e.g. '512MB' or '2GB'",
        )
    return value


def format_bytes(num_bytes: int) -> str:
    """Format a byte count in human-readable binary units.

    Args:
      num_bytes: Size in bytes.

    Returns:
      formatted: Human-readable size string (B, KiB, MiB, GiB, or TiB).

    """
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0:
            return f"{num_bytes} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TiB"


def format_plan(plan: CompactionPlan) -> str:
    """Render a scannable dry-run summary for operators.

    Args:
      plan: Dry-run plan returned by :func:`inspect_change_log`.

    Returns:
      report: Multi-line human-readable inspection summary.

    """
    pct = (100.0 * plan.prune_rows / plan.total_rows) if plan.total_rows else 0.0
    ts = plan.table_schema
    lines = [
        f"Target table: {ts.schema}.change_log (owner={ts.owner})",
        f"  total_rows: {plan.total_rows:,}",
        f"  prune_rows: {plan.prune_rows:,} ({pct:.1f}%)",
        f"  keep_rows: {plan.keep_rows:,}",
        f"  reparent_rows: {plan.reparent_rows:,} (kept rows whose caused_by points to a pruned row)",
        (
            f"  current_size: {format_bytes(plan.total_bytes)} "
            f"(heap={format_bytes(plan.heap_bytes)}, indexes={format_bytes(plan.index_bytes)})"
        ),
        f"  estimated_new_size: ~{format_bytes(plan.estimated_new_bytes)}",
        (
            f"  other_client_connections: {plan.other_connections} "
            f"(non-idle: {plan.active_other_connections})"
        ),
        f"  primary_key: {ts.primary_key.name} ({ts.primary_key.definition})",
        f"  foreign_keys ({len(ts.foreign_keys)}): "
        + (", ".join(fk.name for fk in ts.foreign_keys) or "none"),
        f"  secondary_indexes ({len(ts.secondary_indexes)}): "
        + (", ".join(idx.name for idx in ts.secondary_indexes) or "none"),
        f"  reloptions: {', '.join(ts.reloptions) or 'default'}",
        f"  explicit_grants: {len(ts.grants) if ts.has_explicit_acl else 'default'}",
    ]
    if plan.blockers:
        lines.append("Blockers (must resolve before --apply):")
        lines.extend(f"  - {blocker}" for blocker in plan.blockers)
    else:
        lines.append("Schema checks: PASS (no blockers)")
    lines.append(
        "Dry-run complete (no changes made). Re-run with --apply (after stopping "
        "trackinizer and taking a backup) to compact.",
    )
    return "\n".join(lines)


def confirm_apply(*, yes: bool, stdin: TextIO, is_tty: bool) -> bool:
    """Gate destructive ``--apply`` execution on ``--yes`` or interactive reply.

    Args:
      yes: True when the caller explicitly passed ``--yes``.
      stdin: Input stream read when interactive confirmation is required.
      is_tty: Whether ``stdin`` is attached to an interactive terminal.

    Returns:
      confirmed: True iff compaction is authorized to proceed.

    Raises:
      CompactionError: ``yes`` is False and ``is_tty`` is False.

    """
    if yes:
        return True
    if not is_tty:
        raise CompactionError(
            "--apply in a non-interactive shell requires --yes after reviewing "
            "the dry-run output",
        )
    answer = stdin.readline().strip()
    return answer == "compact"


async def inspect_change_log(conn: Conn) -> CompactionPlan:
    """Inspect ``change_log`` read-only and build a :class:`CompactionPlan`.

    ``change_log`` is the table the connection's ``search_path`` names, as it is
    for the server's own SQL.

    Args:
      conn: Open PostgreSQL connection.

    Returns:
      plan: Counts, sizes, catalog metadata, and any pre-flight blockers.

    Raises:
      CompactionError: Target table does not exist or lacks a primary key.

    """
    target = await conn.fetchrow(
        "SELECT c.oid, n.nspname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = to_regclass('change_log')",
    )
    if target is None:
        raise CompactionError("table change_log does not exist on the search_path")
    table_oid = from_plain(target["oid"], int)
    schema = from_plain(target["nspname"], str)

    blockers = await _find_blockers(conn, table_oid=table_oid, schema=schema)
    table_schema = await _read_table_schema(
        conn,
        table_oid=table_oid,
        schema=schema,
        blockers=blockers,
    )
    meta_row = _require_row(
        await conn.fetchrow(
            "SELECT pg_total_relation_size($1::oid) AS total_bytes, "
            "pg_relation_size($1::oid) AS heap_bytes, "
            "pg_indexes_size($1::oid) AS index_bytes",
            table_oid,
        ),
        f"sizes for {schema}.change_log",
    )
    total_bytes = from_plain(meta_row["total_bytes"], int)
    heap_bytes = from_plain(meta_row["heap_bytes"], int)
    index_bytes = from_plain(meta_row["index_bytes"], int)

    counts_row = _require_row(
        await conn.fetchrow(
            vetted_sql(
                "SELECT COUNT(*) AS total_rows, COUNT(*) FILTER (WHERE ",
                _PRUNE_WHERE,
                ") AS prune_rows, COUNT(*) FILTER (WHERE ",
                _KEEP_WHERE,
                ") AS keep_rows FROM change_log",
            ),
        ),
        f"row counts for {schema}.change_log",
    )
    total_rows = from_plain(counts_row["total_rows"], int)
    prune_rows = from_plain(counts_row["prune_rows"], int)
    keep_rows = from_plain(counts_row["keep_rows"], int)

    reparent_raw = await conn.fetchval(
        "SELECT COUNT(*) FROM change_log k JOIN change_log p ON p.id = k.caused_by "
        "WHERE (k.kind <> 'dependency_changed' OR k.subscribers_snapshot <> '{}') "
        "AND k.caused_by IS NOT NULL "
        "AND p.kind = 'dependency_changed' "
        "AND p.subscribers_snapshot = '{}'",
    )
    reparent_rows = from_plain(reparent_raw, int)

    sessions = await conn.fetch(
        "SELECT backend_type, state FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()",
    )
    # Without pg_read_all_stats, another role's session reads NULL in both columns
    # (pgstatfuncs.c, "No permissions to view data about this session"); counting
    # it as an idle non-client would let --apply run under a live server.
    clients = [s for s in sessions if s["backend_type"] in {"client backend", None}]
    other_connections = len(clients)
    active_other_connections = sum(1 for s in clients if s["state"] != "idle")
    estimated_new_bytes = (
        int(total_bytes * (keep_rows / total_rows)) if total_rows else 0
    )

    return CompactionPlan(
        table_schema=table_schema,
        total_rows=total_rows,
        prune_rows=prune_rows,
        keep_rows=keep_rows,
        reparent_rows=reparent_rows,
        total_bytes=total_bytes,
        heap_bytes=heap_bytes,
        index_bytes=index_bytes,
        estimated_new_bytes=estimated_new_bytes,
        other_connections=other_connections,
        active_other_connections=active_other_connections,
        blockers=tuple(blockers),
    )


async def compact_change_log(
    conn: Conn,
    plan: CompactionPlan,
    *,
    maintenance_work_mem: str = "",
    allow_active_connections: bool = False,
) -> CompactionResult:
    """Compact ``change_log`` via staged build, FK re-parenting, and atomic swap.

    Runs the entire build, ``caused_by`` re-parenting, index rebuild, FK
    validation, invariant verification, and rename swap inside a single
    transaction holding ``ACCESS EXCLUSIVE MODE NOWAIT`` on the source table.
    Any failure rolls back cleanly leaving the live table untouched. Keeps
    ``change_log_old`` intact after commit so the operator can verify and drop
    it manually.

    Args:
      conn: Open PostgreSQL connection.
      plan: Dry-run plan produced by :func:`inspect_change_log`.
      maintenance_work_mem: Optional PostgreSQL memory override for index
        builds (e.g. ``'2GB'``).
      allow_active_connections: Allow running when other non-idle client
        connections exist in the same database.

    Returns:
      result: Measured post-compaction row counts and table sizes.

    Raises:
      CompactionError: Pre-flight blockers, active connections, or post-build
        verification checks fail.

    """
    if plan.blockers:
        details = "; ".join(plan.blockers)
        raise CompactionError(f"cannot apply compaction with blockers: {details}")
    if plan.active_other_connections > 0 and not allow_active_connections:
        raise CompactionError(
            f"found {plan.active_other_connections} active client connection(s) "
            "on this database; stop trackinizer first or pass "
            "--allow-active-connections",
        )
    if maintenance_work_mem:
        validate_maintenance_work_mem(maintenance_work_mem)

    ts = plan.table_schema
    q_schema = quote_ident(ts.schema)

    used_names: set[str] = {
        ts.primary_key.name,
        *(c.name for c in ts.unique_constraints),
        *(idx.name for idx in ts.secondary_indexes),
    }
    key_constraints = (ts.primary_key, *ts.unique_constraints)
    key_new_names = {
        c.name: staged_name(c.name, "__new", used_names) for c in key_constraints
    }
    key_old_names = {
        c.name: staged_name(c.name, "__old", used_names) for c in key_constraints
    }
    idx_new_names = {
        idx.name: staged_name(idx.name, "__new", used_names)
        for idx in ts.secondary_indexes
    }
    idx_old_names = {
        idx.name: staged_name(idx.name, "__old", used_names)
        for idx in ts.secondary_indexes
    }

    await conn.execute("BEGIN")
    try:
        await conn.execute("SET LOCAL statement_timeout = 0")
        # The statements below name their tables bare, so each resolves in the
        # schema inspection found; the new-tables gate in
        # server/private/new_tables_test.py reads table names only from literals.
        await conn.execute("SELECT set_config('search_path', $1, true)", q_schema)
        if maintenance_work_mem:
            await conn.execute(
                f"SET LOCAL maintenance_work_mem = '{maintenance_work_mem}'",
            )
        await conn.execute("LOCK TABLE change_log IN ACCESS EXCLUSIVE MODE NOWAIT")
        await conn.execute(
            "CREATE TABLE change_log_new ("
            "LIKE change_log "
            "INCLUDING DEFAULTS "
            "INCLUDING CONSTRAINTS "
            "INCLUDING STORAGE "
            "INCLUDING GENERATED "
            "INCLUDING IDENTITY"
            ")",
        )
        await conn.execute(
            vetted_sql(
                "INSERT INTO change_log_new SELECT * FROM change_log WHERE ",
                _KEEP_WHERE,
            ),
        )
        for constraint in key_constraints:
            staged = quote_ident(key_new_names[constraint.name])
            await conn.execute(
                f"ALTER TABLE change_log_new ADD CONSTRAINT {staged} "
                f"{constraint.definition}",
            )
        reparented_rows = await _reparent_caused_by(conn)
        for idx in ts.secondary_indexes:
            ddl = rewrite_index_def(
                idx.definition,
                schema=ts.schema,
                old_table="change_log",
                new_table="change_log_new",
                old_index=idx.name,
                new_index=idx_new_names[idx.name],
            )
            await conn.execute(ddl)
        for fk in ts.foreign_keys:
            fk_def = (
                rewrite_self_fk_def(
                    fk.definition,
                    schema=ts.schema,
                    old_table="change_log",
                    new_table="change_log_new",
                )
                if fk.is_self_ref
                else fk.definition
            )
            q_fk = quote_ident(fk.name)
            await conn.execute(
                f"ALTER TABLE change_log_new ADD CONSTRAINT {q_fk} {fk_def} NOT VALID",
            )
            await conn.execute(f"ALTER TABLE change_log_new VALIDATE CONSTRAINT {q_fk}")
        await _apply_table_attributes(conn, ts=ts)
        kept_rows, pruned_rows = await _verify_before_swap(conn)
        await _swap_tables(
            conn,
            q_schema=q_schema,
            ts=ts,
            key_constraints=key_constraints,
            key_new_names=key_new_names,
            key_old_names=key_old_names,
            idx_new_names=idx_new_names,
            idx_old_names=idx_old_names,
        )
        await conn.execute("COMMIT")
    except BaseException:
        await conn.execute("ROLLBACK")
        raise

    await conn.execute(f"ANALYZE {q_schema}.change_log")
    new_bytes_raw = await conn.fetchval(
        "SELECT pg_total_relation_size($1::regclass)",
        f"{q_schema}.change_log",
    )
    new_bytes = from_plain(new_bytes_raw, int)
    return CompactionResult(
        kept_rows=kept_rows,
        pruned_rows=pruned_rows,
        reparented_rows=reparented_rows,
        old_bytes=plan.total_bytes,
        new_bytes=new_bytes,
        backup_table=f"{ts.schema}.change_log_old",
    )


def main() -> int:
    """Run the CLI; return the process exit code.

    Returns:
      exit_code: 0 on success, non-zero on validation or compaction failure.

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(Flags, parser.parse_args())
    dsn = flags.dsn or os.environ.get("TRACKINIZER_DSN", "")
    if not dsn:
        parser.error("--dsn is required (or set TRACKINIZER_DSN)")
    if flags.maintenance_work_mem:
        try:
            validate_maintenance_work_mem(flags.maintenance_work_mem)
        except ValueError as err:
            parser.error(str(err))
    try:
        return asyncio.run(_run_cli(flags, dsn))
    except CompactionError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument(
        "--dsn",
        default="",
        help="PostgreSQL connection DSN (defaults to $TRACKINIZER_DSN).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply compaction and atomic swap (default is read-only dry-run).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt when --apply is set.",
    )
    parser.add_argument(
        "--maintenance-work-mem",
        default="",
        help="Optional SET LOCAL maintenance_work_mem during --apply (e.g. '2GB').",
    )
    parser.add_argument(
        "--allow-active-connections",
        action="store_true",
        help="Allow --apply even when other non-idle client sessions exist.",
    )


async def _run_cli(flags: Flags, dsn: str) -> int:
    """Connect, run dry-run inspection, and optionally apply compaction."""
    conn = await asyncpg.connect(dsn)
    try:
        plan = await inspect_change_log(conn)
        print(format_plan(plan))
        if not flags.apply:
            return 1 if plan.blockers else 0
        if plan.blockers:
            raise CompactionError("resolve blockers shown above before --apply")
        if not flags.yes:
            print("Type 'compact' to proceed: ", end="", flush=True)
        if not confirm_apply(
            yes=flags.yes,
            stdin=sys.stdin,
            is_tty=sys.stdin.isatty(),
        ):
            print("Aborted (confirmation did not match 'compact').")
            return 1
        result = await compact_change_log(
            conn,
            plan,
            maintenance_work_mem=flags.maintenance_work_mem,
            allow_active_connections=flags.allow_active_connections,
        )
        print(
            "\nCompaction committed:\n"
            f"  kept_rows: {result.kept_rows:,}\n"
            f"  pruned_rows: {result.pruned_rows:,}\n"
            f"  reparented_rows: {result.reparented_rows:,}\n"
            f"  size: {format_bytes(result.old_bytes)} -> "
            f"{format_bytes(result.new_bytes)}\n"
            f"  backup_table: {result.backup_table} "
            f"(verify and run: DROP TABLE {result.backup_table};)",
        )
        return 0
    finally:
        await conn.close()


async def _find_blockers(
    conn: Conn,
    *,
    table_oid: int,
    schema: str,
) -> list[str]:
    """Return every catalog condition that makes an automated swap unsafe."""
    blockers: list[str] = []
    for staged in ("change_log_new", "change_log_old"):
        candidate = f"{schema}.{staged}"
        existing = await conn.fetchval(
            "SELECT to_regclass($1)::oid",
            f"{quote_ident(schema)}.{staged}",
        )
        if existing is not None:
            blockers.append(
                f"existing table {candidate} (inspect or drop it before running)",
            )
    inbound_fks = await conn.fetch(
        "SELECT n.nspname || '.' || r.relname AS referencing_table, c.conname "
        "FROM pg_constraint c "
        "JOIN pg_class r ON r.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = r.relnamespace "
        "WHERE c.confrelid = $1::oid AND c.conrelid <> $1::oid AND c.contype = 'f' "
        "ORDER BY 1, 2",
        table_oid,
    )
    blockers.extend(
        f"inbound foreign key {row['conname']} from {row['referencing_table']}"
        for row in inbound_fks
    )
    views = await conn.fetch(
        "SELECT DISTINCT n.nspname || '.' || v.relname AS view_name "
        "FROM pg_depend d "
        "JOIN pg_rewrite rw ON rw.oid = d.objid "
        "JOIN pg_class v ON v.oid = rw.ev_class "
        "JOIN pg_namespace n ON n.oid = v.relnamespace "
        "WHERE d.refobjid = $1::oid AND d.deptype = 'n' "
        "AND v.oid <> $1::oid AND v.relkind IN ('v', 'm') "
        "ORDER BY 1",
        table_oid,
    )
    blockers.extend(f"dependent view {row['view_name']}" for row in views)
    triggers = await conn.fetch(
        "SELECT tgname FROM pg_trigger "
        "WHERE tgrelid = $1::oid AND NOT tgisinternal ORDER BY tgname",
        table_oid,
    )
    blockers.extend(f"user trigger {row['tgname']}" for row in triggers)
    col_acls = await conn.fetch(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = $1::oid AND attnum > 0 "
        "AND NOT attisdropped AND attacl IS NOT NULL ORDER BY attnum",
        table_oid,
    )
    blockers.extend(f"column-level ACL on {row['attname']}" for row in col_acls)
    return blockers


async def _read_table_schema(
    conn: Conn,
    *,
    table_oid: int,
    schema: str,
    blockers: list[str],
) -> TableSchema:
    """Read owner, reloptions, constraints, indexes, and grants from catalogs."""
    class_row = _require_row(
        await conn.fetchrow(
            "SELECT pg_get_userbyid(c.relowner) AS owner, "
            "current_user AS current_user_name, "
            "c.reloptions, c.relrowsecurity, (c.relacl IS NOT NULL) AS has_explicit_acl "
            "FROM pg_class c WHERE c.oid = $1::oid",
            table_oid,
        ),
        f"pg_class entry for {schema}.change_log",
    )
    if bool(class_row["relrowsecurity"]):
        blockers.append("row-level security is enabled on target table")
    raw_reloptions = class_row["reloptions"]
    reloptions = (
        () if raw_reloptions is None else tuple(from_plain(raw_reloptions, list[str]))
    )
    has_explicit_acl = bool(class_row["has_explicit_acl"])

    constraint_rows = await conn.fetch(
        "SELECT c.conname, c.contype::text AS contype, "
        "pg_get_constraintdef(c.oid) AS definition, "
        "(c.contype = 'f' AND c.confrelid = c.conrelid) AS is_self_ref "
        "FROM pg_constraint c "
        "WHERE c.conrelid = $1::oid AND c.contype IN ('p', 'u', 'f') "
        "ORDER BY c.conname",
        table_oid,
    )
    primary_key: ConstraintSpec | None = None
    uniques: list[ConstraintSpec] = []
    foreign_keys: list[ConstraintSpec] = []
    for row in constraint_rows:
        spec = ConstraintSpec(
            name=from_plain(row["conname"], str),
            contype=from_plain(row["contype"], str),
            definition=from_plain(row["definition"], str),
            is_self_ref=bool(row["is_self_ref"]),
        )
        if spec.contype == "p":
            primary_key = spec
        elif spec.contype == "u":
            uniques.append(spec)
        else:
            foreign_keys.append(spec)
    if primary_key is None:
        raise CompactionError(
            f"table {schema}.change_log has no PRIMARY KEY constraint",
        )

    index_rows = await conn.fetch(
        "SELECT idx_cls.relname AS indexname, "
        "pg_get_indexdef(i.indexrelid) AS indexdef "
        "FROM pg_index i "
        "JOIN pg_class idx_cls ON idx_cls.oid = i.indexrelid "
        "WHERE i.indrelid = $1::oid "
        "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid) "
        "ORDER BY idx_cls.relname",
        table_oid,
    )
    secondary_indexes = tuple(
        IndexSpec(
            name=from_plain(row["indexname"], str),
            definition=from_plain(row["indexdef"], str),
        )
        for row in index_rows
    )

    grants: list[GrantSpec] = []
    if has_explicit_acl:
        grant_rows = await conn.fetch(
            "SELECT CASE WHEN acl.grantee = 0 THEN NULL "
            "ELSE pg_get_userbyid(acl.grantee) END AS grantee, "
            "acl.privilege_type, acl.is_grantable "
            "FROM pg_class c, LATERAL aclexplode(c.relacl) AS acl "
            "WHERE c.oid = $1::oid "
            "ORDER BY grantee NULLS FIRST, acl.privilege_type",
            table_oid,
        )
        grants = [
            GrantSpec(
                grantee=(
                    None if row["grantee"] is None else from_plain(row["grantee"], str)
                ),
                privilege=from_plain(row["privilege_type"], str),
                is_grantable=bool(row["is_grantable"]),
            )
            for row in grant_rows
        ]

    return TableSchema(
        schema=schema,
        owner=from_plain(class_row["owner"], str),
        current_user=from_plain(class_row["current_user_name"], str),
        reloptions=reloptions,
        primary_key=primary_key,
        unique_constraints=tuple(uniques),
        foreign_keys=tuple(foreign_keys),
        secondary_indexes=secondary_indexes,
        has_explicit_acl=has_explicit_acl,
        grants=tuple(grants),
    )


async def _reparent_caused_by(conn: Conn) -> int:
    """Re-point kept rows whose ``caused_by`` was pruned to the nearest kept ancestor."""
    tag = await conn.execute(
        "WITH RECURSIVE walk(start_id, cur_id, depth, visited) AS ("
        "    SELECT n.id, p.caused_by, 1, ARRAY[n.id, p.id] "
        "    FROM change_log_new n "
        "    LEFT JOIN change_log_new kept ON kept.id = n.caused_by "
        "    JOIN change_log p ON p.id = n.caused_by "
        "    WHERE n.caused_by IS NOT NULL AND kept.id IS NULL "
        "    UNION ALL "
        "    SELECT w.start_id, "
        "           CASE WHEN p.caused_by = ANY(w.visited) THEN NULL "
        "                ELSE p.caused_by END, "
        "           w.depth + 1, "
        "           w.visited || p.id "
        "    FROM walk w "
        "    LEFT JOIN change_log_new kept ON kept.id = w.cur_id "
        "    JOIN change_log p ON p.id = w.cur_id "
        "    WHERE w.cur_id IS NOT NULL AND kept.id IS NULL AND w.depth < 1000"
        "), "
        "reparented AS ("
        "    SELECT DISTINCT ON (w.start_id) "
        "        w.start_id, "
        "        CASE "
        "            WHEN w.cur_id IS NULL THEN NULL "
        "            WHEN EXISTS (SELECT 1 FROM change_log_new k WHERE k.id = w.cur_id) "
        "                THEN w.cur_id "
        "            ELSE NULL "
        "        END AS resolved_caused_by "
        "    FROM walk w "
        "    ORDER BY w.start_id, w.depth DESC"
        ") "
        "UPDATE change_log_new n "
        "SET caused_by = r.resolved_caused_by "
        "FROM reparented r "
        "WHERE n.id = r.start_id",
    )
    return _parse_command_count(tag)


async def _apply_table_attributes(conn: Conn, *, ts: TableSchema) -> None:
    """Restore reloptions, owner, and table privileges on ``change_log_new``."""
    if ts.reloptions:
        opts = ", ".join(ts.reloptions)
        await conn.execute(f"ALTER TABLE change_log_new SET ({opts})")
    if ts.owner != ts.current_user:
        await conn.execute(
            f"ALTER TABLE change_log_new OWNER TO {quote_ident(ts.owner)}",
        )
    if ts.has_explicit_acl:
        await conn.execute("REVOKE ALL ON TABLE change_log_new FROM PUBLIC")
        for grant in ts.grants:
            if grant.grantee == ts.owner:
                continue
            target = "PUBLIC" if grant.grantee is None else quote_ident(grant.grantee)
            suffix = " WITH GRANT OPTION" if grant.is_grantable else ""
            await conn.execute(
                f"GRANT {grant.privilege} ON TABLE change_log_new TO {target}{suffix}",
            )


async def _verify_before_swap(conn: Conn) -> tuple[int, int]:
    """Assert row-count parity, zero pruned leaks, and zero dangling ``caused_by``."""
    row = _require_row(
        await conn.fetchrow(
            vetted_sql(
                "SELECT (SELECT COUNT(*) FROM change_log WHERE ",
                _KEEP_WHERE,
                ") AS live_keep, (SELECT COUNT(*) FROM change_log WHERE ",
                _PRUNE_WHERE,
                ") AS live_prune, "
                "(SELECT COUNT(*) FROM change_log_new) AS new_total, "
                "(SELECT COUNT(*) FROM change_log_new WHERE ",
                _PRUNE_WHERE,
                ") AS new_pruned_leak, (SELECT COUNT(*) FROM change_log_new n "
                "LEFT JOIN change_log_new p ON p.id = n.caused_by "
                "WHERE n.caused_by IS NOT NULL AND p.id IS NULL) AS new_dangling_fk",
            ),
        ),
        "staged table verification",
    )
    live_keep = from_plain(row["live_keep"], int)
    live_prune = from_plain(row["live_prune"], int)
    new_total = from_plain(row["new_total"], int)
    new_pruned_leak = from_plain(row["new_pruned_leak"], int)
    new_dangling_fk = from_plain(row["new_dangling_fk"], int)
    if new_total != live_keep:
        raise CompactionError(
            f"row count mismatch before swap: staged={new_total}, expected={live_keep}",
        )
    if new_pruned_leak != 0:
        raise CompactionError(
            f"staged table still contains {new_pruned_leak} unsubscribed cascade row(s)",
        )
    if new_dangling_fk != 0:
        raise CompactionError(
            f"staged table still contains {new_dangling_fk} dangling caused_by row(s)",
        )
    return live_keep, live_prune


async def _swap_tables(
    conn: Conn,
    *,
    q_schema: str,
    ts: TableSchema,
    key_constraints: tuple[ConstraintSpec, ...],
    key_new_names: dict[str, str],
    key_old_names: dict[str, str],
    idx_new_names: dict[str, str],
    idx_old_names: dict[str, str],
) -> None:
    """Atomically swap ``orig -> old`` and ``new -> orig`` with original names."""
    await conn.execute("ALTER TABLE change_log RENAME TO change_log_old")
    for constraint in key_constraints:
        await conn.execute(
            "ALTER TABLE change_log_old RENAME CONSTRAINT "
            f"{quote_ident(constraint.name)} "
            f"TO {quote_ident(key_old_names[constraint.name])}",
        )
    for idx in ts.secondary_indexes:
        await conn.execute(
            f"ALTER INDEX {q_schema}.{quote_ident(idx.name)} "
            f"RENAME TO {quote_ident(idx_old_names[idx.name])}",
        )
    await conn.execute("ALTER TABLE change_log_new RENAME TO change_log")
    for constraint in key_constraints:
        await conn.execute(
            "ALTER TABLE change_log RENAME CONSTRAINT "
            f"{quote_ident(key_new_names[constraint.name])} "
            f"TO {quote_ident(constraint.name)}",
        )
    for idx in ts.secondary_indexes:
        await conn.execute(
            f"ALTER INDEX {q_schema}.{quote_ident(idx_new_names[idx.name])} "
            f"RENAME TO {quote_ident(idx.name)}",
        )


def _ident_pattern(name: str) -> str:
    """Regex matching ``name`` as emitted by Postgres (unquoted or double-quoted)."""
    quoted = re.escape(quote_ident(name))
    bare = re.escape(name)
    return f"(?:{quoted}|{bare})"


def _require_row[R](row: R | None, description: str) -> R:
    """Ensure a single-row catalog or verification query returned a row."""
    if row is None:
        raise CompactionError(f"failed to read {description}")
    return row


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` without splitting a multi-byte UTF-8 character."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _parse_command_count(tag: str) -> int:
    """Extract the trailing integer row count from an asyncpg command tag."""
    parts = tag.rsplit(" ", 1)
    return int(parts[-1]) if len(parts) == 2 and parts[-1].isdigit() else 0


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
