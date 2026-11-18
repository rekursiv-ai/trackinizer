"""What a `trax run` session tells the server about where it started, and the child."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import uuid

import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.trax.context import workspace_from_env
from trackinizer.trax.run.placement import git_ref, place_run, placement_env
from trackinizer.trax.run.worktree_diff import run_git
from trackinizer.wire.wire_sessions import SessionPlacement


if TYPE_CHECKING:
    from pathlib import Path


_STATION = uuid.UUID(int=5)
_SESSION = uuid.UUID(int=1)


class _Client:
    def __init__(self, *, fail: bool = False) -> None:
        self.asked: list[tuple[str, object]] = []
        self._fail = fail

    def post(self, path: str, *, body: object = None) -> dict[str, str]:
        self.asked.append((path, body))
        if self._fail:
            raise ClientError("404 Not Found")
        return {"workstream_id": str(uuid.UUID(int=2))}


def _git(cwd: Path, *argv: str) -> None:
    done = run_git(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *argv],
        4096,
    )
    assert done.returncode == 0, done.stdout
    assert not done.cut


@pytest.fixture(autouse=True)
def _own_git_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def test_the_station_comes_from_trax_workspace_before_its_ssh_twin() -> None:
    both = {"TRAX_WORKSPACE": str(_STATION), "LC_TRAX_WORKSPACE": str(uuid.UUID(int=6))}

    assert workspace_from_env(both) == _STATION
    assert workspace_from_env({"LC_TRAX_WORKSPACE": str(_STATION)}) == _STATION
    assert workspace_from_env({"TRAX_WORKSPACE": "not-a-uuid"}) is None
    assert workspace_from_env({}) is None


def test_a_checkout_on_a_branch_names_its_origin_and_branch(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "feature")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/o/r.git")

    ref = git_ref(tmp_path)

    assert ref is not None
    assert (ref.origin, ref.branch) == ("https://github.com/o/r.git", "feature")


def test_no_ref_for_a_directory_that_is_not_a_checkout_on_a_branch(
    tmp_path: Path,
) -> None:
    assert git_ref(tmp_path) is None
    _git(tmp_path, "init", "-q", "-b", "main")
    assert git_ref(tmp_path) is None  # No origin.
    _git(tmp_path, "remote", "add", "origin", "https://github.com/o/r.git")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "x")
    _git(tmp_path, "checkout", "-q", "--detach")
    assert git_ref(tmp_path) is None  # Detached HEAD.


def test_a_run_asks_the_server_with_its_station_and_checkout(tmp_path: Path) -> None:
    client = _Client()
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/o/r.git")

    placed = place_run(
        cast(Client, client),
        session_id=_SESSION,
        environ={"TRAX_WORKSPACE": str(_STATION)},
        cwd=tmp_path,
    )

    assert placed == SessionPlacement(workstream_id=uuid.UUID(int=2))
    assert client.asked == [
        (
            f"/api/sessions/{_SESSION}/place",
            {
                "workspace": str(_STATION),
                "git": {"origin": "https://github.com/o/r.git", "branch": "main"},
            },
        ),
    ]


def test_a_failed_ask_warns_and_leaves_the_run_unplaced(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    placed = place_run(
        cast(Client, _Client(fail=True)),
        session_id=_SESSION,
        environ={},
        cwd=tmp_path,
    )

    assert placed is None
    assert "running unplaced" in capsys.readouterr().err


def test_the_child_env_holds_only_what_the_server_placed() -> None:
    assert placement_env(None) == {}
    assert placement_env(SessionPlacement()) == {}
    assert placement_env(
        SessionPlacement(workstream_id=uuid.UUID(int=2), workspace_id=_STATION),
    ) == {"TRAX_WORKSTREAM": str(uuid.UUID(int=2)), "TRAX_WORKSPACE": str(_STATION)}


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
