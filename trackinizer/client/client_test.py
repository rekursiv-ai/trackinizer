"""Tests for trackinizer CLI HTTP client."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast, override

import argparse
import inspect
import json
import logging
import uuid

import httpx
import pytest

from trackinizer.client.client import (
    Client,
    EdgeWrite,
    server_url,
)
from trackinizer.client.errors import ClientError
from trackinizer.lib.custom_json import dict_val, float_val, int_val, str_val
from trackinizer.trax import cli, profile
from trackinizer.trax.conftest import FakeClient
from trackinizer.trax.grammar import parse_kind, parse_ref
from trackinizer.trax.profile import Profile
from trackinizer.wire.filters import Filter
from trackinizer.wire.refs import SeqRef, UuidRef
from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.seq_ranges import SeqRange
from trackinizer.wire.wire_sessions import EventBody, SessionStart


def _install_mock_transport(
    client: Client,
    handler: Any,
) -> None:
    """Replace the client's transport with one that calls ``handler``.

    ``handler`` receives an ``httpx.Request`` and returns an
    ``httpx.Response``. The test asserts on the request that arrives
    in the handler.
    """
    client._http.close()
    client._http = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        headers=dict(client._http.headers),
    )


class _ClientSpy(Client):
    def __init__(
        self,
        *,
        get_results: list[object] | None = None,
        post_result: object | None = None,
    ) -> None:
        super().__init__("http://server")
        self.get_results = list(get_results or [])
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
    ) -> Any:
        self.get_calls.append((path, None if params is None else dict(params)))
        return self.get_results.pop(0) if self.get_results else None

    @override
    def post(
        self,
        path: str,
        *,
        body: object = None,
    ) -> Any:
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
    ) -> Any:
        del change_id, params, retry_attempts
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
            parents=True, exist_ok=True
        )
        monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)
        monkeypatch.delenv("TRACKINIZER_URL", raising=False)

    def test_flags_default_to_none(self) -> None:
        parser = argparse.ArgumentParser()
        cli.connect_flags(parser)
        args = parser.parse_args([])
        assert args.profile is None
        assert args.host is None
        assert args.port is None

    def test_flags_parse_user_values(self) -> None:
        parser = argparse.ArgumentParser()
        cli.connect_flags(parser)
        args = parser.parse_args(
            ["--profile", "prod", "--host", "1.2.3.4", "--port", "9000"]
        )
        assert args.profile == "prod"
        assert args.host == "1.2.3.4"
        assert args.port == 9000

    def test_from_args_host_port_override_profile(self) -> None:
        profile.save_profile("prod", Profile(url="http://prod:1000"))
        client = cli.connect(argparse.Namespace(profile="prod", host="ex", port=9090))
        assert client.base_url == "http://ex:9090"

    def test_from_args_selects_named_profile(self) -> None:
        profile.save_profile("prod", Profile(url="http://prod:9000", author="alice"))
        client = cli.connect(argparse.Namespace(profile="prod", host=None, port=None))
        assert client.base_url == "http://prod:9000"
        assert client.author == "alice"

    def test_from_args_partial_flags_fill_from_profile(self) -> None:
        profile.save_profile("default", Profile(url="http://defaulthost:8888"))
        host_only = cli.connect(
            argparse.Namespace(profile=None, host="other", port=None)
        )
        assert host_only.base_url == "http://other:8888"
        port_only = cli.connect(argparse.Namespace(profile=None, host=None, port=4242))
        assert port_only.base_url == "http://defaulthost:4242"

    def test_from_args_without_flags_uses_trackinizer_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_URL", "http://127.0.0.1:8766/")
        client = cli.connect(argparse.Namespace())
        assert client.base_url == "http://127.0.0.1:8766"
        assert client.author == ""

    def test_from_args_rejects_invalid_trackinizer_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_URL", "actor")
        with pytest.raises(ClientError, match="TRACKINIZER_URL has invalid URL"):
            cli.connect(argparse.Namespace())

    def test_constructor_rejects_invalid_base_url(self) -> None:
        with pytest.raises(ClientError, match="base_url has invalid URL"):
            Client("actor")

    def test_from_args_raises_when_named_profile_missing(self) -> None:
        with pytest.raises(ClientError, match="profile 'typo' not found"):
            cli.connect(argparse.Namespace(profile="typo", host=None, port=None))

    def test_bare_client_reads_author_from_profile(self) -> None:
        profile.save_profile(
            "default", Profile(url="http://defaulthost:8888", author="alice")
        )
        client = cli.connect(argparse.Namespace(profile=None, host=None, port=None))
        assert client.base_url == "http://defaulthost:8888"
        assert client.author == "alice"

    def test_from_args_without_flags_uses_default_profile(self) -> None:
        bare = cli.connect(argparse.Namespace())
        assert bare.base_url == "http://127.0.0.1:8765"
        assert bare.author == ""

    def test_from_args_preserves_https_scheme(self) -> None:
        profile.save_profile(
            "prod", Profile(url="https://example.com:443", author="alice")
        )
        client = cli.connect(argparse.Namespace(profile="prod", host=None, port=None))
        assert client.base_url == "https://example.com:443"
        assert client.author == "alice"

    def test_from_args_preserves_portless_profile(self) -> None:
        profile.save_profile("prod", Profile(url="https://example.com"))
        client = cli.connect(argparse.Namespace(profile="prod", host=None, port=None))
        assert client.base_url == "https://example.com"

    def test_from_args_host_override_keeps_profile_scheme_and_port(self) -> None:
        profile.save_profile("prod", Profile(url="https://prod.example:8443"))
        client = cli.connect(
            argparse.Namespace(profile="prod", host="other", port=None)
        )
        assert client.base_url == "https://other:8443"

    def test_from_args_port_override_on_portless_profile(self) -> None:
        profile.save_profile("prod", Profile(url="https://example.com"))
        client = cli.connect(argparse.Namespace(profile="prod", host=None, port=9000))
        assert client.base_url == "https://example.com:9000"


class TestRequests:
    def test_client_bounds_the_transport_connection_pool(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_limits = httpx.Limits
        real_transport = httpx.HTTPTransport
        observed: list[tuple[int, int, float]] = []
        transport_limits: list[httpx.Limits] = []

        def make_limits(
            *,
            max_connections: int,
            max_keepalive_connections: int,
            keepalive_expiry: float,
        ) -> httpx.Limits:
            observed.append(
                (max_connections, max_keepalive_connections, keepalive_expiry)
            )
            return real_limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
                keepalive_expiry=keepalive_expiry,
            )

        def make_transport(
            *,
            retries: int,
            limits: httpx.Limits,
        ) -> httpx.HTTPTransport:
            transport_limits.append(limits)
            return real_transport(retries=retries, limits=limits)

        monkeypatch.setattr(httpx, "Limits", make_limits)
        monkeypatch.setattr(httpx, "HTTPTransport", make_transport)

        with Client("https://server"):
            pass

        assert observed == [(8, 8, 90.0)]
        assert len(transport_limits) == 1

    def test_request_builds_url_headers_and_body(self) -> None:
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["req"] = request
            return httpx.Response(200, json={"ok": True})

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
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["req"] = request
            return httpx.Response(200, content=b"")

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
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["req"] = request
            return httpx.Response(200, content=b"")

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            client.get("/api/x")
        assert "Authorization" not in seen["req"].headers

    def test_request_adds_bearer_when_api_key_set(self) -> None:
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["req"] = request
            return httpx.Response(200, content=b"")

        with Client("http://server", api_key="trax_abcdef") as client:
            _install_mock_transport(client, handler)
            client.post("/api/x", body={"a": 1})
        assert seen["req"].headers["Authorization"] == "Bearer trax_abcdef"

    def test_request_wraps_http_and_connection_errors(self) -> None:
        def http_error(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(409, content=b'{"detail":"bad"}')

        with Client("http://server") as client:
            _install_mock_transport(client, http_error)
            with pytest.raises(ClientError, match="POST /x -> 409"):
                client.post("/x")

    def test_http_error_carries_structured_code(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
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

    def test_wraps_connection_errors(self) -> None:
        def connect_error(request: httpx.Request) -> httpx.Response:
            del request
            raise httpx.ConnectError("offline")

        with Client("http://server") as client:
            _install_mock_transport(client, connect_error)
            with pytest.raises(ClientError, match="GET /x failed: offline"):
                client.get("/x")

    def test_transport_failure_logs_client_pool_context(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def handshake_timeout(request: httpx.Request) -> httpx.Response:
            del request
            raise httpx.ConnectTimeout("_ssl.c:1063: The handshake operation timed out")

        with Client("https://server") as client:
            _install_mock_transport(client, handshake_timeout)
            with caplog.at_level(logging.WARNING), pytest.raises(ClientError):
                client.get("/api/version")

        record = next(
            record
            for record in caplog.records
            if getattr(record, "event", "") == "trackinizer_transport_failure"
        )
        fields = dict_val(record.__dict__)
        assert str_val(fields.get("method")) == "GET"
        assert str_val(fields.get("path")) == "/api/version"
        assert str_val(fields.get("server")) == "https://server"
        assert int_val(fields.get("client_request_index"), 0) == 1
        assert int_val(fields.get("attempt"), 0) == 1
        assert str_val(fields.get("failure_class")) == "connect_timeout"
        assert str_val(fields.get("failure_detail")) == "tls_handshake_timeout"
        assert str_val(fields.get("error_type")) == "ConnectTimeout"
        assert float_val(fields.get("client_age_sec"), -1) >= 0
        assert len(str_val(fields.get("client_id"))) == 12

    def test_retries_5xx_with_same_change_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 502/503/504 retries the same request body and Idempotency-Key."""

        def _no_sleep(_s: float) -> None:
            pass

        monkeypatch.setattr("trackinizer.client.client.time.sleep", _no_sleep)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if len(seen) < 3:
                return httpx.Response(502, content=b"bad gateway")
            return httpx.Response(200, json={"ok": True})

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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient 500 retries the same body + Idempotency-Key, then succeeds.

        The single-writer PGlite substrate can return a transient 500 under
        concurrent load. The idempotency key makes the replay dedup-safe (a
        write that already landed collides on the change_log PK), so 500 is
        retried like the other 5xx -- one logical write, not two.
        """

        def _no_sleep(_s: float) -> None:
            pass

        monkeypatch.setattr("trackinizer.client.client.time.sleep", _no_sleep)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if len(seen) < 2:
                return httpx.Response(500, content=b"internal server error")
            return httpx.Response(200, json={"ok": True})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            result = client.post("/api/x", body={"a": 1})
        assert result == {"ok": True}
        assert len(seen) == 2  # one 500, then the retry succeeded
        change_ids = {req.headers["Idempotency-Key"] for req in seen}
        assert len(change_ids) == 1, "the 500 retry must reuse the same UUID"

    def test_gives_up_after_max_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After exhausting retries the final 5xx surfaces as ClientError."""

        def _no_sleep(_s: float) -> None:
            pass

        monkeypatch.setattr("trackinizer.client.client.time.sleep", _no_sleep)
        attempts: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            return httpx.Response(503, content=b"down")

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="POST /x -> 503"):
                client.post("/x")
        assert len(attempts) == 3

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.WriteError("broken pipe"),
            httpx.WriteTimeout("write timed out"),
            httpx.ConnectTimeout("connect timed out"),
        ],
    )
    def test_wraps_write_and_connect_timeouts_without_retry(
        self, exc: httpx.HTTPError
    ) -> None:
        """Write/connect-timeout errors surface as ``ClientError``, not raw httpx.

        ``WriteError`` / ``WriteTimeout`` / ``ConnectTimeout`` must be wrapped
        in the client's ``ClientError`` contract like the other transport
        failures. They are *not* retried: the request bytes may already have
        reached the server, so a blind retry could duplicate a mutation.
        """
        attempts: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
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

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=big.encode())

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError) as exc_info:
            client.get("/x")
    message = str(exc_info.value)
    assert len(message) < 5_000, "error text must be truncated, not embedded whole"
    assert "..." in message, "truncation must be marked with an ellipsis"


