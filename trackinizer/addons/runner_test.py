"""The runner starts standalone services and reports bad input cleanly."""

from __future__ import annotations

from dataclasses import field

import asyncio
import runpy
import signal
import sys

from configgle import Fig, Makes

import pytest

from trackinizer.addons.addon import AddonManifest, StandaloneContext
from trackinizer.addons.deployment import Deployment, DeploymentError
from trackinizer.addons.runner import main, run_standalone, serve_standalone
from trackinizer.client.client import Client


class _Recorder:
    """A standalone service that records the context it was given, then waits."""

    def __init__(self, *, name: str, seen: list[tuple[str, StandaloneContext]]) -> None:
        self.name = name
        self.description = "Records its context."
        self.started = asyncio.Event()
        self._seen = seen

    async def run(self, context: StandaloneContext) -> None:
        self._seen.append((self.name, context))
        self.started.set()
        await asyncio.Event().wait()


class _Stopper:
    """A standalone service that asks its own process to stop."""

    name = "stopper"
    description = "Sends SIGINT to the runner."

    async def run(self, context: StandaloneContext) -> None:
        del context
        signal.raise_signal(signal.SIGINT)
        await asyncio.Event().wait()


class StopperAddon:
    """A test addon with a standalone service that stops the runner."""

    class Config(Fig["StopperAddon"]):
        pass

    def __init__(self, config: StopperAddon.Config) -> None:
        del config

    def manifest(self) -> AddonManifest:
        return AddonManifest(
            title="Stopper",
            description="Stops.",
            standalone_services=[_Stopper()],
        )


class ServerOnlyAddon:
    """A test addon with no standalone service."""

    class Config(Fig["ServerOnlyAddon"]):
        pass

    def __init__(self, config: ServerOnlyAddon.Config) -> None:
        del config

    def manifest(self) -> AddonManifest:
        return AddonManifest(title="Server only", description="Server.")


class OneAddon(Makes["Deployment"], Deployment.Config):
    """A deployment with the one addon that stops the runner."""

    stopper: StopperAddon.Config = field(default_factory=StopperAddon.Config)
    """Stops the runner."""


class TwoAddons(OneAddon):
    """A deployment with a stopping addon and a server-only one."""

    plain: ServerOnlyAddon.Config = field(default_factory=ServerOnlyAddon.Config)
    """Has no standalone service."""


def one_addon() -> OneAddon:
    """Return a deployment with the one addon that stops the runner."""
    return OneAddon()


def two_addons() -> TwoAddons:
    """Return a deployment with a stopping addon and a server-only one."""
    return TwoAddons()


@pytest.mark.asyncio
async def test_every_standalone_service_runs_with_the_context() -> None:
    seen: list[tuple[str, StandaloneContext]] = []
    services = [_Recorder(name="one", seen=seen), _Recorder(name="two", seen=seen)]
    manifest = AddonManifest(
        title="Pair",
        description="Two services.",
        standalone_services=services,
    )
    with Client("http://127.0.0.1:9") as client:
        context = StandaloneContext(client=client)
        task = asyncio.create_task(
            run_standalone("pair", manifest=manifest, context=context),
        )
        async with asyncio.timeout(5):
            for service in services:
                await service.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert sorted(name for name, _ in seen) == ["one", "two"]
    assert all(given is context for _, given in seen)


def test_an_addon_without_standalone_services_is_refused() -> None:
    manifest = AddonManifest(title="S", description="S.")
    with pytest.raises(DeploymentError, match="inside the trackinizer server"):
        serve_standalone("server-only", manifest=manifest)


def test_show_prints_the_config_and_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["addons", "show", f"{__name__}.two_addons"])
    assert main() == 0
    out = capsys.readouterr().out
    assert '"name": "stopper"' in out
    assert "addons" in out


def test_run_serves_the_only_addon_until_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACKINIZER_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(sys, "argv", ["addons", "run", f"{__name__}.one_addon"])
    assert main() == 0


def test_run_takes_a_named_addon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACKINIZER_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(
        sys,
        "argv",
        ["addons", "run", f"{__name__}.two_addons", "--addon", "stopper"],
    )
    assert main() == 0


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["run", "no.such.factory"], "Cannot import module"),
        (
            ["run", f"{__name__}.two_addons"],
            r"--addon is required.*\['stopper', 'plain'\]",
        ),
        (["run", f"{__name__}.two_addons", "--addon", "nope"], "no addon 'nope'"),
        (["run", f"{__name__}.two_addons", "--addon", "plain"], "no standalone"),
        (["run", f"{__name__}.one_addon", "--profile", "nope"], "profile 'nope'"),
        (["show", f"{__name__}.one_addon", "--override", "nope=1"], "no field"),
    ],
)
def test_bad_input_exits_with_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    message: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["addons", *argv])
    with pytest.raises(SystemExit, match=message):
        main()


def test_the_module_entry_point_dispatches_to_main() -> None:
    namespace = runpy.run_module(
        "trackinizer.addons.__main__",
        run_name="not_main",
    )
    assert namespace["main"] is main


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
