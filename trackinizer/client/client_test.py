"""Tests for trackinizer CLI HTTP client."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast, override
from urllib.parse import parse_qsl

import argparse
import inspect
import json
import logging
import uuid

from treekle import PlainTree, from_plain, json_loads

import httpx2
import pytest

from trackinizer.client.client import (
    ArtifactRevision,
    Client,
    EdgeWrite,
    server_url,
)
from trackinizer.client.errors import ClientError
from trackinizer.trax import connection, profile
from trackinizer.trax.conftest import FakeClient
from trackinizer.trax.grammar import parse_kind, parse_ref
from trackinizer.trax.profile import Profile
from trackinizer.wire.filters import Filter
from trackinizer.wire.refs import SeqRef, UuidRef
from trackinizer.wire.routes import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from trackinizer.wire.seq_ranges import SeqRange
from trackinizer.wire.wire_code import CODE_BLOBS_PATH, CODE_MISSING_PATH
from trackinizer.wire.wire_export import EXPORT_API_PATH
from trackinizer.wire.wire_job import (
    JOB_CANCEL_PATH,
    JOB_GRANT_PATH,
    JOB_GRANT_SERIAL_PATH,
    JOB_GRANTS_PATH,
    JOB_PATH,
    JOB_SECRET_PATH,
    JOB_SECRETS_PATH,
    JOB_START_PATH,
    JOBS_PATH,
    ReadGrantBody,
    StartBody,
    SubmitBody,
)
from trackinizer.wire.wire_machine_host import (
    ENROLL_PATH,
    HEARTBEAT_PATH,
    JOIN_PATH,
)
from trackinizer.wire.wire_machine_job import (
    MACHINE_JOB_CANCEL_PATH,
    MACHINE_JOB_FILE_PATH,
    MACHINE_JOB_LOG_PATH,
    MACHINE_JOB_MANIFEST_PATH,
    MACHINE_JOB_PATH,
    MACHINE_JOB_REPORT_PATH,
    MACHINE_JOBS_PATH,
    MACHINE_READ_REPLY_PATH,
    MACHINE_WORK_PATH,
    CancelWork,
    JobReport,
    MachineJobSubmit,
    ReadRefused,
    ReadReply,
)
from trackinizer.wire.wire_machines import (
    MACHINE_PATH,
    MACHINES_PATH,
    MAX_HOW_CHARS,
)
from trackinizer.wire.wire_session_ir import (
    ManifestBody,
    RecordBody,
    SlashCommandBody,
)
from trackinizer.wire.wire_sessions import SessionStart
from trackinizer.wire.wire_terminals import (
    TERMINAL_MACHINES_PATH,
    TERMINALS_PATH,
    OpenTerminal,
)
from trackinizer.wire.wire_variables import VARIABLES_PATH


if TYPE_CHECKING:
    from pathlib import Path

    from trackinizer.wire.wire_machines import Facts


# ``handler`` receives an ``httpx2.Request`` and returns an ``httpx2.Response``. The
# test asserts on the request that arrives in the handler.
def _install_mock_transport(
    client: Client,
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    """Replace the client's transport with one that calls ``handler``."""
    client._http.close()
    client._http = httpx2.Client(
        base_url=client.base_url,
        transport=httpx2.MockTransport(handler),
        headers=dict(client._http.headers),
    )


def _read_timeout(request: httpx2.Request) -> float | None:
    """Return the read timeout the client attached to ``request``."""
    timeouts = from_plain(request.extensions["timeout"], dict[str, float | None])
    return timeouts["read"]


class _ClientSpy(Client):
    def __init__(
        self,
        *,
        get_results: list[PlainTree] | None = None,
        post_result: PlainTree | None = None,
    ) -> None:
        super().__init__("http://server")
        self.get_results: list[PlainTree] = list(get_results or [])
        self.post_result = post_result
        self.get_calls: list[tuple[str, dict[str, object] | None]] = []
        self.post_calls: list[tuple[str, object]] = []
        # ``(method, path, body)`` for every non-GET verb the client
        # issues. The new REST surface fans a single CLI method out into
        # PUT/PATCH/DELETE calls, so tests assert against this log.
        self.request_calls: list[tuple[str, str, object]] = []

    @override
    def get(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> PlainTree:
        del timeout
        self.get_calls.append((path, None if params is None else dict(params)))
        return self.get_results.pop(0) if self.get_results else None

    @override
    def post(
        self,
        path: str,
        *,
        body: object = None,
    ) -> PlainTree:
        self.post_calls.append((path, body))
        self.request_calls.append(("POST", path, body))
        return self.post_result

    @override
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        params: Mapping[str, object] | None = None,
        change_id: uuid.UUID | None = None,
        retry_attempts: int = 3,
        timeout: float | None = None,
    ) -> PlainTree:
        del change_id, params, retry_attempts, timeout
        self.request_calls.append((method, path, body))
        # ``submit`` and other write paths route through the HTTP verb
        # helpers (``post``/``put``/``patch``/``delete``), which call
        # ``_request`` so they can thread the freshly minted change_id
        # into the ``Idempotency-Key`` header. Mirror ``post``'s
        # bookkeeping so tests asserting against ``post_calls`` see POSTs.
        if method == "POST":
            self.post_calls.append((path, body))
        return self.post_result


class TestParseRef:
    def test_parse_seq_ref_case_insensitive(self) -> None:
        ref = parse_ref(" issue#7 ")
        assert ref == SeqRef(kind="Issue", seq=7)
        assert str(ref) == "Issue#7"

    def test_parse_uuid_ref(self) -> None:
        value = uuid.uuid4()
        ref = parse_ref(str(value))
        assert ref == UuidRef(uuid=value)
        assert str(ref) == str(value)

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("", "empty reference"),
            ("Nope#1", "unknown kind"),
            ("Issue:not-a-seq", "cannot parse"),
        ],
    )
    def test_rejects_empty_unknown_kind_and_bad_shape(
        self,
        value: str,
        message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            parse_ref(value)


class TestParseKind:
    def test_parse_kind_case_insensitive_and_plural(self) -> None:
        assert parse_kind("belief") == "Belief"
        assert parse_kind("Issues") == "Issue"

    def test_parse_kind_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="unknown kind"):
            parse_kind("widgets")


class TestFlags:
    @pytest.fixture(autouse=True)
    def _isolated_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "rekursiv-ai" / "trax" / "profiles").mkdir(
            parents=True,
            exist_ok=True,
        )
        monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)
        monkeypatch.delenv("TRACKINIZER_URL", raising=False)

    def test_flags_default_to_none(self) -> None:
        parser = argparse.ArgumentParser()
        connection.connect_flags(parser)
        args = parser.parse_args([])
        profile_arg = cast(str | None, args.profile)
        host_arg = cast(str | None, args.host)
        port_arg = cast(int | None, args.port)
        assert profile_arg is None
        assert host_arg is None
        assert port_arg is None

    def test_flags_parse_user_values(self) -> None:
        parser = argparse.ArgumentParser()
        connection.connect_flags(parser)
        args = parser.parse_args(
            ["--profile", "prod", "--host", "1.2.3.4", "--port", "9000"],
        )
        profile_arg = cast(str | None, args.profile)
        host_arg = cast(str | None, args.host)
        port_arg = cast(int | None, args.port)
        assert profile_arg == "prod"
        assert host_arg == "1.2.3.4"
        assert port_arg == 9000

    def test_from_args_host_port_override_profile(self) -> None:
        profile.save_profile("prod", Profile(url="http://prod:1000"))
        client = connection.connect(
            argparse.Namespace(profile="prod", host="ex", port=9090),
        )
        assert client.base_url == "http://ex:9090"

    def test_from_args_selects_named_profile(self) -> None:
        profile.save_profile("prod", Profile(url="http://prod:9000", author="alice"))
        client = connection.connect(
            argparse.Namespace(profile="prod", host=None, port=None),
        )
        assert client.base_url == "http://prod:9000"
        assert client.author == "alice"

    def test_from_args_partial_flags_fill_from_profile(self) -> None:
        profile.save_profile("default", Profile(url="http://defaulthost:8888"))
        host_only = connection.connect(
            argparse.Namespace(profile=None, host="other", port=None),
        )
        assert host_only.base_url == "http://other:8888"
        port_only = connection.connect(
            argparse.Namespace(profile=None, host=None, port=4242),
        )
        assert port_only.base_url == "http://defaulthost:4242"

    def test_from_args_without_flags_uses_trackinizer_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_URL", "http://127.0.0.1:8766/")
        client = connection.connect(argparse.Namespace())
        assert client.base_url == "http://127.0.0.1:8766"
        assert client.author == ""

    def test_from_args_rejects_invalid_trackinizer_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_URL", "actor")
        with pytest.raises(ClientError, match="TRACKINIZER_URL has invalid URL"):
            connection.connect(argparse.Namespace())

    def test_constructor_rejects_invalid_base_url(self) -> None:
        with pytest.raises(ClientError, match="base_url has invalid URL"):
            Client("actor")

    def test_from_args_raises_when_named_profile_missing(self) -> None:
        with pytest.raises(ClientError, match="profile 'typo' not found"):
            connection.connect(argparse.Namespace(profile="typo", host=None, port=None))

    def test_bare_client_reads_author_from_profile(self) -> None:
        profile.save_profile(
            "default",
            Profile(url="http://defaulthost:8888", author="alice"),
        )
        client = connection.connect(
            argparse.Namespace(profile=None, host=None, port=None),
        )
        assert client.base_url == "http://defaulthost:8888"
        assert client.author == "alice"

    def test_from_args_without_flags_uses_default_profile(self) -> None:
        bare = connection.connect(argparse.Namespace())
        assert bare.base_url == "http://127.0.0.1:8765"
        assert bare.author == ""

    def test_from_args_preserves_https_scheme(self) -> None:
        profile.save_profile(
            "prod",
            Profile(url="https://example.com:443", author="alice"),
        )
        client = connection.connect(
            argparse.Namespace(profile="prod", host=None, port=None),
        )
        assert client.base_url == "https://example.com:443"
        assert client.author == "alice"

    def test_from_args_preserves_portless_profile(self) -> None:
        profile.save_profile("prod", Profile(url="https://example.com"))
        client = connection.connect(
            argparse.Namespace(profile="prod", host=None, port=None),
        )
        assert client.base_url == "https://example.com"

    def test_from_args_host_override_keeps_profile_scheme_and_port(self) -> None:
        profile.save_profile("prod", Profile(url="https://prod.example:8443"))
        client = connection.connect(
            argparse.Namespace(profile="prod", host="other", port=None),
        )
        assert client.base_url == "https://other:8443"

    def test_from_args_port_override_on_portless_profile(self) -> None:
        profile.save_profile("prod", Profile(url="https://example.com"))
        client = connection.connect(
            argparse.Namespace(profile="prod", host=None, port=9000),
        )
        assert client.base_url == "https://example.com:9000"