class TestVersion:
    def test_returns_server_sha(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"sha": "deadbeef"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            assert client.version() == "deadbeef"

    def test_servers_own_unknown_passes_through(self) -> None:
        # The server resolved its build to the literal "unknown"; that is a
        # real answer, returned verbatim (distinct from a malformed payload).
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"sha": "unknown"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            assert client.version() == "unknown"

    def test_malformed_payload_raises_not_silent_unknown(self) -> None:
        # A response with no ``sha`` key is a contract violation; it must raise,
        # not masquerade as the server's own "unknown".
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

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

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"wrong": "shape"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.resolve_id(SeqRef(kind="Issue", seq=4))

    def test_resolve_id_uuid_non_dict_raises_client_error(self) -> None:
        """A dict-shaped seam fed a list must not leak ``TypeError``."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[1, 2, 3])

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.resolve_id(UuidRef(uuid=uuid.uuid4()))

    def test_resolve_ids_missing_found_raises_client_error(self) -> None:
        """``resolve_ids`` reads ``response['found']``; absence must wrap."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"nope": {}})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.resolve_ids([UuidRef(uuid=uuid.uuid4())])

    def test_submit_missing_id_raises_client_error(self) -> None:
        """``submit`` reads the server-minted ``id``; absence must wrap."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"nope": "x"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.submit("Issue", {"title": "x"})

    def test_submit_non_uuid_id_raises_client_error(self) -> None:
        """A non-UUID ``id`` value must wrap, not leak ``ValueError``."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "not-a-uuid"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.submit("Issue", {"title": "x"})

    def test_submit_batch_missing_ids_raises_client_error(self) -> None:
        """A list-returning seam: ``submit_batch`` reads ``response['ids']``."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"nope": []})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.submit_batch([("Issue", {"title": "x"})])

    def test_next_issue_non_dict_raises_client_error(self) -> None:
        """``next_issue`` promises ``dict | None``; a list is malformed."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[1, 2, 3])

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.next_issue()

    def test_recent_changes_non_list_raises_client_error(self) -> None:
        """A list-returning read fed a dict must surface ``ClientError``."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"not": "a list"})

        with Client("http://server") as client:
            _install_mock_transport(client, handler)
            with pytest.raises(ClientError, match="malformed"):
                client.recent_changes()

    def test_session_start_malformed_wraps_validation_error(self) -> None:
        """A pydantic ``model_validate`` seam must wrap ``ValidationError``."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"bogus": True})

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
            ]
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
                [{"id": "s"}],
                [{"id": "c"}],
                {"agent_usd": 1.0},
            ]
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
        assert client.search("hello", kind="Belief") == [{"id": "s"}]
        assert client.recent_changes(limit=2) == [{"id": "c"}]
        assert client.cost_for(target_id, deep=True) == {"agent_usd": 1.0}

    def test_list_kind_serializes_filters_as_repeated_query_params(self) -> None:
        """Each filter rides the wire as a discrete ``filter`` query param.

        Concatenating field/op/value with a separator would break for
        values containing the separator; JSON-per-filter sidesteps
        escaping entirely. The order in which the CLI emits filters
        must be preserved so server-side semantics stay deterministic.
        """
        captured: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["r"] = request
            return httpx.Response(200, json=[])

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
        # ``httpx.URL.params`` exposes repeated keys via ``get_list``.
        assert req.url.params.get_list("filter") == [
            '{"field":"title","op":"re","value":"needle:with:colons"}',
            '{"field":"priority","op":"gt","value":"5"}',
        ]

    def test_list_kind_serializes_seq_ranges_as_repeated_query_params(self) -> None:
        """Each interval rides the wire as a discrete ``seq_range`` param.

        The union of disjoint seq windows becomes one query: the server
        ORs the intervals, so the CLI sends them as repeated params in the
        order it parsed them and never fans out per interval.
        """
        captured: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["r"] = request
            return httpx.Response(200, json=[])

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

    def test_list_kind_all_pages_past_the_cap(self) -> None:
        """``list_kind_all`` concatenates every page until a short one.

        The route caps ``limit`` at ``MAX_LIST_LIMIT``; a whole-collection view
        pages by ``offset`` to get them all. Simulate one full page (exactly the
        cap) followed by a short page, and assert: both offsets requested, every
        row returned, and termination on the short page.
        """
        offsets: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            offset = request.url.params.get("offset")
            offsets.append(offset)
            if offset == "0":
                return httpx.Response(
                    200, json=[{"seq": i} for i in range(MAX_LIST_LIMIT)]
                )
            return httpx.Response(200, json=[{"seq": MAX_LIST_LIMIT}])

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

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if request.url.params.get("offset") == "0":
                return httpx.Response(
                    200, json=[{"seq": i} for i in range(MAX_LIST_LIMIT)]
                )
            return httpx.Response(200, json=[])

        client = Client("http://server")
        _install_mock_transport(client, handler)
        rows = client.list_kind_all("Issue")
        assert len(rows) == MAX_LIST_LIMIT
        assert calls == 2, "second page (empty) confirms the end"

    def test_resolve_ids_posts_bare_array(self) -> None:
        target_id = uuid.uuid4()
        client = _ClientSpy(
            post_result={"found": {str(target_id): "Issue"}, "missing": []}
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
            target_id, "marginal_cost_agent_usd", -0.5, actor="alice", reason="fix"
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
            )
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
            def put(self, path: str, *, body: object = None) -> Any:
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
        # priority landed first, note failed; valence/labels never sent.
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
        body = cast(dict[str, object], client.post_calls[0][1])
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


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)


