from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from typing import TYPE_CHECKING

import subprocess

import pytest

from trackinizer.server import version
from trackinizer.server.version import build_sha


if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the per-process SHA cache around each test, and the deploy SHA."""
    monkeypatch.delenv("TRACKINIZER_SHA", raising=False)
    build_sha.cache_clear()
    yield
    build_sha.cache_clear()


def _git_answers(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
) -> list[tuple[object, ...]]:
    """Stand in for ``git rev-parse HEAD``; return the recorded argv list."""
    calls: list[tuple[object, ...]] = []

    def fake_run(
        args: tuple[str, ...],
        *,
        cwd: Path,
        **kwargs: object,
    ) -> CompletedProcess[str]:
        calls.append((args, cwd, kwargs.get("check"), kwargs.get("timeout")))
        return CompletedProcess(args=args, returncode=0, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_env_sha_wins_and_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _git_answers(monkeypatch, "from-git\n")
    monkeypatch.setenv("TRACKINIZER_SHA", "  abc123\n")
    assert build_sha() == "abc123"
    assert calls == []


def test_blank_env_falls_through_to_git(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _git_answers(monkeypatch, "deadbeef\n")
    monkeypatch.setenv("TRACKINIZER_SHA", "   ")
    assert build_sha() == "deadbeef"
    assert calls == [
        (
            ("git", "rev-parse", "HEAD"),
            Path(version.__file__).resolve().parent,
            True,
            2.0,
        ),
    ]


def test_result_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _git_answers(monkeypatch, "deadbeef\n")
    assert build_sha() == build_sha() == "deadbeef"
    assert len(calls) == 1


def test_empty_git_output_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _git_answers(monkeypatch, "\n")
    assert build_sha() == "unknown"


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(128, "git"),
        subprocess.TimeoutExpired("git", 2.0),
    ],
)
def test_git_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def failing_run(*args: object, **kwargs: object) -> CompletedProcess[str]:
        del args, kwargs
        raise error

    monkeypatch.setattr(subprocess, "run", failing_run)
    assert build_sha() == "unknown"


def test_build_sha_falls_back_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env var and no resolvable git, the SHA is the literal 'unknown'.

    Patch the env away and force the git probe to fail, so the fallback
    branch is exercised deterministically regardless of the test host.
    """
    monkeypatch.delenv("TRACKINIZER_SHA", raising=False)

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", _boom)
    build_sha.cache_clear()
    try:
        assert build_sha() == "unknown"
    finally:
        build_sha.cache_clear()


def test_build_sha_whitespace_env_falls_back_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only ``TRACKINIZER_SHA`` resolves to "unknown", not "".

    The env is checked for truthiness, so ``"   "`` is truthy; stripping it
    afterward yields ``""``, breaking the docstring promise of a SHA or
    "unknown" (TRK-SRV-003). The git probe is forced to fail so the only
    way to reach a non-"unknown" answer would be the (blank) env value.
    """
    monkeypatch.setenv("TRACKINIZER_SHA", "   ")

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", _boom)
    build_sha.cache_clear()
    try:
        assert build_sha() == "unknown"
    finally:
        build_sha.cache_clear()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
