"""Tests for trackinizer CLI HTTP client."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict, cast, override
from urllib.parse import parse_qsl

import argparse
import base64
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
from trackinizer.trax.profile import Profile
from trackinizer.wire.filters import Filter
from trackinizer.wire.refs import SeqRef, UuidRef
from trackinizer.wire.routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    inquiry_field_path,
)
from trackinizer.wire.seq_ranges import SeqRange
from trackinizer.wire.wire_code import CODE_BLOBS_PATH, CODE_MISSING_PATH
from trackinizer.wire.wire_daemon import (
    ENROLL_PATH,
    HEARTBEAT_PATH,
    JOIN_PATH,
)
from trackinizer.wire.wire_export import EXPORT_API_PATH
from trackinizer.wire.wire_host_job import (
    HOST_JOB_CANCEL_PATH,
    HOST_JOB_FILE_PATH,
    HOST_JOB_LOG_PATH,
    HOST_JOB_MANIFEST_PATH,
    HOST_JOB_PATH,
    HOST_JOB_REPORT_PATH,
    HOST_JOBS_PATH,
    HOST_READ_REPLY_PATH,
    HOST_WORK_PATH,
    CancelWork,
    HostJobSubmit,
    JobReport,
    ReadRefused,
    ReadReply,
)
from trackinizer.wire.wire_hosts import (
    HOST_PATH,
    HOSTS_PATH,
    MAX_HOW_CHARS,
)
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
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_metrics_query import MetricMaskClause
from trackinizer.wire.wire_session_ir import (
    ManifestBody,
    RecordBody,
)
from trackinizer.wire.wire_sessions import SessionAgent, SessionStart
from trackinizer.wire.wire_terminals import (
    HOST_TERMINAL_WAIT_PATH,
    TERMINAL_HOSTS_CHANGES_PATH,
    TERMINAL_HOSTS_PATH,
    TERMINALS_PATH,
    HostsVersion,
    OpenTerminal,
)
from trackinizer.wire.wire_variables import VARIABLES_PATH


if TYPE_CHECKING:
    from trackinizer.wire.wire_hosts import Facts


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
        timeout: float | None = None,
        retry_attempts: int = 3,
    ) -> PlainTree:
        del timeout, retry_attempts
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


class TestFlags:
    @pytest.fixture(autouse=True)
    def _isolated_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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
    def test_injected_transport_receives_the_base_url_and_client_headers(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"ok": True})

        with Client(
            "https://server/root/",
            api_key="trax_fake",
            transport=httpx2.MockTransport(handler),
        ) as client:
            assert (client.author, client.api_key, client.timeout_sec) == (
                "",
                "trax_fake",
                30.0,
            )
            answer = client.get("/api/x")

        assert answer == {"ok": True}
        [request] = seen
        assert str(request.url) == "https://server/root/api/x"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Authorization"] == "Bearer trax_fake"
        assert (b"Accept", b"application/json") in request.headers.raw
        assert (b"Authorization", b"Bearer trax_fake") in request.headers.raw
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 30.0,
            "write": 10.0,
            "pool": 5.0,
        }

    def test_client_bounds_the_transport_connection_pool(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_limits = httpx2.Limits
        real_transport = httpx2.HTTPTransport
        observed: list[tuple[int, int, float]] = []
        transport_limits: list[httpx2.Limits] = []
        transport_retries: list[int] = []

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
            transport_retries.append(retries)
            return real_transport(retries=retries, limits=limits)

        monkeypatch.setattr(httpx2, "Limits", make_limits)
        monkeypatch.setattr(httpx2, "HTTPTransport", make_transport)

        with Client("https://server"):
            pass

        assert observed == [(8, 8, 90.0)]
        assert len(transport_limits) == 1
        assert transport_retries == [1]

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

    def test_post_timeout_and_single_attempt_are_forwarded(self) -> None:
        attempts: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            attempts.append(request)
            return httpx2.Response(503, content=b"down")

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="POST /x -> 503"):
                client.post("/x", timeout=1.0, retry_attempts=1)

        assert len(attempts) == 1
        assert attempts[0].extensions["timeout"] == {
            "connect": 1.0,
            "read": 1.0,
            "write": 1.0,
            "pool": 1.0,
        }

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

    @pytest.mark.parametrize("payload", [[], 7], ids=["list", "scalar"])
    def test_resolve_seq_rejects_non_mapping_responses(
        self,
        payload: PlainTree,
    ) -> None:
        with _answering(payload) as client, pytest.raises(ClientError) as raised:
            client.resolve_id(SeqRef(kind="Issue", seq=4))

        assert str(raised.value).startswith("/api/inquiries/Issue/4 returned")

    @pytest.mark.parametrize("payload", [[], 7], ids=["list", "scalar"])
    def test_submit_rejects_non_mapping_responses(
        self,
        payload: PlainTree,
    ) -> None:
        with _answering(payload) as client, pytest.raises(ClientError) as raised:
            client.submit("Issue", {"title": "x"})

        assert str(raised.value).startswith("/api/inquiries/issue returned")

    @pytest.mark.parametrize("found", [[], 7], ids=["list", "scalar"])
    def test_resolve_ids_rejects_non_mapping_found(
        self,
        found: PlainTree,
    ) -> None:
        with _answering({"found": found}) as client, pytest.raises(ClientError):
            client.resolve_ids([UuidRef(uuid=uuid.UUID(int=1))])

    @pytest.mark.parametrize(
        "ids",
        [None, {}, "00000000-0000-0000-0000-000000000001"],
        ids=["null", "mapping", "uuid-string"],
    )
    def test_submit_batch_rejects_non_list_ids(
        self,
        ids: PlainTree,
    ) -> None:
        with _answering({"ids": ids}) as client, pytest.raises(ClientError):
            client.submit_batch([("Issue", {"title": "x"})])

    @pytest.mark.parametrize("bad_id", [None, 7, "not-a-uuid"])
    def test_submit_batch_rejects_a_bad_id_after_a_valid_id(
        self,
        bad_id: PlainTree,
    ) -> None:
        ids = [str(uuid.UUID(int=1)), bad_id]
        with _answering({"ids": ids}) as client, pytest.raises(ClientError):
            client.submit_batch(
                [("Issue", {"title": "first"}), ("Issue", {"title": "second"})],
            )

    @pytest.mark.parametrize("bad_row", [None, [], 7], ids=["null", "list", "scalar"])
    def test_recent_changes_rejects_a_bad_row_after_a_valid_row(
        self,
        bad_row: PlainTree,
    ) -> None:
        with (
            _answering([{"id": "change"}, bad_row]) as client,
            pytest.raises(ClientError),
        ):
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
                {"agent_usd": 0.5},
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
        assert client.inquiry_cost(target_id, deep=True) == {"agent_usd": 1.0}
        assert client.inquiry_cost(target_id, deep=False) == {"agent_usd": 0.5}
        assert client.get_calls[-3] == ("/api/web/recent_changes", {"limit": 2})
        assert [
            (path, str((params or {}).get("deep", False)).casefold())
            for path, params in client.get_calls[-2:]
        ] == [
            (f"/api/inquiries/{target_id}/cost", "true"),
            (f"/api/inquiries/{target_id}/cost", "false"),
        ]

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
            assert offset == str((len(offsets) - 1) * MAX_LIST_LIMIT)
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
            assert request.url.params.get("offset") == str((calls - 1) * MAX_LIST_LIMIT)
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
        seq_id = uuid.uuid4()
        client = _ClientSpy(
            get_results=[{"id": str(seq_id)}],
            post_result={"found": {str(target_id): "Issue"}, "missing": []},
        )
        result = client.resolve_ids(
            [SeqRef(kind="Belief", seq=4), UuidRef(uuid=target_id)],
        )
        assert result == [("Belief", seq_id), ("Issue", target_id)]
        assert client.get_calls == [("/api/inquiries/Belief/4", None)]
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

    def test_a_fractional_cost_increase_sends_an_add_delta(self) -> None:
        target_id = uuid.uuid4()
        client = _ClientSpy(post_result={"ok": True})

        client.add_cost(target_id, "marginal_cost_agent_usd", 0.5, actor="alice")

        assert client.request_calls == [
            (
                "PATCH",
                f"/api/inquiries/{target_id}/marginal_cost_agent_usd",
                {"op": "add", "value": 0.5, "actor": "alice"},
            ),
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
        assert [body for _, _, body in client.request_calls[2:8]] == [
            {"op": "add", "value": "urgent", "actor": "alice"},
            {"op": "sub", "value": "urgent", "actor": "alice"},
            {"op": "add", "value": "bug", "actor": "alice"},
            {"op": "sub", "value": "bug", "actor": "alice"},
            {"op": "add", "value": str(codechange_id), "actor": "alice"},
            {"op": "sub", "value": str(codechange_id), "actor": "alice"},
        ]
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
# ``trax/variables_test.py``, and no ``trax`` command reads hosts or jobs, so the
# shared fake carries none of them. No ``trax`` command files code, so it holds no
# code-store method either. ``trax/canvas_test.py`` drives the canvas capture.
# The tool and exec waits are a daemon's, which no ``trax`` command is, and
# ``client/client_exec_test.py`` drives them. The session relay's calls are an agent
# process's and a daemon's, and ``client/client_session_relay_test.py`` drives those.
_FAKE_EXEMPT: frozenset[str] = frozenset(
    {"flags", "from_args", "get", "post", "put", "patch", "delete"}
    | {"list_variables", "put_variable", "delete_variable"}
    | {"list_hosts", "get_host", "put_host"}
    | {"change_host_labels", "delete_host"}
    | {"enroll_host", "join_host", "heartbeat_host", "revoke_host"}
    | {"host_runs", "stop_host_run", "disconnect_host", "logout_host"}
    | {"wait_terminals", "hosts_version"}
    | {"wait_tools", "wait_exec", "post_exec_result"}
    | {"start_process", "read_process", "stop_process", "send_input"}
    | {"open_lines", "wait_session", "report_process_ended"}
    | {"job_submit", "job_list", "job_view", "job_grant", "job_read_grant"}
    | {"job_grant_serial", "job_start", "job_cancel", "job_secret_names"}
    | {"job_put_secret", "job_delete_secret"}
    | {"host_job_submit", "host_job_list", "host_job_view"}
    | {"host_job_cancel", "host_job_log", "host_job_manifest"}
    | {"host_job_file", "wait_host_work", "report_host_job"}
    | {"post_host_read"}
    | {"capture_canvas"}
    | {"code_missing", "code_put_blobs", "code_put_snapshot", "code_snapshot"},
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


class _CanvasOperationBody(TypedDict):
    revision: int
    operation: dict[str, str]


def test_a_stale_canvas_operation_is_retried_once_on_the_live_revision() -> None:
    """A 409 re-reads the revision and resends with a fresh ``Idempotency-Key``."""
    canvas = uuid.uuid4()
    revisions = iter([3, 5])
    sent: list[tuple[int, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            assert request.url.path == f"/api/canvases/{canvas}"
            return httpx2.Response(
                200,
                json={"id": str(canvas), "revision": next(revisions)},
            )
        assert (request.method, request.url.path) == (
            "POST",
            f"/api/canvases/{canvas}/operations",
        )
        body = json_loads(request.content, _CanvasOperationBody)
        revision = body["revision"]
        assert body["operation"] == {"kind": "hide"}
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
    canvas, tile = uuid.uuid4(), uuid.uuid4()
    seen: list[tuple[str, str]] = []
    sent = {
        "width": 4,
        "height": 3,
        "mime": "image/png",
        "data": base64.b64encode(b"\x89PNG\r\n\x1a\n-").decode(),
        "tiles": [{"id": str(tile), "left": 0, "top": 0, "right": 1, "bottom": 1}],
        "added_later": True,
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path))
        return httpx2.Response(200, json=sent)

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        picture = client.capture_canvas(canvas)

    assert picture.picture == b"\x89PNG\r\n\x1a\n-"
    assert (picture.width, picture.height, picture.mime) == (4, 3, "image/png")
    assert [each.id for each in picture.tiles] == [tile]
    assert seen == [("POST", f"/api/canvases/{canvas}/captures")]


def test_a_capture_that_is_not_the_picture_it_claims_is_refused() -> None:
    canvas = uuid.uuid4()
    lie = base64.b64encode(b"GIF89a").decode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            200,
            json={"width": 4, "height": 3, "mime": "image/png", "data": lie},
        )

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError) as raised:
            client.capture_canvas(canvas)

    assert str(raised.value).startswith(
        f"/api/canvases/{canvas}/captures returned a malformed payload: ",
    )


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


def test_append_records_with_no_arguments_sends_a_bare_body() -> None:
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
            "records": [],
        },
    ]


def test_append_records_posts_the_file_its_manifest_and_records() -> None:
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
    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        response = client.append_records(
            session,
            name="a.jsonl",
            manifest=manifest,
            records=[RecordBody(idx=0, kind="UserMessage")],
        )

    assert (response.part, response.written, response.skipped) == (2, 1, 0)
    [(method, path, body)] = sent
    assert (method, path) == ("POST", f"/api/sessions/{session}/records")
    assert body["name"] == "a.jsonl"
    assert from_plain(body["manifest"], dict[str, object])["name"] == "a.jsonl"
    assert [
        row["idx"] for row in from_plain(body["records"], list[dict[str, object]])
    ] == [0]
    assert set(body) == {"name", "manifest", "records"}


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


@pytest.mark.parametrize("plaintext_only", [False, True], ids=["sealed", "plaintext"])
def test_read_session_records_pages_until_empty_and_preserves_controls(
    plaintext_only: bool,
) -> None:
    session = uuid.UUID(int=1)
    records = [
        {
            "idx": idx,
            "kind": "Thinking",
            "context_id": 2,
            "timestamp": f"2026-11-06T12:00:{idx:02d}Z",
            "model": f"model-{idx}",
            "payload": {"summary": [f"thought-{idx}", {"complete": True}]},
            "text": f"search-{idx}",
            "ciphertext": None if plaintext_only else f"sealed-{idx}",
        }
        for idx in (2, 5, 9, 14)
    ]
    pages = iter([records[:3], records[3:], []])
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json={"part": -1, "records": next(pages)})

    with Client("https://server", transport=httpx2.MockTransport(handler)) as client:
        found = client.read_session_records(
            session,
            part=-1,
            after_idx=1,
            limit=3,
            plaintext_only=plaintext_only,
        )

    assert all(isinstance(record, RecordBody) for record in found)
    assert [
        (
            record.idx,
            record.kind,
            record.context_id,
            record.timestamp,
            record.model,
            record.payload,
            record.text,
            record.ciphertext,
        )
        for record in found
    ] == [
        (
            idx,
            "Thinking",
            2,
            datetime(2026, 11, 6, 12, 0, idx, tzinfo=UTC),
            f"model-{idx}",
            {"summary": [f"thought-{idx}", {"complete": True}]},
            f"search-{idx}",
            None if plaintext_only else f"sealed-{idx}",
        )
        for idx in (2, 5, 9, 14)
    ]
    assert [(request.method, request.url.path) for request in sent] == [
        ("GET", f"/api/sessions/{session}/records"),
    ] * 3
    assert [dict(request.url.params) for request in sent] == [
        {
            "part": "-1",
            "after_idx": str(cursor),
            "limit": "3",
            "plaintext_only": str(plaintext_only),
        }
        for cursor in (1, 9, 14)
    ]


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
            post_result={"part": 2, "written": 1, "skipped": 4},
        )
        resp = client.append_records(
            sid,
            name="a.jsonl",
            manifest=ManifestBody(name="a.jsonl", ir_id=ir_id, records=1),
            records=[RecordBody(idx=0, kind="Turn")],
        )
        [(method, path, raw)] = client.request_calls
        assert (method, path) == ("POST", f"/api/sessions/{sid}/records")
        body = from_plain(raw, dict[str, PlainTree])
        assert body["name"] == "a.jsonl"
        assert from_plain(body["manifest"], dict[str, PlainTree])["ir_id"] == str(ir_id)
        records = from_plain(body["records"], list[dict[str, PlainTree]])
        assert [r["idx"] for r in records] == [0]
        assert (resp.part, resp.written, resp.skipped) == (2, 1, 4)

    def test_append_records_defaults_to_an_empty_batch(self) -> None:
        client = _ClientSpy(post_result={"written": 0, "skipped": 0})
        client.append_records(uuid.uuid4())
        [(_, _, raw)] = client.request_calls
        body = from_plain(raw, dict[str, PlainTree])
        assert (body["name"], body["records"]) == ("", [])

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

    def test_send_message_to_an_agent_names_its_session_and_the_sender_s_own(
        self,
    ) -> None:
        """An agent process's send carries its key, so a retry is sent once."""
        sent: list[tuple[object, str | None]] = []
        own, key = uuid.UUID(int=415), uuid.UUID(int=7)
        lead = SessionAgent(label="lead", session=412)

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert (request.method, request.url.path) == ("POST", "/api/messages")
            sent.append(
                (json_loads(request.content), request.headers.get("Idempotency-Key")),
            )
            return httpx2.Response(200, json={"delivered": [str(uuid.UUID(int=412))]})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            by_person = client.message_agent(lead, "hi")
            _ = client.message_agent(
                lead,
                "hi",
                from_session=own,
                from_label="Agent",
                booked="ask",
                key=key,
            )
            # The label is the sender's own, never a default.
            with pytest.raises(ValueError, match="from_label"):
                _ = client.message_agent(lead, "hi", from_session=own, booked="ask")

        assert by_person == [uuid.UUID(int=412)]
        assert [body for body, _ in sent] == [
            {"to": {"label": "lead", "session": 412}, "text": "hi"},
            {
                "to": {"label": "lead", "session": 412},
                "text": "hi",
                "from_session": str(own),
                "from_label": "Agent",
                "booked": "ask",
            },
        ]
        assert sent[1][1] == str(key)

    def test_both_sends_post_to_the_messages_route(self) -> None:
        lead = SessionAgent(label="lead", session=412)
        with _ClientSpy(post_result={"delivered": []}) as client:
            _ = client.send_message("lead", "hi")
            _ = client.message_agent(lead, "hi")

        assert [(method, path) for method, path, _ in client.request_calls] == [
            ("POST", "/api/messages"),
        ] * 2

    def test_a_send_answered_with_no_receipt_says_where_it_went(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"sent": []})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match=r"^/api/messages returned"):
                _ = client.message_agent(SessionAgent(label="lead", session=412), "hi")


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


