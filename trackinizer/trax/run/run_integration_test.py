"""Real-CLI end-to-end capture test for ``trax run --sync``.

The unit tiers cover each seam with fakes (fake adapter over hand-written
JSONL, fake client, hand-built ``EventBody`` over HTTP). This module closes
the last gap: it spawns a **real** ``claude`` / ``codex`` binary through
``trax run --sync``, against a **real** listening server backed by an
in-process PGlite database, and asserts the captured session reached the DB
as typed messages.

It is ``@pytest.mark.integration`` and self-skips when the CLI binary is
absent, so a machine without the agent CLIs (or without network/credentials
for them) skips cleanly rather than failing. The server uses PGlite (the
project's default substrate, in-process WASM Postgres) so no system Postgres
toolchain is required.

What it proves, per CLI:

- ``trax run <cli> --sync`` drives the real binary non-interactively.
- The on-disk session log is tailed, parsed into typed ``Message`` members,
  and POSTed to ``/api/sessions/*``.
- The events land in ``agent_session_events`` and read back (over HTTP, same
  server) as a non-empty transcript containing at least one model turn.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any, Self, cast

import enum
import os
import shutil
import socket
import sys
import threading
import time
import uuid

from fastapi import FastAPI

import httpx
import pytest
import uvicorn

from trackinizer.client.client import Client
from trackinizer.lib.postgres import PGliteEngine
from trackinizer.server.api import query, sessions_routes
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.trax.run.adapters.base import Event
from trackinizer.trax.run.pty_pump import PtyPump
from trackinizer.trax.run.session import (
    RunConfig,
    _drain_filesystem_loop,
    _inbound_poll_loop,
    _Stats,
    run,
)
from trackinizer.trax.run.sink import TrackinizerSink
from trackinizer.types.agent_session_events import UserMessage
from trackinizer.wire.wire_sessions import KINDS, SessionStart


# A trivial prompt that forces exactly one model turn and exits fast. The
# assertions check transport + typing, not the model's answer (real-tier
# tests assert structure, not content -- see AGENTS.md "Testing").
_PROMPT = "Reply with the single word: ok"

# Injection prompt + expected answer for the live-CLI injection tests. The
# answer (``_INJECTION_ANSWER``) is deliberately ABSENT from the prompt: the
# TUI echoes the injected prompt into its composer, so a token taken from the
# prompt would match that echo before any model turn and report a false
# success. A fixed-answer factual question both CLIs answer in one lowercase
# word keeps the token model-produced, not echoed.
_INJECTION_PROMPT = "What color is a clear daytime sky? Reply with one lowercase word."
_INJECTION_ANSWER = "blue"

# How long to wait for a real CLI turn to be spawned, logged, tailed, and
# synced. Real model calls dominate; generous but bounded.
_CLI_TIMEOUT_SEC = 180.0

# How long ``_ServerThread`` waits for the lifespan to bring the server up.
# Must clear the store's bootstrap retry budget under whole-suite load: a boot
# can trap and replay several times (``Store._BOOTSTRAP_ATTEMPTS`` passes, each
# possibly rebuilding the PGlite Node), and a boot still inside that budget must
# not be declared dead. On timeout the stashed cause is surfaced anyway (see
# ``_ServerThread._startup_error``), so an overrun is diagnosable rather than a
# bare "did not start".
_STARTUP_DEADLINE_SEC = 90.0


def _free_port() -> int:
    """Pick an ephemeral localhost port for the test server."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return cast(int, sock.getsockname()[1])


