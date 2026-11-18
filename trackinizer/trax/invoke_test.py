"""Tests for ``invoke``: one trax command run inside another program."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING, Final

import sys

import httpx2

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.trax.cli import parse_and_run
from trackinizer.trax.commands import TraxArgumentParser
from trackinizer.trax.context import (
    CWD,
    ENV,
    ERR_STREAM,
    LOCAL_INPUT,
    OUT_STREAM,
    cwd,
    env,
)
from trackinizer.trax.invoke import invoke
from trackinizer.trax.render import echo, table_width
from trackinizer.trax.verbs import Kind, Version


if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest


_UNBOUND: Final = (None, None, None, "", True)
"""The ambient state of a context no run has bound: :func:`_ambient`'s defaults."""


def test_captures_what_the_command_echoes() -> None:
    def both(argv: Sequence[str]) -> None:
        del argv
        echo("out")
        echo("err", err=True)

    ran = invoke(["x"], env={}, width=0, local_input=None, run=both)

    assert (ran.stdout, ran.stderr, ran.exit_code) == ("out\n", "err\n", 0)


def test_maps_how_a_command_ends_as_the_cli_does() -> None:
    def failed(argv: Sequence[str]) -> None:
        del argv
        raise ClientError("nope")

    def exited(argv: Sequence[str]) -> None:
        del argv
        sys.exit(3)

    def said(argv: Sequence[str]) -> None:
        del argv
        sys.exit("gone")

    def broke(argv: Sequence[str]) -> None:
        del argv
        raise RuntimeError("kaboom")

    ends = [
        invoke(["x"], env={}, width=0, local_input=None, run=run)
        for run in (failed, exited, said, broke)
    ]

    assert [(ran.exit_code, ran.stderr) for ran in ends[:3]] == [
        (2, "trax: nope\n"),
        (3, ""),
        (1, "gone\n"),
    ]
    assert ends[3].exit_code == 70
    assert "RuntimeError: kaboom" in ends[3].stderr


def test_a_bare_exit_is_success() -> None:
    def done(argv: Sequence[str]) -> None:
        echo(" ".join(argv))
        sys.exit()

    ran = invoke(["x"], env={}, width=0, local_input=None, run=done)

    assert (ran.stdout, ran.stderr, ran.exit_code) == ("x\n", "", 0)


def test_binds_the_callers_state_and_claims_every_routing_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routing name absent from ``env`` reads unset, never as this process's.

    A folder binds where ``@path`` resolves and lets local input be read; without one
    the folder is this process's own and nothing local is read.
    """
    monkeypatch.setenv("TRACKINIZER_URL", "https://other.example")
    monkeypatch.setenv("TRACKINIZER_TOKEN", "secret")
    monkeypatch.setenv("TRAX_CANVAS", "c1")

    def seen(argv: Sequence[str]) -> None:
        del argv
        echo(
            [
                env("TRACKINIZER_URL"),
                env("TRACKINIZER_TOKEN"),
                env("TRAX_CANVAS"),
                env("USER"),
                str(cwd()),
                table_width(),
                LOCAL_INPUT.get(),
            ],
        )

    ran = invoke(["x"], env={"USER": "agent"}, width=120, local_input="/work", run=seen)
    sealed = invoke(["x"], env={}, width=0, local_input=None, run=seen)

    assert ran.stdout == "[None, None, None, 'agent', '/work', 120, True]\n"
    assert sealed.stdout == f"[None, None, None, None, {str(Path.cwd())!r}, 0, False]\n"


def test_writes_nothing_to_the_process_streams(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A usage error reaches the caller's stderr, not the host program's terminal."""

    def parsed(argv: Sequence[str]) -> None:
        TraxArgumentParser(prog="trax x").parse_args(list(argv))

    ran = invoke(["--bad"], env={}, width=0, local_input=None, run=parsed)

    assert ran.exit_code == 2
    assert "usage: trax x" in ran.stderr
    assert "--bad" in ran.stderr
    assert capsys.readouterr() == ("", "")