_HOST_ROW = {
    "name": "gpu-box",
    "role": "dev",
    "how": "ssh gpu-box",
    "labels": ["gpu", "a100"],
    "updated_by": "alice",
    "updated": "2026-10-06T12:00:00Z",
}


class TestHostMethods:
    """The host methods build the right requests and parse the answers."""

    def test_list_hosts_parses_the_listing(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"hosts": [_HOST_ROW]})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            hosts = client.list_hosts()

        assert [(r.method, r.url.path) for r in seen] == [("GET", HOSTS_PATH)]
        assert [(m.name, m.role, m.labels) for m in hosts] == [
            ("gpu-box", "dev", ["gpu", "a100"]),
        ]

    def test_list_hosts_wraps_a_malformed_payload(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"hosts": [{"name": "x"}]})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed payload"):
                client.list_hosts()

    def test_a_field_a_newer_server_added_is_ignored_at_any_depth(self) -> None:
        answer = {"hosts": [{**_HOST_ROW, "rack": "b3"}], "cursor": "c1"}

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json=answer)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            hosts = client.list_hosts()

        assert [(m.name, m.labels) for m in hosts] == [("gpu-box", ["gpu", "a100"])]

    def test_a_credential_answer_with_an_added_field_still_parses(self) -> None:
        answer = {
            "host_id": str(_HOST_ID),
            "credential": _CREDENTIAL,
            "expires_at": None,
        }

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(201, json=answer)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            joined = client.join_host(
                "gpu-box",
                token=_ENROLLMENT_TOKEN,
                instance=_INSTANCE,
                daemon_version="0.1",
                facts={},
            )

        assert (joined.host_id, joined.credential) == (_HOST_ID, _CREDENTIAL)

    def test_get_host_reads_one_and_reports_absence(self) -> None:
        answers = iter(
            (
                httpx2.Response(200, json=_HOST_ROW),
                httpx2.Response(404, json={"detail": "no such host"}),
            ),
        )
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return next(answers)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            host = client.get_host("gpu-box")
            with pytest.raises(ClientError) as err:
                client.get_host("gpu-box")

        assert (host.name, host.how) == ("gpu-box", "ssh gpu-box")
        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", HOST_PATH.format(name="gpu-box")),
        ] * 2
        assert err.value.status_code == 404

    def test_put_host_sends_only_the_fields_it_names(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(204)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.put_host("gpu-box", role="dev") is None
            client.put_host("gpu-box", how="")
            client.put_host("gpu-box", role="", how="ssh gpu-box")

        assert [(r.method, r.url.path) for r in seen] == [
            ("PUT", "/api/hosts/gpu-box"),
        ] * 3
        assert [json_loads(r.content) for r in seen] == [
            {"role": "dev"},
            {"how": ""},
            {"role": "", "how": "ssh gpu-box"},
        ]

    def test_put_host_escapes_the_name_in_the_path(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(204)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            client.put_host("a/b", role="dev")

        assert str(seen[0].url).endswith("/api/hosts/a%2Fb")

    @pytest.mark.parametrize(
        ("role", "how", "expected"),
        [
            ("Dev", None, "role"),
            (None, "x" * (MAX_HOW_CHARS + 1), "how"),
            (None, "a\x00b", "how"),
        ],
        ids=["role-pattern", "how-too-long", "how-nul"],
    )
    def test_put_host_refuses_a_bad_field_before_any_request(
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
            with pytest.raises(ClientError, match=f"^host 'gpu-box' {expected}: "):
                client.put_host("gpu-box", role=role, how=how)

        assert seen == []

    def test_change_host_labels_patches_both_lists(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(204)

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.change_host_labels("gpu-box", add=["gpu"]) is None
            client.change_host_labels("gpu-box", remove=("a100",))

        assert [(r.method, r.url.path) for r in seen] == [
            ("PATCH", "/api/hosts/gpu-box/labels"),
        ] * 2
        assert [json_loads(r.content) for r in seen] == [
            {"add": ["gpu"], "remove": []},
            {"add": [], "remove": ["a100"]},
        ]

    def test_delete_host_deletes_and_reports_absence(self) -> None:
        statuses = iter((204, 404))
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(next(statuses), json={"detail": "no such host"})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.delete_host("gpu-box") is None
            with pytest.raises(ClientError) as err:
                client.delete_host("gpu-box")

        assert [(r.method, r.url.path) for r in seen] == [
            ("DELETE", "/api/hosts/gpu-box"),
        ] * 2
        assert err.value.status_code == 404


_INSTANCE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_HOST_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ENROLLMENT_TOKEN = "enr_" + "0" * 32 + "_" + "S" * 43
_CREDENTIAL = "trax_machine_" + "1" * 32 + "_" + "C" * 43


class TestHostHostMethods:
    """Enroll, join, heartbeat and revoke build the right requests."""

    def test_enroll_host_posts_the_name_and_parses_the_token(self) -> None:
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
            enrolled = client.enroll_host("gpu-box")

        assert [(r.method, r.url.path) for r in seen] == [("POST", ENROLL_PATH)]
        assert json_loads(seen[0].content) == {"name": "gpu-box"}
        assert enrolled.token == _ENROLLMENT_TOKEN
        assert enrolled.owner == "ada@x.y"

    def test_join_host_sends_once_and_parses_the_credential(self) -> None:
        seen: list[httpx2.Request] = []
        statuses = iter((503, 201))

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            status = next(statuses)
            return httpx2.Response(
                status,
                json={"host_id": str(_HOST_ID), "credential": _CREDENTIAL},
            )

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError) as err:
                client.join_host(
                    "gpu-box",
                    token=_ENROLLMENT_TOKEN,
                    instance=_INSTANCE,
                    daemon_version="0.1",
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
            "daemon_version": "0.1",
            "facts": {"os": "linux", "cpus": 8},
        }

    def test_join_host_parses_the_host_id_and_credential(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(
                201,
                json={"host_id": str(_HOST_ID), "credential": _CREDENTIAL},
            )

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            joined = client.join_host(
                "gpu-box",
                token=_ENROLLMENT_TOKEN,
                instance=_INSTANCE,
                daemon_version="0.1",
                facts={},
            )

        assert (joined.host_id, joined.credential) == (_HOST_ID, _CREDENTIAL)

    def test_join_host_refuses_a_bad_field_without_quoting_the_token(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"no request expected, got {request.url}")

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="facts") as err:
                client.join_host(
                    "gpu-box",
                    token=_ENROLLMENT_TOKEN,
                    instance=_INSTANCE,
                    daemon_version="0.1",
                    facts={"Bad Key": "x"},
                )

        assert _ENROLLMENT_TOKEN not in str(err.value)
        assert "S" * 43 not in repr(err.value.__cause__)

    def test_join_host_never_quotes_a_malformed_answer(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(
                201,
                json={"host_id": "x", "credential": _CREDENTIAL},
            )

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed payload") as err:
                client.join_host(
                    "gpu-box",
                    token=_ENROLLMENT_TOKEN,
                    instance=_INSTANCE,
                    daemon_version="0.1",
                    facts={},
                )

        assert "C" * 43 not in str(err.value)

    def test_heartbeat_host_posts_to_the_hosts_path_with_the_credential(
        self,
    ) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"server_time": "2026-10-07T12:00:00Z"})

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            beat = client.heartbeat_host(
                _HOST_ID,
                instance=_INSTANCE,
                daemon_version="0.1",
            )
            client.heartbeat_host(
                _HOST_ID,
                instance=_INSTANCE,
                daemon_version="0.1",
                facts={"cpus": 4},
            )
            client.heartbeat_host(
                _HOST_ID,
                instance=_INSTANCE,
                daemon_version="",
                claim="take",
            )

        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", HEARTBEAT_PATH.format(host_id=_HOST_ID)),
        ] * 3
        assert seen[0].headers["Authorization"] == f"Bearer {_CREDENTIAL}"
        # ``keep`` is the default and stays off the wire; an empty version does not.
        assert [json_loads(r.content) for r in seen] == [
            {"instance": str(_INSTANCE), "daemon_version": "0.1"},
            {"instance": str(_INSTANCE), "daemon_version": "0.1", "facts": {"cpus": 4}},
            {"instance": str(_INSTANCE), "daemon_version": "", "claim": "take"},
        ]
        assert beat.server_time.year == 2026

    def test_wait_terminals_asks_with_the_instance_and_reads_a_terminal_or_null(
        self,
    ) -> None:
        seen: list[httpx2.Request] = []
        answers: list[object] = [
            None,
            {
                "terminal_id": str(_HOST_ID),
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
            none = client.wait_terminals(_HOST_ID, instance=_INSTANCE, wait_sec=25)
            found = client.wait_terminals(_HOST_ID, instance=_INSTANCE, wait_sec=25)

        assert none is None
        assert found is not None
        assert (found.opener, found.rows, found.canvas) == ("ada@x.y", 24, None)
        assert seen[0].url.path == (f"/api/hosts/{_HOST_ID}/terminals/wait")
        assert seen[0].url.params["instance"] == str(_INSTANCE)
        assert seen[0].url.params["wait_sec"] == "25"
        assert seen[0].headers["Authorization"] == f"Bearer {_CREDENTIAL}"

    def test_a_terminal_wait_naming_a_field_the_host_does_not_know_still_opens(
        self,
    ) -> None:
        # Trackinizer 0.1.8 refused every wait once the server added ``canvas``.
        answer = {
            "terminal_id": str(_HOST_ID),
            "opener": "ada@x.y",
            "rows": 24,
            "cols": 80,
            "lane": "chat",
        }

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json=answer)

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            found = client.wait_terminals(_HOST_ID, instance=_INSTANCE, wait_sec=25)

        assert found is not None
        assert (found.terminal_id, found.opener) == (_HOST_ID, "ada@x.y")

    def test_heartbeat_host_surfaces_a_revoked_credential_as_410(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(410, json={"detail": "host_revoked"})

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError) as err:
                client.heartbeat_host(
                    _HOST_ID,
                    instance=_INSTANCE,
                    daemon_version="0.1",
                )

        assert err.value.status_code == 410

    def test_heartbeat_host_refuses_bad_facts_before_any_request(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"no request expected, got {request.url}")

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="facts"):
                client.heartbeat_host(
                    _HOST_ID,
                    instance=_INSTANCE,
                    daemon_version="0.1",
                    facts=cast("Facts", cast("object", {"a": 1.5})),
                )

    def test_revoke_host_posts_to_the_name_and_reports_absence(self) -> None:
        statuses = iter((204, 404))
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(next(statuses), json={"detail": "no such host"})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.revoke_host("gpu-box") is None
            with pytest.raises(ClientError) as err:
                client.revoke_host("gpu-box")

        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", "/api/hosts/gpu-box/revoke"),
        ] * 2
        assert err.value.status_code == 404

    def test_a_hosts_runs_are_read_and_disconnect_returns_those_it_stopped(
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
            listed = client.host_runs("gpu-box")
            stopped = client.disconnect_host("gpu-box")

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", "/api/hosts/gpu-box/runs"),
            ("POST", "/api/hosts/gpu-box/disconnect"),
        ]
        assert [each.run for each in listed] == [each.run for each in stopped]
        assert listed[0].run == "chat-11111111"

    def test_stopping_a_run_posts_to_its_host_and_session_and_reports_absence(
        self,
    ) -> None:
        session = uuid.UUID("11111111-1111-1111-1111-111111111111")
        statuses = iter((204, 404))
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(next(statuses), json={"detail": "no live session"})

        with Client("https://server") as client:
            _install_mock_transport(client, handler)
            assert client.stop_host_run("gpu-box", session) is None
            with pytest.raises(ClientError) as err:
                client.stop_host_run("gpu-box", session)

        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", f"/api/hosts/gpu-box/runs/{session}/stop"),
        ] * 2
        assert err.value.status_code == 404