# Folded in from former fake_surface_test.py.


# Members on ``Client`` that the fake intentionally does not implement:
# argparse-flag registration, the connection-resolution factory, and the
# raw HTTP verbs. The fake bypasses HTTP entirely, so these would be dead
# on the fake.
_FAKE_EXEMPT: frozenset[str] = frozenset(
    {"flags", "from_args", "get", "post", "put", "patch", "delete"}
)


def _public_methods(cls: type) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(cls, callable)
        if not name.startswith("_") and not inspect.isclass(member)
    } - _FAKE_EXEMPT


def test_fake_client_covers_real_client_surface() -> None:
    """Every public ``Client`` method must exist on ``FakeClient``."""
    real = _public_methods(Client)
    fake = _public_methods(FakeClient)
    missing = real - fake
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
        real_sig = inspect.signature(getattr(Client, name))
        fake_sig = inspect.signature(getattr(FakeClient, name))
        real_params = list(real_sig.parameters)
        fake_params = list(fake_sig.parameters)
        if real_params != fake_params:
            mismatches.append(f"{name}: real={real_params!r} fake={fake_params!r}")
        if real_sig.return_annotation != fake_sig.return_annotation:
            mismatches.append(
                f"{name}: return real={real_sig.return_annotation!r} "
                f"fake={fake_sig.return_annotation!r}"
            )
    assert not mismatches, (
        "FakeClient method signatures diverge from Client:\n"
        + "\n".join(f"  {m}" for m in mismatches)
    )


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
        "http://:8765",  # missing host
        "http://example.com:abc",  # non-numeric port
        "http://example.com:99999",  # out-of-range port
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

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops")

    with Client("http://server") as client:
        _install_mock_transport(client, handler)
        with pytest.raises(ClientError, match="malformed JSON"):
            client.get("/x")


