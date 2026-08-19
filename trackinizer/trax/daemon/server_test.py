"""Behavior tests for the traxd request handler."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import os
import tempfile
import threading
import time

from trackinizer.client.errors import ClientError
from trackinizer.trax.cli import parse_and_run
from trackinizer.trax.context import cwd, env
from trackinizer.trax.daemon.protocol import PROTOCOL_VERSION, Request
from trackinizer.trax.daemon.server import handle
from trackinizer.trax.render import echo, table_width


if TYPE_CHECKING:
    import pytest


type Verb = Callable[[Sequence[str]], None]
"""What ``handle`` dispatches to; the seam tests inject through."""

_ACTORS: Sequence[str] = ("alice", "bob", "carol", "dave")
"""Four concurrent callers. Enough overlap to surface a shared-state race:
two threads let one finish before the other starts and reported clean."""

_POLL_ITERATIONS: int = 200
_POLL_INTERVAL_SEC: float = 0.000_1


def make_request(
    argv: Sequence[str],
    *,
    cwd: str = "",
    env: dict[str, str] | None = None,
    isatty: bool = False,
    columns: int = 0,
) -> Request:
    """Build a request, defaulting every field a given test does not pin."""
    return Request(
        argv=tuple(argv),
        cwd=cwd or tempfile.gettempdir(),
        env=env if env is not None else {},
        isatty=isatty,
        columns=columns,
        protocol_version=PROTOCOL_VERSION,
        source_version="v1",
    )


class TestHandle:
    def test_captures_stdout_from_the_verb(self) -> None:
        response = handle(make_request(["x"]), run=lambda _argv: echo("hello"))

        assert response.stdout == "hello\n"
        assert response.exit_code == 0

    def test_captures_stderr_separately(self) -> None:
        response = handle(make_request(["x"]), run=lambda _argv: echo("bad", err=True))

        assert response.stderr == "bad\n"
        assert response.stdout == ""

    def test_maps_client_error_to_the_cli_exit_code(self) -> None:
        """``main`` exits 2 on ClientError; scripts branch on that."""

        def boom(_argv: Sequence[str]) -> None:
            raise ClientError("nope")

        response = handle(make_request(["x"]), run=boom)

        assert response.exit_code == 2
        assert response.stderr == "trax: nope\n"

    def test_survives_an_unexpected_exception(self) -> None:
        """A verb bug must fail one request, never kill the shared daemon."""

        def boom(_argv: Sequence[str]) -> None:
            raise RuntimeError("kaboom")

        response = handle(make_request(["x"]), run=boom)

        assert response.exit_code != 0
        assert "kaboom" in response.stderr

    def test_applies_the_client_terminal_width(self) -> None:
        """Width comes from the CALLER's terminal, not the daemon's stdout.

        The daemon writes to a socket, so ``sys.stdout.isatty()`` is always
        False there; without injection every table renders unbounded.
        """
        seen: list[int] = []
        handle(
            make_request(["x"], isatty=True, columns=100),
            run=lambda _argv: seen.append(table_width()),
        )

        assert seen == [100]

    def test_applies_the_client_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identity resolves from the caller's env, never the daemon's."""
        monkeypatch.setenv("USER", "daemon-user")
        seen: list[str] = []
        handle(
            make_request(["x"], env={"USER": "caller"}),
            run=lambda _argv: seen.append(_current_user()),
        )

        assert seen == ["caller"]

    def test_leaves_the_process_environment_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A request binds a ContextVar overlay; it never writes ``os.environ``.

        Mutating the process environment would be visible to every other
        request in flight, and to the daemon itself.
        """
        monkeypatch.setenv("USER", "daemon-user")
        handle(make_request(["x"], env={"USER": "caller"}), run=lambda _argv: None)

        assert os.environ["USER"] == "daemon-user"

    def test_falls_back_to_the_process_environment_for_unforwarded_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a few names are forwarded; the rest still resolve normally."""
        monkeypatch.setenv("HOME", "/home/somebody")
        seen: list[str | None] = []
        handle(
            make_request(["x"], env={"USER": "caller"}),
            run=lambda _argv: seen.append(env("HOME")),
        )

        assert seen == ["/home/somebody"]

    def test_passes_argv_through_verbatim(self) -> None:
        seen: list[Sequence[str]] = []
        handle(make_request(["issue", "7", "title"]), run=seen.append)

        assert seen == [("issue", "7", "title")]


class TestStreamCapture:
    """Not every writer can be taught the ContextVars.

    ``argparse`` prints usage and errors straight to ``sys.stderr`` before
    raising ``SystemExit``. Under the daemon those are ``/dev/null``, so
    without a redirect an invalid flag returns exit 2 and NOTHING else -- the
    user sees a bare failure with no reason.
    """

    def test_captures_an_argparse_error(self) -> None:
        response = handle(
            make_request(["issue", "--format", "bogus"]), run=parse_and_run
        )

        assert response.exit_code != 0
        assert "bogus" in response.stderr, (
            "argparse wrote its diagnostic to the real stderr, which is the "
            "daemon's /dev/null; the user would see an exit code and no message"
        )

    def test_captures_a_direct_stdout_write(self) -> None:
        """Any stray ``print`` must land in the response, not the daemon's fd 1."""
        response = handle(make_request(["x"]), run=lambda _argv: print("direct"))  # noqa: T201 -- exercising the capture of a direct write.

        assert response.stdout == "direct\n"

    def test_reports_a_string_exit_payload(self) -> None:
        """``sys.exit("message")`` prints the message and exits 1.

        Coercing the payload with ``int()`` would raise instead, replacing a
        user-facing message with an internal-error traceback.
        """

        def bail(argv: Sequence[str]) -> None:
            del argv
            raise SystemExit("something went wrong")

        response = handle(make_request(["x"]), run=bail)

        assert response.exit_code == 1
        assert "something went wrong" in response.stderr