class TestTerminalMethods:
    """The terminal methods build the right requests and parse the answers."""

    def test_terminal_hosts_asks_for_a_canvas_only_when_named(self) -> None:
        seen: list[httpx2.Request] = []
        canvas = uuid.UUID("22222222-2222-2222-2222-222222222222")
        listing = {
            "hosts": [
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
            named = client.terminal_hosts(canvas=canvas)
            bare = client.terminal_hosts()
            asked = client.terminal_hosts(owner="ada@x.y")

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", TERMINAL_HOSTS_PATH),
        ] * 3
        assert seen[0].url.params["canvas"] == str(canvas)
        assert "canvas" not in seen[1].url.params
        assert "owner" not in seen[1].url.params
        assert seen[2].url.params["owner"] == "ada@x.y"
        assert asked.hosts[0].tools is False
        assert [(m.name, m.terminal_ready, m.clis) for m in named.hosts] == [
            ("gx10", True, ["claude", "codex"]),
        ]
        assert bare.terminals == []

    def test_hosts_version_holds_past_its_wait_and_names_what_it_holds(
        self,
    ) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"version": 8})

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            first = client.hosts_version(owner="ada@x.y", since=None, wait_sec=25)
            held = client.hosts_version(since=7, wait_sec=25)

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", TERMINAL_HOSTS_CHANGES_PATH),
        ] * 2
        assert dict(seen[0].url.params) == {"owner": "ada@x.y", "wait_sec": "25"}
        assert dict(seen[1].url.params) == {"since": "7", "wait_sec": "25"}
        assert (_read_timeout(seen[1]) or 0) > 25
        assert (first, held) == (HostsVersion(version=8), HostsVersion(version=8))

    def test_terminal_hosts_wraps_a_malformed_payload(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            return httpx2.Response(200, json={"hosts": [{"name": "gx10"}]})

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed payload"):
                client.terminal_hosts()

    def test_open_terminal_sends_the_fields_given_and_reads_the_ticket(self) -> None:
        seen: list[httpx2.Request] = []
        canvas = uuid.UUID("22222222-2222-2222-2222-222222222222")

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(
                201,
                json={"terminal_id": str(_HOST_ID), "ticket": "ticket-1"},
            )

        with Client("https://server", api_key=_CREDENTIAL) as client:
            _install_mock_transport(client, handler)
            opened = client.open_terminal(
                OpenTerminal(
                    host="gx10",
                    rows=40,
                    cols=120,
                    canvas=canvas,
                    cwd="~/work",
                ),
            )

        assert [(r.method, r.url.path) for r in seen] == [("POST", TERMINALS_PATH)]
        assert json_loads(seen[0].content) == {
            "host": "gx10",
            "rows": 40,
            "cols": 120,
            "canvas": str(canvas),
            "cwd": "~/work",
        }
        assert (opened.terminal_id, opened.ticket) == (_HOST_ID, "ticket-1")

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
                client.open_terminal(OpenTerminal(host="gx10", rows=40, cols=120))

        assert (err.value.status_code, err.value.code) == (
            403,
            "terminal_forbidden",
        )


class TestMetricQueries:
    def test_query_metrics_preserves_the_mask_page_and_returned_points(self) -> None:
        experiment = uuid.UUID(int=7)
        sent: list[httpx2.Request] = []
        points = [
            {"key": "loss", "step": 3, "value": 0.25},
            {"key": "loss", "step": 7, "value": 0.5},
        ]

        with _recording(sent, httpx2.Response(200, json={"points": points})) as client:
            found = client.query_metrics(
                experiment,
                masks=[
                    MetricMaskClause(axis="key", op="is", value="loss"),
                    MetricMaskClause(axis="step", op="ge", value="3"),
                ],
                sort="asc",
                limit=2,
                offset=1,
            )

        assert [(point.key, point.step, point.value) for point in found] == [
            ("loss", 3, 0.25),
            ("loss", 7, 0.5),
        ]
        [request] = sent
        assert (request.method, request.url.path) == (
            "POST",
            f"/api/experiments/{experiment}/metrics/query",
        )
        assert json_loads(request.content) == {
            "masks": [
                {"axis": "key", "op": "is", "value": "loss"},
                {"axis": "step", "op": "ge", "value": "3"},
            ],
            "write": None,
            "sort": "asc",
            "limit": 2,
            "offset": 1,
        }

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
    "gpu_seconds": None,
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
        "user": "rai-ext-job-7",
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
            "node": None,
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
        assert ready.grant.node.user == "rai-ext-job-7"

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


_HOST_JOB_VIEW: dict[str, object] = {
    "id": 8,
    "host": "gx10",
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


class TestHostJobMethods:
    """Each ``host_job_*`` method and the host's three calls hit their paths."""

    def test_submit_posts_the_body_once_and_parses_the_job(self) -> None:
        seen: list[httpx2.Request] = []
        body = HostJobSubmit(
            host="gx10",
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
            httpx2.Response(201, json=_HOST_JOB_VIEW),
        ) as client:
            with pytest.raises(ClientError) as err:
                _ = client.host_job_submit(body)
            job = client.host_job_submit(body)

        # A retry after a lost answer would queue the job twice.
        assert err.value.status_code == 503
        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", HOST_JOBS_PATH),
        ] * 2
        assert json_loads(seen[0].content) == body.model_dump(mode="json")
        assert (job.id, job.host, job.state) == (8, "gx10", "running")

    def test_list_view_and_cancel_hit_their_paths(self) -> None:
        seen: list[httpx2.Request] = []
        with _job_client(
            seen,
            httpx2.Response(200, json={"jobs": [_HOST_JOB_VIEW]}),
            httpx2.Response(200, json=_HOST_JOB_VIEW),
            httpx2.Response(200, json=_HOST_JOB_VIEW | {"state": "cancelled"}),
        ) as client:
            listing = client.host_job_list()
            one = client.host_job_view(8)
            cancelled = client.host_job_cancel(8)

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", HOST_JOBS_PATH),
            ("GET", HOST_JOB_PATH.format(job_id=8)),
            ("POST", HOST_JOB_CANCEL_PATH.format(job_id=8)),
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
                _ = client.host_job_log(8, offset=4, wait=20)
            log = client.host_job_log(8, offset=4, wait=20)
            manifest = client.host_job_manifest(8)
            refused = client.host_job_file(8, "a/b.txt", offset=9)

        assert lost.value.status_code == 504
        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", HOST_JOB_LOG_PATH.format(job_id=8)),
            ("GET", HOST_JOB_LOG_PATH.format(job_id=8)),
            ("GET", HOST_JOB_MANIFEST_PATH.format(job_id=8)),
            ("GET", HOST_JOB_FILE_PATH.format(job_id=8)),
        ]
        assert dict(seen[1].url.params) == {"offset": "4", "wait": "20"}
        assert dict(seen[3].url.params) == {"path": "a/b.txt", "offset": "9"}
        assert [_read_timeout(r) for r in seen] == [55, 55, 35, 35]
        assert log.reply.kind == "chunk"
        assert manifest.reply.kind == "manifest"
        assert refused.reply == ReadRefused(reason="big")

    def test_the_host_waits_reports_and_answers_on_its_own_paths(self) -> None:
        seen: list[httpx2.Request] = []
        host, instance, read_id = uuid.UUID(int=1), uuid.UUID(int=2), uuid.uuid4()
        with _job_client(
            seen,
            httpx2.Response(200, json={"work": {"kind": "cancel", "job_id": 8}}),
            httpx2.Response(204),
            httpx2.Response(204),
        ) as client:
            offer = client.wait_host_work(host, instance=instance, wait_sec=25)
            client.report_host_job(host, 8, JobReport(state="running"))
            client.post_host_read(host, read_id, ReadReply.model_validate(_CHUNK))

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", HOST_WORK_PATH.format(host_id=host)),
            ("POST", HOST_JOB_REPORT_PATH.format(host_id=host, job_id=8)),
            (
                "POST",
                HOST_READ_REPLY_PATH.format(host_id=host, read_id=read_id),
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
            _ = client.wait_host_work(
                uuid.UUID(int=1),
                instance=uuid.UUID(int=2),
                wait_sec=0,
            )

        assert len(seen) == 1


def test_a_held_waits_delivery_identity_is_independent_between_pending_hosts() -> None:
    first, second, instance = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)
    offer = {
        "terminal_id": str(uuid.UUID(int=4)),
        "opener": "a@x.y",
        "rows": 24,
        "cols": 80,
    }
    sent: list[httpx2.Request] = []
    answers = iter(
        [
            httpx2.Response(200, json={"terminal_id": "not a uuid"}),
            httpx2.Response(200, json=offer),
            httpx2.Response(200, json=offer),
            httpx2.Response(200, json=None),
        ],
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return next(answers)

    with Client("https://server", transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(ClientError):
            client.wait_terminals(first, instance=instance, wait_sec=0)
        assert client.wait_terminals(second, instance=instance, wait_sec=0) is not None
        assert client.wait_terminals(first, instance=instance, wait_sec=0) is not None
        assert client.wait_terminals(first, instance=instance, wait_sec=0) is None

    assert [(request.method, request.url.path) for request in sent] == [
        ("GET", HOST_TERMINAL_WAIT_PATH.format(host_id=host))
        for host in (first, second, first, first)
    ]
    keys = [uuid.UUID(request.headers["Idempotency-Key"]) for request in sent]
    assert keys[0] == keys[2]
    assert len({keys[0], keys[1], keys[3]}) == 3


@pytest.mark.parametrize("use_filters", [False, True], ids=["ids", "filters"])
def test_rank_metrics_preserves_selection_controls_and_ranked_rows(
    use_filters: bool,
) -> None:
    experiments = [uuid.UUID(int=2), uuid.UUID(int=1)]
    filters = [
        Filter(field="labels", op="is", value="study:alpha"),
        Filter(field="status", op="ne", value="abandoned"),
    ]
    sent: list[httpx2.Request] = []
    answer = {
        "rows": [
            {
                "experiment_id": str(experiments[1]),
                "point": {"key": "loss", "step": 7, "value": 0.25},
            },
            {
                "experiment_id": str(experiments[0]),
                "point": {"key": "loss", "step": 9, "value": 0.75},
            },
        ],
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json=answer)

    with Client("https://server", transport=httpx2.MockTransport(handler)) as client:
        ranked = client.rank_metrics(
            filters if use_filters else experiments,
            masks=[
                MetricMaskClause(axis="key", op="is", value="loss"),
                MetricMaskClause(axis="step", op="max"),
            ],
            sort="asc",
            limit=3,
            offset=2,
        )

    assert [
        (row.experiment_id, row.point.key, row.point.step, row.point.value)
        for row in ranked
    ] == [
        (experiments[1], "loss", 7, 0.25),
        (experiments[0], "loss", 9, 0.75),
    ]
    [request] = sent
    assert (request.method, request.url.path) == ("POST", "/api/metrics/rank")
    assert json_loads(request.content) == {
        "experiment_ids": None if use_filters else [str(e) for e in experiments],
        "filters": [
            {"field": "labels", "op": "is", "value": "study:alpha"},
            {"field": "status", "op": "ne", "value": "abandoned"},
        ]
        if use_filters
        else None,
        "query": {
            "masks": [
                {"axis": "key", "op": "is", "value": "loss"},
                {"axis": "step", "op": "max", "value": ""},
            ],
            "write": None,
            "sort": "asc",
            "limit": 3,
            "offset": 2,
        },
    }


def test_list_kind_all_keeps_selectors_and_page_cap_on_every_request() -> None:
    full_page = [{"seq": seq} for seq in range(MAX_LIST_LIMIT)]
    pages = iter([full_page, [{"seq": MAX_LIST_LIMIT}]])
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json=next(pages))

    with Client("https://server", transport=httpx2.MockTransport(handler)) as client:
        rows = client.list_kind_all(
            "Issue",
            status="active",
            seq_ranges=[SeqRange(start=2, stop=7), SeqRange(start=11)],
            filters=[
                Filter(field="labels", op="is", value="team:alpha"),
                Filter(field="priority", op="le", value="20"),
            ],
        )

    assert rows == [*full_page, {"seq": MAX_LIST_LIMIT}]
    assert [(r.method, r.url.path) for r in sent] == [
        ("GET", "/api/inquiries"),
    ] * 2
    assert [parse_qsl(r.url.query.decode()) for r in sent] == [
        [
            ("kind", "Issue"),
            ("status", "active"),
            ("limit", str(MAX_LIST_LIMIT)),
            ("offset", str(offset)),
            ("seq_range", "2..7"),
            ("seq_range", "11.."),
            ("filter", '{"field":"labels","op":"is","value":"team:alpha"}'),
            ("filter", '{"field":"priority","op":"le","value":"20"}'),
        ]
        for offset in (0, MAX_LIST_LIMIT)
    ]


def test_export_keeps_selector_order_and_returns_the_streamed_lines() -> None:
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, content=b'{"selector":true}\n\n{"row":2}\n')

    with Client("https://server", transport=httpx2.MockTransport(handler)) as client:
        lines = list(
            client.export(
                selector=[
                    Filter(field="labels", op="is", value="org:alpha"),
                    Filter(field="labels", op="ne", value="private"),
                ],
            ),
        )

    assert lines == ['{"selector":true}', '{"row":2}']
    [request] = sent
    assert (request.method, request.url.path) == ("GET", EXPORT_API_PATH)
    assert parse_qsl(request.url.query.decode()) == [
        ("filter", '{"field":"labels","op":"is","value":"org:alpha"}'),
        ("filter", '{"field":"labels","op":"ne","value":"private"}'),
    ]