def _edge_post(
    *, created: bool, change_id: str | None
) -> Callable[..., Mapping[str, object]]:
    """A fake ``post`` returning the edge route's ``{change_id, created}``."""

    def fake_post(path: str, *, body: object = None) -> Mapping[str, object]:
        del path, body
        return {"change_id": change_id, "created": created}

    return fake_post


def test_add_edge_reports_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """A brand-new edge -> ``EdgeWrite(created=True, changed=True)``."""
    client = Client("http://example.com")
    monkeypatch.setattr(
        client, "post", _edge_post(created=True, change_id=str(uuid.uuid4()))
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
        client, "post", _edge_post(created=False, change_id=str(uuid.uuid4()))
    )
    result = client.add_edge(
        uuid.uuid4(), uuid.uuid4(), "proves", actor="a", note="load-bearing"
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
        (f"/api/edges/{src}/requires/{dst}/labels", {"value": [], "actor": "a"})
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
            ("Issue", {"title": "a"}),  # absent body kind
            ("Belief", {"title": "b", "kind": "Belief"}),  # matching body kind
        ]
    )
    body = cast(dict[str, object], client.request_calls[0][2])
    items = cast(list[dict[str, object]], body["items"])
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
    """The session-ingest client methods build the right requests and parse
    the responses, via a mock transport that inspects each call.
    """

    def test_session_start_posts_and_parses(self) -> None:
        sid = uuid.uuid4()
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": str(sid), "seq": 3})

        client = Client("http://server")
        _install_mock_transport(client, handler)
        resp = client.session_start(SessionStart(cli="codex"))
        assert seen["path"] == "/api/sessions/start"
        body = cast(dict[str, object], seen["body"])
        assert body["cli"] == "codex"
        # A missing idempotency key is minted client-side.
        assert body["idempotency_key"] is not None
        assert resp.id == sid
        assert resp.seq == 3

    def test_append_events_posts_batch(self) -> None:
        sid = uuid.uuid4()
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"appended": 2, "skipped": 0})

        client = Client("http://server")
        _install_mock_transport(client, handler)
        resp = client.append_events(
            sid,
            [
                EventBody(seq=0, kind="UserMessage"),
                EventBody(seq=1, kind="AssistantMessage", model="gpt-5.5"),
            ],
        )
        assert seen["path"] == f"/api/sessions/{sid}/events"
        body = cast(dict[str, object], seen["body"])
        events = cast(list[dict[str, object]], body["events"])
        assert [e["seq"] for e in events] == [0, 1]
        assert resp.appended == 2

    def test_read_events_gets_and_parses(self) -> None:
        sid = uuid.uuid4()
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = request.url.params
            return httpx.Response(
                200,
                json={
                    "events": [
                        {"seq": 5, "kind": "AssistantMessage", "model": "gpt-5.5"}
                    ]
                },
            )

        client = Client("http://server")
        _install_mock_transport(client, handler)
        events = client.read_events(
            sid,
            limit=10,
            seq_ranges=(SeqRange(start=5, stop=9), SeqRange(start=20)),
            kind="AssistantMessage",
        )
        assert seen["path"] == f"/api/sessions/{sid}/events"
        params = cast(httpx.QueryParams, seen["params"])
        assert params["limit"] == "10"
        assert params.get_list("seq_range") == ["5..9", "20.."]
        assert params["kind"] == "AssistantMessage"
        assert len(events) == 1
        assert events[0].seq == 5
        assert events[0].model == "gpt-5.5"

    def test_session_end_posts(self) -> None:
        sid = uuid.uuid4()
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json={"id": str(sid)})

        client = Client("http://server")
        _install_mock_transport(client, handler)
        resp = client.session_end(sid)
        assert seen["path"] == f"/api/sessions/{sid}/end"
        assert resp.id == sid
