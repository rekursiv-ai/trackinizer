from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import sys

from trackinizer.trax.context import (
    CWD,
    ENV,
    ERR_STREAM,
    OUT_STREAM,
    OVERLAID_NAMES,
    cwd,
    env,
    err_stream,
    out_stream,
    trax_env,
)


if TYPE_CHECKING:
    import pytest


def test_streams_default_to_the_process_streams() -> None:
    assert out_stream() is sys.stdout
    assert err_stream() is sys.stderr


def test_bound_streams_win() -> None:
    out, err = StringIO(), StringIO()
    out_token, err_token = OUT_STREAM.set(out), ERR_STREAM.set(err)
    try:
        assert out_stream() is out
        assert err_stream() is err
    finally:
        OUT_STREAM.reset(out_token)
        ERR_STREAM.reset(err_token)


def test_env_without_overlay_reads_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAX_CTX_PROBE", "process")
    assert env("TRAX_CTX_PROBE") == "process"


def test_overlay_is_authoritative_for_claimed_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAIMED", "daemon")
    monkeypatch.setenv("UNCLAIMED", "daemon")
    env_token = ENV.set({"GIVEN": "caller"})
    names_token = OVERLAID_NAMES.set(frozenset({"GIVEN", "CLAIMED"}))
    try:
        assert env("GIVEN") == "caller"
        # Claimed but unset by the caller: unset, never the daemon's value.
        assert env("CLAIMED") is None
        assert env("UNCLAIMED") == "daemon"
    finally:
        ENV.reset(env_token)
        OVERLAID_NAMES.reset(names_token)


def test_overlay_without_claimed_names_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTHER", "daemon")
    token = ENV.set({})
    try:
        assert env("OTHER") == "daemon"
    finally:
        ENV.reset(token)


def test_cwd_defaults_to_the_process_and_honors_binding(tmp_path: Path) -> None:
    assert cwd() == Path.cwd()
    token = CWD.set(str(tmp_path))
    try:
        assert cwd() == tmp_path
    finally:
        CWD.reset(token)


def test_trax_env_prefers_the_plain_name_over_the_ssh_forwarded_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRAX_CANVAS", raising=False)
    monkeypatch.delenv("LC_TRAX_CANVAS", raising=False)
    assert trax_env("CANVAS") is None
    monkeypatch.setenv("LC_TRAX_CANVAS", "from-ssh")
    assert trax_env("CANVAS") == "from-ssh"
    monkeypatch.setenv("TRAX_CANVAS", "plain")
    assert trax_env("CANVAS") == "plain"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
