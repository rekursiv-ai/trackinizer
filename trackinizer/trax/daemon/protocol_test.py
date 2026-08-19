"""Wire-contract tests for the traxd request/response framing."""

from __future__ import annotations

from pathlib import Path

import json
import socket
import subprocess
import sys

import pytest

from trackinizer.trax.daemon.protocol import (
    PROTOCOL_VERSION,
    Request,
    Response,
    read_frame,
    socket_path,
    source_version,
    write_frame,
)


class TestFraming:
    """A frame must survive a stream that delivers it in arbitrary pieces."""

    def test_round_trips_a_request(self) -> None:
        request = Request(
            argv=("issue", "status", "is", "active"),
            cwd="/home/agent",
            env={"USER": "agent"},
            isatty=True,
            columns=120,
            stdin="",
            protocol_version=PROTOCOL_VERSION,
            source_version="abc123",
        )
        left, right = socket.socketpair()
        try:
            write_frame(left, request.to_json())
            assert Request.from_json(read_frame(right)) == request
        finally:
            left.close()
            right.close()

    def test_round_trips_a_response(self) -> None:
        response = Response(stdout="rows\n", stderr="", exit_code=0)
        left, right = socket.socketpair()
        try:
            write_frame(left, response.to_json())
            assert Response.from_json(read_frame(right)) == response
        finally:
            left.close()
            right.close()

    def test_reassembles_a_frame_split_across_reads(self) -> None:
        """A length-prefixed frame must not assume one ``recv`` per message.

        A 167KB issue listing spans many TCP segments; a reader that treats
        one ``recv`` as one frame truncates it and the CLI prints a partial
        table with no error.
        """
        payload = Response(stdout="x" * 200_000, stderr="", exit_code=0).to_json()
        left, right = socket.socketpair()
        try:
            write_frame(left, payload)
            left.shutdown(socket.SHUT_WR)
            assert Response.from_json(read_frame(right)).stdout == "x" * 200_000
        finally:
            left.close()
            right.close()

    def test_raises_on_truncated_frame(self) -> None:
        """A peer that dies mid-frame must raise, not yield a short read."""
        left, right = socket.socketpair()
        try:
            left.sendall((1024).to_bytes(4, "big") + b"partial")
            left.shutdown(socket.SHUT_WR)
            with pytest.raises(ConnectionError):
                read_frame(right)
        finally:
            left.close()
            right.close()

    def test_rejects_an_oversized_frame_without_allocating(self) -> None:
        """A bogus length prefix must be refused, not used to size a buffer."""
        left, right = socket.socketpair()
        try:
            left.sendall((2**31).to_bytes(4, "big"))
            with pytest.raises(ValueError, match="frame too large"):
                read_frame(right)
        finally:
            left.close()
            right.close()


class TestRequestPayload:
    def test_rejects_unknown_protocol_version(self) -> None:
        raw = json.dumps({"protocol_version": PROTOCOL_VERSION + 1, "argv": []})
        with pytest.raises(ValueError, match="protocol version"):
            Request.from_json(raw.encode())

    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(ValueError, match="malformed request frame"):
            Request.from_json(b"{not json")


class TestSocketPath:
    def test_lives_under_the_user_state_dir(self) -> None:
        """The socket is per-user session state, not scratch or config."""
        path = socket_path()
        assert "rekursiv-ai" in path.parts
        assert "traxd" in path.parts

    def test_is_stable_across_calls(self) -> None:
        assert socket_path() == socket_path()


class TestSourceVersion:
    def test_changes_when_a_source_file_changes(self, tmp_path: Path) -> None:
        """A daemon serving stale code is the sharpest footgun here.

        The monorepo is edited constantly, so the client must be able to tell
        that the running daemon predates the source it was launched from --
        without importing anything to find out.
        """
        (tmp_path / "a.py").write_text("x = 1\n")
        before = source_version(tmp_path)
        (tmp_path / "a.py").write_text("x = 2\n")
        assert source_version(tmp_path) != before

    def test_is_stable_when_nothing_changes(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n")
        assert source_version(tmp_path) == source_version(tmp_path)


class TestImportPurity:
    """The thin client's whole value is importing nothing expensive.

    Pulling in ``client.client`` -- directly or through any sibling -- costs
    the ~190ms the daemon exists to remove, and it would do so SILENTLY: the
    CLI would still be correct, just as slow as before the daemon existed.
    Asserted behaviorally against a real import in a fresh interpreter, since
    the cost comes from the transitive graph, not the import statements this
    file happens to spell.
    """

    @pytest.mark.parametrize(
        "module",
        [
            "trackinizer.trax.daemon.protocol",
            "trackinizer.trax.daemon.client",
        ],
    )
    def test_import_pulls_in_nothing_expensive(self, module: str) -> None:
        probe = (
            "import sys;"
            f"import {module};"
            "print(','.join(sorted(m for m in ('httpx', 'pydantic', 'wrapt')"
            " if m in sys.modules)))"
        )
        result = subprocess.run(  # noqa: S603 -- fixed interpreter, literal probe.
            [sys.executable, "-c", probe],
            check=True,
            capture_output=True,
            text=True,
        )

        assert result.stdout.strip() == "", (
            f"{module} transitively imports {result.stdout.strip()}; the thin "
            "client must not pay the cost the daemon exists to avoid"
        )

    def test_import_pulls_in_nothing_from_the_cli_graph(self) -> None:
        """No timing here on purpose.

        The property that matters is WHICH modules load, not how many
        milliseconds they take: import time swings several fold with the CPU
        governor, page-cache warmth, and parallel-test load, so a wall-clock
        budget fails on a busy machine while the module set it stands in for
        is unchanged. Naming the modules asserts the same thing
        deterministically.
        """
        probe = (
            "import sys;"
            "import trackinizer.trax.daemon.client;"
            "print(','.join(sorted(m for m in sys.modules"
            " if m.startswith('trackinizer.trax.cli')"
            " or m.startswith('trackinizer.client.client')"
            " or m.startswith('trackinizer.wire'))))"
        )
        result = subprocess.run(  # noqa: S603 -- fixed interpreter, literal probe.
            [sys.executable, "-c", probe],
            check=True,
            capture_output=True,
            text=True,
        )

        assert result.stdout.strip() == "", (
            f"thin client pulled in {result.stdout.strip()}; delegating only "
            "pays while the client path avoids the CLI's import graph"
        )


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
