"""Behavior tests for the thin connect-or-spawn daemon client."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import contextlib
import socket
import threading

import pytest

from trackinizer.trax.daemon.client import (
    DaemonRequestLostError,
    delegate,
    should_delegate,
)
from trackinizer.trax.daemon.protocol import (
    PROTOCOL_VERSION,
    Request,
    Response,
    read_frame,
    write_frame,
)


@contextlib.contextmanager
def serving(path: Path, *, version: str = "v1") -> Generator[list[Request]]:
    """Run a one-shot echo daemon on ``path``, recording requests it sees."""
    seen: list[Request] = []
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                try:
                    request = Request.from_json(read_frame(conn))
                except (ConnectionError, ValueError):
                    continue
                seen.append(request)
                reply = Response(
                    stdout=f"served {' '.join(request.argv)}",
                    stderr="",
                    exit_code=0,
                )
                if request.source_version != version:
                    reply = Response(stdout="", stderr="stale", exit_code=75)
                write_frame(conn, reply.to_json())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield seen
    finally:
        listener.close()


class TestShouldDelegate:
    """Some verbs must never leave this process."""

    def test_delegates_an_ordinary_list(self) -> None:
        assert should_delegate(["issue", "status", "is", "active"])

    def test_refuses_trax_run(self) -> None:
        """``trax run`` owns a PTY and the terminal; it cannot run remotely."""
        assert not should_delegate(["run", "claude"])

    def test_refuses_a_stdin_sentinel(self) -> None:
        """``field to -`` reads this process's stdin, which the daemon lacks."""
        assert not should_delegate(["issue", "7", "description", "to", "-"])

    def test_refuses_the_serve_flag(self) -> None:
        assert not should_delegate(["--__serve"])

    def test_delegates_a_row_whose_value_is_the_word_run(self) -> None:
        """``run`` is the PTY verb only in verb position, never as a value.

        Scanning every token instead of the verb makes any row mentioning
        "run" -- an experiment title, a label -- silently forfeit the daemon.
        """
        assert should_delegate(["issue", "title", "to", "run"])

    def test_delegates_a_row_whose_value_is_a_bare_dash(self) -> None:
        """A ``-`` after ``to`` is the stdin sentinel; elsewhere it is a value."""
        assert should_delegate(["issue", "label", "add", "-"])

    def test_still_refuses_the_stdin_sentinel_after_to(self) -> None:
        assert not should_delegate(["issue", "7", "title", "to", "-"])

    def test_refuses_run_in_verb_position_after_global_flags(self) -> None:
        """Global flags precede the verb, so the scan cannot just read argv[0]."""
        assert not should_delegate(["--profile", "origin", "run", "claude"])


class TestDelegate:
    def test_returns_the_daemon_response(self, tmp_path: Path) -> None:
        sock = tmp_path / "traxd.sock"
        with serving(sock) as seen:
            response = delegate(["issue"], socket_override=sock, source_version="v1")

        assert response is not None
        assert response.stdout == "served issue"
        assert response.exit_code == 0
        assert seen[0].argv == ("issue",)

    def test_sends_terminal_geometry(self, tmp_path: Path) -> None:
        """The daemon's stdout is a socket, so it cannot detect the terminal.

        Without the client's ``isatty``/``columns``, ``render`` would size
        every table as if piped and print unbounded-width rows.
        """
        sock = tmp_path / "traxd.sock"
        with serving(sock) as seen:
            delegate(["issue"], socket_override=sock, source_version="v1")

        assert seen[0].isatty in (True, False)
        assert seen[0].columns >= 0

    def test_sends_ambient_environment(self, tmp_path: Path) -> None:
        """The daemon must not resolve identity from its OWN environment."""
        sock = tmp_path / "traxd.sock"
        with serving(sock) as seen:
            delegate(["issue"], socket_override=sock, source_version="v1")

        assert set(seen[0].env) <= {
            "USER",
            "USERNAME",
            "TRACKINIZER_PROFILE",
            "TRACKINIZER_URL",
        }

    def test_sends_cwd_for_relative_path_values(self, tmp_path: Path) -> None:
        """``field to @rel/path`` resolves against the CALLER's directory."""
        sock = tmp_path / "traxd.sock"
        with serving(sock) as seen:
            delegate(["issue"], socket_override=sock, source_version="v1")

        assert Path(seen[0].cwd).is_absolute()

    def test_returns_none_when_no_daemon_listens(self, tmp_path: Path) -> None:
        """A missing daemon is not an error: the caller falls back in-process."""
        assert (
            delegate(
                ["issue"],
                socket_override=tmp_path / "absent.sock",
                source_version="v1",
                spawn=False,
            )
            is None
        )

    def test_returns_none_on_a_stale_socket_file(self, tmp_path: Path) -> None:
        """A socket file left by a killed daemon must not hang the CLI."""
        sock = tmp_path / "traxd.sock"
        sock.write_bytes(b"")
        assert (
            delegate(["issue"], socket_override=sock, source_version="v1", spawn=False)
            is None
        )

    def test_raises_when_a_delivered_request_loses_its_response(
        self, tmp_path: Path
    ) -> None:
        """A request that reached the daemon must never fall back in-process.

        The daemon runs the verb BEFORE it replies, so a connection lost
        after the request was written may have already applied a write. The
        in-process fallback would then re-run it under a fresh idempotency
        key and duplicate the effect -- a second edit, a second edge, a
        second cost delta. Losing the response is a failure to report, not a
        licence to retry.
        """
        sock = tmp_path / "traxd.sock"
        received: list[Request] = []
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(sock))
        listener.listen(1)

        def serve_then_die() -> None:
            conn, _ = listener.accept()
            with conn:
                received.append(Request.from_json(read_frame(conn)))
                # Reply never sent: the daemon "crashed" after running the verb.

        thread = threading.Thread(target=serve_then_die, daemon=True)
        thread.start()
        try:
            with pytest.raises(DaemonRequestLostError):
                delegate(
                    ["issue", "7", "status", "to", "complete"],
                    socket_override=sock,
                    source_version="v1",
                    spawn=False,
                )
        finally:
            thread.join(timeout=5)
            listener.close()

        assert received, "the daemon received the request before the connection died"

    def test_falls_back_when_the_connection_never_opened(self, tmp_path: Path) -> None:
        """Nothing was delivered, so re-running locally cannot duplicate work."""
        assert (
            delegate(
                ["issue", "7", "status", "to", "complete"],
                socket_override=tmp_path / "absent.sock",
                source_version="v1",
                spawn=False,
            )
            is None
        )

    def test_returns_none_when_the_daemon_reports_stale_code(
        self, tmp_path: Path
    ) -> None:
        """A daemon running older source must not serve this request.

        The worktree is edited constantly; a daemon that keeps answering with
        last hour's code produces wrong output that looks correct.
        """
        sock = tmp_path / "traxd.sock"
        with serving(sock, version="v1"):
            assert (
                delegate(
                    ["issue"], socket_override=sock, source_version="v2", spawn=False
                )
                is None
            )

    def test_carries_the_protocol_version(self, tmp_path: Path) -> None:
        sock = tmp_path / "traxd.sock"
        with serving(sock) as seen:
            delegate(["issue"], socket_override=sock, source_version="v1")

        assert seen[0].protocol_version == PROTOCOL_VERSION


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