class TestRequests:
    def test_client_bounds_the_transport_connection_pool(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_limits = httpx2.Limits
        real_transport = httpx2.HTTPTransport
        observed: list[tuple[int, int, float]] = []
        transport_limits: list[httpx2.Limits] = []

        def make_limits(
            *,
            max_connections: int,
            max_keepalive_connections: int,
            keepalive_expiry: float,
        ) -> httpx2.Limits:
            observed.append(
                (max_connections, max_keepalive_connections, keepalive_expiry),
            )
            return real_limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
                keepalive_expiry=keepalive_expiry,
            )

        def make_transport(
            *,
            retries: int,
            limits: httpx2.Limits,
        ) -> httpx2.HTTPTransport:
            transport_limits.append(limits)
            return real_transport(retries=retries, limits=limits)

        monkeypatch.setattr(httpx2, "Limits", make_limits)
        monkeypatch.setattr(httpx2, "HTTPTransport", make_transport)

        with Client("https://server"):
            pass

        assert observed == [(8, 8, 90.0)]
        assert len(transport_limits) == 1

    def test_request_builds_url_headers_and_body(self) -> None:
        seen: dict[str, httpx2.Request] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["req"] = request
            return httpx2.Response(200, json={"ok": True})

        with Client("http://server/") as client:
            _install_mock_transport(client, handler)
            result = client.post("/api/x", body={"a": 1})
        assert result == {"ok": True}
        req = seen["req"]
        assert str(req.url) == "http://server/api/x"
        assert req.method == "POST"
        assert json.loads(req.content) == {"a": 1}
        assert req.headers["Accept"] == "application/json"
        # Mutating requests carry the Idempotency-Key header.
        uuid.UUID(req.headers["Idempotency-Key"])

    def test_request_encodes_query_and_empty_response(self) -> None:
        seen: dict[str, httpx2.Request] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["req"] = request
            return httpx2.Response(200, content=b"")

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            result = client.get(
                "/api/search",
                params={"q": "a b", "skip": "", "none": None},
            )
        assert result is None
        req = seen["req"]
        assert str(req.url) == "http://server/api/search?q=a+b"
        # GETs do not carry Idempotency-Key (no mutation to dedup).
        assert "Idempotency-Key" not in req.headers

    def test_request_omits_authorization_header_without_api_key(self) -> None:
        seen: dict[str, httpx2.Request] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["req"] = request
            return httpx2.Response(200, content=b"")

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            client.get("/api/x")
        assert "Authorization" not in seen["req"].headers

    def test_request_adds_bearer_when_api_key_set(self) -> None:
        seen: dict[str, httpx2.Request] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["req"] = request
            return httpx2.Response(200, content=b"")

        with Client("http://server", api_key="trax_abcdef") as client:
            _install_mock_transport(client, handler)
            client.post("/api/x", body={"a": 1})
        assert seen["req"].headers["Authorization"] == "Bearer trax_abcdef"

    def test_request_wraps_http_and_connection_errors(self) -> None:
        def http_error(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(409, content=b'{"detail":"bad"}')

        with Client("http://server") as client:
            _install_mock_transport(client, http_error)
            with pytest.raises(ClientError, match="POST /x -> 409"):
                client.post("/x")

    def test_http_error_carries_structured_code(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(
                409,
                json={"detail": "clash", "code": "conflict"},
            )

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            src = uuid.uuid4()
            dst = uuid.uuid4()
            with pytest.raises(ClientError) as exc_info:
                client.post(f"/api/edges/{src}/requires/{dst}")
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "conflict"

    def test_http_error_carries_the_code_a_detail_nests(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(
                410,
                json={"detail": {"code": "chat_deleted", "message": "gone"}},
            )

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError) as exc_info:
                client.get("/api/chats/x")
        assert (exc_info.value.status_code, exc_info.value.code) == (
            410,
            "chat_deleted",
        )

    def test_wraps_connection_errors(self) -> None:
        def connect_error(request: httpx2.Request) -> httpx2.Response:
            del request
            raise httpx2.ConnectError("offline")

        with Client("http://server") as client:
            _install_mock_transport(client, connect_error)
            with pytest.raises(ClientError, match="GET /x failed: offline"):
                client.get("/x")

    def test_transport_failure_logs_client_pool_context(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def handshake_timeout(request: httpx2.Request) -> httpx2.Response:
            del request
            raise httpx2.ConnectTimeout(
                "_ssl.c:1063: The handshake operation timed out",
            )

        with Client("https://server") as client:
            _install_mock_transport(client, handshake_timeout)
            with caplog.at_level(logging.WARNING), pytest.raises(ClientError):
                client.get("/api/version")

        record = next(
            record
            for record in caplog.records
            if getattr(record, "event", "") == "trackinizer_transport_failure"
        )
        fields = from_plain(record.__dict__, dict[str, object])
        assert from_plain(fields.get("method"), str, default="") == "GET"
        assert from_plain(fields.get("path"), str, default="") == "/api/version"
        assert from_plain(fields.get("server"), str, default="") == "https://server"
        assert from_plain(fields.get("client_request_index"), int, default=0) == 1
        assert from_plain(fields.get("attempt"), int, default=0) == 1
        assert (
            from_plain(fields.get("failure_class"), str, default="")
            == "connect_timeout"
        )
        assert (
            from_plain(fields.get("failure_detail"), str, default="")
            == "tls_handshake_timeout"
        )
        assert from_plain(fields.get("error_type"), str, default="") == "ConnectTimeout"
        assert from_plain(fields.get("client_age_sec"), float, default=-1.0) >= 0
        assert len(from_plain(fields.get("client_id"), str, default="")) == 12

    def test_retries_5xx_with_same_change_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 502/503/504 retries the same request body and Idempotency-Key."""

        def _no_sleep(s: float) -> None:
            del s

        monkeypatch.setattr("trackinizer.client.client.time.sleep", _no_sleep)
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            if len(seen) < 3:
                return httpx2.Response(502, content=b"bad gateway")
            return httpx2.Response(200, json={"ok": True})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            result = client.post("/api/x", body={"a": 1})
        assert result == {"ok": True}
        assert len(seen) == 3
        change_ids = {req.headers["Idempotency-Key"] for req in seen}
        assert len(change_ids) == 1, (
            "every retry must reuse the same UUID so the server "
            "recognizes it as a replay rather than a duplicate operation"
        )

    def test_retries_500_with_same_change_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A transient 500 retries the same body + Idempotency-Key, then succeeds.

        The single-writer PGlite substrate can return a transient 500 under
        concurrent load. The idempotency key makes the replay dedup-safe (a
        write that already landed collides on the change_log PK), so 500 is
        retried like the other 5xx -- one logical write, not two.
        """

        def _no_sleep(s: float) -> None:
            del s

        monkeypatch.setattr("trackinizer.client.client.time.sleep", _no_sleep)
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            if len(seen) < 2:
                return httpx2.Response(500, content=b"internal server error")
            return httpx2.Response(200, json={"ok": True})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            result = client.post("/api/x", body={"a": 1})
        assert result == {"ok": True}
        assert len(seen) == 2  # One 500, then the retry succeeded.
        change_ids = {req.headers["Idempotency-Key"] for req in seen}
        assert len(change_ids) == 1, "the 500 retry must reuse the same UUID"

    def test_gives_up_after_max_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After exhausting retries the final 5xx surfaces as ClientError."""

        def _no_sleep(s: float) -> None:
            del s

        monkeypatch.setattr("trackinizer.client.client.time.sleep", _no_sleep)
        attempts: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            attempts.append(request)
            return httpx2.Response(503, content=b"down")

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="POST /x -> 503"):
                client.post("/x")
        assert len(attempts) == 3

    @pytest.mark.parametrize(
        "exc",
        [
            httpx2.WriteError("broken pipe"),
            httpx2.WriteTimeout("write timed out"),
            httpx2.ConnectTimeout("connect timed out"),
        ],
    )
    def test_wraps_write_and_connect_timeouts_without_retry(
        self,
        exc: httpx2.HTTPError,
    ) -> None:
        """Write/connect-timeout errors surface as ``ClientError``, not raw httpx2.

        ``WriteError`` / ``WriteTimeout`` / ``ConnectTimeout`` must be wrapped
        in the client's ``ClientError`` contract like the other transport
        failures. They are *not* retried: the request bytes may already have
        reached the server, so a blind retry could duplicate a mutation.
        """
        attempts: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            attempts.append(request)
            raise exc

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="POST /x failed:"):
                client.post("/x")
        assert len(attempts) == 1


def test_request_truncates_oversized_error_text() -> None:
    """R-54: a huge error body is truncated before landing in ``ClientError``.

    Embedding the full ``response.text`` is unbounded log/memory growth and
    can echo a secret verbatim; the message keeps a bounded prefix plus an
    ellipsis marker so the truncation is visible.
    """
    big = "x" * 4_096

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(400, content=big.encode())

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError) as exc_info:
            client.get("/x")
    message = str(exc_info.value)
    assert len(message) < 5_000, "error text must be truncated, not embedded whole"
    assert "..." in message, "truncation must be marked with an ellipsis"


class TestVersion:
    def test_returns_server_sha(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"sha": "deadbeef"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            assert client.version() == "deadbeef"

    def test_servers_own_unknown_passes_through(self) -> None:
        # The server resolved its build to the literal "unknown"; that is a
        # real answer, returned verbatim (distinct from a malformed payload).
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"sha": "unknown"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            assert client.version() == "unknown"

    def test_malformed_payload_raises_not_silent_unknown(self) -> None:
        # A response with no ``sha`` key is a contract violation; it must raise,
        # not masquerade as the server's own "unknown".
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed payload"):
                client.version()


class TestClientErrorContract:
    """Every documented method wraps malformed server responses in ``ClientError``.

    R-14/R-55/R-63/R-69: a response missing a field, of the wrong JSON type,
    or failing pydantic validation must surface as ``ClientError`` -- never a
    raw ``KeyError`` / ``TypeError`` / ``pydantic.ValidationError`` past the
    contract the module promises (mirroring ``version``).
    """

    def test_resolve_id_seq_missing_id_raises_client_error(self) -> None:
        """A scalar-returning seam: ``resolve_id`` SeqRef with no ``id`` key."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"wrong": "shape"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.resolve_id(SeqRef(kind="Issue", seq=4))

    def test_resolve_id_uuid_non_dict_raises_client_error(self) -> None:
        """A dict-shaped seam fed a list must not leak ``TypeError``."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json=[1, 2, 3])

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.resolve_id(UuidRef(uuid=uuid.uuid4()))

    def test_resolve_id_errors_name_the_route_they_read(self) -> None:
        """A malformed id or kind is reported against its own lookup path."""
        target = uuid.uuid4()
        body: dict[str, object] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json=body)

        with Client("http://server") as client:
            _install_mock_transport(client, handler=handler)
            with pytest.raises(ClientError) as missing_id:
                client.resolve_id(SeqRef(kind="Issue", seq=4))
            body["id"] = "not-a-uuid"
            with pytest.raises(ClientError) as bad_id:
                client.resolve_id(SeqRef(kind="Issue", seq=4))
            with pytest.raises(ClientError) as missing_kind:
                client.resolve_id(UuidRef(uuid=target))

        assert str(missing_id.value).startswith("/api/inquiries/Issue/4 returned")
        assert str(bad_id.value).startswith("/api/inquiries/Issue/4 returned")
        assert str(missing_kind.value).startswith(f"/api/web/lookup/{target} returned")

    def test_resolve_id_rejects_a_uuid_ref_of_the_wrong_kind(self) -> None:
        target = uuid.uuid4()

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"kind": "Issue"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler=handler)
            with pytest.raises(ClientError) as err:
                client.resolve_id(UuidRef(uuid=target, expected_kind="Belief"))
            assert client.resolve_id(UuidRef(uuid=target, expected_kind="Issue")) == (
                "Issue",
                target,
            )

        assert str(err.value) == f"ref Belief {target} resolves to a Issue row"

    def test_resolve_ids_missing_found_raises_client_error(self) -> None:
        """``resolve_ids`` reads ``response['found']``; absence must wrap."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"nope": {}})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.resolve_ids([UuidRef(uuid=uuid.uuid4())])

    def test_submit_missing_id_raises_client_error(self) -> None:
        """``submit`` reads the server-minted ``id``; absence must wrap."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"nope": "x"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.submit("Issue", {"title": "x"})

    def test_submit_non_uuid_id_raises_client_error(self) -> None:
        """A non-UUID ``id`` value must wrap, not leak ``ValueError``."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"id": "not-a-uuid"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.submit("Issue", {"title": "x"})

    def test_submit_batch_missing_ids_raises_client_error(self) -> None:
        """A list-returning seam: ``submit_batch`` reads ``response['ids']``."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"nope": []})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.submit_batch([("Issue", {"title": "x"})])

    def test_next_issue_non_dict_raises_client_error(self) -> None:
        """``next_issue`` promises ``dict | None``; a list is malformed."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json=[1, 2, 3])

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.next_issue()

    def test_recent_changes_non_list_raises_client_error(self) -> None:
        """A list-returning read fed a dict must surface ``ClientError``."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"not": "a list"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.recent_changes()

    def test_session_start_malformed_wraps_validation_error(self) -> None:
        """A pydantic ``model_validate`` seam must wrap ``ValidationError``."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(201, json={"bogus": True})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.session_start(SessionStart(cli="codex"))


class TestClientMethods:
    def test_resolve_seq_and_uuid_refs(self) -> None:
        target_id = uuid.uuid4()
        client = _ClientSpy(
            get_results=[
                {"id": str(target_id)},
                {"kind": "Issue"},
            ],
        )
        assert client.resolve_id(SeqRef(kind="Issue", seq=4)) == ("Issue", target_id)
        assert client.resolve_id(UuidRef(uuid=target_id)) == ("Issue", target_id)
        assert client.get_calls[0][0] == "/api/inquiries/Issue/4"
        assert client.get_calls[1][0] == f"/api/web/lookup/{target_id}"

    def test_resolve_missing_seq_raises(self) -> None:
        client = _ClientSpy()
        with pytest.raises(ClientError, match="Issue#9 not found"):
            client.resolve_id(SeqRef(kind="Issue", seq=9))

    def test_read_methods_dispatch(self) -> None:
        target_id = uuid.uuid4()
        client = _ClientSpy(
            get_results=[
                [{"id": "1"}],
                {"kind": "Issue"},
                {"self": {"id": str(target_id)}},
                {"id": str(target_id)},
                [{"id": "c"}],
                {"agent_usd": 1.0},
            ],
        )
        assert client.list_kind(
            "Issue",
            status="active",
            limit=3,
            offset=2,
            seq_ranges=(SeqRange(start=4, stop=9),),
        ) == [{"id": "1"}]
        assert client.get_inquiry(UuidRef(uuid=target_id))[0] == "Issue"
        assert client.next_issue() == {"id": str(target_id)}
        assert client.recent_changes(limit=2) == [{"id": "c"}]
        assert client.cost_for(target_id, deep=True) == {"agent_usd": 1.0}

    def test_list_kind_serializes_filters_as_repeated_query_params(self) -> None:
        """Each filter rides the wire as a discrete ``filter`` query param.

        Concatenating field/op/value with a separator would break for
        values containing the separator; JSON-per-filter sidesteps
        escaping entirely. The order in which the CLI emits filters
        must be preserved so server-side semantics stay deterministic.
        """
        captured: dict[str, httpx2.Request] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured["r"] = request
            return httpx2.Response(200, json=[])

        client = Client("http://server")
        _install_mock_transport(client, handler)
        assert (
            client.list_kind(
                "Issue",
                filters=(
                    Filter(field="title", op="re", value="needle:with:colons"),
                    Filter(field="priority", op="gt", value="5"),
                ),
            )
            == []
        )
        req = captured["r"]
        assert req.url.path == "/api/inquiries"
        # ``kind`` is now a (repeatable) query param, not a path segment.
        assert req.url.params.get_list("kind") == ["Issue"]
        # ``httpx2.URL.params`` exposes repeated keys via ``get_list``.
        assert req.url.params.get_list("filter") == [
            '{"field":"title","op":"re","value":"needle:with:colons"}',
            '{"field":"priority","op":"gt","value":"5"}',
        ]

    def test_list_kind_sends_the_page_window_and_status(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json=[])

        client = Client("http://server")
        _install_mock_transport(client, handler)
        client.list_kind("Issue")
        client.list_kind("Belief", status="active", limit=7, offset=3)
        keys = ("kind", "status", "limit", "offset")
        assert [{k: r.url.params.get_list(k) for k in keys} for r in seen] == [
            {
                "kind": ["Issue"],
                "status": [],
                "limit": [str(DEFAULT_LIST_LIMIT)],
                "offset": ["0"],
            },
            {
                "kind": ["Belief"],
                "status": ["active"],
                "limit": ["7"],
                "offset": ["3"],
            },
        ]

    @pytest.mark.parametrize("payload", [{"rows": []}, [1]])
    def test_list_kind_names_the_route_for_a_malformed_payload(
        self,
        payload: PlainTree,
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json=payload)

        client = Client("http://server")
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError, match="/api/inquiries returned a malformed"):
            client.list_kind("Issue")

    def test_list_kind_serializes_seq_ranges_as_repeated_query_params(self) -> None:
        """Each interval rides the wire as a discrete ``seq_range`` param.

        The union of disjoint seq windows becomes one query: the server
        ORs the intervals, so the CLI sends them as repeated params in the
        order it parsed them and never fans out per interval.
        """
        captured: dict[str, httpx2.Request] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured["r"] = request
            return httpx2.Response(200, json=[])

        client = Client("http://server")
        _install_mock_transport(client, handler)
        assert (
            client.list_kind(
                "Issue",
                seq_ranges=(
                    SeqRange(start=222, stop=260),
                    SeqRange(start=279),
                ),
            )
            == []
        )
        assert captured["r"].url.params.get_list("seq_range") == ["222..260", "279.."]

    def test_list_kind_names_the_fields_to_send_or_none_for_whole_rows(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json=[])

        client = Client("http://server")
        _install_mock_transport(client, handler)
        client.list_kind("Issue", fields=("id", "labels"))
        client.list_kind("Issue")
        assert [r.url.params.get_list("fields") for r in seen] == [["id", "labels"], []]

    def test_list_kind_all_pages_past_the_cap(self) -> None:
        """``list_kind_all`` concatenates every page until a short one.

        The route caps ``limit`` at ``MAX_LIST_LIMIT``; a whole-collection view
        pages by ``offset`` to get them all. Simulate one full page (exactly the
        cap) followed by a short page, and assert: both offsets requested, every
        row returned, and termination on the short page.
        """
        offsets: list[str | None] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            offset = cast(object, request.url.params.get("offset"))
            assert offset is None or isinstance(offset, str)
            offsets.append(offset)
            if offset == "0":
                return httpx2.Response(
                    200,
                    json=[{"seq": i} for i in range(MAX_LIST_LIMIT)],
                )
            return httpx2.Response(200, json=[{"seq": MAX_LIST_LIMIT}])

        client = Client("http://server")
        _install_mock_transport(client, handler)
        rows = client.list_kind_all("Issue")
        assert len(rows) == MAX_LIST_LIMIT + 1, "all rows across both pages"
        assert offsets == ["0", str(MAX_LIST_LIMIT)], (
            "paged by offset, stopped on short"
        )

    def test_list_kind_all_terminates_on_empty_after_exact_multiple(self) -> None:
        """A collection that is an exact multiple of the cap still terminates.

        A full final page cannot be assumed to be the last, so the loop fetches
        once more and stops on the empty page.
        """
        calls = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal calls
            calls += 1
            if request.url.params.get("offset") == "0":
                return httpx2.Response(
                    200,
                    json=[{"seq": i} for i in range(MAX_LIST_LIMIT)],
                )
            return httpx2.Response(200, json=[])

        client = Client("http://server")
        _install_mock_transport(client, handler)
        rows = client.list_kind_all("Issue")
        assert len(rows) == MAX_LIST_LIMIT
        assert calls == 2, "second page (empty) confirms the end"

    def test_resolve_ids_posts_bare_array(self) -> None:
        target_id = uuid.uuid4()
        client = _ClientSpy(
            post_result={"found": {str(target_id): "Issue"}, "missing": []},
        )
        result = client.resolve_ids([UuidRef(uuid=target_id)])
        assert result == [("Issue", target_id)]
        path, body = client.post_calls[0]
        assert path == "/api/inquiries/lookup"
        assert body == [str(target_id)]

    def test_write_methods_dispatch(self) -> None:
        target_id = uuid.uuid4()
        client = _ClientSpy(post_result={"id": str(target_id)})
        assert client.submit("Issue", {"title": "x"}) == target_id
        client.edit(target_id, "title", "y", actor="alice", reason="why")
        client.add_edge(
            target_id,
            target_id,
            "narrows",
            actor="alice",
            priority=10,
        )
        client.annotate_edge(
            target_id,
            target_id,
            "narrows",
            actor="alice",
            priority=0,
            note="context",
        )
        client.remove_edge(target_id, target_id, "narrows", actor="alice")
        client.add_cost(
            target_id,
            "marginal_cost_agent_usd",
            -0.5,
            actor="alice",
            reason="fix",
        )
        client.purge(target_id, actor="alice", reason="bad")
        # ``submit`` POSTs to the kind-token create route; ``kind`` is the
        # URL token, not a body field.
        assert client.request_calls[0][:2] == ("POST", "/api/inquiries/issue")
        # ``edit`` overwrites a field via PUT with the uniform ``value``
        # key and ``actor`` provenance.
        assert client.request_calls[1] == (
            "PUT",
            f"/api/inquiries/{target_id}/title",
            {"value": "y", "actor": "alice", "reason": "why"},
        )
        # ``add_edge`` POSTs to the path-identity edge route.
        assert client.request_calls[2][:2] == (
            "POST",
            f"/api/edges/{target_id}/narrows/{target_id}",
        )
        # ``annotate_edge`` fans out one PUT per sent field.
        assert client.request_calls[3] == (
            "PUT",
            f"/api/edges/{target_id}/narrows/{target_id}/priority",
            {"value": 0, "actor": "alice"},
        )
        assert client.request_calls[4] == (
            "PUT",
            f"/api/edges/{target_id}/narrows/{target_id}/note",
            {"value": "context", "actor": "alice"},
        )
        # ``remove_edge`` DELETEs the edge path with an ``actor`` body.
        assert client.request_calls[5] == (
            "DELETE",
            f"/api/edges/{target_id}/narrows/{target_id}",
            {"actor": "alice"},
        )
        # A negative cost delta is sent as ``op=sub`` with a positive
        # ``value`` via PATCH on the marginal-cost axis.
        assert client.request_calls[6] == (
            "PATCH",
            f"/api/inquiries/{target_id}/marginal_cost_agent_usd",
            {"op": "sub", "value": 0.5, "actor": "alice", "reason": "fix"},
        )
        # ``purge`` DELETEs the inquiry itself.
        assert client.request_calls[7] == (
            "DELETE",
            f"/api/inquiries/{target_id}",
            {"actor": "alice", "reason": "bad"},
        )

    def test_annotate_edge_sends_explicit_nulls_and_empty_values(self) -> None:
        """An explicit ``None``/``""`` is a sent field: one PUT each.

        Only :data:`ABSENT` suppresses a field. A caller passing
        ``priority=None`` means "clear it", which rides the wire as a
        PUT whose ``value`` is ``None`` -- distinct from omitting it.
        """
        target_id = uuid.uuid4()
        client = _ClientSpy(post_result={"ok": True})
        client.annotate_edge(
            target_id,
            target_id,
            "narrows",
            actor="alice",
            priority=None,
            note="",
            valence=None,
            labels=None,
        )
        base = f"/api/edges/{target_id}/narrows/{target_id}"
        assert client.request_calls == [
            ("PUT", f"{base}/priority", {"value": None, "actor": "alice"}),
            ("PUT", f"{base}/note", {"value": "", "actor": "alice"}),
            ("PUT", f"{base}/valence", {"value": None, "actor": "alice"}),
            ("PUT", f"{base}/labels", {"value": None, "actor": "alice"}),
        ]

    def test_atomic_list_primitives_dispatch(self) -> None:
        """Every new server primitive has a one-shot Client method."""
        target_id = uuid.uuid4()
        codechange_id = uuid.uuid4()
        client = _ClientSpy(post_result={"ok": True})
        client.add_subscriber(target_id, "bob", actor="alice")
        client.remove_subscriber(target_id, "bob", actor="alice")
        client.add_label(target_id, "urgent", actor="alice")
        client.remove_label(target_id, "urgent", actor="alice")
        client.add_issue_kind(target_id, "bug", actor="alice")
        client.remove_issue_kind(target_id, "bug", actor="alice")
        client.add_codechange(target_id, codechange_id, actor="alice")
        client.remove_codechange(target_id, codechange_id, actor="alice")
        client.transition_status(
            target_id,
            expected_from="active",
            to="complete",
            actor="alice",
            reason="ship",
        )
        # Every list primitive is a PATCH on ``/api/inquiries/<id>/<field>``
        # except the compare-and-set status, a PUT carrying the ``expected``
        # guard. The field name lives in the URL, the verb (add/sub) in
        # the body ``op``.
        method_paths = [(m, p) for m, p, _ in client.request_calls]
        assert method_paths == [
            ("PATCH", f"/api/inquiries/{target_id}/subscribers"),
            ("PATCH", f"/api/inquiries/{target_id}/subscribers"),
            ("PATCH", f"/api/inquiries/{target_id}/labels"),
            ("PATCH", f"/api/inquiries/{target_id}/labels"),
            ("PATCH", f"/api/issue/{target_id}/issue_kind"),
            ("PATCH", f"/api/issue/{target_id}/issue_kind"),
            ("PATCH", f"/api/experiment/{target_id}/codechanges"),
            ("PATCH", f"/api/experiment/{target_id}/codechanges"),
            ("PUT", f"/api/inquiries/{target_id}/status"),
        ]
        # ``add_subscriber`` augments the list with the single element in
        # ``value``; ``actor`` is the audit provenance.
        assert client.request_calls[0][2] == {
            "op": "add",
            "value": "bob",
            "actor": "alice",
        }
        # ``remove_subscriber`` is the same field with ``op=sub``.
        assert client.request_calls[1][2] == {
            "op": "sub",
            "value": "bob",
            "actor": "alice",
        }
        # compare-and-set status: PUT with mode='cas' + the ``value``/
        # ``expected`` pair.
        assert client.request_calls[-1][2] == {
            "actor": "alice",
            "value": "complete",
            "mode": "cas",
            "expected": "active",
            "reason": "ship",
        }

    def test_transition_owner_dispatches_nullable_compare_and_set(self) -> None:
        """Owner acquisition sends an explicit NULL expectation."""
        target_id = uuid.uuid4()
        client = _ClientSpy(post_result={"ok": True})

        client.transition_owner(
            target_id,
            expected_from=None,
            to="worker-1",
            actor="worker-1",
        )

        assert client.request_calls == [
            (
                "PUT",
                f"/api/inquiries/{target_id}/owner",
                {
                    "actor": "worker-1",
                    "value": "worker-1",
                    "mode": "cas",
                    "expected": None,
                },
            ),
        ]

    def test_transition_app_dispatches_the_whole_expected_object(self) -> None:
        """A row's ``app`` is compared and set whole, on the base fields' route."""
        target_id = uuid.uuid4()
        client = _ClientSpy(post_result={"ok": True})

        client.transition_app(
            target_id,
            expected_from={"tracker": {"verdict": "works"}},
            to={"tracker": {"verdict": "works", "patch": "p"}},
            actor="meter",
            reason="code change",
        )

        assert client.request_calls == [
            (
                "PUT",
                f"/api/inquiries/{target_id}/app",
                {
                    "actor": "meter",
                    "value": {"tracker": {"verdict": "works", "patch": "p"}},
                    "mode": "cas",
                    "expected": {"tracker": {"verdict": "works"}},
                    "reason": "code change",
                },
            ),
        ]

    def test_annotate_edge_is_best_effort_in_fixed_field_order(self) -> None:
        """``annotate_edge`` is documented best-effort: deterministic order, partial on failure.

        I3: there is no composite edge-update route, so the multi-PUT fan-out
        cannot be atomic. The contract this pins is that fields go out in the
        fixed order ``priority, note, valence, labels``, and a mid-sequence
        failure surfaces with the earlier field already applied -- so callers
        can reason about (and retry from) a deterministic partial state.
        """
        target_id = uuid.uuid4()

        class _FailingSecondPut(_ClientSpy):
            @override
            def put(self, path: str, *, body: object = None) -> PlainTree:
                recorded = super().put(path, body=body)
                if path.endswith("/note"):
                    raise ClientError("put note failed")
                return recorded

        client = _FailingSecondPut(post_result={"ok": True})
        with pytest.raises(ClientError, match="put note failed"):
            client.annotate_edge(
                target_id,
                target_id,
                "narrows",
                actor="alice",
                priority=7,
                note="ctx",
                valence=0.5,
            )
        base = f"/api/edges/{target_id}/narrows/{target_id}"
        sent = [(m, p) for m, p, _ in client.request_calls]
        # Priority landed first, note failed; valence/labels never sent.
        assert sent == [
            ("PUT", f"{base}/priority"),
            ("PUT", f"{base}/note"),
        ]

    def test_author_primitives_dispatch_to_paper_authors_route(self) -> None:
        """``add_author``/``remove_author`` PATCH the Paper authors byline.

        The grammar wires the author field to these methods; they must exist
        and route to ``/api/paper/<id>/authors`` (the kind-scoped list route)
        with ``op=add``/``op=sub`` -- the same shape as the other atomic list
        primitives.
        """
        target_id = uuid.uuid4()
        client = _ClientSpy(post_result={"ok": True})
        client.add_author(target_id, "Vaswani", actor="alice")
        client.remove_author(target_id, "Vaswani", actor="alice")
        method_paths = [(m, p) for m, p, _ in client.request_calls]
        assert method_paths == [
            ("PATCH", f"/api/paper/{target_id}/authors"),
            ("PATCH", f"/api/paper/{target_id}/authors"),
        ]
        assert client.request_calls[0][2] == {
            "op": "add",
            "value": "Vaswani",
            "actor": "alice",
        }
        assert client.request_calls[1][2] == {
            "op": "sub",
            "value": "Vaswani",
            "actor": "alice",
        }

    def test_add_edge_threads_optional_annotations(self) -> None:
        """``add_edge`` only sends the optional fields the caller supplies."""
        target_id = uuid.uuid4()
        client = _ClientSpy(post_result={"ok": True})
        client.add_edge(
            target_id,
            target_id,
            "proves",
            actor="alice",
            note="load-bearing",
            valence=0.9,
            labels=["important"],
        )
        body = from_plain(client.post_calls[0][1], dict[str, object])
        assert body["note"] == "load-bearing"
        assert body["valence"] == 0.9
        assert body["labels"] == ["important"]

    def test_resolve_ids_handles_seq_refs_and_missing(self) -> None:
        """``resolve_ids`` mixes UUID-batch and seq-fallback paths and 404s."""
        seq_id = uuid.uuid4()
        client = _ClientSpy(get_results=[{"id": str(seq_id), "kind": "Issue"}])
        # No UUID refs in the list, so the bulk POST never fires; the
        # per-ref ``resolve_id`` GET handles it.
        results = client.resolve_ids([SeqRef(kind="Issue", seq=1)])
        assert results == [("Issue", seq_id)]
        # Missing ref: the bulk POST returns the id in ``missing`` (empty
        # ``found``), which triggers the not-found branch.
        missing = uuid.uuid4()
        client.post_result = {"found": {}, "missing": [str(missing)]}
        with pytest.raises(ClientError, match="not found"):
            client.resolve_ids([UuidRef(uuid=missing)])


# Folded in from former fake_surface_test.py.


# Members on ``Client`` that the fake intentionally does not implement:
# argparse-flag registration, the connection-resolution factory, and the
# raw HTTP verbs. The fake bypasses HTTP entirely, so these would be dead
# on the fake. The variable methods are driven through stub clients in
# ``trax/variables_test.py``, and no ``trax`` command reads machines or jobs, so the
# shared fake carries none of them. No ``trax`` command files code, so it holds no
# code-store method either. The canvas capture is an agent's alone, and no command takes one.
# The exec calls are the assistant's and a machine host's, which no ``trax`` command is,
# and ``client/client_exec_test.py`` drives them.
_FAKE_EXEMPT: frozenset[str] = frozenset(
    {"flags", "from_args", "get", "post", "put", "patch", "delete"}
    | {"list_variables", "put_variable", "delete_variable"}
    | {"list_machines", "get_machine", "put_machine"}
    | {"change_machine_labels", "delete_machine"}
    | {"enroll_machine", "join_machine", "heartbeat_machine", "revoke_machine"}
    | {"machine_runs", "disconnect_machine"}
    | {"wait_terminals"}
    | {"exec_command", "wait_exec", "post_exec_result"}
    | {"job_submit", "job_list", "job_view", "job_grant", "job_read_grant"}
    | {"job_grant_serial", "job_start", "job_cancel", "job_secret_names"}
    | {"job_put_secret", "job_delete_secret"}
    | {"machine_job_submit", "machine_job_list", "machine_job_view"}
    | {"machine_job_cancel", "machine_job_log", "machine_job_manifest"}
    | {"machine_job_file", "wait_machine_work", "report_machine_job"}
    | {"post_machine_read"}
    | {"code_missing", "code_put_blobs", "code_put_snapshot", "code_snapshot"}
    | {"capture_canvas"},
)


def _public_methods(cls: type) -> set[str]:
    # The mutation hook wraps every method of a mutated class in trampolines named
    # ``xǁClassǁname__mutmut_N``. No FakeClient can have them, so without this filter
    # the surface test fails whenever mutmut touches ``client.py``.
    return {
        name
        for name, member in inspect.getmembers(cls, callable)
        if not name.startswith("_")
        and not inspect.isclass(member)
        # Mutmut adds ``xǁ<Class>ǁ<method>__mutmut_<N>`` copies; not API surface.
        and "ǁ" not in name
        and "__mutmut_" not in name
    } - _FAKE_EXEMPT


def _missing_methods(real: type, *, fake: type) -> set[str]:
    return _public_methods(real) - _public_methods(fake)


def test_the_surface_test_ignores_mutmut_names_but_not_a_missing_method() -> None:
    class Complete:
        def kept(self) -> None: ...

    class Missing:
        pass

    # A class as mutmut leaves it: its real method plus a mangled trampoline.
    mutated = type(
        "Mutated",
        (),
        {"kept": Complete.kept, "xǁMutatedǁkept__mutmut_1": Complete.kept},
    )

    assert _public_methods(mutated) == {"kept"}
    assert _missing_methods(mutated, fake=Complete) == set()
    assert _missing_methods(mutated, fake=Missing) == {"kept"}


def test_fake_client_covers_real_client_surface() -> None:
    """Every public ``Client`` method must exist on ``FakeClient``."""
    missing = _missing_methods(Client, fake=FakeClient)
    assert not missing, (
        f"FakeClient is missing the following Client methods: {sorted(missing)}. "
        "Add them to conftest.py:FakeClient or tests will silently take wrong "
        "code paths when these methods are exercised."
    )


def test_fake_client_method_signatures_match_client() -> None:
    """Shared methods must keep matching parameter names AND return types.

    Param-name parity catches "the CLI passes ``valence=`` but Fake
    accepts ``rel=``" mismatches. Return-type parity catches "real
    returns ``bool`` but Fake returns ``None``" -- the kind of drift
    that would silently let a verb test pass with the wrong contract.
    """
    mismatches: list[str] = []
    for name in _public_methods(Client) & _public_methods(FakeClient):
        real_sig = inspect.signature(cast(Callable[..., object], getattr(Client, name)))
        fake_sig = inspect.signature(
            cast(Callable[..., object], getattr(FakeClient, name)),
        )
        real_params = list(real_sig.parameters)
        fake_params = list(fake_sig.parameters)
        if real_params != fake_params:
            mismatches.append(f"{name}: real={real_params!r} fake={fake_params!r}")
        real_return = cast(object, real_sig.return_annotation)
        fake_return = cast(object, fake_sig.return_annotation)
        if real_return != fake_return:
            mismatches.append(
                f"{name}: return real={real_return!r} fake={fake_return!r}",
            )
    assert not mismatches, (
        "FakeClient method signatures diverge from Client:\n"
        + "\n".join(f"  {m}" for m in mismatches)
    )


def test_public_methods_ignores_mutmut_copies() -> None:
    """The mutation hook's baseline run must not see mutmut's copies as API."""
    copies = {
        f"xǁClientǁadd_author__mutmut_{suffix}": Client.add_author
        for suffix in ("orig", "1")
    }
    mutated = type("Client", (Client,), copies)
    assert "add_author" in _public_methods(mutated)
    assert _public_methods(mutated) == _public_methods(Client)


# Coverage for the URL validator's reject paths.


def test_server_url_rejects_credentials() -> None:
    with pytest.raises(ClientError, match="must not embed credentials"):
        server_url("http://alice:secret@example.com", "test")


def test_server_url_rejects_query() -> None:
    with pytest.raises(ClientError, match="must not contain query"):
        server_url("http://example.com?x=1", "test")


def test_server_url_rejects_fragment() -> None:
    with pytest.raises(ClientError, match="must not contain query"):
        server_url("http://example.com#section", "test")


@pytest.mark.parametrize(
    "raw",
    [
        "http://:8765",  # `missing` host.
        "http://example.com:abc",  # non-numeric port.
        "http://example.com:99999",  # out-of-range port.
    ],
)
def test_server_url_rejects_malformed_host_or_port(raw: str) -> None:
    """A missing host or malformed/out-of-range port is a ``ClientError``.

    TRAX-REV-010: ``server_url`` validated scheme/netloc and rejected
    credentials/query/fragment, but never the host or port -- so these slipped
    through and failed later outside the ``ClientError`` contract.
    """
    with pytest.raises(ClientError, match="invalid URL"):
        server_url(raw, "test")


@pytest.mark.parametrize(
    "raw",
    ["http://example.com:8765", "http://example.com"],
)
def test_server_url_accepts_valid_host_and_port(raw: str) -> None:
    """A well-formed host (with or without a port) still passes unchanged."""
    assert server_url(raw, "test") == raw


def test_request_wraps_malformed_json_on_2xx() -> None:
    """A 2xx with a non-empty malformed body raises ``ClientError``.

    TRAX-REV-004: the success path returned ``response.json()`` directly, so a
    200 whose body is not valid JSON leaked a raw ``json.JSONDecodeError`` past
    the ``ClientError`` contract the module promises.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, content=b"<html>oops")

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError, match="malformed JSON"):
            client.get("/x")


def test_a_stale_canvas_operation_is_retried_once_on_the_live_revision() -> None:
    """A 409 re-reads the revision and resends with a fresh ``Idempotency-Key``."""
    canvas = uuid.uuid4()
    revisions = iter([3, 5])
    sent: list[tuple[int, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(
                200,
                json={"id": str(canvas), "revision": next(revisions)},
            )
        revision = from_plain(
            json_loads(request.content, dict[str, object])["revision"],
            int,
        )
        sent.append((revision, request.headers["Idempotency-Key"]))
        if revision == 3:
            return httpx2.Response(409, json={"detail": "stale canvas revision"})
        return httpx2.Response(200, json={"id": str(canvas), "revision": 6})

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        state = client.apply_canvas_operation(canvas, operation={"kind": "hide"})

    assert state["revision"] == 6
    assert [revision for revision, _ in sent] == [3, 5]
    assert sent[0][1] != sent[1][1]


def test_a_canvas_operation_refused_for_another_reason_is_not_retried() -> None:
    canvas = uuid.uuid4()
    posts: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json={"id": str(canvas), "revision": 3})
        posts.append(1)
        return httpx2.Response(422, json={"detail": "Visual instance not found."})

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError, match="422"):
            _ = client.apply_canvas_operation(canvas, operation={"kind": "hide"})

    assert posts == [1]


def test_a_navigation_reads_no_revision_and_names_the_route() -> None:
    canvas = uuid.uuid4()
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                json_loads(request.content, dict[str, object]),
            ),
        )
        return httpx2.Response(200, json={"id": str(canvas), "revision": 4})

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        state = client.navigate(canvas, route="#/lookup/abc")

    assert state["revision"] == 4
    assert seen == [
        (
            "POST",
            f"/api/canvases/{canvas}/operations",
            {"revision": 0, "operation": {"kind": "navigate", "route": "#/lookup/abc"}},
        ),
    ]


def test_a_highlight_reads_no_revision_and_names_the_ids() -> None:
    canvas = uuid.uuid4()
    ids = [uuid.uuid4(), uuid.uuid4()]
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                json_loads(request.content, dict[str, object]),
            ),
        )
        return httpx2.Response(200, json={"id": str(canvas), "revision": 4})

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        state = client.highlight(canvas, ids=ids)

    assert state["revision"] == 4
    assert seen == [
        (
            "POST",
            f"/api/canvases/{canvas}/operations",
            {
                "revision": 0,
                "operation": {"kind": "highlight", "ids": [str(i) for i in ids]},
            },
        ),
    ]


def test_a_chat_window_reads_no_revision_and_names_the_flag() -> None:
    canvas = uuid.uuid4()
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                json_loads(request.content, dict[str, object]),
            ),
        )
        return httpx2.Response(200, json={"id": str(canvas), "revision": 4})

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        state = client.chat_window(canvas, hidden=True)

    assert state["revision"] == 4
    assert seen == [
        (
            "POST",
            f"/api/canvases/{canvas}/operations",
            {"revision": 0, "operation": {"kind": "chat", "hidden": True}},
        ),
    ]


def test_a_capture_posts_once_and_returns_the_picture() -> None:
    canvas = uuid.uuid4()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path))
        return httpx2.Response(
            200,
            json={"width": 4, "mime": "image/png", "tiles": [{"id": "t"}]},
        )

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        picture = client.capture_canvas(canvas)

    assert picture == {"width": 4, "mime": "image/png", "tiles": [{"id": "t"}]}
    assert seen == [("POST", f"/api/canvases/{canvas}/captures")]


def test_a_capture_no_page_answered_is_a_424_that_is_not_retried() -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        return httpx2.Response(424, json={"detail": "No open page answered."})

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError) as raised:
            client.capture_canvas(uuid.uuid4())

    assert raised.value.status_code == 424
    assert "No open page answered" in str(raised.value)
    assert len(calls) == 1


def test_a_preferences_change_reads_no_revision_and_sends_the_keys() -> None:
    canvas = uuid.uuid4()
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                json_loads(request.content, dict[str, object]),
            ),
        )
        return httpx2.Response(200, json={"id": str(canvas), "revision": 4})

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        state = client.set_preferences(
            canvas,
            change={"theme": "light", "chat_side": None},
        )

    assert state["revision"] == 4
    assert seen == [
        (
            "POST",
            f"/api/canvases/{canvas}/operations",
            {
                "revision": 0,
                "operation": {
                    "kind": "preferences",
                    "change": {"theme": "light", "chat_side": None},
                },
            },
        ),
    ]


def _answering(payload: PlainTree) -> Client:
    """Return a client whose server answers every request with ``payload``."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json=payload)

    client = Client("http://server")
    _install_mock_transport(client, handler)
    return client


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, r"/api/inquiries/Issue/3 returned a malformed payload: missing 'id'"),
        (
            {"id": "not-a-uuid"},
            r"/api/inquiries/Issue/3 returned a malformed id 'not-a-uuid'",
        ),
    ],
)
def test_resolve_id_names_the_route_when_a_seq_lookup_is_malformed(
    payload: PlainTree,
    message: str,
) -> None:
    with _answering(payload) as client, pytest.raises(ClientError, match=message):
        _ = client.resolve_id(SeqRef(kind="Issue", seq=3))