def test_runs_parse_and_run_with_the_given_client() -> None:
    """Connection flags are inert: the given client is the one every verb uses."""
    sent: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(f"{request.url.host} {request.url.path}")
        return httpx2.Response(200, json={"sha": "abc123"})

    with Client("https://given.test", transport=httpx2.MockTransport(answer)) as client:
        ran = invoke(
            ["--host", "elsewhere.test", "version"],
            env={},
            width=0,
            local_input=None,
            run=partial(parse_and_run, client_factory=lambda: client),
        )

    assert (ran.stdout, ran.exit_code) == ("abc123\n", 0)
    assert sent == ["given.test /api/version"]


def test_refuses_a_verb_outside_allowed_by_name() -> None:
    """A refused verb is named as such, never read as a filter field."""
    refused = [
        invoke(
            argv,
            env={},
            width=0,
            local_input=None,
            run=partial(
                parse_and_run,
                client_factory=_no_client,
                allowed=(Kind, Version),
            ),
        )
        for argv in (["send", "lead#1", "hi"], ["profile"], ["export"])
    ]

    assert [(ran.exit_code, ran.stdout) for ran in refused] == [(2, "")] * 3
    assert [ran.stderr for ran in refused] == [
        "trax: send is not available here\n",
        "trax: profile is not available here\n",
        "trax: export is not available here\n",
    ]


def test_refuses_local_input_before_reading_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no folder, ``-`` and ``@path`` read no stdin and no file."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do not send")
    monkeypatch.setattr(sys, "stdin", _Unreadable())
    sent: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.url.path)
        return httpx2.Response(500)

    with Client("https://given.test", transport=httpx2.MockTransport(answer)) as client:
        refused = [
            invoke(
                argv,
                env={},
                width=0,
                local_input=None,
                run=partial(parse_and_run, client_factory=lambda: client),
            )
            for argv in (
                ["issue", "title", "to", "-"],
                ["issue", "title", "to", "@secret.txt"],
                ["issue", "title", "to", f"@{secret}"],
                ["env", "secret", "NAME", "to", "-"],
            )
        ]

    assert [ran.exit_code for ran in refused] == [2] * 4
    assert all("reads no local input here" in ran.stderr for ran in refused)
    assert all("do not send" not in ran.stderr + ran.stdout for ran in refused)
    assert sent == []


def test_keeps_concurrent_runs_apart() -> None:
    """Every run is inside its command at once, and the caller's state is untouched."""
    names = [f"agent{index}" for index in range(4)]
    together = Barrier(len(names))

    def both_at_once(argv: Sequence[str]) -> None:
        del argv
        echo(env("USER"), err=True)
        together.wait(timeout=10)
        echo(env("USER"))

    def one(name: str) -> tuple[str, str, tuple[object, ...]]:
        ran = invoke(
            [name],
            env={"USER": name},
            width=0,
            local_input=f"/{name}",
            run=both_at_once,
        )
        return ran.stdout, ran.stderr, _ambient()

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        outputs = list(pool.map(one, names))

    assert outputs == [(f"{name}\n", f"{name}\n", _UNBOUND) for name in names]


def test_leaves_the_callers_own_state_as_it_was() -> None:
    """A run binds its state in a context of its own, never the caller's."""
    ran = invoke(
        ["version"],
        env={"USER": "agent"},
        width=80,
        local_input="/elsewhere",
        run=lambda argv: echo(" ".join(argv)),
    )

    assert (ran.stdout, _ambient()) == ("version\n", _UNBOUND)


def _ambient() -> tuple[object, ...]:
    """Return this context's streams, environment, folder and local-input switch."""
    return OUT_STREAM.get(), ERR_STREAM.get(), ENV.get(), CWD.get(), LOCAL_INPUT.get()


def _no_client() -> Client:
    """Fail the test if a refused verb reached its client."""
    raise AssertionError("a refused verb must not build a client")


class _Unreadable:
    """A stdin that fails the test when anything reads it."""

    @property
    def buffer(self) -> _Unreadable:
        return self

    def read(self) -> str:
        raise AssertionError("stdin was read")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
