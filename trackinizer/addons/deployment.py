"""A deployment: the configgle composition of one site's visuals and addons."""

from __future__ import annotations

from dataclasses import field, fields
from typing import TYPE_CHECKING

import asyncio
import logging
import time

from configgle import Fig, Makeable
from configgle.cli_override import apply_overrides
from configgle.launch import resolve_config

from trackinizer.addons.addon import (
    Addon,
    AddonCatalogBody,
    AddonDescription,
    AddonManifest,
)
from trackinizer.server.visuals.catalog import Workspace


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator


__all__ = [
    "Deployment",
    "DeploymentError",
    "deployment_config",
    "supervise",
]

_logger = logging.getLogger(__name__)


class DeploymentError(Exception):
    """A deployment cannot be resolved or built from what the operator supplied.

    Raised for operator input only (a factory path, an override, an addon name),
    so a command line can turn it into a clean exit instead of a traceback.
    """


class Deployment:
    """One site's visuals and addons, built once and shared by server and runner.

    A site subclasses ``Deployment.Config`` and adds one field per addon, typed
    with that addon's own config, so a factory reads and sets it with full type
    checking. The field name is the addon's name.
    """

    class Config(Fig["Deployment"]):
        visuals: Workspace.Config = field(default_factory=Workspace.Config)
        """The visual modules the browser offers, and the one a new canvas opens."""

    def __init__(self, config: Config) -> None:
        """Build the visual catalog and every addon's manifest; validate both."""
        self.visuals = config.visuals.make()
        self.manifests: dict[str, AddonManifest] = {}
        descriptions: list[AddonDescription] = []
        for name, addon in _addon_configs(config):
            built = addon.make()
            if not isinstance(built, Addon):
                raise DeploymentError(
                    f"Field {name!r} builds a {type(built).__name__}, which has no "
                    "manifest(); every field of a deployment but 'visuals' is an "
                    "addon config or None.",
                )
            manifest = self.manifests[name] = built.manifest()
            try:
                descriptions.append(manifest.describe(name))
            except TypeError as err:
                raise DeploymentError(f"Addon {name!r}: {err}") from err
        self._catalog = AddonCatalogBody(addons=descriptions)

    def catalog(self) -> AddonCatalogBody:
        """Describe every addon without starting any of them."""
        return self._catalog

    def manifest(self, name: str) -> AddonManifest:
        """Return the named addon's manifest.

        Args:
          name: The addon's field name in the deployment config.

        Returns:
          manifest: That addon's parts.

        Raises:
          DeploymentError: No addon in this deployment has that name.

        """
        if name not in self.manifests:
            raise DeploymentError(
                f"Deployment has no addon {name!r}; it runs {list(self.manifests)}.",
            )
        return self.manifests[name]


def deployment_config(factory: str, *, overrides: list[str]) -> Deployment.Config:
    """Resolve a dotted factory path, then apply ``PATH=VALUE`` overrides.

    An empty ``factory`` is the base deployment: the default visuals and no
    addons. The factory is operator input (a flag or a unit file), never browser
    or agent input: resolving it imports the named module.

    ``<addon>.<field>=VALUE`` reaches a field of that addon's config, and
    ``<addon>=null`` switches an optional addon off.

    Args:
      factory: Dotted path to a zero-argument function returning a
        :class:`Deployment` config.
      overrides: configgle overrides applied to the returned config.

    Returns:
      config: The deployment config, not yet built.

    Raises:
      DeploymentError: The factory does not resolve to a deployment config, or
        an override does not apply.

    """
    try:
        config = resolve_config(factory) if factory else Deployment.Config()
    except (AttributeError, ImportError, TypeError, ValueError) as err:
        raise DeploymentError(str(err)) from err
    if not isinstance(config, Deployment.Config):
        raise DeploymentError(
            f"{factory!r} returned {type(config).__name__}, not a Deployment config.",
        )
    try:
        apply_overrides(config, overrides=overrides)
    except ValueError as err:
        raise DeploymentError(str(err)) from err
    return config


async def supervise(
    name: str,
    run: Callable[[], Awaitable[None]],
    *,
    backoff_sec: float = 1.0,
    max_backoff_sec: float = 60.0,
) -> None:
    """Run one service, restarting it after a crash with capped backoff.

    A service that returns has finished and is not restarted. One that has run
    longer than ``max_backoff_sec`` before crashing restarts after the base
    delay again, so a rare crash never inherits an old streak's wait.

    Args:
      name: ``<addon>.<service>``, used in the log lines.
      run: Starts one attempt of the service.
      backoff_sec: First restart delay.
      max_backoff_sec: Longest restart delay.

    """
    delay_sec = backoff_sec
    while True:
        started_sec = time.monotonic()
        try:
            await run()
        except Exception:
            _logger.exception(
                "addon service %s crashed; restarting in %.1f s",
                name,
                delay_sec,
            )
        else:
            _logger.info("addon service %s finished", name)
            return
        if time.monotonic() - started_sec > max_backoff_sec:
            delay_sec = backoff_sec
        await asyncio.sleep(delay_sec)
        delay_sec = min(delay_sec * 2, max_backoff_sec)


def _addon_configs(config: Deployment.Config) -> Iterator[tuple[str, Makeable[object]]]:
    """Yield ``(name, config)`` for every addon field that is switched on."""
    for config_field in fields(config):
        value: object = getattr(config, config_field.name, None)
        if config_field.name == "visuals" or value is None:
            continue
        if not isinstance(value, Makeable):
            raise DeploymentError(
                f"Field {config_field.name!r} holds a {type(value).__name__}, not an "
                "addon config; every field of a deployment but 'visuals' is an "
                "addon config or None.",
            )
        yield config_field.name, value
