"""Deployment composition, factory resolution, and service supervision."""

from __future__ import annotations

from dataclasses import field
from functools import partial
from typing import TYPE_CHECKING

import asyncio
import logging

from configgle import Fig, Makes
from fastapi import APIRouter

import pytest

from trackinizer.addons.addon import AddonManifest
from trackinizer.addons.deployment import (
    Deployment,
    DeploymentError,
    deployment_config,
    supervise,
)
from trackinizer.server.visuals.catalog import Workspace


if TYPE_CHECKING:
    from trackinizer.addons.addon import ServerContext, StandaloneContext


class NamedAddon:
    """A test addon with one service of each placement and one route."""

    class Config(Fig["NamedAddon"]):
        title: str = "Named"
        """Title shown in the catalog."""

    def __init__(self, config: Config) -> None:
        self.title = config.title

    def manifest(self) -> AddonManifest:
        router = APIRouter()
        router.add_api_route("/ping", _ping)
        return AddonManifest(
            title=self.title,
            description="A test addon.",
            server_services=[_Idle(name="sweep")],
            standalone_services=[_IdleStandalone(name="relay")],
            routers=[router],
        )


class SocketAddon:
    """A test addon whose router holds a WebSocket route."""

    class Config(Fig["SocketAddon"]):
        pass

    def __init__(self, config: Config) -> None:
        del config

    def manifest(self) -> AddonManifest:
        router = APIRouter()
        router.add_api_websocket_route("/live", _socket)
        return AddonManifest(title="Socket", description="Bad.", routers=[router])


class Site(Makes["Deployment"], Deployment.Config):
    """A site deployment: one field per addon, typed with the addon's config."""

    first: NamedAddon.Config = field(default_factory=NamedAddon.Config)
    """The first addon."""

    second: NamedAddon.Config | None = field(default_factory=NamedAddon.Config)
    """The second addon; ``None`` switches it off."""


class Broken(Makes["Deployment"], Deployment.Config):
    """A deployment with a field that is neither an addon config nor None."""

    stray: int = 3
    """Not an addon."""


class ChildOnly:
    """A buildable object that is not an addon."""

    class Config(Fig["ChildOnly"]):
        pass

    def __init__(self, config: Config) -> None:
        del config


class Odd(Makes["Deployment"], Deployment.Config):
    """A deployment whose one field builds something that is not an addon."""

    first: ChildOnly.Config = field(default_factory=ChildOnly.Config)
    """Builds a ChildOnly."""


class Socketed(Makes["Deployment"], Deployment.Config):
    """A deployment whose one addon mounts a WebSocket route."""

    live: SocketAddon.Config = field(default_factory=SocketAddon.Config)
    """The offending addon."""


def two_addons() -> Site:
    """Return a deployment of two differently named test addons."""
    return Site()


def not_a_deployment() -> NamedAddon.Config:
    """Return an addon config where a deployment is expected."""
    return NamedAddon.Config()


def test_the_catalog_describes_each_addon_by_placement() -> None:
    catalog = two_addons().make().catalog()
    assert [addon.name for addon in catalog.addons] == ["first", "second"]
    first = catalog.addons[0]
    assert [(service.name, service.placement) for service in first.services] == [
        ("sweep", "server"),
        ("relay", "standalone"),
    ]
    assert first.routes == ["/api/addons/first/ping"]


def test_the_visual_catalog_is_part_of_the_deployment() -> None:
    cfg = Deployment.Config()
    cfg.visuals.default_visual = "trax.chat"
    deployment = cfg.make()
    assert isinstance(deployment.visuals, Workspace)
    assert deployment.visuals.default_visual == "trax.chat"


def test_a_field_that_is_not_an_addon_config_is_refused() -> None:
    with pytest.raises(DeploymentError, match=r"Field 'stray' holds a int"):
        Broken().make()


def test_an_addon_config_that_builds_no_addon_is_refused() -> None:
    with pytest.raises(DeploymentError, match=r"Field 'first' builds a ChildOnly"):
        Odd().make()


def test_a_websocket_route_is_refused_naming_the_addon() -> None:
    with pytest.raises(DeploymentError, match=r"Addon 'live'.*WebSocket"):
        Socketed().make()


def test_manifest_lookup_names_what_exists() -> None:
    deployment = two_addons().make()
    assert deployment.manifest("second").title == "Named"
    with pytest.raises(DeploymentError, match=r"\['first', 'second'\]"):
        deployment.manifest("third")


