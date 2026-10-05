"""Wire-contract tests for the traxd request/response framing."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast

import inspect
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.daemon.protocol import (
    PROTOCOL_VERSION,
    ProtocolVersionError,
    Request,
    Response,
    daemon_source_version,
    package_root,
    read_frame,
    socket_address,
    socket_path,
    source_version,
    write_frame,
)


if TYPE_CHECKING:
    from collections.abc import Callable


_CWD: Final = Path(__file__).resolve().parent


class _FragmentedSocket:
    """Socket-shaped reader that deterministically limits every ``recv``."""

    def __init__(self, framed: bytes, *, chunk_size: int) -> None:
        self._framed = framed
        self._chunk_size = chunk_size
        self.read_calls = 0

    def recv(self, size: int) -> bytes:
        self.read_calls += 1
        take = min(size, self._chunk_size, len(self._framed))
        chunk = self._framed[:take]
        self._framed = self._framed[take:]
        return chunk


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
        framed = len(payload).to_bytes(4, "big") + payload
        fragmented = _FragmentedSocket(framed, chunk_size=31)

        result = read_frame(cast(socket.socket, fragmented))

        assert Response.from_json(result).stdout == "x" * 200_000
        assert fragmented.read_calls > 2

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
    def test_lives_under_the_user_state_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The socket is per-user session state, not scratch or config."""
        monkeypatch.setenv("XDG_STATE_HOME", "/s")
        path = socket_path()
        assert "rekursiv-ai" in path.parts
        assert "traxd" in path.parts

    def test_is_stable_across_calls(self) -> None:
        assert socket_path() == socket_path()

    def test_resolves_through_the_shared_userdirs_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-deriving the XDG layout gets the wrong answer off Linux.

        ``state_dir`` resolves under ``Library/Application Support`` on macOS
        and ``LOCALAPPDATA`` on Windows; a hand-rolled ``~/.local/state``
        silently puts the socket somewhere else on those platforms.
        """
        monkeypatch.setenv("XDG_STATE_HOME", "/s")
        assert socket_path().parent == state_dir() / "rekursiv-ai" / "traxd"

    def test_differs_when_the_config_directory_differs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
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

    def test_a_long_state_directory_still_produces_a_bindable_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """AF_UNIX limits the encoded address even when the filesystem does not."""
        state_root = tmp_path / ("long-state-segment-" * 12)
        logical_parent = state_root / "rekursiv-ai" / "traxd"
        logical_parent.mkdir(parents=True)
        monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
        path = socket_path()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        try:
            listener.bind(str(path))
        finally:
            listener.close()
            path.unlink(missing_ok=True)

        assert path.resolve().parent == logical_parent.resolve()

    def test_a_long_relative_override_targets_the_callers_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A relative override must not become relative to the alias directory."""
        monkeypatch.chdir(tmp_path)
        logical_path = Path("long-relative-segment-" * 8) / "traxd.sock"
        logical_path.parent.mkdir()
        path = socket_address(logical_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        try:
            listener.bind(str(path))
        finally:
            listener.close()
            path.unlink(missing_ok=True)

        assert path.resolve().parent == logical_path.parent.resolve()

    def test_rejects_a_runtime_root_with_group_access(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A shared alias root would let another local user replace the socket."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(tempfile, "tempdir", "runtime")
        alias_root = Path(tempfile.gettempdir()) / f"t-{os.getuid():x}"
        alias_root.mkdir(parents=True)
        alias_root.chmod(0o750)

        with pytest.raises(PermissionError, match="unsafe traxd socket alias root"):
            socket_address(Path("long-logical-segment-" * 8) / "traxd.sock")


class TestSourceVersionCoverage:
    def test_covers_every_package_the_daemon_serves(self, tmp_path: Path) -> None:
        """A daemon holds ``client/`` and ``wire/`` resident too, not just ``trax/``.

        Fingerprinting only the CLI package leaves a daemon serving stale
        behavior after an edit to the HTTP client or a wire contract -- output
        that looks correct and is not.
        """
        modules = [
            tmp_path / package / "mod.py"
            for package in ("trax", "client", "wire", "types")
        ]
        for module in modules:
            module.parent.mkdir()
            module.write_text("x = 1\n")
        # Model filesystems with coarse timestamp resolution: modules written
        # together share one tick, and a rewrite within it keeps that mtime.
        tick_ns = modules[0].stat().st_mtime_ns
        for module in modules:
            os.utime(module, ns=(tick_ns, tick_ns))
        client_source = tmp_path / "client" / "mod.py"
        before = source_version(tmp_path)

        client_source.write_text("x = 2\n")
        os.utime(client_source, ns=(tick_ns, tick_ns))

        assert source_version(tmp_path) != before, (
            "an edit under client/ left the fingerprint unchanged; a running "
            "daemon would keep serving the old code"
        )

    def test_leaves_out_tests_the_daemon_never_imports(self, tmp_path: Path) -> None:
        """Editing a test must not restart a daemon that never imported it.

        Test modules were nearly half the files and more than half the bytes
        the fingerprint covered, and the daemon loads none of them.
        """
        (tmp_path / "mod.py").write_text("x = 1\n")
        before = source_version(tmp_path)

        (tmp_path / "mod_test.py").write_text("def test_x() -> None: ...\n")
        (tmp_path / "conftest.py").write_text("import pytest\n")

        assert source_version(tmp_path) == before

    def test_the_daemon_fingerprint_counts_what_it_lists(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each module it reads beside the root, and ``userdirs``, must move it.

        The fast half of the drift guard below: a listed path naming no file
        is skipped without a word, and ``userdirs`` -- which this module
        imports -- must count in either layout. Names are matched exactly,
        since a case-insensitive filesystem would stat a miscased one.
        """
        stated: list[str] = []
        with monkeypatch.context() as patch:
            patch.setattr(os, "stat", partial(_recorded_stat, os.stat, stated=stated))
            before = daemon_source_version()
        listings = {
            directory: {entry.name for entry in directory.glob("*")}
            for directory in {Path(path).parent for path in stated}
        }
        unnamed = [
            path
            for path in stated
            if Path(path).name not in listings[Path(path).parent]
        ]
        inside = f"{package_root()}{os.sep}"
        beside = {
            os.path.normpath(path)
            for path in stated
            if not os.path.normpath(path).startswith(inside)
        }
        unmoved: list[str] = []
        for path in sorted(beside | {os.path.realpath(inspect.getfile(state_dir))}):
            with monkeypatch.context() as patch:
                patch.setattr(os, "stat", partial(_grown_stat, os.stat, grown=path))
                if daemon_source_version() == before:
                    unmoved.append(path)

        assert not unnamed, (
            f"the fingerprint lists modules that do not exist: {unnamed}"
        )
        assert not unmoved, f"growing these left the fingerprint unchanged: {unmoved}"

    def test_covers_modules_outside_the_root(self, tmp_path: Path) -> None:
        """A module imported from beside the package root is source too.

        The newest tick is taken across both, so a same-size rewrite of the
        outside module within its tick still shows.
        """
        root = tmp_path / "pkg"
        _write_module(root / "mod.py", "x = 1\n", mtime_ns=2_000_000_000)
        shared = tmp_path / "shared.py"
        _write_module(shared, "y = 1\n", mtime_ns=4_000_000_000)
        before = source_version(root, outside=("../shared.py",))

        _write_module(shared, "y = 2\n", mtime_ns=4_000_000_000)

        assert source_version(root, outside=("../shared.py",)) != before

    @pytest.mark.cli_python_subprocess
    def test_covers_every_module_the_daemon_imports(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every first-party module the daemon imports must move its fingerprint.

        One the fingerprint misses can change under a running daemon, which
        then serves the old code under a matching version. Modules outside the
        package root are listed by hand, so this catches the next import that
        lands outside the list: growing each imported file by a byte must
        change the version.
        """
        server = "trackinizer.trax.daemon.server"
        probe = (
            "import os, sys;"
            f"import {server};"
            "print('\\n'.join(sorted(os.path.realpath(module.__file__)"
            " for name, module in list(sys.modules.items())"
            f" if name.split('.')[0] == {server.split('.', maxsplit=1)[0]!r}"
            " and getattr(module, '__file__', None))))"
        )
        result = subprocess.run(  # noqa: S603 -- fixed interpreter, literal probe.
            [sys.executable, "-c", probe],
            cwd=_import_probe_cwd(_CWD, module=__name__),
            check=True,
            capture_output=True,
            text=True,
        )
        imported = result.stdout.split()
        before = daemon_source_version()
        missed: list[str] = []
        for path in imported:
            with monkeypatch.context() as patch:
                patch.setattr(os, "stat", partial(_grown_stat, os.stat, grown=path))
                if daemon_source_version() == before:
                    missed.append(path)

        assert imported, "the probe found no first-party module"
        assert not missed, (
            f"the fingerprint misses modules the daemon imports: {missed}"
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
        source = tmp_path / "a.py"
        source.write_text("x = 1\n")
        mtime_ns = source.stat().st_mtime_ns
        before = source_version(tmp_path)
        source.write_text("x = 2\n")
        # Preserve mtime to model filesystems with coarse timestamp resolution.
        os.utime(source, ns=(mtime_ns, mtime_ns))
        assert source_version(tmp_path) != before

    def test_is_stable_when_nothing_changes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An untouched tree keeps its version, however recently it was written.

        A window measured back from the clock would read a fresh module on one
        call and only stat it an hour later, and the changed value would
        restart a daemon that is serving current code.
        """
        _write_module(tmp_path / "old.py", "x = 1\n", mtime_ns=2_000_000_000)
        (tmp_path / "new.py").write_text("y = 1\n")
        before = source_version(tmp_path)
        later_ns = time.time_ns() + 3_600_000_000_000
        monkeypatch.setattr(time, "time_ns", lambda: later_ns)
        monkeypatch.setattr(time, "time", lambda: later_ns / 1e9)

        assert source_version(tmp_path) == before

    def test_reads_only_modules_in_the_newest_tick(self, tmp_path: Path) -> None:
        """A module older than the newest tick is fingerprinted by its stat alone.

        Every write after a fingerprint lands in the newest tick or a later
        one, so an older module cannot change without its mtime moving; only
        restoring an old mtime by hand gets past this. Reading just the newest
        tick keeps the cost near one ``stat`` per module.
        """
        old = tmp_path / "old.py"
        _write_module(old, "x = 1\n", mtime_ns=2_000_000_000)
        (tmp_path / "new.py").write_text("y = 1\n")
        before = source_version(tmp_path)

        _write_module(old, "x = 2\n", mtime_ns=2_000_000_000)

        assert source_version(tmp_path) == before

    def test_pins_the_fingerprint_of_a_fixed_tree(self, tmp_path: Path) -> None:
        """Pin which bytes the fingerprint covers, so any change to them shows.

        Both ends compute the value with one copy of this module, so it is no
        wire contract. Pinning it catches what still yields a plausible
        digest: a dropped field, a read of an older module, a test let back in.
        """
        root = tmp_path / "pkg"
        _write_module(root / "a.py", "x = 1\n", mtime_ns=2_000_000_000)
        _write_module(root / "sub" / "b.py", "y = 2\n", mtime_ns=4_000_000_000)
        _write_module(root / "sub" / "b_test.py", "", mtime_ns=6_000_000_000)
        _write_module(root / "conftest.py", "", mtime_ns=6_000_000_000)
        _write_module(tmp_path / "lib" / "c.py", "z = 3\n", mtime_ns=5_000_000_000)

        assert (
            source_version(root, outside=("../lib/c.py",))
            == "66aaaa21a3f8174fcd4ee66fc314b957"
        )

    def test_an_empty_tree_has_a_version(self, tmp_path: Path) -> None:
        """A root holding no modules has no newest mtime, and still fingerprints."""
        assert source_version(tmp_path) == "cae66941d9efbd404e4d88758ea67670"

    def test_skips_a_module_deleted_after_the_listing(self, tmp_path: Path) -> None:
        """A module deleted between the walk and its ``stat`` must not fail the call.

        A dangling symlink is listed and then fails its ``stat``, as a module
        removed mid-walk by a branch switch would.
        """
        (tmp_path / "a.py").symlink_to(tmp_path / "missing.py")
        (tmp_path / "b.py").write_text("x = 1\n")
        with_vanished = source_version(tmp_path)

        (tmp_path / "a.py").unlink()

        assert source_version(tmp_path) == with_vanished

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
    def test_an_unreadable_module_counts_by_its_stat(self, tmp_path: Path) -> None:
        """A module locked or deleted between its ``stat`` and its read is not read.

        Its stat alone stands in -- which, for an empty module, is all its
        readable form contributes too.
        """
        source = tmp_path / "a.py"
        source.write_bytes(b"")
        readable = source_version(tmp_path)
        source.chmod(0o000)
        try:
            assert source_version(tmp_path) == readable
        finally:
            source.chmod(0o644)


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
            # The exported layout, which has no top-level ``loop`` package.
            "trackinizer.trax.daemon.protocol_test",
        ],
    )
    @pytest.mark.parametrize("instrumented", [False, True])
    def test_probe_runs_from_the_import_root(
        self,
        tmp_path: Path,
        module: str,
        instrumented: bool,
    ) -> None:
        """The probe imports from the checkout root, in either layout.

        Mutmut copies the tree under ``mutants/``; probing from there would
        import the instrumented package instead of the one under test.
        """
        tree = tmp_path / "mutants" if instrumented else tmp_path
        test_directory = tree.joinpath(*module.split(".")[:-1])

        assert _import_probe_cwd(test_directory, module=module) == tmp_path

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
            "('httpx2', 'pydantic', 'wrapt', 'dataclasses')"
            " if m in sys.modules)))"
        )
        result = subprocess.run(  # noqa: S603 -- fixed interpreter, literal probe.
            [sys.executable, "-c", probe],
            cwd=_import_probe_cwd(_CWD, module=__name__),
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
            cwd=_import_probe_cwd(_CWD, module=__name__),
            check=True,
            capture_output=True,
            text=True,
        )

        assert result.stdout.strip() == "", (
            f"thin client pulled in {result.stdout.strip()}; delegating only "
            "pays while the client path avoids the CLI's import graph"
        )


# The depth comes from the dotted name, not a fixed count: the export drops the top-
# level ``loop`` package, so its import root is one directory nearer.
def _import_probe_cwd(test_directory: Path, *, module: str) -> Path:
    """Return the directory ``module`` imports from, outside mutmut's ``mutants/``."""
    import_root = test_directory.resolve().parents[module.count(".") - 1]
    return import_root.parent if import_root.name == "mutants" else import_root


def _recorded_stat(
    real_stat: Callable[[str], os.stat_result],
    path: str,
    *,
    stated: list[str],
) -> os.stat_result:
    """Stat ``path``, recording that it was asked for."""
    stated.append(path)
    return real_stat(path)


def _grown_stat(
    real_stat: Callable[[str], os.stat_result],
    path: str,
    *,
    grown: str,
) -> os.stat_result | SimpleNamespace:
    """Stat ``path``, reporting ``grown`` one byte larger than it is."""
    status = real_stat(path)
    if os.path.normpath(path) != grown:
        return status
    return SimpleNamespace(st_size=status.st_size + 1, st_mtime_ns=status.st_mtime_ns)


def _write_module(path: Path, text: str, *, mtime_ns: int) -> None:
    """Write a module stamped with a fixed modification time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, ns=(mtime_ns, mtime_ns))


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