def test_resolve_id_names_the_route_when_a_uuid_lookup_has_no_kind() -> None:
    row = uuid.uuid4()

    with (
        _answering({}) as client,
        pytest.raises(
            ClientError,
            match=rf"/api/web/lookup/{row} returned a malformed payload: missing 'kind'",
        ),
    ):
        _ = client.resolve_id(UuidRef(uuid=row))


def test_resolve_id_accepts_the_expected_kind_and_refuses_another() -> None:
    row = uuid.uuid4()

    with _answering({"kind": "Belief"}) as client:
        assert client.resolve_id(UuidRef(uuid=row, expected_kind="Belief")) == (
            "Belief",
            row,
        )
        with pytest.raises(ClientError, match="resolves to a Belief row"):
            _ = client.resolve_id(UuidRef(uuid=row, expected_kind="Issue"))


def test_append_records_with_no_arguments_sends_a_bare_slash_command_free_body() -> (
    None
):
    sent: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json_loads(request.content, dict[str, object]))
        return httpx2.Response(200, json={"part": None, "written": 0, "skipped": 0})

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        _ = client.append_records(uuid.uuid4())

    assert sent == [
        {
            "name": "",
            "manifest": None,
            "restart": False,
            "records": [],
            "slash_commands": [],
        },
    ]


