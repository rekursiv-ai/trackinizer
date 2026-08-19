"""Wire-contract tests for the traxd request/response framing."""

from __future__ import annotations

from pathlib import Path

import json
import socket
import subprocess
import sys

import pytest

from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.daemon.protocol import (
    PROTOCOL_VERSION,
    ProtocolVersionError,
    Request,
    Response,
    package_root,
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
        with pytest.raises(ProtocolVersionError, match="protocol version"):
            Request.from_json(raw.encode())

    def test_a_version_mismatch_is_distinguishable_from_other_garbage(self) -> None:
        """The daemon answers a version mismatch instead of dropping it.

        A dropped connection is indistinguishable from "no daemon", so the
        client would fall back in-process and the user would never learn the
        two ends disagree.
        """
        raw = json.dumps({"protocol_version": PROTOCOL_VERSION + 1, "argv": []})
        with pytest.raises(ProtocolVersionError):
            Request.from_json(raw.encode())

    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(ValueError, match="malformed request frame"):
            Request.from_json(b"{not json")

    def test_rejects_a_frame_missing_a_field(self) -> None:
        """A truncated frame must raise, not silently default."""
        raw = json.dumps({"protocol_version": PROTOCOL_VERSION, "argv": ["issue"]})
        with pytest.raises(ValueError, match="missing"):
            Request.from_json(raw.encode())


class TestResponsePayload:
    def test_rejects_a_frame_missing_the_exit_code(self) -> None:
        """Defaulting a missing exit code to 0 reports failure as success.

        That is the worst outcome this protocol can produce: a script
        branching on the status proceeds as though the command worked.
        """
        with pytest.raises(ValueError, match="missing 'exit_code'"):
            Response.from_json(b'{"stdout":"","stderr":"boom"}')

    def test_rejects_a_non_integer_exit_code(self) -> None:
        with pytest.raises(ValueError, match="expected an integer"):
            Response.from_json(b'{"stdout":"","stderr":"","exit_code":"0"}')


class TestSocketPath:
    def test_lives_under_the_user_state_dir(self) -> None:
        """The socket is per-user session state, not scratch or config."""
        path = socket_path()
        assert "rekursiv-ai" in path.parts
        assert "traxd" in path.parts

    def test_is_stable_across_calls(self) -> None:
        assert socket_path() == socket_path()

    def test_resolves_through_the_shared_userdirs_helper(self) -> None:
        """Re-deriving the XDG layout gets the wrong answer off Linux.

        ``state_dir`` resolves under ``Library/Application Support`` on macOS
        and ``LOCALAPPDATA`` on Windows; a hand-rolled ``~/.local/state``
        silently puts the socket somewhere else on those platforms.
        """
        assert socket_path().parent == state_dir() / "rekursiv-ai" / "traxd"

    def test_differs_when_the_config_directory_differs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A caller with another profile store must get another daemon.

        The daemon resolves the store from its OWN environment, so serving a
        caller whose ``XDG_CONFIG_HOME`` differs would answer from a store
        that caller never chose -- the wrong profiles, under the wrong token.
        Keying the socket on the config directory routes them apart instead.
        """
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "one"))
        first = socket_path()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "two"))

        assert socket_path() != first


class TestSourceVersionCoverage:
    def test_covers_every_package_the_daemon_serves(self, tmp_path: Path) -> None:
        """A daemon holds ``client/`` and ``wire/`` resident too, not just ``trax/``.

        Fingerprinting only the CLI package leaves a daemon serving stale
        behavior after an edit to the HTTP client or a wire contract -- output
        that looks correct and is not.
        """
        for package in ("trax", "client", "wire", "types"):
            (tmp_path / package).mkdir()
            (tmp_path / package / "mod.py").write_text("x = 1\n")
        before = source_version(tmp_path)

        (tmp_path / "client" / "mod.py").write_text("x = 2\n")

        assert source_version(tmp_path) != before, (
            "an edit under client/ left the fingerprint unchanged; a running "
            "daemon would keep serving the old code"
        )

    def test_the_fingerprint_root_spans_the_whole_distribution(self) -> None:
        """The root must be the package, not the CLI subpackage inside it."""
        root = package_root()

        assert (root / "trax").is_dir()
        assert (root / "client").is_dir()
        assert (root / "wire").is_dir()


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
        # ``dataclasses`` is here for cost, not layering: it pulls ``inspect``
        # -> ``ast`` + ``dis`` for 8.4ms, which is 12% of a 70ms ``trax`` spent
        # generating an ``__init__`` and ``__eq__`` this module writes by hand.
        # Re-adding the decorator would be invisible without this name.
        probe = (
            "import sys;"
            f"import {module};"
            "print(','.join(sorted(m for m in "
            "('httpx', 'pydantic', 'wrapt', 'dataclasses')"
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