class _BatchItemBody(TypedDict):
    kind: str
    title: str
    idempotency_key: str


class _BatchSubmitBody(TypedDict):
    items: list[_BatchItemBody]
    edges: list[dict[str, str | int]]
    actor: str


def test_submit_batch_preserves_keys_actor_edges_and_server_result_order() -> None:
    explicit_key = str(uuid.UUID(int=3))
    bodies = [
        {"title": "first", "idempotency_key": explicit_key},
        {"title": "second"},
        {"title": "third", "idempotency_key": None},
    ]
    ids = [uuid.UUID(int=9), uuid.UUID(int=7), uuid.UUID(int=8)]
    edges = [{"from_index": 2, "to_index": 0, "edge_kind": "requires"}]
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(201, json={"ids": [str(row) for row in ids]})

    with Client("https://server", transport=httpx2.MockTransport(handler)) as client:
        created = client.submit_batch(
            [("Issue", bodies[0]), ("Belief", bodies[1]), ("Issue", bodies[2])],
            edges=edges,
            actor="alice",
        )

    assert created == ids
    [request] = sent
    assert (request.method, request.url.path) == ("POST", "/api/inquiries/batch")
    body = json_loads(request.content, _BatchSubmitBody)
    assert (body["actor"], body["edges"]) == ("alice", edges)
    assert [(item["kind"], item["title"]) for item in body["items"]] == [
        ("Issue", "first"),
        ("Belief", "second"),
        ("Issue", "third"),
    ]
    keys = [uuid.UUID(item["idempotency_key"]) for item in body["items"]]
    assert keys[0] == uuid.UUID(explicit_key)
    assert len(set(keys)) == 3
    assert bodies == [
        {"title": "first", "idempotency_key": explicit_key},
        {"title": "second"},
        {"title": "third", "idempotency_key": None},
    ]


