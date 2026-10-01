"""Run or inspect a deployment's addons outside the trackinizer server.

``run`` starts one addon's standalone services in this process and supervises
them until SIGINT or SIGTERM; a crashed service restarts with backoff. The
services reach trax exactly as ``trax`` would: ``--profile``, then
``$TRACKINIZER_URL``, then ``$TRACKINIZER_PROFILE``, then the current profile.
Server services are not started here: they run inside the trackinizer server
started with ``--addons FACTORY``.

``show`` prints the finalized deployment config and the addon catalog, so an
operator can see what a factory switches on before deploying it.

Examples:
  python -m trackinizer.addons show mysite.deployments.production
  python -m trackinizer.addons run mysite.deployments.production --addon chat

"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Protocol, cast

import argparse
import asyncio
import logging
import signal

from trackinizer.addons.addon import StandaloneContext
from trackinizer.addons.deployment import (
    DeploymentError,
    deployment_config,
    supervise,
)
from trackinizer.client.errors import ClientError
from trackinizer.trax.cli import connect


if TYPE_CHECKING:
    from collections.abc import Coroutine

    from trackinizer.addons.addon import AddonManifest
    from trackinizer.addons.deployment import Deployment


__all__ = ["main", "run_standalone", "serve_standalone", "standalone_context"]


def main() -> int:
    """Run the program; return the process exit code.

    Returns:
      code: 0 once ``show`` printed or ``run`` was stopped by a signal.

    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(Flags, parser.parse_args())
    # The operator's flags are the input here, so a bad one is a message and a
    # nonzero exit, not a traceback.
    try:
        config = deployment_config(flags.factory, overrides=flags.override)
        deployment = config.make()
        if flags.command == "show":
            print(  # noqa: T201 -- The CLI's main prints the report it was asked for.
                config.pformat(hide_default_values=False),
                deployment.catalog().model_dump_json(indent=2),
                sep="\n",
            )
            return 0
        name = flags.addon or _only(deployment)
        _configure_logging(flags.log_level)
        serve_standalone(
            name,
            manifest=deployment.manifest(name),
            profile=flags.profile,
        )
    except (ClientError, DeploymentError) as err:
        raise SystemExit(str(err)) from err
    return 0


def serve_standalone(
    name: str,
    manifest: AddonManifest,
    *,
    profile: str = "",
) -> None:
    """Run ``manifest``'s standalone services until SIGINT or SIGTERM.

    Args:
      name: The addon's field name in the deployment config.
      manifest: The addon to run; it needs at least one standalone service.
      profile: A saved trax profile to use; empty resolves as ``trax`` does.

    Raises:
      DeploymentError: The addon has no standalone services.

    """
    if not manifest.standalone_services:
        raise DeploymentError(
            f"Addon {name!r} has no standalone services; its server "
            "services run inside the trackinizer server (--addons).",
        )
    context = standalone_context(profile=profile)
    asyncio.run(
        _until_signalled(run_standalone(name, manifest=manifest, context=context)),
    )


def standalone_context(*, profile: str = "") -> StandaloneContext:
    """Return a context whose trax client resolves as the ``trax`` CLI's does.

    The client is ``trax``'s shared one for the resolved server, so it lives as
    long as the process.

    Args:
      profile: A saved trax profile; empty falls through to
        ``$TRACKINIZER_URL``, ``$TRACKINIZER_PROFILE``, and the current profile.

    Returns:
      context: What standalone services may use.

    """
    return StandaloneContext(
        client=connect(argparse.Namespace(profile=profile or None)),
    )


async def run_standalone(
    name: str,
    manifest: AddonManifest,
    *,
    context: StandaloneContext,
) -> None:
    """Supervise every standalone service of ``manifest`` until cancelled.

    Args:
      name: The addon's field name in the deployment config, for log lines.
      manifest: The addon whose standalone services to run.
      context: What the services may use.

    """
    async with asyncio.TaskGroup() as group:
        for service in manifest.standalone_services:
            group.create_task(
                supervise(f"{name}.{service.name}", run=partial(service.run, context)),
            )


class Flags(Protocol):
    """Parsed command-line flags."""

    command: str
    factory: str
    addon: str
    override: list[str]
    profile: str
    log_level: str


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument(
        "command",
        choices=["run", "show"],
        help="run: start one addon's standalone services; show: print the config.",
    )
    parser.add_argument(
        "factory",
        help="Dotted path to a function returning a Deployment config.",
    )
    parser.add_argument(
        "--addon",
        default="",
        help="Addon to run; may be omitted when the deployment has one addon.",
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="PATH=VALUE",
        help=(
            "configgle override on the Deployment config, e.g. "
            "ADDON.FIELD=VALUE, or ADDON=null to switch an addon off. Repeatable."
        ),
    )
    parser.add_argument(
        "--profile",
        default="",
        help="Saved trax profile for the services' client; default resolves as trax.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="run: level for loop's own loggers; other libraries log WARNING and up.",
    )


def _configure_logging(level: str) -> None:
    """Log ``loop``'s records at ``level`` to stderr, other libraries' at WARNING."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("loop").setLevel(level)


def _only(deployment: Deployment) -> str:
    """Return the deployment's single addon name, or ask for ``--addon``."""
    if len(deployment.manifests) != 1:
        raise DeploymentError(
            f"--addon is required; the deployment runs {list(deployment.manifests)}.",
        )
    return next(iter(deployment.manifests))


async def _until_signalled(serve: Coroutine[object, object, None]) -> None:
    """Await ``serve``; cancel it on SIGINT or SIGTERM so services clean up."""
    task = asyncio.ensure_future(serve)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        if not task.cancelled():
            raise


# This module is imported, never run directly, so it carries no run guard and
# no shebang. `__main__.py` is the sole executable; run it via
# `python -m trackinizer.addons`.