def test_append_records_posts_the_file_its_manifest_records_and_flags() -> None:
    session = uuid.uuid4()
    sent: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(
            (
                request.method,
                request.url.path,
                json_loads(request.content, dict[str, object]),
            ),
        )
        return httpx2.Response(200, json={"part": 2, "written": 1, "skipped": 0})

    manifest = ManifestBody(name="a.jsonl", ir_id=uuid.uuid4(), records=1)
    when = datetime(2026, 10, 3, tzinfo=UTC)
    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        response = client.append_records(
            session,
            name="a.jsonl",
            manifest=manifest,
            records=[RecordBody(idx=0, kind="UserMessage")],
            restart=True,
            slash_commands=[SlashCommandBody(timestamp=when, command="exit")],
        )

    assert (response.part, response.written, response.skipped) == (2, 1, 0)
    [(method, path, body)] = sent
    assert (method, path) == ("POST", f"/api/sessions/{session}/records")
    assert body["name"] == "a.jsonl"
    assert body["restart"] is True
    assert from_plain(body["manifest"], dict[str, object])["name"] == "a.jsonl"
    assert [
        row["idx"] for row in from_plain(body["records"], list[dict[str, object]])
    ] == [0]
    assert [
        row["command"]
        for row in from_plain(body["slash_commands"], list[dict[str, object]])
    ] == [
        "exit",
    ]