class TestForwardedEnvironment:
    """The overlay is authoritative for the names the client claims.

    The daemon inherits the environment of whichever shell spawned it. If an
    unset forwarded name fell through to that environment, a caller with no
    ``TRACKINIZER_PROFILE`` would silently adopt the spawning shell's -- and
    write to another server under another token.
    """

    def test_an_unset_forwarded_name_reads_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_PROFILE", "daemon-shell-profile")
        seen: list[str | None] = []
        handle(
            make_request(["x"], env={"USER": "caller"}),
            run=lambda _argv: seen.append(env("TRACKINIZER_PROFILE")),
        )

        assert seen == [None], (
            "an unset forwarded name resolved from the daemon's own "
            "environment; the caller would inherit another shell's profile"
        )

    def test_an_unforwarded_name_still_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``HOME`` and friends are process-wide by nature."""
        monkeypatch.setenv("HOME", "/home/somebody")
        seen: list[str | None] = []
        handle(
            make_request(["x"], env={"USER": "caller"}),
            run=lambda _argv: seen.append(env("HOME")),
        )

        assert seen == ["/home/somebody"]


class TestConcurrentRequests:
    """The daemon is threaded, so per-request state must be per-THREAD.

    ``os.environ`` and the process working directory are process-global: a
    handler that swaps them around a verb leaks one caller's values into
    every request running concurrently. That is not a cosmetic race --
    ``resolve_actor`` reads ``$USER`` to stamp the audit actor on writes, so
    under a polling swarm one agent's edit gets attributed to another.
    """

    def test_env_does_not_leak_between_concurrent_requests(self) -> None:
        contaminated: list[tuple[str, str]] = []
        lock = threading.Lock()

        def watcher(expected: str) -> Verb:
            def run(argv: Sequence[str]) -> None:
                del argv
                for _ in range(_POLL_ITERATIONS):
                    seen = env("USER") or ""
                    if seen != expected:
                        with lock:
                            contaminated.append((expected, seen))
                        return
                    time.sleep(_POLL_INTERVAL_SEC)

            return run

        def worker(name: str) -> None:
            handle(make_request(["x"], env={"USER": name}), run=watcher(name))

        _run_concurrently(worker, _ACTORS)

        assert not contaminated, (
            f"{len(contaminated)} requests observed another request's $USER: "
            f"{contaminated[:3]}; audit actors would be misattributed"
        )

    def test_cwd_does_not_leak_between_concurrent_requests(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """``field to @relative/path`` resolves against the caller's cwd."""
        dirs = [str(tmp_path_factory.mktemp(name)) for name in _ACTORS]
        contaminated: list[tuple[str, str]] = []
        lock = threading.Lock()

        def watcher(expected: str) -> Verb:
            def run(argv: Sequence[str]) -> None:
                del argv
                for _ in range(_POLL_ITERATIONS):
                    seen = str(cwd())
                    if seen != expected:
                        with lock:
                            contaminated.append((expected, seen))
                        return
                    time.sleep(_POLL_INTERVAL_SEC)

            return run

        def worker(path: str) -> None:
            handle(make_request(["x"], cwd=path), run=watcher(path))

        _run_concurrently(worker, dirs)

        assert not contaminated, (
            f"{len(contaminated)} requests observed another request's cwd: "
            f"{contaminated[:3]}; '@path' values would read the wrong file"
        )

    def test_stdout_does_not_leak_between_concurrent_requests(self) -> None:
        """``echo`` writes to a process-wide stream unless it is bound per call."""
        started = threading.Barrier(len(_ACTORS))
        results: dict[str, str] = {}
        lock = threading.Lock()

        def writer(name: str) -> Verb:
            def run(argv: Sequence[str]) -> None:
                del argv
                started.wait(timeout=5)
                for _ in range(20):
                    echo(name, nl=False)
                    time.sleep(_POLL_INTERVAL_SEC)

            return run

        def worker(name: str) -> None:
            response = handle(make_request(["x"]), run=writer(name))
            with lock:
                results[name] = response.stdout

        _run_concurrently(worker, _ACTORS)

        for name, captured in results.items():
            assert captured == name * 20, (
                f"{name}'s response carried another request's output: {captured[:60]!r}"
            )


def _run_concurrently(worker: Callable[[str], None], items: Sequence[str]) -> None:
    """Run ``worker`` over ``items`` in parallel threads and join them all."""
    threads = [threading.Thread(target=worker, args=(item,)) for item in items]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def _current_user() -> str:
    return env("USER") or ""


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
