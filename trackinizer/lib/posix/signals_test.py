"""Tests for ``kill`` and ``killpg``: one process or child's group, never more.

``os.kill``, ``os.killpg`` and ``os.getpgrp`` are faked, so no test here sends a
real signal. This process's group is 500; a target the fake knows nothing of
raises ``ProcessLookupError``, as the kernel does for one that has gone.
"""

from __future__ import annotations

from functools import partial

import os
import signal

import pytest

from trackinizer.lib.posix.signals import kill, killpg


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Record what reaches ``os.kill`` and ``os.killpg``; only 1000 is alive."""
    sent: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "getpgrp", lambda: 500)
    monkeypatch.setattr(os, "kill", partial(_record, sent, "kill"))
    monkeypatch.setattr(os, "killpg", partial(_record, sent, "killpg"))
    return sent


@pytest.mark.parametrize(
    "pid",
    [1, 0, -1, -500, os.getpid()],
    ids=["init", "own-group-by-zero", "every-process", "a-group", "this-process"],
)
def test_a_signal_past_one_other_process_is_refused(
    sent: list[tuple[str, int, int]],
    pid: int,
) -> None:
    """``kill(-1)`` is every process the user may signal; ``kill(-g)`` a whole group."""
    with pytest.raises(ValueError, match=f"pid {pid}:"):
        kill(pid, signal.SIGTERM)
    assert sent == []


@pytest.mark.parametrize(
    "pgid",
    [1, 0, -1, 500],
    ids=["every-process", "own-group-by-zero", "negative", "own-group"],
)
def test_a_group_signal_past_a_childs_own_group_is_refused(
    sent: list[tuple[str, int, int]],
    pgid: int,
) -> None:
    """Under glibc ``killpg(1)`` is ``kill(-1)``: every process the user may signal."""
    with pytest.raises(ValueError, match=f"process group {pgid}:"):
        killpg(pgid, signal.SIGTERM)
    assert sent == []


def test_the_lowest_ids_past_inits_are_handed_to_the_kernel(
    sent: list[tuple[str, int, int]],
) -> None:
    """Pid and group 2 may be a child's; whether they exist is the kernel's to say."""
    with pytest.raises(ProcessLookupError):
        kill(2, signal.SIGTERM)
    with pytest.raises(ProcessLookupError):
        killpg(2, signal.SIGTERM)
    assert sent == [("kill", 2, signal.SIGTERM), ("killpg", 2, signal.SIGTERM)]


def test_a_child_and_its_group_are_signalled(sent: list[tuple[str, int, int]]) -> None:
    kill(1000, signal.SIGKILL)
    killpg(1000, signal.SIGTERM)
    assert sent == [("kill", 1000, signal.SIGKILL), ("killpg", 1000, signal.SIGTERM)]


@pytest.mark.parametrize(
    "target",
    [1, 0, -1, 500, os.getpid()],
    ids=["init", "own-group-by-zero", "every-process", "own-group", "this-process"],
)
def test_a_probe_sends_nothing_and_reaches_the_kernel_whatever_its_target(
    sent: list[tuple[str, int, int]],
    target: int,
) -> None:
    """Signal 0 only asks whether the target exists."""
    with pytest.raises(ProcessLookupError):
        kill(target, 0)
    with pytest.raises(ProcessLookupError):
        killpg(target, 0)
    assert sent == [("kill", target, 0), ("killpg", target, 0)]


def _record(sent: list[tuple[str, int, int]], call: str, target: int, sig: int) -> None:
    sent.append((call, target, sig))
    if target != 1000:
        raise ProcessLookupError(3, "No such process")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