@pytest.mark.parametrize(
    ("status", "payload", "message"),
    [
        (422, {"detail": "no"}, r"POST /api/sessions/[0-9a-f-]+/records -> 422"),
        (200, {"written": "many"}, r"/api/sessions/[0-9a-f-]+/records returned a mal"),
    ],
)
def test_append_records_names_the_verb_and_route_when_the_server_refuses_or_garbles(
    status: int,
    payload: PlainTree,
    message: str,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(status, json=payload)

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError, match=message):
            _ = client.append_records(uuid.uuid4())


def test_list_kind_sends_its_defaults_under_the_servers_parameter_names() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(parse_qsl(request.url.query.decode())))
        return httpx2.Response(200, json=[{"id": "a"}])

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        rows = client.list_kind("Issue", status="active", limit=7)

    assert rows == [{"id": "a"}]
    assert seen == [
        {"kind": "Issue", "status": "active", "limit": "7", "offset": "0"},
    ]


@pytest.mark.parametrize("payload", [{"rows": []}, [1]])
def test_list_kind_names_the_route_when_the_reply_is_malformed(
    payload: PlainTree,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json=payload)

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError, match=r"/api/inquiries returned a malformed"):
            _ = client.list_kind("Issue")


def _edge_post(
    *,
    created: bool,
    change_id: str | None,
) -> Callable[..., Mapping[str, object]]:
    """Return a fake ``post`` returning the edge route's ``{change_id, created}``."""

    def fake_post(path: str, *, body: object = None) -> Mapping[str, object]:
        del path, body
        return {"change_id": change_id, "created": created}

    return fake_post


@pytest.mark.parametrize("previous", [None, uuid.uuid4()])
def test_publish_artifact_content_posts_html_and_reads_the_revision(
    monkeypatch: pytest.MonkeyPatch,
    previous: uuid.UUID | None,
) -> None:
    """The route gets an HTML body; ``previous_id`` is sent only when given."""
    client = Client("http://example.com")
    issue_id, artifact_id = uuid.uuid4(), uuid.uuid4()
    posted: list[tuple[str, object]] = []

    def fake_post(path: str, *, body: object = None) -> Mapping[str, object]:
        posted.append((path, body))
        return {"artifact_id": str(artifact_id), "revision": 2, "title": "Mock"}

    monkeypatch.setattr(client, "post", fake_post)

    result = client.publish_artifact_content(
        issue_id,
        title="Mock",
        summary="Edges first.",
        html="<!doctype html><p>x</p>",
        previous_id=previous,
    )

    assert result == ArtifactRevision(artifact_id=artifact_id, revision=2)
    expected: dict[str, object] = {
        "issue_id": str(issue_id),
        "title": "Mock",
        "summary": "Edges first.",
        "format": "html",
        "html": "<!doctype html><p>x</p>",
    }
    if previous is not None:
        expected["previous_artifact_id"] = str(previous)
    assert posted == [("/api/artifacts/content", expected)]


def test_add_edge_reports_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """A brand-new edge -> ``EdgeWrite(created=True, changed=True)``."""
    client = Client("http://example.com")
    monkeypatch.setattr(
        client,
        "post",
        _edge_post(created=True, change_id=str(uuid.uuid4())),
    )
    result = client.add_edge(uuid.uuid4(), uuid.uuid4(), "requires", actor="a")
    assert result == EdgeWrite(created=True, changed=True)


def test_add_edge_existing_with_annotation_reports_changed_not_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing edge whose annotations were applied -> changed, not created.

    Creation is an upsert: the server applies the supplied annotation to the
    existing edge and emits a change. No 409, no error.
    """
    client = Client("http://example.com")
    monkeypatch.setattr(
        client,
        "post",
        _edge_post(created=False, change_id=str(uuid.uuid4())),
    )
    result = client.add_edge(
        uuid.uuid4(),
        uuid.uuid4(),
        "proves",
        actor="a",
        note="load-bearing",
    )
    assert result == EdgeWrite(created=False, changed=True)


def test_add_edge_existing_no_op_reports_neither(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare re-create of an existing edge is a no-op: ``change_id`` is None."""
    client = Client("http://example.com")
    monkeypatch.setattr(client, "post", _edge_post(created=False, change_id=None))
    result = client.add_edge(uuid.uuid4(), uuid.uuid4(), "requires", actor="a")
    assert result == EdgeWrite(created=False, changed=False)


def test_add_edge_clear_labels_threads_through_labels_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``labels=None`` clears the edge's labels via the per-field labels route.

    The upsert POST collapses an empty list to "unset" (the store cannot clear
    through it), so an explicit clear is threaded as a PUT to the labels route
    that writes NULL. The POST carries no ``labels`` key; the PUT carries the
    empty list (TRAX-CLI-004).
    """
    client = Client("http://example.com")
    monkeypatch.setattr(client, "post", _edge_post(created=False, change_id=None))
    puts: list[tuple[str, object]] = []

    def fake_put(path: str, *, body: object = None) -> Mapping[str, object]:
        puts.append((path, body))
        return {}

    monkeypatch.setattr(client, "put", fake_put)
    src, dst = uuid.uuid4(), uuid.uuid4()
    result = client.add_edge(src, dst, "requires", actor="a", labels=None)
    assert puts == [
        (f"/api/edges/{src}/requires/{dst}/labels", {"value": [], "actor": "a"}),
    ]
    assert result == EdgeWrite(created=False, changed=True)


def test_add_edge_reraises_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """``add_edge`` no longer special-cases any 409; transport errors raise."""
    client = Client("http://example.com")

    def fake_post(path: str, *, body: object = None) -> None:
        del path, body
        raise ClientError("POST /api/edges/... -> 500: oops")

    monkeypatch.setattr(client, "post", fake_post)
    with pytest.raises(ClientError, match="500"):
        client.add_edge(uuid.uuid4(), uuid.uuid4(), "edge", actor="a")


def test_submit_batch_rejects_body_kind_conflict() -> None:
    """A body ``kind`` that disagrees with the tuple kind is a hard error.

    ``submit_batch`` keys each item on its ``(kind, body)`` tuple. A body that
    also carries a conflicting ``kind`` must raise rather than be silently
    overwritten -- a silent overwrite would create the wrong inquiry kind from
    a caller's mistaken body.
    """
    client = _ClientSpy(post_result={"ids": []})
    with pytest.raises(ClientError, match="kind"):
        client.submit_batch(
            [("Issue", {"title": "x", "kind": "Belief"})],
        )
    # No request was issued -- the guard fires before the POST.
    assert client.request_calls == []


def test_submit_batch_accepts_matching_or_absent_body_kind() -> None:
    """A body kind equal to the tuple kind (or absent) is fine."""
    client = _ClientSpy(post_result={"ids": []})
    client.submit_batch(
        [
            ("Issue", {"title": "a"}),  # Absent body kind.
            ("Belief", {"title": "b", "kind": "Belief"}),  # Matching body kind.
        ],
    )
    body = from_plain(client.request_calls[0][2], dict[str, object])
    items = from_plain(body["items"], list[dict[str, object]])
    assert items[0]["kind"] == "Issue"
    assert items[1]["kind"] == "Belief"


def test_annotate_edge_accepts_metadata_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``annotate_edge`` issues one PUT per sent metadata field.

    The REST surface exposes one route per annotation field
    (``/api/edges/<from>/<kind>/<to>/<field>``); the method fans the
    kwargs out to a ``PUT`` carrying ``{"value": ..., "actor": ...}``
    for each field the caller actually supplied.
    """
    client = Client("http://example.com")
    captured: list[tuple[str, object]] = []

    def fake_put(path: str, *, body: object = None) -> None:
        captured.append((path, body))

    monkeypatch.setattr(client, "put", fake_put)
    src = uuid.uuid4()
    dst = uuid.uuid4()
    client.annotate_edge(
        src,
        dst,
        "edge",
        actor="alice",
        valence=0.9,
        labels=["alpha", "beta"],
    )
    base = f"/api/edges/{src}/edge/{dst}"
    assert captured == [
        (f"{base}/valence", {"value": 0.9, "actor": "alice"}),
        (f"{base}/labels", {"value": ["alpha", "beta"], "actor": "alice"}),
    ]