@pytest.mark.parametrize("as_tuple", [False, True], ids=["list", "tuple"])
def test_get_drops_empty_middle_query_values_without_losing_later_values(
    as_tuple: bool,
) -> None:
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(
            200,
            json={"selected": request.url.params.get_list("select")},
        )

    values = ["first", "", None, "last", 0, False]
    with Client("https://server", transport=httpx2.MockTransport(handler)) as client:
        result = client.get(
            "/api/x",
            params={
                "select": tuple(values) if as_tuple else values,
                "empty": "",
                "missing": None,
            },
        )

    assert result == {"selected": ["first", "last", "0", "False"]}
    [request] = sent
    assert parse_qsl(request.url.query.decode(), keep_blank_values=True) == [
        ("select", "first"),
        ("select", "last"),
        ("select", "0"),
        ("select", "False"),
    ]


@pytest.mark.parametrize("length", [2_047, 2_048, 2_049])
@pytest.mark.parametrize("streaming", [False, True], ids=["request", "export"])
def test_error_text_truncates_only_past_the_2048_character_boundary(
    length: int,
    streaming: bool,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(400, text="x" * length)

    with Client("https://server", transport=httpx2.MockTransport(handler)) as client:
        if streaming:
            with pytest.raises(ClientError) as raised:
                list(client.export())
        else:
            with pytest.raises(ClientError) as raised:
                client.get("/api/x")

    path = EXPORT_API_PATH if streaming else "/api/x"
    expected = "x" * length if length <= 2_048 else "x" * 2_048 + "... (truncated)"
    assert str(raised.value) == f"GET {path} -> 400: {expected}"
    assert raised.value.status_code == 400


def _recording(
    sent: list[httpx2.Request],
    *answers: httpx2.Response,
) -> Client:
    """Return a client that records each request and answers ``answers`` in order."""
    queue = list(answers)

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return queue.pop(0)

    return Client("https://server", transport=httpx2.MockTransport(handler))


def test_server_url_rejects_a_url_it_cannot_parse() -> None:
    with pytest.raises(ClientError, match=r"^test has invalid URL$"):
        server_url("http://[::1", "test")


def test_resolve_ids_refuses_a_uuid_ref_of_the_wrong_kind() -> None:
    row = uuid.UUID(int=5)

    with (
        _answering({"found": {str(row): "Issue"}}) as client,
        pytest.raises(ClientError, match=f"ref Belief {row} resolves to a Issue row"),
    ):
        _ = client.resolve_ids([UuidRef(uuid=row, expected_kind="Belief")])


def test_next_issue_is_none_when_the_server_names_none() -> None:
    sent: list[httpx2.Request] = []
    with _recording(sent, httpx2.Response(200, content=b"")) as client:
        assert client.next_issue() is None
    assert [(r.method, r.url.path) for r in sent] == [
        ("GET", "/api/inquiries/next_issue"),
    ]


def test_claim_next_issue_posts_owner_actor_and_reason_and_reads_the_claim() -> None:
    sent: list[httpx2.Request] = []
    claimed = {"id": str(uuid.UUID(int=3)), "owner": "ada"}
    with _recording(
        sent,
        httpx2.Response(200, json=claimed),
        httpx2.Response(200, content=b""),
    ) as client:
        first = client.claim_next_issue(owner="ada", actor="bot", reason="queue")
        second = client.claim_next_issue(owner="ada")

    assert (first, second) == (claimed, None)
    assert [(r.method, r.url.path) for r in sent] == [
        ("POST", "/api/inquiries/next_issue"),
    ] * 2
    assert [json_loads(r.content, dict[str, object]) for r in sent] == [
        {"owner": "ada", "actor": "bot", "reason": "queue"},
        {"owner": "ada"},
    ]


class TestWaitUntilReady:
    """Readiness is any non-5xx answer; a dead server or the deadline ends the wait."""

    def test_a_server_without_the_version_route_is_up(self) -> None:
        sent: list[httpx2.Request] = []
        with _recording(sent, httpx2.Response(404, json={})) as client:
            client.wait_until_ready(timeout_sec=0)
        assert len(sent) == 1

    def test_probes_past_a_refused_connection_until_the_server_answers(self) -> None:
        answers = iter([httpx2.ConnectError("refused"), None])

        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            error = next(answers)
            if error is not None:
                raise error
            return httpx2.Response(200, json={"sha": "abc"})

        with Client(
            "https://server",
            transport=httpx2.MockTransport(handler),
        ) as client:
            client.wait_until_ready(timeout_sec=60, probe_interval_sec=0)

        assert next(answers, "drained") == "drained"

    def test_a_dead_server_fails_at_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sent: list[httpx2.Request] = []
        pauses: list[float] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(request)
            if len(sent) == 1:
                raise httpx2.ConnectError("refused")
            return httpx2.Response(200, json={"sha": "abc"})

        monkeypatch.setattr("trackinizer.client.client.time.sleep", pauses.append)
        with Client(
            "https://server",
            transport=httpx2.MockTransport(handler),
        ) as client:
            monkeypatch.setattr(
                "trackinizer.client.client.time.monotonic",
                lambda: 10.0,
            )
            with pytest.raises(
                ClientError,
                match="died before becoming ready",
            ) as raised:
                client.wait_until_ready(timeout_sec=60, alive=lambda: False)

        assert isinstance(raised.value.__cause__, ClientError)
        assert len(sent) == 1
        assert pauses == []

    @pytest.mark.parametrize("status", [500, 503])
    def test_a_5xx_at_the_deadline_reraises_without_another_pause(
        self,
        monkeypatch: pytest.MonkeyPatch,
        status: int,
    ) -> None:
        error = ClientError("booting", status_code=status)
        pauses: list[float] = []

        def probe(client: Client) -> str:
            del client
            raise error

        monkeypatch.setattr(Client, "version", probe)
        monkeypatch.setattr("trackinizer.client.client.time.sleep", pauses.append)

        with Client("https://server") as client:
            moments = iter([10.0, 12.0, 13.0])
            monkeypatch.setattr(
                "trackinizer.client.client.time.monotonic",
                lambda: next(moments),
            )
            with pytest.raises(ClientError) as raised:
                client.wait_until_ready(timeout_sec=2, probe_interval_sec=0.5)

        assert raised.value is error
        assert pauses == []


def test_search_sessions_sends_the_query_arms_and_limit() -> None:
    sent: list[httpx2.Request] = []
    found: dict[str, PlainTree] = {"hits": [], "semantic": False, "degraded": True}
    with _recording(
        sent,
        httpx2.Response(200, json=found),
        httpx2.Response(200, json=found),
    ) as client:
        assert client.search_sessions("loss spike", semantic=False, limit=3) == found
        assert client.search_sessions("loss spike") == found

    assert [r.url.path for r in sent] == ["/api/web/search_sessions"] * 2
    assert [dict(r.url.params) for r in sent] == [
        {"q": "loss spike", "semantic": "false", "limit": "3"},
        {"q": "loss spike", "semantic": "true", "limit": "20"},
    ]


def test_derived_confidence_and_authority_read_their_routes() -> None:
    row = uuid.UUID(int=8)
    sent: list[httpx2.Request] = []
    with _recording(
        sent,
        httpx2.Response(200, json={"confidence": 0.75}),
        httpx2.Response(200, json={"proves": 0.25}),
    ) as client:
        confidence = client.derived_confidence(row)
        authority = client.derived_authority(row)

    assert (confidence, authority) == (0.75, {"proves": 0.25})
    assert [r.url.path for r in sent] == [
        f"/api/inquiries/{row}/confidence",
        f"/api/inquiries/{row}/authority",
    ]


def test_add_edge_sends_its_audit_reason() -> None:
    src, dst = uuid.UUID(int=1), uuid.UUID(int=2)
    sent: list[httpx2.Request] = []
    with _recording(
        sent,
        httpx2.Response(
            200,
            json={"change_id": str(uuid.UUID(int=9)), "created": True},
        ),
    ) as client:
        write = client.add_edge(src, dst, "requires", actor="ada", reason="blocks it")

    assert write == EdgeWrite(created=True, changed=True)
    [request] = sent
    assert request.url.path == f"/api/edges/{src}/requires/{dst}"
    assert json_loads(request.content, dict[str, object]) == {
        "actor": "ada",
        "reason": "blocks it",
    }


def test_read_session_parts_returns_the_listed_parts() -> None:
    session = uuid.UUID(int=4)
    sent: list[httpx2.Request] = []
    listed = {
        "parts": [
            {"part": 0, "name": "a.jsonl", "format": "claude", "records": 3},
            {"part": 1, "name": "b.jsonl", "format": "", "records": 0},
        ],
    }
    with _recording(sent, httpx2.Response(200, json=listed)) as client:
        parts = client.read_session_parts(session)

    assert [(p.part, p.name, p.format, p.records) for p in parts] == [
        (0, "a.jsonl", "claude", 3),
        (1, "b.jsonl", "", 0),
    ]
    assert [(r.method, r.url.path) for r in sent] == [
        ("GET", f"/api/sessions/{session}/parts"),
    ]


def test_set_cli_session_id_overwrites_the_field_with_a_resume_reason() -> None:
    session = uuid.UUID(int=6)
    sent: list[httpx2.Request] = []
    with _recording(sent, httpx2.Response(200, content=b"")) as client:
        client.set_cli_session_id(session, "cli-123")

    [request] = sent
    assert (request.method, request.url.path) == (
        "PUT",
        inquiry_field_path("cli_session_id").format(target_id=session),
    )
    assert json_loads(request.content, dict[str, object]) == {
        "value": "cli-123",
        "actor": "agent",
        "reason": "resume: minted a fresh CLI session id",
    }


class TestMetricPoints:
    """Logging, reading and masked writes of one experiment's points."""

    def test_log_metrics_posts_the_points_and_reads_the_counts(self) -> None:
        experiment = uuid.UUID(int=7)
        sent: list[httpx2.Request] = []
        with _recording(
            sent,
            httpx2.Response(200, json={"logged": 1, "skipped": 1}),
        ) as client:
            logged = client.log_metrics(
                experiment,
                [
                    MetricPoint(
                        key="loss",
                        step=1,
                        value=0.5,
                        timestamp=datetime(2026, 11, 7, 12, tzinfo=UTC),
                    ),
                    MetricPoint(key="loss", step=2, value=0.25),
                ],
            )

        assert (logged.logged, logged.skipped) == (1, 1)
        [request] = sent
        assert (request.method, request.url.path) == (
            "POST",
            f"/api/experiments/{experiment}/metrics",
        )
        assert json_loads(request.content, dict[str, object]) == {
            "points": [
                {
                    "key": "loss",
                    "step": step,
                    "value": value,
                    "kind": "scalar",
                    "timestamp": "2026-11-07T12:00:00Z" if step == 1 else None,
                }
                for step, value in ((1, 0.5), (2, 0.25))
            ],
        }

    def test_read_metrics_pages_and_narrows_to_a_key_only_when_given(self) -> None:
        experiment = uuid.UUID(int=7)
        sent: list[httpx2.Request] = []
        page = {"points": [{"key": "loss", "step": 3, "value": 0.125}]}
        with _recording(
            sent,
            httpx2.Response(200, json=page),
            httpx2.Response(200, json={"points": []}),
        ) as client:
            narrowed = client.read_metrics(experiment, key="loss", limit=5, offset=2)
            every = client.read_metrics(experiment)

        assert [(p.key, p.step, p.value) for p in narrowed] == [("loss", 3, 0.125)]
        assert every == []
        assert [(request.method, request.url.path) for request in sent] == [
            ("GET", f"/api/experiments/{experiment}/metrics"),
        ] * 2
        assert [dict(r.url.params) for r in sent] == [
            {"limit": "5", "offset": "2", "key": "loss"},
            {"limit": str(DEFAULT_LIST_LIMIT), "offset": "0"},
        ]

    def test_write_metrics_masked_posts_the_value_and_returns_the_count(self) -> None:
        experiment = uuid.UUID(int=7)
        sent: list[httpx2.Request] = []
        with _recording(sent, httpx2.Response(200, json={"written": 4})) as client:
            written = client.write_metrics_masked(
                experiment,
                masks=[MetricMaskClause(axis="key", op="is", value="loss")],
                value=1.5,
            )

        assert written == 4
        [request] = sent
        assert (request.method, request.url.path) == (
            "POST",
            f"/api/experiments/{experiment}/metrics/write",
        )
        body = json_loads(request.content, dict[str, object])
        assert (body["write"], body["masks"]) == (
            1.5,
            [{"axis": "key", "op": "is", "value": "loss"}],
        )


def test_a_canvas_with_a_malformed_revision_is_refused_before_posting() -> None:
    canvas = uuid.UUID(int=2)
    sent: list[httpx2.Request] = []
    with (
        _recording(
            sent,
            httpx2.Response(200, json={"id": str(canvas), "revision": "3"}),
        ) as client,
        pytest.raises(ClientError, match="returned a malformed revision '3'"),
    ):
        _ = client.apply_canvas_operation(canvas, operation={"kind": "hide"})

    assert [r.method for r in sent] == ["GET"]


def test_logout_host_reads_what_it_stopped_and_revoked() -> None:
    sent: list[httpx2.Request] = []
    with _recording(sent, httpx2.Response(200, json={"runs": [], "keys": 2})) as client:
        logged_out = client.logout_host("gpu box")

    assert (logged_out.runs, logged_out.keys) == ([], 2)
    assert [(r.method, str(r.url)) for r in sent] == [
        ("POST", "https://server/api/hosts/gpu%20box/logout"),
    ]


def test_a_request_allowed_no_attempts_sends_nothing() -> None:
    sent: list[httpx2.Request] = []
    with (
        _recording(sent) as client,
        pytest.raises(AssertionError, match=r"^unreachable: retry_attempts >= 1$"),
    ):
        client.post("/api/x", retry_attempts=0)
    assert sent == []


def _transport_records(
    caplog: pytest.LogCaptureFixture,
) -> list[dict[str, object]]:
    return [
        from_plain(record.__dict__, dict[str, object])
        for record in caplog.records
        if getattr(record, "event", "") == "trackinizer_transport_failure"
    ]


class TestRetryBackoff:
    """A retried failure waits 0.1 s, then 0.3 s, and reports every attempt."""

    @pytest.fixture
    def slept(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        waits: list[float] = []
        monkeypatch.setattr(
            "trackinizer.client.client.time.sleep",
            waits.append,
        )
        return waits

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_each_retried_5xx_backs_off_geometrically(
        self,
        status: int,
        slept: list[float],
    ) -> None:
        sent: list[httpx2.Request] = []
        with (
            _recording(
                sent,
                *(httpx2.Response(status, text="down") for _ in range(3)),
            ) as client,
            pytest.raises(ClientError) as raised,
        ):
            client.post("/api/x")

        assert len(sent) == 3
        assert slept == pytest.approx([0.1, 0.3])
        assert (raised.value.status_code, raised.value.code) == (status, "")

    def test_a_retried_read_timeout_logs_each_attempt_then_names_the_request(
        self,
        slept: list[float],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            del request
            raise httpx2.ReadTimeout("timed out")

        with (
            Client("https://server", transport=httpx2.MockTransport(handler)) as client,
            caplog.at_level(logging.WARNING),
        ):
            with pytest.raises(ClientError, match=r"^GET /api/a failed: timed out$"):
                client.get("/api/a")
            with pytest.raises(ClientError):
                client.post("/api/b", retry_attempts=1)

        assert slept == pytest.approx([0.1, 0.3])
        assert [
            (
                fields.get("method"),
                fields.get("path"),
                fields.get("attempt"),
                fields.get("client_request_index"),
            )
            for fields in _transport_records(caplog)
        ] == [
            ("GET", "/api/a", 1, 1),
            ("GET", "/api/a", 2, 1),
            ("GET", "/api/a", 3, 1),
            ("POST", "/api/b", 1, 2),
        ]


def test_the_idempotency_key_is_sent_with_its_canonical_casing() -> None:
    sent: list[httpx2.Request] = []
    with _recording(sent, httpx2.Response(200, content=b"")) as client:
        client.post("/api/x")

    [request] = sent
    assert [
        name for name, _ in request.headers.raw if name.lower() == b"idempotency-key"
    ] == [
        b"Idempotency-Key",
    ]


@pytest.mark.parametrize(
    ("error", "classified"),
    [
        (
            httpx2.ConnectError("[Errno 111] Connection refused"),
            ("connect_error", "connection_refused"),
        ),
        (
            httpx2.ConnectError("[Errno 104] Connection reset by peer"),
            ("connect_error", "connection_reset"),
        ),
        (httpx2.ConnectError("Name or service not known"), ("connect_error",) * 2),
        (httpx2.ReadTimeout("timed out"), ("read_timeout",) * 2),
        (httpx2.ReadError("eof"), ("read_error",) * 2),
        (httpx2.WriteTimeout("timed out"), ("write_timeout",) * 2),
        (httpx2.WriteError("broken pipe"), ("write_error",) * 2),
        (httpx2.PoolTimeout("pool full"), ("pool_timeout",) * 2),
        (httpx2.RemoteProtocolError("closed"), ("remote_protocol_error",) * 2),
        (httpx2.UnsupportedProtocol("ftp"), ("transport_error",) * 2),
        (httpx2.ProxyError("407"), ("transport_error",) * 2),
    ],
)
def test_a_transport_failure_is_logged_with_its_class_and_detail(
    error: httpx2.TransportError,
    classified: tuple[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        raise error

    with (
        Client("https://server", transport=httpx2.MockTransport(handler)) as client,
        caplog.at_level(logging.WARNING),
        pytest.raises(ClientError),
    ):
        client.post("/api/x", retry_attempts=1)

    [fields] = _transport_records(caplog)
    assert (fields.get("failure_class"), fields.get("failure_detail")) == classified


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"code": "conflict", "detail": {"code": "inner"}}, "conflict"),
        ({"detail": {"code": "chat_deleted"}}, "chat_deleted"),
        ({"detail": "plain"}, ""),
        ({"code": 7}, ""),
        ([{"code": "listed"}], ""),
        ("not json", ""),
    ],
    ids=["top", "nested", "uncoded", "non-string", "list", "text"],
)
def test_an_error_carries_only_a_string_code_the_body_names(
    body: PlainTree,
    code: str,
) -> None:
    answer = (
        httpx2.Response(409, text=body)
        if isinstance(body, str)
        else httpx2.Response(409, json=body)
    )
    with _recording([], answer) as client, pytest.raises(ClientError) as raised:
        client.get("/api/x")

    assert raised.value.code == code


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
