"""Tests for the ``trackinizer`` console-script entry point."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.server import entry


if TYPE_CHECKING:
    from collections.abc import Callable


def _serving(code: int) -> Callable[[], int]:
    """Return a stand-in for ``server.server.main`` that exits with ``code``."""

    def serve() -> int:
        return code

    return serve


def _missing(name: str | None) -> Callable[[], int]:
    """Return a stand-in for ``server.server.main`` whose import finds no ``name``."""

    def serve() -> int:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    return serve


def test_main_returns_the_servers_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """An installed ``server`` extra changes nothing: the server's code comes back."""
    monkeypatch.setattr(entry, "serve", _serving(7))

    assert entry.main() == 7


@pytest.mark.parametrize("name", sorted(entry.SERVER_ONLY))
def test_main_names_the_extra_when_a_server_package_is_missing(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each package of the extra, missing, prints the install line and exits 2."""
    monkeypatch.setattr(entry, "serve", _missing(name))

    assert entry.main() == 2

    captured = capsys.readouterr()
    assert captured.err == "the server needs: pip install 'trackinizer[server]'\n"
    assert not captured.out


def test_main_names_the_extra_when_a_submodule_of_a_server_package_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dotted name is matched by its top-level package."""
    monkeypatch.setattr(entry, "serve", _missing("fastapi.responses"))

    assert entry.main() == 2
    assert "trackinizer[server]" in capsys.readouterr().err


@pytest.mark.parametrize(
    "name",
    ["trackinizer.nope", "not_a_server_package", None],
)
def test_main_raises_any_other_missing_module(
    name: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module outside the extra is a defect, not a missing extra: it propagates."""
    monkeypatch.setattr(entry, "serve", _missing(name))

    with pytest.raises(ModuleNotFoundError):
        entry.main()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
