"""The stable hostname: macOS's configured names over the network's, else the kernel's."""

from __future__ import annotations

from typing import TYPE_CHECKING

import socket

import pytest

from trackinizer.lib.posix.hostname import stable_hostname


if TYPE_CHECKING:
    from pathlib import Path


# What macOS hands the kernel on a network whose router names the computer.
_NETWORK_NAME = "unknown9626d97ae742.attlocal.net"


@pytest.fixture(autouse=True)
def _on_a_router_named_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: _NETWORK_NAME)


def test_a_mac_is_named_by_its_local_hostname_not_by_its_network(
    tmp_path: Path,
) -> None:
    scutil = _scutil(tmp_path, LocalHostName="Ada-MacBook")

    assert stable_hostname(scutil=scutil) == "Ada-MacBook"


def test_a_hostname_someone_set_wins_over_the_local_hostname(tmp_path: Path) -> None:
    scutil = _scutil(tmp_path, HostName="lab-mac.example.org", LocalHostName="Lab-Mac")

    assert stable_hostname(scutil=scutil) == "lab-mac.example.org"


def test_a_mac_with_no_name_configured_falls_back_to_the_kernels(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    assert stable_hostname(scutil=_scutil(tmp_path)) == _NETWORK_NAME
    # Scutil's "not set" is its answer, not a fault to show whoever runs the caller.
    assert capfd.readouterr().err == ""


def test_a_computer_without_scutil_is_named_by_the_kernel(tmp_path: Path) -> None:
    assert stable_hostname(scutil=tmp_path / "absent") == _NETWORK_NAME


def test_an_scutil_that_cannot_run_falls_back_to_the_kernels(tmp_path: Path) -> None:
    scutil = _scutil(tmp_path, LocalHostName="Ada-MacBook")
    scutil.chmod(0o600)

    assert stable_hostname(scutil=scutil) == _NETWORK_NAME


def _scutil(directory: Path, **names: str) -> Path:
    """Write an ``scutil`` that answers ``--get KEY`` from ``names``, as macOS does."""
    answers = "".join(
        f'if [ "$1 $2" = "--get {key}" ]; then echo {value}; exit 0; fi\n'
        for key, value in names.items()
    )
    path = directory / "scutil"
    _ = path.write_text(f'#!/bin/sh\n{answers}echo "$2: not set" >&2\nexit 1\n')
    path.chmod(0o700)
    return path


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
