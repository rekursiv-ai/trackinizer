"""Local administrative command for restoring a logical Trackinizer export."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import argparse
import asyncio
import json
import sys

import asyncpg

from trackinizer.server.access_policy import DefaultPolicy
from trackinizer.server.config import Config, ConfigError, build_engine
from trackinizer.server.restore import GraphExportReader
from trackinizer.server.store.core import Store


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.server.store.restore import RestoreReport


__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Restore one complete logical export into the configured database."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(_restore(args))
    except (ConfigError, OSError, ValueError, RuntimeError, asyncpg.PostgresError) as error:
        parser.error(str(error))
    sys.stdout.write(
        json.dumps(
            {
                "rows": dict(report.rows_by_table),
                "redacted_auth_references": report.redacted_auth_references,
                "source_schema": report.source_schema,
            },
            sort_keys=True,
        )
        + "\n",
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    """Build the restore parser without importing the HTTP server."""
    parser = argparse.ArgumentParser(
        prog="trackinizer restore",
        description="Restore a complete logical export into an empty database.",
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--engine", choices=("pglite", "pg"), default=None)
    parser.add_argument("--datadir", type=Path, default=None)
    parser.add_argument("--dsn", default=None)
    return parser


async def _restore(args: argparse.Namespace) -> RestoreReport:
    """Open the configured engine, bootstrap it, and restore the file."""
    config = Config.from_env()
    config = replace(
        config,
        engine=args.engine or config.engine,
        datadir=args.datadir if args.datadir is not None else config.datadir,
        dsn=args.dsn if args.dsn is not None else config.dsn,
    )
    async with build_engine(config) as engine:
        store = Store(engine, access_policy=DefaultPolicy())
        await store.bootstrap()
        return await store.restore_export(GraphExportReader(args.path))


if __name__ == "__main__":
    raise SystemExit(main())