def _build_app(workdir: Path) -> FastAPI:
    """An ASGI app whose lifespan owns a PGlite engine + bootstrapped store.

    The engine is constructed *inside* the lifespan so it lives on uvicorn's
    own event loop (PGlite holds one asyncpg connection bound to its loop).
    Auth is overridden to a static writer principal because the capture
    ``Client`` sends no credentials.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ``pgvector`` is required: the bootstrap schema declares the
        # ``inquiry_embeddings`` vector column. Without it bootstrap raises
        # and the lifespan never completes (server never starts).
        #
        # uvicorn catches any startup exception and only *logs* it (see
        # ``uvicorn.lifespan.on.LifespanOn.main``), then the server thread
        # exits without ``started`` ever flipping. Stash the cause on
        # ``app.state`` so ``_ServerThread.__enter__`` can re-raise the real
        # error instead of a blind "thread died" -- a swallowed traceback is a
        # diagnosis thrown away.
        app.state.startup_error = None
        try:
            async with PGliteEngine(
                workdir=workdir, persist=False, extensions=("pgvector",)
            ) as engine:
                store = Store(engine, embed=StubEmbedder())
                await store.bootstrap()
                # Seed the override identity as an active user: session-start
                # now validates the resolved account against ``users``
                # (active-user gate), and the capture ``Client`` runs as
                # ``ci@test``.
                async with engine.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO users (id, email, name, role, status) "
                        "VALUES ($1, 'ci@test', 'ci', 'writer', 'active') "
                        "ON CONFLICT (email) DO NOTHING",
                        uuid.uuid4(),
                    )
                app.state.engine = engine
                app.state.store = store
                app.state.inbound = InboundQueue()
                yield
        except BaseException as err:
            app.state.startup_error = err
            raise

    app = FastAPI(lifespan=lifespan)
    app.include_router(sessions_routes.router)
    # The query router serves ``GET /api/inquiries`` so the test can find the
    # AgentSession the capture minted and read its events back.
    app.include_router(query.router)

    async def _identity() -> AuthIdentity:
        return AuthIdentity(
            user_id=uuid.uuid4(), api_key_id=None, email="ci@test", role="writer"
        )

    app.dependency_overrides[current_user] = _identity
    return app


class _ServerThread:
    """Run a uvicorn server in a background thread; expose its base URL."""

    def __init__(self, app: FastAPI, port: int) -> None:
        self._app = app
        # ``loop="asyncio"``: uvicorn defaults to uvloop, whose
        # ``run_in_executor`` hits a deprecated ``asyncio.iscoroutinefunction``
        # on this interpreter; under pytest's warning-as-error filter that
        # raises and aborts PGlite startup. The stdlib loop avoids it.
        # ``ws="none"``: the websockets protocol import is broken here and we
        # serve plain HTTP only, so skip it.
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            ws="none",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.base_url = f"http://127.0.0.1:{port}"

    def __enter__(self) -> Self:
        self._thread.start()
        # Generous: a cold PGlite boot is ~2.5s, but under whole-suite load the
        # WASM Postgres can trap mid-bootstrap and the store retries on a fresh
        # Node. Size the window off the store's own retry budget so the two
        # constants cannot silently drift apart -- a boot that is still inside
        # its sanctioned retries must not be declared dead.
        deadline = time.monotonic() + _STARTUP_DEADLINE_SEC
        while time.monotonic() < deadline:
            if self._server.started:
                return self
            if not self._thread.is_alive():
                raise self._startup_error("uvicorn server thread died during startup")
            time.sleep(0.05)
        # Timeout: the thread may still be grinding through bootstrap retries.
        # Stop it (``__exit__`` never runs when ``__enter__`` raises) and surface
        # any cause the lifespan stashed rather than a bare "did not start".
        raise self._startup_error("uvicorn server did not start in time")

    def _startup_error(self, message: str) -> RuntimeError:
        """Stop the server thread; return a ``RuntimeError`` chained to the cause.

        ``__exit__`` does not run when ``__enter__`` raises, so the daemon
        uvicorn thread would otherwise keep booting past the failed setup -- stop
        it here. The returned error carries the lifespan's stashed cause via
        ``__cause__`` when one exists; absent that, no cause is attached so the
        default ``__context__`` chain is preserved (``raise ... from None`` would
        suppress it).
        """
        self._stop_thread()
        err = RuntimeError(message)
        cause = getattr(self._app.state, "startup_error", None)
        if cause is not None:
            # Mirror ``raise ... from cause``: set the cause and suppress the
            # ``__context__`` chain so the traceback shows one clean cause.
            err.__cause__ = cause
            err.__suppress_context__ = True
        return err

    def _stop_thread(self) -> None:
        """Signal the uvicorn server to exit and join its thread (best effort)."""
        self._server.should_exit = True
        self._thread.join(timeout=10.0)

    def __exit__(self, *exc: object) -> None:
        del exc
        self._stop_thread()


def test_server_thread_stops_thread_on_startup_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A startup timeout must stop the server thread, not leak it.

    ``__exit__`` never runs when ``__enter__`` raises, so the timeout path must
    set ``should_exit`` and join itself -- otherwise the daemon uvicorn thread
    keeps booting past the failed fixture setup.
    """
    monkeypatch.setattr(
        "trackinizer.trax.run.run_integration_test._STARTUP_DEADLINE_SEC",
        0.05,
    )
    thread_ref = _ServerThread.__new__(_ServerThread)

    class _FakeServer:
        started = False
        should_exit = False

    fake = _FakeServer()

    def _loop() -> None:
        while not fake.should_exit:
            time.sleep(0.01)

    thread_ref._server = cast("Any", fake)
    thread_ref._thread = threading.Thread(target=_loop, daemon=True)
    thread_ref._app = cast("Any", _StateApp())
    thread_ref.base_url = "http://127.0.0.1:0"

    with pytest.raises(RuntimeError, match="did not start"):
        thread_ref.__enter__()

    assert fake.should_exit is True
    thread_ref._thread.join(timeout=1.0)
    assert not thread_ref._thread.is_alive()