class TestSessionMethods:
    """The session-ingest client methods build the right requests and parse the.

    Responses, via a mock transport that inspects each call.
    """

    def test_session_start_posts_and_parses(self) -> None:
        sid = uuid.uuid4()
        seen: dict[str, object] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx2.Response(201, json={"id": str(sid), "seq": 3})

        client = Client("http://server")
        _install_mock_transport(client, handler)
        resp = client.session_start(SessionStart(cli="codex"))
        assert seen["path"] == "/api/sessions/start"
        body = from_plain(seen["body"], dict[str, object])
        assert body["cli"] == "codex"
        # A missing idempotency key is minted client-side.
        assert body["idempotency_key"] is not None
        assert resp.id == sid
        assert resp.seq == 3

    def test_append_records_posts_the_batch_and_parses_the_answer(self) -> None:
        sid = uuid.uuid4()
        ir_id = uuid.uuid4()
        client = _ClientSpy(
            post_result={"part": 2, "written": 1, "skipped": 4, "slash_commands": 1},
        )
        resp = client.append_records(
            sid,
            name="a.jsonl",
            manifest=ManifestBody(name="a.jsonl", ir_id=ir_id, records=1),
            records=[RecordBody(idx=0, kind="Turn")],
            restart=True,
            slash_commands=[
                SlashCommandBody(
                    timestamp=datetime(2026, 10, 6, tzinfo=UTC),
                    command="model",
                ),
            ],
        )
        [(method, path, raw)] = client.request_calls
        assert (method, path) == ("POST", f"/api/sessions/{sid}/records")
        body = from_plain(raw, dict[str, PlainTree])
        assert body["name"] == "a.jsonl"
        assert body["restart"] is True
        assert from_plain(body["manifest"], dict[str, PlainTree])["ir_id"] == str(ir_id)
        records = from_plain(body["records"], list[dict[str, PlainTree]])
        assert [r["idx"] for r in records] == [0]
        commands = from_plain(body["slash_commands"], list[dict[str, PlainTree]])
        assert [c["command"] for c in commands] == ["model"]
        assert (resp.part, resp.written, resp.skipped, resp.slash_commands) == (
            2,
            1,
            4,
            1,
        )

    def test_append_records_defaults_to_a_non_restarting_empty_batch(self) -> None:
        client = _ClientSpy(post_result={"written": 0, "skipped": 0})
        client.append_records(uuid.uuid4())
        [(_, _, raw)] = client.request_calls
        body = from_plain(raw, dict[str, PlainTree])
        assert (body["restart"], body["records"], body["slash_commands"]) == (
            False,
            [],
            [],
        )

    def test_append_records_names_the_route_for_a_malformed_answer(self) -> None:
        sid = uuid.uuid4()

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"part": 1})

        client = Client("http://server")
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError, match=f"/api/sessions/{sid}/records returned"):
            client.append_records(sid)

    def test_session_end_posts(self) -> None:
        sid = uuid.uuid4()
        seen: dict[str, object] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["path"] = request.url.path
            return httpx2.Response(200, json={"id": str(sid)})

        client = Client("http://server")
        _install_mock_transport(client, handler)
        resp = client.session_end(sid)
        assert seen["path"] == f"/api/sessions/{sid}/end"
        assert resp.id == sid

    def test_inbound_drain_preserves_canvas_context(self) -> None:
        session_id = uuid.uuid4()
        canvas_id = uuid.uuid4()
        record_id = uuid.uuid4()
        visual_id = uuid.uuid4()

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert request.url.path == f"/api/sessions/{session_id}/inbound"
            return httpx2.Response(
                200,
                json={
                    "messages": [
                        {
                            "text": "What led here?",
                            "source": "viewer@example.com",
                            "context": {
                                "canvas_id": str(canvas_id),
                                "agent_instructions": "Trace the evidence.",
                                "continuation_record_id": str(record_id),
                                "page": {
                                    "route": f"#/lookup/{record_id}",
                                    "record": {
                                        "id": str(record_id),
                                        "kind": "Issue",
                                        "seq": 21_706,
                                        "title": "ARC3 effort",
                                    },
                                },
                                "visible_visuals": [
                                    {"id": str(visual_id), "type": "trax.browse"},
                                ],
                            },
                        },
                    ],
                },
            )

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            drained = client.drain_inbound(session_id)

        [item] = drained
        assert (item.text, item.source, item.room, item.kind) == (
            "What led here?",
            "viewer@example.com",
            None,
            "line",
        )
        context = item.context
        assert context is not None
        assert context.model_dump(mode="json") == {
            "canvas_id": str(canvas_id),
            "agent_instructions": "Trace the evidence.",
            "continuation_record_id": str(record_id),
            "visible_visuals": [
                # A server from before params sends none; the drain must still read
                # the visual, as one with no narrowing, not drop the whole context.
                {
                    "id": str(visual_id),
                    "type": "trax.browse",
                    "record": None,
                    "params": {},
                },
            ],
            "conversation_id": None,
            "fork": None,
            "page": {
                "route": f"#/lookup/{record_id}",
                "record": {
                    "id": str(record_id),
                    "kind": "Issue",
                    "seq": 21_706,
                    "title": "ARC3 effort",
                },
            },
            "trail": [],
            "marks": [],
            "relay": None,
        }

    def test_inbound_drain_preserves_a_console_relay(self) -> None:
        session_id = uuid.uuid4()
        relay = {
            "targets": [{"actor": "lead-2", "room": None}],
            "text": "rerun the suite",
        }

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            context: dict[str, object] = {
                "canvas_id": str(uuid.uuid4()),
                "visible_visuals": [],
                "relay": relay,
            }
            message: dict[str, object] = {"text": "rerun the suite", "context": context}
            return httpx2.Response(200, json={"messages": [message]})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            [item] = client.drain_inbound(session_id)

        context = item.context
        assert context is not None
        assert context.relay is not None
        assert context.relay.model_dump(mode="json") == relay
        assert context.relay.line() == "→ lead-2: rerun the suite"

    @pytest.mark.parametrize("wait_sec", [0.0, 30.0])
    def test_inbound_drain_read_timeout_outlasts_the_hold(
        self,
        wait_sec: float,
    ) -> None:
        reads: list[float | None] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            reads.append(_read_timeout(request))
            return httpx2.Response(200, json={"messages": []})

        with Client("http://server", timeout_sec=5.0) as client:
            _install_mock_transport(client, handler)
            assert client.drain_inbound(uuid.uuid4(), wait_sec=wait_sec) == []

        [read] = reads
        assert (read is not None and read > wait_sec) if wait_sec else read == 5.0

    def test_inbound_drain_says_where_the_run_is_only_when_it_knows(self) -> None:
        queries: list[dict[str, str]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            queries.append(
                {
                    key: request.url.params.get_list(key)[0]
                    for key in ("wait_sec", "run", "machine", "cwd", "cli", "instance")
                    if request.url.params.get_list(key)
                },
            )
            return httpx2.Response(200, json={"messages": []})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            _ = client.drain_inbound(
                uuid.uuid4(),
                wait_sec=25.0,
                run="chat-1a2b3c4d",
                machine="gx10",
                cwd="~/work",
                cli="codex",
                instance="a" * 32,
            )
            _ = client.drain_inbound(uuid.uuid4(), machine="gx10")
            _ = client.drain_inbound(uuid.uuid4(), cli="claude")
            _ = client.drain_inbound(uuid.uuid4())

        assert queries == [
            {
                "wait_sec": "25.0",
                "run": "chat-1a2b3c4d",
                "machine": "gx10",
                "cwd": "~/work",
                "cli": "codex",
                "instance": "a" * 32,
            },
            {"machine": "gx10"},
            {"cli": "claude"},
            {},
        ]

    def test_get_sends_the_timeout_it_is_given(self) -> None:
        reads: list[float | None] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            reads.append(_read_timeout(request))
            return httpx2.Response(200, json={})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            _ = client.get("/api/x", timeout=42.0)

        assert reads == [42.0]

    def test_send_message_sends_the_body_an_older_server_accepts(self) -> None:
        """A server that predates ``asker`` forbids a field it does not know."""
        bodies: list[object] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            bodies.append(json_loads(request.content))
            return httpx2.Response(200, json={"delivered": []})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            _ = client.send_message("lead", "hi")
            _ = client.send_message("lead", "hi", room="ops")

        assert bodies == [
            {"actor": "lead", "room": None, "text": "hi"},
            {"actor": "lead", "room": "ops", "text": "hi"},
        ]

    def test_enqueue_inbound_sends_the_body_an_older_server_accepts(self) -> None:
        bodies: list[object] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            bodies.append(json_loads(request.content))
            return httpx2.Response(200, json={"queued": 1})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            _ = client.enqueue_inbound(uuid.uuid4(), "hi")

        assert bodies == [{"text": "hi"}]

    def test_enqueue_inbound_names_a_kind_and_its_payload(self) -> None:
        bodies: list[object] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            bodies.append(json_loads(request.content))
            return httpx2.Response(200, json={"queued": 1})

        answer: dict[str, object] = {"prompt_id": "p1", "decision": "allow"}
        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            _ = client.enqueue_inbound(uuid.uuid4(), "/compact", kind="command")
            _ = client.enqueue_inbound(
                uuid.uuid4(),
                "Yes",
                kind="answer",
                payload=answer,
            )

        assert bodies == [
            {"text": "/compact", "kind": "command"},
            {"text": "Yes", "kind": "answer", "payload": answer},
        ]

    def test_inbound_drain_returns_each_kind_and_payload(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(
                200,
                json={
                    "messages": [
                        {"text": "hello", "source": "ann@x"},
                        {"text": "interrupt", "kind": "interrupt"},
                        {
                            "text": "model",
                            "kind": "settings",
                            "payload": {"model": "opus", "effort": None},
                        },
                    ],
                },
            )

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            drained = client.drain_inbound(uuid.uuid4())

        assert [(item.kind, item.payload) for item in drained] == [
            ("line", None),
            ("interrupt", None),
            ("settings", {"model": "opus", "effort": None}),
        ]
        assert drained[0].source == "ann@x"


class TestExport:
    def test_yields_each_line_without_its_newline(self) -> None:
        body = b'{"format":"trackinizer-export"}\n\n{"table":"edges","row":{}}\n'
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, content=body)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            lines = list(client.export())

        assert lines == [
            '{"format":"trackinizer-export"}',
            '{"table":"edges","row":{}}',
        ]
        assert [(r.method, r.url.path) for r in seen] == [("GET", EXPORT_API_PATH)]

    def test_an_error_status_raises_client_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(401, json={"detail": "not authenticated"})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError) as err:
                list(client.export())

        assert err.value.status_code == 401
        assert "not authenticated" in str(err.value)

    def test_a_transport_failure_raises_client_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            raise httpx2.ConnectError("refused")

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="refused"):
                list(client.export())


class TestVariableMethods:
    """The variable methods build the right requests and parse the listing."""

    def test_list_variables_parses_plain_and_secret(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            row = {
                "layer": "org",
                "owner": "",
                "updated_by": "alice",
                "updated": "2026-10-06T12:00:00Z",
            }
            return httpx2.Response(
                200,
                json={
                    "variables": [
                        {**row, "name": "REGION", "secret": False, "value": "eu"},
                        {**row, "name": "TOKEN", "secret": True, "value": None},
                    ],
                },
            )

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            variables = client.list_variables()

        assert [(r.method, r.url.path) for r in seen] == [("GET", VARIABLES_PATH)]
        assert [(v.name, v.secret, v.value) for v in variables] == [
            ("REGION", False, "eu"),
            ("TOKEN", True, None),
        ]

    def test_list_variables_wraps_a_malformed_payload(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"variables": [{"name": "X"}]})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed payload"):
                client.list_variables()

    def test_put_variable_puts_the_body_and_accepts_no_content(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(204)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.put_variable("TOKEN", value="s3", secret=True) is None

        [request] = seen
        assert (request.method, request.url.path) == ("PUT", "/api/variables/TOKEN")
        assert json_loads(request.content) == {"value": "s3", "secret": True}

    def test_put_variable_escapes_the_name_in_the_path(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(204)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            client.put_variable("a/b", value="v", secret=False)

        assert str(seen[0].url).endswith("/api/variables/a%2Fb")

    def test_put_variable_wraps_an_empty_value_without_echoing_it(self) -> None:
        with Client("https://server") as client, pytest.raises(ClientError) as err:
            client.put_variable("TOKEN", value="", secret=True)
        assert "TOKEN" in str(err.value)
        assert err.value.status_code is None

    def test_delete_variable_deletes_and_reports_absence(self) -> None:
        statuses = iter((204, 404))
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(next(statuses), json={"detail": "no such variable"})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.delete_variable("REGION") is None
            with pytest.raises(ClientError) as err:
                client.delete_variable("REGION")

        assert [(r.method, r.url.path) for r in seen] == [
            ("DELETE", "/api/variables/REGION"),
        ] * 2
        assert err.value.status_code == 404


_CODE_SHA = "ab" * 32
_CODE_HEAD = {
    "parent_record_id": None,
    "private": False,
    "file_count": 1,
    "total_bytes": 3,
    "partial": "",
    "updated_at": "2026-10-15T12:00:00Z",
}


class TestCodeMethods:
    """The code-store methods build the right requests and parse the answers."""

    def test_code_missing_asks_under_the_lowercase_owner(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"missing": [_CODE_SHA]})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            missing = client.code_missing("Ada@X.Y", hashes=[_CODE_SHA])

        [request] = seen
        assert (request.method, request.url.path) == ("POST", CODE_MISSING_PATH)
        assert json_loads(request.content) == {
            "owner": "ada@x.y",
            "sha256": [_CODE_SHA],
        }
        assert missing == [_CODE_SHA]

    def test_code_put_blobs_sends_each_hash_with_its_text(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"stored": 1})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            stored = client.code_put_blobs("a@x.y", blobs={_CODE_SHA: "abc"})

        [request] = seen
        assert (request.method, request.url.path) == ("POST", CODE_BLOBS_PATH)
        assert json_loads(request.content) == {
            "owner": "a@x.y",
            "blobs": [{"sha256": _CODE_SHA, "text": "abc"}],
        }
        assert stored == 1

    def test_code_put_snapshot_puts_it_under_the_record_and_parses_the_head(
        self,
    ) -> None:
        record, parent = uuid.uuid4(), uuid.uuid4()
        seen: list[httpx2.Request] = []

        head = {**_CODE_HEAD, "record_id": str(record)}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json=head)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            head = client.code_put_snapshot(
                record,
                owner="a@x.y",
                private=True,
                parent_record_id=parent,
                files={"a.py": _CODE_SHA},
            )

        [request] = seen
        assert (request.method, request.url.path) == (
            "PUT",
            f"/api/code/records/{record}/snapshot",
        )
        assert json_loads(request.content) == {
            "owner": "a@x.y",
            "private": True,
            "parent_record_id": str(parent),
            "files": {"a.py": _CODE_SHA},
            "partial": "",
        }
        assert (head.record_id, head.file_count) == (record, 1)

    def test_code_snapshot_is_none_on_404_and_raises_on_any_other_refusal(
        self,
    ) -> None:
        record = uuid.uuid4()
        answers = iter(
            (
                httpx2.Response(
                    200,
                    json={
                        **_CODE_HEAD,
                        "record_id": str(record),
                        "files": [{"path": "a.py", "sha256": _CODE_SHA, "size": 3}],
                    },
                ),
                httpx2.Response(404, json={"detail": "No code snapshot"}),
                httpx2.Response(403, json={"detail": "role too low"}),
            ),
        )

        path = f"/api/code/records/{record}/snapshot"

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert (request.method, request.url.path) == ("GET", path)
            return next(answers)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            snapshot = client.code_snapshot(record)
            absent = client.code_snapshot(record)
            with pytest.raises(ClientError) as err:
                client.code_snapshot(record)

        assert snapshot is not None
        assert [(f.path, f.size) for f in snapshot.files] == [("a.py", 3)]
        assert absent is None
        assert err.value.status_code == 403

    def test_a_malformed_hash_is_refused_before_sending(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"sent {request.url}")

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="sha256") as err:
                client.code_missing("a@x.y", hashes=["not-a-hash"])

        assert err.value.status_code is None


_MACHINE_ROW = {
    "name": "gpu-box",
    "role": "dev",
    "how": "ssh gpu-box",
    "labels": ["gpu", "a100"],
    "updated_by": "alice",
    "updated": "2026-10-06T12:00:00Z",
}