def test_an_empty_factory_runs_no_addons() -> None:
    assert deployment_config("", overrides=[]).make().manifests == {}


def test_a_factory_path_resolves_and_takes_overrides() -> None:
    config = deployment_config(f"{__name__}.two_addons", overrides=["second=null"])
    assert isinstance(config, Site)
    assert config.second is None
    assert list(config.make().manifests) == ["first"]


def test_an_override_reaches_a_field_of_a_named_addon() -> None:
    config = deployment_config(
        f"{__name__}.two_addons",
        overrides=["second.title=Renamed", "visuals.default_visual=trax.chat"],
    )
    deployment = config.make()
    assert deployment.manifest("first").title == "Named"
    assert deployment.manifest("second").title == "Renamed"
    assert deployment.visuals.default_visual == "trax.chat"


def test_an_override_naming_an_unknown_addon_is_refused() -> None:
    with pytest.raises(DeploymentError, match="no field `third`"):
        deployment_config(f"{__name__}.two_addons", overrides=["third.title=x"])


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        ("no.such.factory", "Cannot import module"),
        ("nodots", "not a dotted path"),
        (f"{__name__}.missing_function", "no attribute"),
        (f"{__name__}.not_a_deployment", "not a Deployment config"),
    ],
)
def test_a_bad_factory_raises_a_deployment_error(factory: str, message: str) -> None:
    with pytest.raises(DeploymentError, match=message):
        deployment_config(factory, overrides=[])


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("nope=1", "no field"),
        ("first", "Malformed override"),
        ("first.nope=1", "no field `nope`"),
    ],
)
def test_a_bad_override_raises_a_deployment_error(override: str, message: str) -> None:
    with pytest.raises(DeploymentError, match=message):
        deployment_config(f"{__name__}.two_addons", overrides=[override])


@pytest.mark.asyncio
async def test_supervise_restarts_a_crashed_service_until_it_finishes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts: list[int] = []

    async def flaky() -> None:
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise RuntimeError("boom")

    with caplog.at_level(logging.INFO):
        await supervise(
            "test.flaky",
            run=flaky,
            backoff_sec=0.001,
            max_backoff_sec=0.01,
        )
    assert attempts == [0, 1, 2]
    messages = [record.getMessage() for record in caplog.records]
    assert sum("crashed" in message for message in messages) == 2
    assert messages[-1] == "addon service test.flaky finished"


@pytest.mark.asyncio
async def test_supervise_resets_the_delay_after_a_long_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts: list[int] = []
    fifth_attempt = asyncio.Event()

    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(
            supervise(
                "test.late",
                run=partial(
                    _crash_late,
                    attempts=attempts,
                    fifth_attempt=fifth_attempt,
                ),
                backoff_sec=0.001,
                max_backoff_sec=0.02,
            ),
        )
        await asyncio.wait_for(fifth_attempt.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    delays = [
        record.args[1]
        for record in caplog.records
        if isinstance(record.args, tuple) and "crashed" in record.getMessage()
    ]
    assert delays[:4] == [0.001, 0.002, 0.004, 0.002]


@pytest.mark.asyncio
async def test_supervise_propagates_cancellation() -> None:
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(supervise("test.forever", run=forever))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _ping() -> dict[str, bool]:
    return {"pong": True}


async def _socket() -> None:
    return None


class _Idle:
    """A server service that does nothing until cancelled."""

    def __init__(self, *, name: str) -> None:
        self.name = name
        self.description = "Waits."

    async def run(self, context: ServerContext) -> None:
        del context
        await asyncio.Event().wait()


class _IdleStandalone:
    """A standalone service that does nothing until cancelled."""

    def __init__(self, *, name: str) -> None:
        self.name = name
        self.description = "Waits."

    async def run(self, context: StandaloneContext) -> None:
        del context
        await asyncio.Event().wait()


async def _crash_late(*, attempts: list[int], fifth_attempt: asyncio.Event) -> None:
    """Crash every attempt; attempt 3 runs long enough to reset the backoff."""
    attempts.append(len(attempts))
    if len(attempts) == 3:
        await asyncio.sleep(0.03)
    if len(attempts) == 5:
        fifth_attempt.set()
    raise RuntimeError("boom")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
