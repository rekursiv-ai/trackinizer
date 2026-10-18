"""Tests for the hook's process entry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.trax.run.box_hook import entry


if TYPE_CHECKING:
    import pytest


def test_main_reads_its_flags_from_the_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRAX_WORKSPACE", raising=False)
    monkeypatch.delenv("LC_TRAX_WORKSPACE", raising=False)
    monkeypatch.setattr("sys.argv", ["hook", "--seen", "/nonexistent/seen"])

    assert entry.main() == 0


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