class TestMachineMethods:
    """The machine methods build the right requests and parse the answers."""

    def test_list_machines_parses_the_listing(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"machines": [_MACHINE_ROW]})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            machines = client.list_machines()

        assert [(r.method, r.url.path) for r in seen] == [("GET", MACHINES_PATH)]
        assert [(m.name, m.role, m.labels) for m in machines] == [
            ("gpu-box", "dev", ["gpu", "a100"]),
        ]

    def test_list_machines_wraps_a_malformed_payload(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"machines": [{"name": "x"}]})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed payload"):
                client.list_machines()

    def test_get_machine_reads_one_and_reports_absence(self) -> None:
        answers = iter(
            (
                httpx2.Response(200, json=_MACHINE_ROW),
                httpx2.Response(404, json={"detail": "no such machine"}),
            ),
        )
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return next(answers)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            machine = client.get_machine("gpu-box")
            with pytest.raises(ClientError) as err:
                client.get_machine("gpu-box")

        assert (machine.name, machine.how) == ("gpu-box", "ssh gpu-box")
        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", MACHINE_PATH.format(name="gpu-box")),
        ] * 2
        assert err.value.status_code == 404

    def test_put_machine_sends_only_the_fields_it_names(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(204)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.put_machine("gpu-box", role="dev") is None
            client.put_machine("gpu-box", how="")
            client.put_machine("gpu-box", role="", how="ssh gpu-box")

        assert [(r.method, r.url.path) for r in seen] == [
            ("PUT", "/api/machines/gpu-box"),
        ] * 3
        assert [json_loads(r.content) for r in seen] == [
            {"role": "dev"},
            {"how": ""},
            {"role": "", "how": "ssh gpu-box"},
        ]

    def test_put_machine_escapes_the_name_in_the_path(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(204)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            client.put_machine("a/b", role="dev")

        assert str(seen[0].url).endswith("/api/machines/a%2Fb")

    @pytest.mark.parametrize(
        ("role", "how", "expected"),
        [
            ("Dev", None, "role"),
            (None, "x" * (MAX_HOW_CHARS + 1), "how"),
            (None, "a\x00b", "how"),
        ],
        ids=["role-pattern", "how-too-long", "how-nul"],
    )
    def test_put_machine_refuses_a_bad_field_before_any_request(
        self,
        role: str | None,
        how: str | None,
        expected: str,
    ) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(204)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match=f"^machine 'gpu-box' {expected}: "):
                client.put_machine("gpu-box", role=role, how=how)

        assert seen == []

    def test_change_machine_labels_patches_both_lists(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(204)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.change_machine_labels("gpu-box", add=["gpu"]) is None
            client.change_machine_labels("gpu-box", remove=("a100",))

        assert [(r.method, r.url.path) for r in seen] == [
            ("PATCH", "/api/machines/gpu-box/labels"),
        ] * 2
        assert [json_loads(r.content) for r in seen] == [
            {"add": ["gpu"], "remove": []},
            {"add": [], "remove": ["a100"]},
        ]

    def test_delete_machine_deletes_and_reports_absence(self) -> None:
        statuses = iter((204, 404))
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(next(statuses), json={"detail": "no such machine"})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.delete_machine("gpu-box") is None
            with pytest.raises(ClientError) as err:
                client.delete_machine("gpu-box")

        assert [(r.method, r.url.path) for r in seen] == [
            ("DELETE", "/api/machines/gpu-box"),
        ] * 2
        assert err.value.status_code == 404


_INSTANCE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_MACHINE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ENROLLMENT_TOKEN = "enr_" + "0" * 32 + "_" + "S" * 43
_CREDENTIAL = "trax_machine_" + "1" * 32 + "_" + "C" * 43


class TestMachineHostMethods:
    """Enroll, join, heartbeat and revoke build the right requests."""

    def test_enroll_machine_posts_the_name_and_parses_the_token(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(
                201,
                json={
                    "token": _ENROLLMENT_TOKEN,
                    "expires_at": "2026-10-07T12:15:00Z",
                    "owner": "ada@x.y",
                },
            )

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            enrolled = client.enroll_machine("gpu-box")

        assert [(r.method, r.url.path) for r in seen] == [("POST", ENROLL_PATH)]
        assert json_loads(seen[0].content) == {"name": "gpu-box"}
        assert enrolled.token == _ENROLLMENT_TOKEN
        assert enrolled.owner == "ada@x.y"

    def test_join_machine_sends_once_and_parses_the_credential(self) -> None:
        seen: list[httpx2.Request] = []
        statuses = iter((503, 201))

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            status = next(statuses)
            return httpx2.Response(
                status,
                json={"machine_id": str(_MACHINE_ID), "credential": _CREDENTIAL},
            )

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError) as err:
                client.join_machine(
                    "gpu-box",
                    token=_ENROLLMENT_TOKEN,
                    instance=_INSTANCE,
                    host_version="0.1",
                    facts={"os": "linux", "cpus": 8},
                )

        # A retry after a lost answer would find the one-use token spent.
        assert len(seen) == 1
        assert err.value.status_code == 503
        assert [(r.method, r.url.path) for r in seen] == [("POST", JOIN_PATH)]
        assert json_loads(seen[0].content) == {
            "name": "gpu-box",
            "token": _ENROLLMENT_TOKEN,
            "instance": str(_INSTANCE),
            "host_version": "0.1",
            "facts": {"os": "linux", "cpus": 8},
        }

    def test_join_machine_parses_the_machine_id_and_credential(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(
                201,
                json={"machine_id": str(_MACHINE_ID), "credential": _CREDENTIAL},
            )

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            joined = client.join_machine(
                "gpu-box",
                token=_ENROLLMENT_TOKEN,
                instance=_INSTANCE,
                host_version="0.1",
                facts={},
            )

        assert (joined.machine_id, joined.credential) == (_MACHINE_ID, _CREDENTIAL)

    def test_join_machine_refuses_a_bad_field_without_quoting_the_token(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"no request expected, got {request.url}")

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="facts") as err:
                client.join_machine(
                    "gpu-box",
                    token=_ENROLLMENT_TOKEN,
                    instance=_INSTANCE,
                    host_version="0.1",
                    facts={"Bad Key": "x"},
                )

        assert _ENROLLMENT_TOKEN not in str(err.value)
        assert "S" * 43 not in repr(err.value.__cause__)

    def test_join_machine_never_quotes_a_malformed_answer(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(
                201,
                json={"machine_id": "x", "credential": _CREDENTIAL},
            )

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed payload") as err:
                client.join_machine(
                    "gpu-box",
                    token=_ENROLLMENT_TOKEN,
                    instance=_INSTANCE,
                    host_version="0.1",
                    facts={},
                )

        assert "C" * 43 not in str(err.value)

    def test_heartbeat_machine_posts_to_the_machines_path_with_the_credential(
        self,
    ) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"server_time": "2026-10-07T12:00:00Z"})

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            beat = client.heartbeat_machine(
                _MACHINE_ID,
                instance=_INSTANCE,
                host_version="0.1",
            )
            client.heartbeat_machine(
                _MACHINE_ID,
                instance=_INSTANCE,
                host_version="0.1",
                facts={"cpus": 4},
            )
            client.heartbeat_machine(
                _MACHINE_ID,
                instance=_INSTANCE,
                host_version="",
                claim="take",
            )

        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", HEARTBEAT_PATH.format(machine_id=_MACHINE_ID)),
        ] * 3
        assert seen[0].headers["Authorization"] == f"Bearer {_CREDENTIAL}"
        # ``keep`` is the default and stays off the wire; an empty version does not.
        assert [json_loads(r.content) for r in seen] == [
            {"instance": str(_INSTANCE), "host_version": "0.1"},
            {"instance": str(_INSTANCE), "host_version": "0.1", "facts": {"cpus": 4}},
            {"instance": str(_INSTANCE), "host_version": "", "claim": "take"},
        ]
        assert beat.server_time.year == 2026

    def test_wait_terminals_asks_with_the_instance_and_reads_a_terminal_or_null(
        self,
    ) -> None:
        seen: list[httpx2.Request] = []
        answers: list[object] = [
            None,
            {
                "terminal_id": str(_MACHINE_ID),
                "opener": "ada@x.y",
                "rows": 24,
                "cols": 80,
            },
        ]

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json=answers.pop(0))

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            none = client.wait_terminals(_MACHINE_ID, instance=_INSTANCE, wait_sec=25)
            found = client.wait_terminals(_MACHINE_ID, instance=_INSTANCE, wait_sec=25)

        assert none is None
        assert found is not None
        assert (found.opener, found.rows, found.canvas) == ("ada@x.y", 24, None)
        assert seen[0].url.path == (f"/api/machines/{_MACHINE_ID}/terminals/wait")
        assert seen[0].url.params["instance"] == str(_INSTANCE)
        assert seen[0].url.params["wait_sec"] == "25"
        assert seen[0].headers["Authorization"] == f"Bearer {_CREDENTIAL}"

    def test_heartbeat_machine_surfaces_a_revoked_credential_as_410(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(410, json={"detail": "machine_revoked"})

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError) as err:
                client.heartbeat_machine(
                    _MACHINE_ID,
                    instance=_INSTANCE,
                    host_version="0.1",
                )

        assert err.value.status_code == 410

    def test_heartbeat_machine_refuses_bad_facts_before_any_request(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"no request expected, got {request.url}")

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="facts"):
                client.heartbeat_machine(
                    _MACHINE_ID,
                    instance=_INSTANCE,
                    host_version="0.1",
                    facts=cast("Facts", cast("object", {"a": 1.5})),
                )

    def test_revoke_machine_posts_to_the_name_and_reports_absence(self) -> None:
        statuses = iter((204, 404))
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(next(statuses), json={"detail": "no such machine"})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.revoke_machine("gpu-box") is None
            with pytest.raises(ClientError) as err:
                client.revoke_machine("gpu-box")

        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", "/api/machines/gpu-box/revoke"),
        ] * 2
        assert err.value.status_code == 404

    def test_a_machines_runs_are_read_and_disconnect_returns_those_it_stopped(
        self,
    ) -> None:
        run = {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "cli": "claude",
            "run": "chat-11111111",
            "cwd": "~/w",
            "state": "idle",
        }
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"runs": [run]})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            listed = client.machine_runs("gpu-box")
            stopped = client.disconnect_machine("gpu-box")

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", "/api/machines/gpu-box/runs"),
            ("POST", "/api/machines/gpu-box/disconnect"),
        ]
        assert [each.run for each in listed] == [each.run for each in stopped]
        assert listed[0].run == "chat-11111111"


class TestTerminalMethods:
    """The terminal methods build the right requests and parse the answers."""

    def test_terminal_machines_asks_for_a_canvas_only_when_named(self) -> None:
        seen: list[httpx2.Request] = []
        canvas = uuid.UUID("22222222-2222-2222-2222-222222222222")
        listing = {
            "machines": [
                {
                    "name": "gx10",
                    "status": "online",
                    "terminal_ready": True,
                    "confirmed": True,
                    "clis": ["claude", "codex"],
                },
            ],
            "terminals": list[PlainTree](),
        }

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json=listing)

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            named = client.terminal_machines(canvas=canvas)
            bare = client.terminal_machines()

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", TERMINAL_MACHINES_PATH),
        ] * 2
        assert seen[0].url.params["canvas"] == str(canvas)
        assert "canvas" not in seen[1].url.params
        assert [(m.name, m.terminal_ready, m.clis) for m in named.machines] == [
            ("gx10", True, ["claude", "codex"]),
        ]
        assert bare.terminals == []

    def test_terminal_machines_wraps_a_malformed_payload(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"machines": [{"name": "gx10"}]})

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed payload"):
                client.terminal_machines()

    def test_open_terminal_sends_the_run_fields_and_reads_the_ticket(self) -> None:
        seen: list[httpx2.Request] = []
        canvas = uuid.UUID("22222222-2222-2222-2222-222222222222")

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(
                201,
                json={"terminal_id": str(_MACHINE_ID), "ticket": "ticket-1"},
            )

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            opened = client.open_terminal(
                OpenTerminal(
                    machine="gx10",
                    rows=40,
                    cols=120,
                    canvas=canvas,
                    command="claude",
                    attach=_INSTANCE,
                    run_name="campaign-issue-7",
                    cwd="~/work",
                ),
            )

        assert [(r.method, r.url.path) for r in seen] == [("POST", TERMINALS_PATH)]
        assert json_loads(seen[0].content) == {
            "machine": "gx10",
            "rows": 40,
            "cols": 120,
            "canvas": str(canvas),
            "requested_by": None,
            "surface": None,
            "command": "claude",
            "attach": str(_INSTANCE),
            "run_name": "campaign-issue-7",
            "cwd": "~/work",
        }
        assert (opened.terminal_id, opened.ticket) == (_MACHINE_ID, "ticket-1")

    def test_open_terminal_surfaces_a_refusal_with_its_status_and_code(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(
                403,
                json={"detail": {"code": "terminal_forbidden", "message": "no"}},
            )

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError) as err:
                client.open_terminal(OpenTerminal(machine="gx10", rows=40, cols=120))

        assert (err.value.status_code, err.value.code) == (
            403,
            "terminal_forbidden",
        )


class TestMetricQueries:
    def test_query_metrics_sends_offset(self) -> None:
        """The query route pages by ``offset``; the client must carry it."""
        sent: list[dict[str, object]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(json_loads(request.content, dict[str, object]))
            return httpx2.Response(200, json={"points": []})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            client.query_metrics(
                uuid.uuid4(),
                masks=[],
                sort="asc",
                limit=1,
                offset=1,
            )
        assert [(body["sort"], body["limit"], body["offset"]) for body in sent] == [
            ("asc", 1, 1),
        ]

    @pytest.mark.parametrize(
        "experiments",
        [
            [],
            [uuid.uuid4(), Filter(field="labels", op="is", value="x")],
        ],
        ids=["empty", "ids-and-filters"],
    )
    def test_rank_metrics_refuses_a_bad_selection_as_a_client_error(
        self,
        experiments: list[uuid.UUID] | list[Filter],
    ) -> None:
        """Nothing to rank, or two selections at once, is refused before sending."""
        sent: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(request)
            return httpx2.Response(200, json={"rows": []})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="metric rank"):
                client.rank_metrics(experiments, masks=[])
        assert sent == []


_JOB_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPtWmP3ce7m3hrwpxvm9x5s3WnCClGsA5R5nJ9J25ir1"
)
_JOB_CERT = "ssh-ed25519-cert-v01@openssh.com " + "CERTBODY" * 6
_JOB_COMMIT = "a" * 40
_JOB_VIEW: dict[str, object] = {
    "id": 7,
    "state": "queued",
    "node": "gpu-node-b",
    "gpus": 1,
    "minutes": 5,
    "label": "t",
    "argv": ["python", "x.py"],
    "commit": _JOB_COMMIT,
    "bundle_sha256": None,
    "created_at": "2026-10-21T12:00:00Z",
    "started_at": None,
    "ended_at": None,
    "gpu_minutes": None,
    "usd": None,
    "reason": "",
    "upload_serial": 3,
}
_JOB_GRANT: dict[str, object] = {
    "job_id": 7,
    "serial": 3,
    "role": "rw",
    "node": {
        "name": "gpu-node-b",
        "host": "203.0.113.7",
        "port": 22,
        "user": "gj7",
        "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPtWmP3ce7m3hrwpxvm9x5s3WnCClGsA5R5nJ9J25ir1",
    },
    "certificate": _JOB_CERT,
    "valid_before": "2026-10-21T13:00:00Z",
    "max_bundle_bytes": 1_000_000,
}