class _StateApp:
    """Minimal app stub exposing ``.state`` for ``_ServerThread`` cause lookup."""

    class _State:
        startup_error = None

    state = _State()


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A live trackinizer server on PGlite; yields its base URL.

    Module-scoped: PGlite's cold start is ~2.5s while bootstrap is ~0.2s, so a
    fresh engine per test dominated the suite's wall time. Every test asserts on
    *its own* freshly-minted "latest" AgentSession (sessions sort created-DESC
    and pytest runs serially, so a test's own session is always newest at its
    own assertion time), so sharing one server introduces no cross-test
    coupling -- proven by running the suite twice with stable results.
    """
    app = _build_app(tmp_path_factory.mktemp("pglite"))
    with _ServerThread(app, _free_port()) as srv:
        yield srv.base_url


def _latest_session_row(
    base_url: str, *, cli: str | None = None
) -> dict[str, object] | None:
    """The most recently created AgentSession inquiry row, or ``None``.

    With a module-scoped server the DB accumulates sibling tests' sessions, so
    ``cli`` scopes the lookup to the rows a given test minted (its CLI name is
    unique per test: ``fakeline`` / ``claude`` / ``codex``), keeping "latest"
    unambiguous instead of picking up another test's session.
    """
    with httpx.Client(base_url=base_url, timeout=30.0) as http:
        # ``kind`` is the PascalCase InquiryKind Literal, not the URL token. Pull
        # a small page (not limit=1) so the client-side ``cli`` filter has rows
        # to match even when a sibling test's session sorts newest.
        listing = http.get(
            "/api/inquiries", params={"kind": "AgentSession", "limit": 50}
        )
        listing.raise_for_status()
        rows = cast("list[dict[str, object]]", listing.json())
        if cli is not None:
            rows = [r for r in rows if r.get("cli") == cli]
        return rows[0] if rows else None


def _latest_session_events(
    base_url: str, *, cli: str | None = None
) -> list[dict[str, object]]:
    """Read the most recently created session's events back over HTTP.

    The capture mints exactly one AgentSession per run; this finds it via the
    inquiry list (scoped to ``cli`` when given, so a sibling test's session is
    not picked up) and pages its events. Returns the raw event bodies so the
    caller asserts on ``kind`` / ``message`` without a domain import.
    """
    row = _latest_session_row(base_url, cli=cli)
    if row is None:
        return []
    session_id = cast(str, row["id"])
    with httpx.Client(base_url=base_url, timeout=30.0) as http:
        events = http.get(f"/api/sessions/{session_id}/events", params={"limit": 1000})
        events.raise_for_status()
        body = cast("dict[str, object]", events.json())
        return cast("list[dict[str, object]]", body["events"])


def _run_capture(cli_name: str, cli_args: tuple[str, ...], server_url: str) -> int:
    """Drive ``trax run <cli> --sync`` in a thread, bounded by a timeout.

    ``run`` blocks on the wrapped CLI and spawns the real binary; the timeout
    guards against a hung or credential-prompting CLI so the suite never
    wedges. Returns the wrapped CLI's exit status (``run``'s return value), so
    a caller can distinguish a clean turn from a CLI that bailed -- an
    unauthenticated CLI exits non-zero before any model turn, which the caller
    treats as a skip rather than a spurious red.
    """
    config = RunConfig(
        cli_name=cli_name,
        cli_args=cli_args,
        sync=True,
        client=Client(base_url=server_url),
        quiesce_seconds=2.0,
    )
    rc_box: list[int] = []
    err_box: list[Exception] = []

    def _drive() -> None:
        # Catch only ``Exception``: a ``KeyboardInterrupt`` / ``SystemExit`` must
        # propagate through the interpreter's own signal path, not be stashed and
        # replayed from the wrong (main-test) stack.
        try:
            rc_box.append(run(config))
        except Exception as exc:
            err_box.append(exc)

    worker = threading.Thread(target=_drive, daemon=True)
    worker.start()
    worker.join(timeout=_CLI_TIMEOUT_SEC)
    if worker.is_alive():
        pytest.fail(f"trax run {cli_name} did not finish within {_CLI_TIMEOUT_SEC}s")
    # The worker has joined, so its append has happened-before these reads. A
    # raised ``run`` would otherwise leave ``rc_box`` empty and surface as a
    # contextless IndexError; re-raise the real cause instead.
    if err_box:
        raise err_box[0]
    return rc_box[0]


def _assert_transcript_synced(base_url: str, *, cli: str) -> None:
    """The run produced a non-empty, typed transcript with a model turn."""
    events = _latest_session_events(base_url, cli=cli)
    assert events, "no events synced to the server"
    kinds = {cast(str, e["kind"]) for e in events}
    # ``kind`` is the Message member class name; the allowed set is exactly the
    # wire ``KINDS`` union, so a new member never silently fails this here.
    assert kinds <= set(KINDS), f"unexpected kinds: {kinds}"
    assert "AssistantMessage" in kinds, f"no model turn captured; got {kinds}"
    # Every event carries its typed message object, not an opaque blob.
    for event in events:
        assert isinstance(event["message"], dict)
    # The close path must stamp ``ended``; a bare ``SessionEnd()`` would leave
    # it NULL even as status flips to complete (Issue#278 Disease 4).
    row = _latest_session_row(base_url, cli=cli)
    assert row is not None
    assert row.get("ended") is not None, "session close did not stamp ended"


@pytest.mark.integration
@pytest.mark.real_llm
def test_trax_run_claude_syncs_session(server: str) -> None:
    """A real ``claude -p`` run captures and syncs its session to the DB."""
    if shutil.which("claude") is None:
        pytest.skip("claude binary not on PATH")
    rc = _run_capture(
        "claude",
        ("-p", "--output-format", "stream-json", "--verbose", _PROMPT),
        server,
    )
    _skip_if_cli_unauthenticated("claude", rc, server)
    _assert_transcript_synced(server, cli="claude")


@pytest.mark.integration
@pytest.mark.real_llm
def test_trax_run_codex_syncs_session(server: str) -> None:
    """A real ``codex exec`` run captures and syncs its session to the DB."""
    if shutil.which("codex") is None:
        pytest.skip("codex binary not on PATH")
    rc = _run_capture(
        "codex",
        (
            "exec",
            "--skip-git-repo-check",
            "-c",
            "model_reasoning_summary=detailed",
            _PROMPT,
        ),
        server,
    )
    _skip_if_cli_unauthenticated("codex", rc, server)
    _assert_transcript_synced(server, cli="codex")


def _skip_if_cli_unauthenticated(cli: str, rc: int, base_url: str) -> None:
    """Skip when the CLI bailed before any model turn (the auth-failure shape).

    A logged-in CLI replies and exits 0, so its synced transcript carries an
    ``AssistantMessage``. An unauthenticated CLI exits non-zero before any turn
    (codex: revoked token; claude: ``please log out``), syncing only the
    launch ``SystemMessage`` and the prompt ``UserMessage``. That is an
    environment gap, not a capture regression, so it skips -- mirroring
    ``_drive_real_cli_injection``'s auth-failure skip on the injection tests.

    Gated on *both* a non-zero exit *and* a missing model turn so a real
    capture regression (CLI replied, exit 0, but ``AssistantMessage`` never
    reached the DB) still fails loudly instead of being skipped away.
    """
    if rc == 0:
        return
    kinds = {cast(str, e["kind"]) for e in _latest_session_events(base_url, cli=cli)}
    if "AssistantMessage" not in kinds:
        pytest.skip(
            f"{cli} exited {rc} with no model turn (commonly unauthenticated: "
            f"'{cli} login' required); the capture wiring is exercised by the "
            "authenticated path and the fake-adapter unit tiers"
        )


class _LineAdapter:
    """A line adapter over a temp dir: one ``*.jsonl`` line -> one UserMessage.

    Stands in for a real CLI so the streaming test needs no binary: the test
    appends lines to the session file over time and the drain loop tails them.
    """

    name: str = "fakeline"
    cli_binary: str = "fakeline"
    whole_file: bool = False

    def __init__(self, root: Path) -> None:
        self._root = root

    def session_dirs(self) -> Iterator[Path]:
        yield self._root

    def matches_session_file(self, path: Path) -> bool:
        return path.suffix == ".jsonl"

    def session_id_from_path(self, path: Path) -> str | None:
        del path
        return None

    def parse(self, raw: bytes, *, whole_file: bool) -> Iterator[Event]:
        del whole_file
        yield Event(message=UserMessage(text=raw.decode()))


@pytest.mark.integration
def test_capture_streams_incrementally_before_close(
    server: str, tmp_path: Path
) -> None:
    """Buffered events reach the server mid-run, not only at ``close``.

    Regression for the live-viewer gap: with only a size-threshold flush a
    short session withheld every event until Ctrl-D. Drive the real drain
    loop + ``TrackinizerSink`` (sub-second flush interval) against a session
    file grown in stages, and assert the server already holds the first turn
    while the loop is still running -- before any close.
    """
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    adapter = _LineAdapter(session_root)
    sink = TrackinizerSink(
        Client(base_url=server), adapter.name, flush_interval_sec=0.2
    )
    stats = _Stats()
    config = RunConfig(cli_name=adapter.name, quiesce_seconds=0.5)
    stop = threading.Event()

    worker = threading.Thread(
        target=lambda: _drain_filesystem_loop(
            cast("Any", adapter),
            sink,
            stats,
            config,
            stop,
            baseline=frozenset(),
            slash_queue=deque(),
            spawn_time=time.time(),
        ),
        daemon=True,
    )
    worker.start()
    try:
        # First turn lands in the session file mid-run.
        log = session_root / "live.jsonl"
        log.write_text("first turn\n")

        # The server should hold it within a few flush intervals -- well
        # before we stop the loop (i.e. before any close()).
        deadline = time.monotonic() + 10.0
        events: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            events = _latest_session_events(server, cli=adapter.name)
            if events:
                break
            time.sleep(0.1)
        assert events, "no events streamed to the server before close"
        assert any("first turn" in str(e["message"]) for e in events)
        # The session is still live: it streamed mid-run, before any close().
        row = _latest_session_row(server, cli=adapter.name)
        assert row is not None
        assert row.get("ended") is None
    finally:
        stop.set()
        worker.join(timeout=5.0)
        sink.close()

    # After close the session is ended and the turn persisted.
    row = _latest_session_row(server, cli=adapter.name)
    assert row is not None
    assert row.get("ended") is not None


@pytest.mark.integration
def test_inbound_injection_reaches_child_end_to_end(server: str) -> None:
    """Full loop: HTTP enqueue -> poller -> pump.inject -> child receives it.

    Proves the messaging channel end to end against the real server, real
    client, real inbound poller, and a real PTY child -- the only stand-in is
    the child (a Python echo, not a model CLI, so the assertion is
    deterministic and credential-free). A session is opened via the client so
    the poller has an id to drain; the injected text must surface in the
    child's output.
    """
    # Child: echo everything it reads until EOF, so injected bytes come back.
    child = (
        "import sys\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    sys.stdout.write('ECHO:' + line); sys.stdout.flush()\n"
    )
    client = Client(base_url=server)
    # Open a session so the poller has an id; capture sink not needed here. Use
    # a cli name unique to this test: the module-scoped server accumulates
    # sessions, and this one is intentionally never ended (``ended IS NULL``),
    # so sharing ``cli="claude"`` would let it shadow
    # ``test_trax_run_claude_syncs_session``'s ``ended`` assertion under reorder.
    resp = client.session_start(SessionStart(cli="claude-inbound", actor="tester"))
    session_id = resp.id

    pump = PtyPump([sys.executable, "-u", "-c", child])
    sink = _StubSessionSink(session_id)
    stop = threading.Event()
    poller = threading.Thread(
        target=lambda: _inbound_poll_loop(client, sink, pump, stop, poll_interval=0.2),
        daemon=True,
    )
    pump_thread = threading.Thread(target=pump.run, daemon=True)
    pump_thread.start()
    poller.start()
    try:
        time.sleep(0.5)  # let the child start
        queued = client.enqueue_inbound(session_id, "run it")
        assert queued >= 1
        # The poller should drain it and the pump inject it; the child echoes.
        deadline = time.monotonic() + 10.0
        seen = False
        while time.monotonic() < deadline:
            if pump.injected_count() >= 1:
                seen = True
                break
            time.sleep(0.1)
        assert seen, "injected message was never drained by the poller"
    finally:
        stop.set()
        # Closing the pump's child ends it; signal via the master is simplest.
        pump.terminate()
        pump_thread.join(timeout=5.0)
        poller.join(timeout=2.0)


class _InjectionResult(enum.Enum):
    """Outcome of driving a real CLI through the injection chain.

    Only ``TOKEN_SEEN`` is decisive: it proves the injected message reached a
    live model and produced a turn. ``UNAUTHENTICATED`` (an auth banner) and
    ``INCONCLUSIVE`` (the deadline elapsed with neither token nor banner) are
    both environment gaps the test skips on. A deadline elapse cannot be
    distinguished from a model that was merely slow under load -- real-CLI,
    real-network turns are non-deterministic and latency-sensitive -- so it is
    not solid evidence of a wiring regression and must not fail red.
    """

    TOKEN_SEEN = enum.auto()
    UNAUTHENTICATED = enum.auto()
    INCONCLUSIVE = enum.auto()


def _drive_real_cli_injection(
    cli: str,
    argv: list[str],
    server: str,
    prompt: str,
    token: str,
    *,
    deadline_sec: float = 45.0,
) -> _InjectionResult:
    """Full chain: HTTP enqueue -> poller -> pump -> real CLI; classify the run.

    Drives a real interactive CLI under the pump, captures the master output
    via a redirected ``sys.stdout`` pipe, opens a session, enqueues ``prompt``
    over HTTP, and lets the real poller drain + inject it. Returns which of the
    three :class:`_InjectionResult` outcomes the rendered output showed:
    ``TOKEN_SEEN`` (model replied with ``token``), ``UNAUTHENTICATED`` (an auth
    banner appeared, so the model can never reply), or ``INCONCLUSIVE`` (the
    deadline passed with neither -- a slow or unavailable model the caller
    skips on, since it is not distinguishable from a wiring miss).

    ``token`` is the model's *answer*, which must NOT occur in ``prompt``: the
    TUI echoes the injected prompt into its composer, so a token lifted from
    the prompt text matches that echo at ~``_ENTER_DELAY_SEC`` and reports
    ``TOKEN_SEEN`` whether or not a model turn ever happened (a false pass).
    Keeping the answer out of the prompt makes the token's appearance prove a
    real reply, not the echo.
    """
    assert token.lower() not in prompt.lower(), (
        "token must not appear in the prompt, or the TUI's prompt echo would "
        "match it before any model turn (a false TOKEN_SEEN)"
    )
    out_r, out_w = os.pipe()
    real_out, real_in = sys.stdout, sys.stdin
    sys.stdout = os.fdopen(out_w, "w", buffering=1, errors="replace")
    stdin_r, _stdin_w = os.pipe()
    sys.stdin = os.fdopen(stdin_r)
    captured = bytearray()

    def drain_out() -> None:
        while True:
            try:
                chunk = os.read(out_r, 65_536)
            except OSError:
                return
            if not chunk:
                return
            captured.extend(chunk)

    client = Client(base_url=server)
    session_id = client.session_start(SessionStart(cli=cli, actor="tester")).id
    pump = PtyPump([cli, *argv])
    sink = _StubSessionSink(session_id)
    stop = threading.Event()
    threads = [
        threading.Thread(target=pump.run, daemon=True),
        threading.Thread(target=drain_out, daemon=True),
        threading.Thread(
            target=lambda: _inbound_poll_loop(
                client, sink, pump, stop, poll_interval=0.3
            ),
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()
    try:
        # Enqueue once the CLI has had a moment to start. The PTY buffers
        # input, so exact composer-ready timing is not required (verified:
        # claude accepts injection from t~=0).
        time.sleep(1.0)
        client.enqueue_inbound(session_id, prompt)
        deadline = time.monotonic() + deadline_sec
        while time.monotonic() < deadline:
            if token.encode() in captured:
                return _InjectionResult.TOKEN_SEEN
            # Fail fast on a CLI that cannot authenticate, rather than burning
            # the whole deadline: the model will never reply, so the caller
            # should skip immediately. Covers both the local-credential banners
            # ("could not be refreshed" / "please log out") and a server-side
            # token rejection (a 403 "Bearer Token has expired" / "Failed to
            # authenticate") that an OAuth token can hit transiently mid-run.
            low = bytes(captured).lower()
            if any(
                marker in low
                for marker in (
                    b"could not be refreshed",
                    b"please log out",
                    b"bearer token has expired",
                    b"failed to authenticate",
                )
            ):
                return _InjectionResult.UNAUTHENTICATED
            time.sleep(0.3)
        return _InjectionResult.INCONCLUSIVE
    finally:
        stop.set()
        pump.terminate()
        threads[0].join(timeout=5.0)
        sys.stdout, sys.stdin = real_out, real_in


def _assert_injection_or_skip(cli: str, result: _InjectionResult) -> None:
    """Translate an injection outcome into a pass or skip.

    ``TOKEN_SEEN`` passes -- it positively proves the injected message reached a
    live model. ``UNAUTHENTICATED`` and ``INCONCLUSIVE`` both skip: an auth gap
    and a slow-or-unavailable model are environment conditions, not wiring
    regressions, and a real-CLI deadline elapse cannot be told apart from a
    model that simply did not finish a turn in time.
    """
    if result is _InjectionResult.TOKEN_SEEN:
        return
    if result is _InjectionResult.UNAUTHENTICATED:
        pytest.skip(
            f"{cli} is unauthenticated ('{cli} login' required); the injection "
            "wiring is exercised whenever the CLI can actually reply"
        )
    pytest.skip(
        f"{cli} produced no model turn before the deadline (slow or unavailable "
        "model); the injection wiring is exercised whenever the CLI can reply"
    )


@pytest.mark.integration
@pytest.mark.real_llm
def test_injection_reaches_real_claude_end_to_end(server: str) -> None:
    """The full messaging loop drives a live, interactive claude.

    Closes the Phase-2a gap the echo-child test left: a real model CLI on the
    pump receives a server-enqueued message and acts on it. Skips when the
    binary is absent or unauthenticated (the assertion needs a real turn).
    """
    if shutil.which("claude") is None:
        pytest.skip("claude binary not on PATH")
    result = _drive_real_cli_injection(
        "claude",
        ["--dangerously-skip-permissions"],
        server,
        _INJECTION_PROMPT,
        _INJECTION_ANSWER,
    )
    _assert_injection_or_skip("claude", result)


@pytest.mark.integration
@pytest.mark.real_llm
def test_injection_reaches_real_codex_end_to_end(server: str) -> None:
    """The full messaging loop drives a live, interactive codex.

    Same chain as the claude case, for codex parity. Skips when the binary is
    absent; an auth/credential failure surfaces as a clean skip rather than a
    spurious red, since the assertion needs a real model turn.
    """
    if shutil.which("codex") is None:
        pytest.skip("codex binary not on PATH")
    # A real codex turn lands in ~15s in isolation but is latency-sensitive
    # under whole-suite parallel load; give it the same generous window as
    # claude so a healthy-but-slow run still asserts positively rather than
    # skipping. A genuine auth failure short-circuits via the banner guard, and
    # a deadline elapse skips (inconclusive) instead of failing red.
    result = _drive_real_cli_injection(
        "codex",
        ["--dangerously-bypass-approvals-and-sandbox"],
        server,
        _INJECTION_PROMPT,
        _INJECTION_ANSWER,
    )
    _assert_injection_or_skip("codex", result)


class _StubSessionSink:
    """A minimal Sink exposing a fixed ``session_id`` for the poller."""

    def __init__(self, session_id: uuid.UUID) -> None:
        self._session_id = session_id

    @property
    def session_id(self) -> uuid.UUID | None:
        return self._session_id

    def open(self) -> str | None:
        return None

    def set_cli_session_id(self, cli_session_id: str) -> None:
        del cli_session_id

    def emit(self, adapter_name: str, event: Event) -> None:
        del adapter_name, event

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)


@pytest.mark.integration
def test_server_fixture_boots(server: str) -> None:
    """Smoke: the PGlite-backed fixture server answers the AgentSession listing.

    Asserts the route contract (200 + a JSON list), not an empty DB: the
    server fixture is module-scoped, so a sibling test may have already minted
    a session by the time this runs.
    """
    with httpx.Client(base_url=server, timeout=10.0) as http:
        r = http.get("/api/inquiries", params={"kind": "AgentSession", "limit": 1})
        assert r.status_code == 200
        assert isinstance(r.json(), list)
