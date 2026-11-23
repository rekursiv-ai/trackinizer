"""How a ``trax run`` session finds its station and tells the child CLI about it.

The server decides the placement (``POST /api/sessions/{id}/place``); this reads the
two things only the client knows, the station the run was started from and its git
checkout, and turns the answer into the child's environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sys

from trackinizer.client.errors import ClientError
from trackinizer.trax.context import workspace_from_env
from trackinizer.trax.run.worktree_diff import run_git
from trackinizer.wire.wire_sessions import (
    GitRef,
    PlaceSession,
    SessionPlacement,
    session_place_path,
)


if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import uuid

    from trackinizer.client.client import Client


def git_ref(cwd: Path) -> GitRef | None:
    """Return the checkout's origin URL and branch.

    Args:
      cwd: The run's working directory.

    Returns:
      ref: ``None`` when ``cwd`` is not in a git work tree, has no ``origin`` or is on
        a detached HEAD.

    """
    where = ["git", "-C", str(cwd)]
    branch = run_git([*where, "symbolic-ref", "--short", "-q", "HEAD"], 4096)
    origin = run_git([*where, "config", "--get", "remote.origin.url"], 4096)
    if branch.returncode != 0 or origin.returncode != 0:
        return None
    name = branch.stdout.decode(errors="replace").strip()
    url = origin.stdout.decode(errors="replace").strip()
    return GitRef(origin=url, branch=name) if name and url else None


def place_run(
    client: Client,
    *,
    session_id: uuid.UUID,
    environ: Mapping[str, str],
    cwd: Path,
) -> SessionPlacement | None:
    """Place the run's session; on a refusal warn once and return ``None``.

    Args:
      client: The run's client.
      session_id: The session the run opened.
      environ: The process environment.
      cwd: The run's working directory.

    Returns:
      placement: The server's answer, or ``None`` when it could not be had.

    """
    body = PlaceSession(workspace=workspace_from_env(environ), git=git_ref(cwd))
    try:
        answer = client.post(
            session_place_path(session_id),
            body=body.model_dump(mode="json"),
        )
        return SessionPlacement.model_validate(answer)
    except (ClientError, ValueError) as error:
        sys.stderr.write(
            f"[trax run] could not place this session ({error}); running unplaced\n",
        )
        return None


def placement_env(placement: SessionPlacement | None) -> dict[str, str]:
    """Return the variables that tell the child CLI where it was placed.

    Args:
      placement: The server's answer, or ``None`` for a run that was not placed.

    Returns:
      env: ``TRAX_WORKSTREAM`` and ``TRAX_WORKSPACE`` for each id the server named.

    """
    env: dict[str, str] = {}
    if placement is None:
        return env
    if placement.workstream_id is not None:
        env["TRAX_WORKSTREAM"] = str(placement.workstream_id)
    if placement.workspace_id is not None:
        env["TRAX_WORKSPACE"] = str(placement.workspace_id)
    return env