def _job_client(
    seen: list[httpx2.Request],
    *answers: httpx2.Response,
) -> Client:
    queue = list(answers)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return queue.pop(0) if queue else httpx2.Response(200, json={})

    client = Client("https://server")
    _install_mock_transport(client, handler)
    return client


class TestJobMethods:
    """Each ``job_*`` method hits its Appendix A path with the right body."""

    def test_submit_posts_the_body_once_and_parses_the_job(self) -> None:
        seen: list[httpx2.Request] = []
        body = SubmitBody(
            public_key=_JOB_KEY,
            gpus=1,
            minutes=5,
            label="t",
            argv=["python", "x.py"],
            commit=_JOB_COMMIT,
        )
        with _job_client(
            seen,
            httpx2.Response(503, json={}),
            httpx2.Response(201, json=_JOB_VIEW),
        ) as client:
            with pytest.raises(ClientError) as err:
                _ = client.job_submit(body)
            job = client.job_submit(body)

        # A retry after a lost answer would hold the credit twice.
        assert err.value.status_code == 503
        assert [(r.method, r.url.path) for r in seen] == [("POST", JOBS_PATH)] * 2
        assert json_loads(seen[0].content) == {
            "public_key": _JOB_KEY,
            "gpus": 1,
            "minutes": 5,
            "label": "t",
            "argv": ["python", "x.py"],
            "commit": _JOB_COMMIT,
        }
        assert (job.id, job.state, job.upload_serial) == (7, "queued", 3)

    def test_jobs_and_job_read_the_callers_jobs(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(
            seen,
            httpx2.Response(200, json={"jobs": [_JOB_VIEW]}),
            httpx2.Response(200, json=_JOB_VIEW),
        ) as client:
            listing = client.job_list()
            one = client.job_view(7)

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", JOBS_PATH),
            ("GET", JOB_PATH.format(job_id=7)),
        ]
        assert [job.id for job in listing.jobs] == [7]
        assert one.node == "gpu-node-b"

    def test_a_malformed_job_is_wrapped(self) -> None:
        seen: list[httpx2.Request] = []
        with (
            _job_client(seen, httpx2.Response(200, json={"id": 1})) as client,
            pytest.raises(ClientError, match="malformed payload"),
        ):
            _ = client.job_view(7)

    def test_grant_long_polls_with_a_read_timeout_past_the_wait(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(
            seen,
            httpx2.Response(200, json={"state": "pending"}),
            httpx2.Response(200, json={"state": "ready", "grant": _JOB_GRANT}),
        ) as client:
            pending = client.job_grant(7, wait=25)
            ready = client.job_grant(7, wait=25)

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", JOB_GRANT_PATH.format(job_id=7)),
        ] * 2
        assert seen[0].url.params["wait"] == "25"
        assert _read_timeout(seen[0]) == 35
        assert pending.grant is None
        assert ready.grant is not None
        assert ready.grant.node.user == "gj7"

    def test_read_grant_posts_the_key_and_returns_the_serial(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(seen, httpx2.Response(201, json={"serial": 9})) as client:
            ticket = client.job_read_grant(7, ReadGrantBody(public_key=_JOB_KEY))

        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", JOB_GRANTS_PATH.format(job_id=7)),
        ]
        assert json_loads(seen[0].content) == {"public_key": _JOB_KEY}
        assert ticket.serial == 9

    def test_read_grant_is_sent_once_too(self) -> None:
        seen: list[httpx2.Request] = []
        with (
            _job_client(seen, httpx2.Response(503, json={})) as client,
            pytest.raises(ClientError),
        ):
            _ = client.job_read_grant(7, ReadGrantBody(public_key=_JOB_KEY))

        assert len(seen) == 1

    def test_grant_serial_long_polls_the_ticket(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(
            seen,
            httpx2.Response(200, json={"state": "refused", "reason": "no room"}),
        ) as client:
            status = client.job_grant_serial(7, 9, wait=10)

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", JOB_GRANT_SERIAL_PATH.format(job_id=7, serial=9)),
        ]
        assert seen[0].url.params["wait"] == "10"
        assert _read_timeout(seen[0]) == 20
        assert (status.state, status.reason) == ("refused", "no room")

    def test_a_malformed_grant_never_quotes_the_certificate(self) -> None:
        seen: list[httpx2.Request] = []
        broken = {**_JOB_GRANT, "role": "admin"}
        with _job_client(
            seen,
            httpx2.Response(200, json={"state": "ready", "grant": broken}),
            httpx2.Response(200, json={"state": "ready", "grant": broken}),
        ) as client:
            with pytest.raises(ClientError, match="malformed payload") as first:
                _ = client.job_grant(7, wait=1)
            with pytest.raises(ClientError, match="malformed payload") as second:
                _ = client.job_grant_serial(7, 9, wait=1)

        for err in (first.value, second.value):
            assert "CERTBODY" not in str(err)
            assert err.__cause__ is None

    def test_start_posts_the_commit_and_bundle_hash(self) -> None:
        seen: list[httpx2.Request] = []
        body = StartBody(commit=_JOB_COMMIT, bundle_sha256="b" * 64, bundle_bytes=10)
        with _job_client(seen, httpx2.Response(200, json=_JOB_VIEW)) as client:
            job = client.job_start(7, body)

        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", JOB_START_PATH.format(job_id=7)),
        ]
        assert json_loads(seen[0].content) == {
            "commit": _JOB_COMMIT,
            "bundle_sha256": "b" * 64,
            "bundle_bytes": 10,
        }
        assert job.id == 7

    def test_cancel_posts_to_the_job(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(seen, httpx2.Response(200, json=_JOB_VIEW)) as client:
            job = client.job_cancel(7)

        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", JOB_CANCEL_PATH.format(job_id=7)),
        ]
        assert job.id == 7

    def test_secret_names_list_names_only(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(
            seen,
            httpx2.Response(200, json={"names": ["HF_TOKEN"]}),
        ) as client:
            names = client.job_secret_names()

        assert [(r.method, r.url.path) for r in seen] == [("GET", JOB_SECRETS_PATH)]
        assert names.names == ["HF_TOKEN"]

    def test_put_and_delete_a_secret(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(seen, httpx2.Response(204), httpx2.Response(204)) as client:
            assert client.job_put_secret("HF_TOKEN", "hf_value") is None
            assert client.job_delete_secret("HF_TOKEN") is None

        assert [(r.method, r.url.path) for r in seen] == [
            ("PUT", JOB_SECRET_PATH.format(name="HF_TOKEN")),
            ("DELETE", JOB_SECRET_PATH.format(name="HF_TOKEN")),
        ]
        assert json_loads(seen[0].content) == {"value": "hf_value"}

    def test_a_secret_name_is_escaped_in_the_path(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(seen, httpx2.Response(204)) as client:
            client.job_delete_secret("a/b")

        assert str(seen[0].url).endswith("/secrets/a%2Fb")

    def test_a_secret_value_is_refused_before_sending_without_quoting_it(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(seen) as client:
            with pytest.raises(ClientError, match="value") as err:
                client.job_put_secret("HF_TOKEN", "")
            with pytest.raises(ClientError, match="value") as long:
                client.job_put_secret("HF_TOKEN", "S" * 5_000)

        assert seen == []
        assert "S" * 20 not in str(long.value)
        assert err.value.__cause__ is None

    def test_a_server_error_that_echoes_the_secret_value_is_redacted(self) -> None:
        seen: list[httpx2.Request] = []
        echo = httpx2.Response(
            422,
            json={"detail": {"code": "bad_secret", "input": "hf_SECRETVALUE"}},
        )
        with (
            _job_client(seen, echo) as client,
            pytest.raises(ClientError) as err,
        ):
            client.job_put_secret("HF_TOKEN", "hf_SECRETVALUE")

        assert "hf_SECRETVALUE" not in str(err.value)
        assert (err.value.status_code, err.value.code) == (422, "bad_secret")
        assert err.value.__cause__ is None


_MACHINE_JOB_VIEW: dict[str, object] = {
    "id": 8,
    "machine": "gx10",
    "state": "running",
    "gpus": 1,
    "memory_mb": 2_048,
    "minutes": 5,
    "label": "",
    "argv": ["python", "x.py"],
    "remote": "https://github.com/o/r.git",
    "ref": "refs/heads/main",
    "commit": _JOB_COMMIT,
    "exit_code": None,
    "reason": "",
    "created_at": "2026-10-23T12:00:00Z",
    "queued_at": None,
    "started_at": None,
    "ended_at": None,
}
_CHUNK: dict[str, object] = {
    "reply": {
        "kind": "chunk",
        "data_b64": "aGk=",
        "offset": 4,
        "size": 6,
        "ended": True,
    },
}


class TestMachineJobMethods:
    """Each ``machine_job_*`` method and the host's three calls hit their paths."""

    def test_submit_posts_the_body_once_and_parses_the_job(self) -> None:
        seen: list[httpx2.Request] = []
        body = MachineJobSubmit(
            machine="gx10",
            argv=["python", "x.py"],
            gpus=1,
            memory_mb=2_048,
            minutes=5,
            remote="https://github.com/o/r.git",
            ref="refs/heads/main",
            commit=_JOB_COMMIT,
        )
        with _job_client(
            seen,
            httpx2.Response(503, json={}),
            httpx2.Response(201, json=_MACHINE_JOB_VIEW),
        ) as client:
            with pytest.raises(ClientError) as err:
                _ = client.machine_job_submit(body)
            job = client.machine_job_submit(body)

        # A retry after a lost answer would queue the job twice.
        assert err.value.status_code == 503
        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", MACHINE_JOBS_PATH),
        ] * 2
        assert json_loads(seen[0].content) == body.model_dump(mode="json")
        assert (job.id, job.machine, job.state) == (8, "gx10", "running")

    def test_list_view_and_cancel_hit_their_paths(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(
            seen,
            httpx2.Response(200, json={"jobs": [_MACHINE_JOB_VIEW]}),
            httpx2.Response(200, json=_MACHINE_JOB_VIEW),
            httpx2.Response(200, json=_MACHINE_JOB_VIEW | {"state": "cancelled"}),
        ) as client:
            listing = client.machine_job_list()
            one = client.machine_job_view(8)
            cancelled = client.machine_job_cancel(8)

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", MACHINE_JOBS_PATH),
            ("GET", MACHINE_JOB_PATH.format(job_id=8)),
            ("POST", MACHINE_JOB_CANCEL_PATH.format(job_id=8)),
        ]
        assert [job.id for job in listing.jobs] == [8]
        assert (one.remote, cancelled.state) == (
            "https://github.com/o/r.git",
            "cancelled",
        )

    def test_a_read_is_sent_once_with_a_timeout_past_the_relay_and_the_hold(
        self,
    ) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(
            seen,
            httpx2.Response(504, json={}),
            httpx2.Response(200, json=_CHUNK),
            httpx2.Response(
                200,
                json={"reply": {"kind": "manifest", "entries": [], "total_bytes": 0}},
            ),
            httpx2.Response(200, json={"reply": {"kind": "refused", "reason": "big"}}),
        ) as client:
            with pytest.raises(ClientError) as lost:
                _ = client.machine_job_log(8, offset=4, wait=20)
            log = client.machine_job_log(8, offset=4, wait=20)
            manifest = client.machine_job_manifest(8)
            refused = client.machine_job_file(8, "a/b.txt", offset=9)

        assert lost.value.status_code == 504
        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", MACHINE_JOB_LOG_PATH.format(job_id=8)),
            ("GET", MACHINE_JOB_LOG_PATH.format(job_id=8)),
            ("GET", MACHINE_JOB_MANIFEST_PATH.format(job_id=8)),
            ("GET", MACHINE_JOB_FILE_PATH.format(job_id=8)),
        ]
        assert dict(seen[1].url.params) == {"offset": "4", "wait": "20"}
        assert dict(seen[3].url.params) == {"path": "a/b.txt", "offset": "9"}
        assert [_read_timeout(r) for r in seen] == [55, 55, 35, 35]
        assert log.reply.kind == "chunk"
        assert manifest.reply.kind == "manifest"
        assert refused.reply == ReadRefused(reason="big")

    def test_the_host_waits_reports_and_answers_on_its_own_paths(self) -> None:
        seen: list[httpx2.Request] = []
        machine, instance, read_id = uuid.UUID(int=1), uuid.UUID(int=2), uuid.uuid4()
        with _job_client(
            seen,
            httpx2.Response(200, json={"work": {"kind": "cancel", "job_id": 8}}),
            httpx2.Response(204),
            httpx2.Response(204),
        ) as client:
            offer = client.wait_machine_work(machine, instance=instance, wait_sec=25)
            client.report_machine_job(machine, 8, JobReport(state="running"))
            client.post_machine_read(machine, read_id, ReadReply.model_validate(_CHUNK))

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", MACHINE_WORK_PATH.format(machine_id=machine)),
            ("POST", MACHINE_JOB_REPORT_PATH.format(machine_id=machine, job_id=8)),
            (
                "POST",
                MACHINE_READ_REPLY_PATH.format(machine_id=machine, read_id=read_id),
            ),
        ]
        assert dict(seen[0].url.params) == {"instance": str(instance), "wait_sec": "25"}
        assert (_read_timeout(seen[0]) or 0) > 25
        assert offer.work == CancelWork(job_id=8)
        assert json_loads(seen[1].content) == {
            "state": "running",
            "exit_code": None,
            "reason": "",
        }
        assert json_loads(seen[2].content) == _CHUNK

    def test_a_host_wait_that_fails_is_not_retried(self) -> None:
        seen: list[httpx2.Request] = []
        with (
            _job_client(seen, httpx2.Response(502, json={})) as client,
            pytest.raises(ClientError),
        ):
            _ = client.wait_machine_work(
                uuid.UUID(int=1),
                instance=uuid.UUID(int=2),
                wait_sec=0,
            )

        assert len(seen) == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
