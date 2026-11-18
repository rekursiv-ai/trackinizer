#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 -P "$0" "$@"
Re-embed every session record, inquiry text and edge note now, in large calls.

The first fill after a settings change, run on a GPU once the server has restarted
under the new settings: the server's sweep would take a day on its CPU threads.
Run it with the server's own DSN, --addons factory and overrides, so both write
the rows of one settings key; --override embedding.model.device=cuda:0 moves the
model, which is no input of the key.

Example:
  python -m trackinizer.addons.embedding.reembed "$DSN" \
      --addons mysite.deployments.production \
      --override embedding.model.device=cuda:0

'''
# fmt: on

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol, cast

import argparse
import asyncio
import logging
import time

from trackinizer.addons.deployment import DeploymentError, deployment_config
from trackinizer.addons.embedding.search import Nearest, ensure_index
from trackinizer.addons.embedding.sweep import SweepState, sweep_once
from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server.notify import NOTIFY_CHANNEL


if TYPE_CHECKING:
    from trackinizer.lib.postgres import DatabaseEngine


__all__ = ["main", "reembed", "vector_arm"]


_LOGGER: Final = logging.getLogger(__name__)


def main() -> int:
    """Re-embed the server's backlog.

    Returns:
      status: The process exit code, 0 once every source is current.

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    flags = _parse_args(parser)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    arm = vector_arm(flags.addons, overrides=flags.override)
    asyncio.run(_reembed_at(flags.dsn, arm, slice_units=flags.slice_units))
    return 0


def vector_arm(factory: str, *, overrides: list[str]) -> Nearest:
    """Return the embedding add-on's vector arm as the server builds it.

    Args:
      factory: The server's ``--addons`` factory.
      overrides: The server's overrides, and any that move the model.

    Returns:
      arm: Its model and its settings, whose key every row gets.

    Raises:
      SystemExit: The factory does not resolve, or runs no embedding add-on.

    """
    try:
        arm = deployment_config(factory, overrides=overrides).make().vector_search
    except DeploymentError as err:
        raise SystemExit(str(err)) from err
    if not isinstance(arm, Nearest):
        raise SystemExit(
            f"{factory or 'The base deployment'} runs no embedding add-on, so there "
            "is nothing to re-embed.",
        )
    return arm


async def reembed(engine: DatabaseEngine, arm: Nearest, *, slice_units: int) -> int:
    """Run the sweep's rounds until one finds nothing, in calls of ``slice_units``.

    Args:
      engine: The server's database.
      arm: The model and settings to embed under.
      slice_units: Sources per page and texts per model call.

    Returns:
      written: The units written.

    """
    _LOGGER.info("embedding settings %s %r", arm.settings.key, arm.settings)
    async with engine.acquire() as conn:
        await ensure_index(conn, arm.settings)
    state, written = SweepState(), 0
    while True:
        started = time.monotonic()
        state = await sweep_once(
            engine,
            model=arm.model,
            settings=arm.settings,
            state=state,
            slice_units=slice_units,
        )
        if not state.worked:
            return written
        written += state.written
        _LOGGER.info(
            "re-embedded %d units, %.1f a second",
            state.written,
            state.written / max(time.monotonic() - started, 1e-9),
        )


class _Flags(Protocol):
    """Parsed command-line flags."""

    dsn: str
    addons: str
    override: list[str]
    slice_units: int


def _parse_args(
    parser: argparse.ArgumentParser,
    argv: list[str] | None = None,
) -> _Flags:
    """Register the flags on ``parser``; return them parsed from ``argv``."""
    parser.add_argument("dsn", help="The server's Postgres DSN.")
    parser.add_argument(
        "--addons",
        default="",
        metavar="FACTORY",
        help="The server's deployment factory, as its --addons names it.",
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="PATH=VALUE",
        help="An override on the deployment config, as the server's. Repeatable.",
    )
    parser.add_argument(
        "--slice-units",
        type=int,
        default=65_536,
        help="Sources per page and texts per model call (default: 65536).",
    )
    return cast(_Flags, parser.parse_args(argv))


async def _reembed_at(dsn: str, arm: Nearest, *, slice_units: int) -> int:
    """Open the server's database at ``dsn`` and re-embed it."""
    async with PostgresEngine(dsn=dsn, listen_channel=NOTIFY_CHANNEL) as engine:
        return await reembed(engine, arm, slice_units=slice_units)


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
