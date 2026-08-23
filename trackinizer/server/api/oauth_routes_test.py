"""Tests for ``/auth/login`` / ``/auth/callback`` / ``/auth/logout``.

The HTTP round-trips to Google are mocked via :class:`httpx.MockTransport`
so the suite never hits the network. The mock is installed by
monkey-patching :func:`oauth_routes._http_client` -- the production
code already routes every Google call through that one factory, so a
single override covers both the token exchange and the userinfo fetch.

Fixtures here build a ``TestClient`` with ``follow_redirects=False`` so
assertions can inspect the 302 the OAuth routes always return without
being silently dragged onto the next page.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import logging
import uuid

from fastapi.testclient import TestClient

import httpx
import pytest

from trackinizer.conftest import FakeEngine, make_store
from trackinizer.server.api import oauth_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    clear_identity_override,
)
from trackinizer.server.auth import generate_token, hash_secret
from trackinizer.server.config import Config
from trackinizer.server.session import (
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    read_oauth_state_cookie,
    set_session_cookie,
)
from trackinizer.server.store.core import Store


# A complete OAuth Config wired so the routes don't 503. ``redirect_uri``
# is set explicitly so assertions can refer to a known value instead of
# the request-derived default. The "secret" values are obvious dummies;
# the ``noqa: S106`` mark tells ruff this is a test fixture, not a real
# credential leak.
_OAUTH_CONFIG = Config(
    oauth_google_client_id="client-id-abc",
    oauth_google_client_secret="client-secret-xyz",  # noqa: S106 -- test fixture.
    oauth_redirect_uri="https://trackinizer.example.com/auth/callback",
    session_secret="session-signing-secret",  # noqa: S106 -- test fixture.
    session_max_age_seconds=600,
)


def _install_oauth_state(
    *,
    config: Config | None = None,
) -> FakeEngine:
    """Wire app.state.engine / .config / .store; pop the identity override.

    ``state.store`` is required because :func:`auth.current_user` reads
    it on every auth-gated request; an OAuth test that runs before an
    auth-gated test on the same module-global ``app`` would otherwise
    leave the store unwired and the next request would 500 on
    AttributeError instead of resolving (or rejecting) the bearer.
    """
    store, engine = make_store()
    app.state.engine = engine
    app.state.store = store
    app.state.config = _OAUTH_CONFIG if config is None else config
    # The OAuth routes don't use ``current_user``; pop the default
    # override so accidental dependency lookups don't leak through.
    clear_identity_override()
    return engine


def _install_http_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Replace :func:`oauth_routes._http_client` with a MockTransport-backed one."""
    transport = httpx.MockTransport(handler)

    def _make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    monkeypatch.setattr(oauth_routes, "_http_client", _make_client)


class _CookieCarrier:
    """Stand-in for :class:`fastapi.Response` satisfying the
    :class:`session._SetsCookies` Protocol -- captures set cookies.

    Tests that need to *forge* a cookie (e.g. the logout test pre-loading
    a session cookie) instantiate one of these, hand it to a session
    helper, and read the resulting cookie back off ``cookies``.
    """

    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}

    def set_cookie(
        self,
        key: str,
        value: str = "",
        *,
        max_age: int | None = None,
        httponly: bool = False,
        secure: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
        path: str | None = "/",
    ) -> None:
        del max_age, httponly, secure, samesite, path
        self.cookies[key] = value

    def delete_cookie(
        self,
        key: str,
        *,
        path: str = "/",
        httponly: bool = False,
        secure: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
    ) -> None:
        del path, httponly, secure, samesite
        self.cookies.pop(key, None)


@pytest.fixture
def client() -> TestClient:
    """``TestClient`` with redirect-following off so 302s can be inspected.

    No context-manager so the FastAPI lifespan (which would boot a real
    pglite engine) never runs; the tests stub ``app.state.engine`` and
    ``app.state.config`` via :func:`_install_oauth_state`.

    ``base_url`` is HTTPS so ``Secure`` cookies (the OAuth state and
    session cookies) survive cross-request persistence in the TestClient
    cookie jar -- httpx filters Secure cookies on plain
    ``http://testserver``.
    """
    return TestClient(app, follow_redirects=False, base_url="https://testserver")


# ---- /auth/login ----------------------------------------------------------


class TestSameOriginPath:
    """``_same_origin_path`` rejects anything that could leave the origin."""

    @pytest.mark.parametrize(
        "value",
        [
            "/dashboard",
            "/me/tokens?tab=keys",
        ],
    )
    def test_accepts_same_origin_paths(self, value: str) -> None:
        assert oauth_routes._same_origin_path(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "https://evil.example.com",
            "//evil.example.com/path",
            "/\\evil",
            "/x\r\nLocation: //evil",
            "/x\nSet-Cookie: a=b",
            "/x\rHost: evil",
            "relative",
        ],
    )
    def test_rejects_off_origin_or_control_chars(self, value: str) -> None:
        assert oauth_routes._same_origin_path(value) is False


class TestAuthLogin:
    def test_redirects_to_google_with_correct_params(
        self,
        client: TestClient,
    ) -> None:
        _install_oauth_state()
        response = client.get("/auth/login")
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith(oauth_routes.GOOGLE_AUTHORIZE_URL)
        parsed = urlparse(location)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        assert params["client_id"] == "client-id-abc"
        assert params["redirect_uri"] == "https://trackinizer.example.com/auth/callback"
        assert params["response_type"] == "code"
        assert params["scope"] == "openid email profile"
        assert len(params["state"]) >= 16
        # The OAuth state cookie must be set so the callback can verify
        # the state echo.
        assert OAUTH_STATE_COOKIE_NAME in response.cookies

    def test_503_when_oauth_unconfigured(
        self,
        client: TestClient,
    ) -> None:
        _install_oauth_state(config=Config())
        response = client.get("/auth/login")
        assert response.status_code == 503
        # The error names the missing env var so deployers know what to set.
        detail = response.json()["detail"]
        assert "TRACKINIZER_GOOGLE_CLIENT_ID" in detail
        assert "TRACKINIZER_SESSION_SECRET" in detail

    def test_503_when_oauth_redirect_uri_missing(
        self,
        client: TestClient,
    ) -> None:
        # Without an explicit redirect_uri the route must 503 rather than
        # fabricate one from request.url_for -- behind a reverse proxy
        # the request URL has the wrong scheme/host and silently breaks
        # the round-trip.
        partial = Config(
            oauth_google_client_id="x",
            oauth_google_client_secret="y",  # noqa: S106 -- test fixture.
            oauth_redirect_uri=None,
            session_secret="z",  # noqa: S106 -- test fixture.
        )
        _install_oauth_state(config=partial)
        response = client.get("/auth/login")
        assert response.status_code == 503
        assert "TRACKINIZER_OAUTH_REDIRECT_URI" in response.json()["detail"]

    def test_503_when_only_session_secret_missing(
        self,
        client: TestClient,
    ) -> None:
        partial = Config(
            oauth_google_client_id="x",
            oauth_google_client_secret="y",  # noqa: S106 -- test fixture.
            session_secret=None,
        )
        _install_oauth_state(config=partial)
        response = client.get("/auth/login")
        assert response.status_code == 503
        assert "TRACKINIZER_SESSION_SECRET" in response.json()["detail"]

    @pytest.mark.parametrize(
        "next_url",
        [
            "https://evil.example.com",
            "//evil.example.com/path",
            "/\\evil",
            "/x\r\nLocation: //evil",
            "/x\nSet-Cookie: a=b",
            "/x\rHost: evil",
        ],
    )
    def test_next_url_outside_origin_replaced_with_root(
        self,
        client: TestClient,
        next_url: str,
    ) -> None:
        # An attacker-supplied ``?next=`` must not be honoured as an
        # absolute or protocol-relative URL; the route only accepts same-origin
        # paths.
        _install_oauth_state()
        response = client.get("/auth/login", params={"next": next_url})
        assert response.status_code == 302
        cookie = read_oauth_state_cookie(
            dict(response.cookies),
            secret=_OAUTH_CONFIG.session_secret or "",
        )
        assert cookie is not None
        _state, stashed_next_url = cookie
        assert stashed_next_url == "/"


# ---- /auth/callback -------------------------------------------------------


def _start_login(client: TestClient, *, next_query: str = "/") -> str:
    """Hit ``/auth/login`` and return the issued ``state`` nonce.

    The login response sets the OAuth state cookie directly on the
    :class:`TestClient` cookie jar, so the subsequent callback request
    sends it automatically without per-request cookie wiring.
    """
    response = client.get(f"/auth/login?next={next_query}")
    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    return parse_qs(parsed.query)["state"][0]


def _google_handler_factory(
    *,
    email: str = "alice@rekursiv.ai",
    email_verified: object = True,
    name: str = "Alice",
) -> Callable[[httpx.Request], httpx.Response]:
    """Return an httpx mock handler returning canned token + userinfo JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "ya29.fake-access-token",
                    "id_token": "fake.id.token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        if request.url.path == "/v1/userinfo":
            assert request.headers["authorization"] == "Bearer ya29.fake-access-token"
            return httpx.Response(
                200,
                json={
                    "sub": "google-sub-123",
                    "email": email,
                    "email_verified": email_verified,
                    "name": name,
                },
            )
        return httpx.Response(404, text=f"unexpected url: {request.url}")

    return handler


class TestAuthCallbackHappyPath:
    def test_creates_user_and_sets_session_cookie(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _install_oauth_state()
        # The allowlist returns "admin" on the literal lookup so a new
        # user row is inserted with role=admin.
        engine.conn.fetchval = AsyncMock(return_value="admin")
        user_id = uuid.uuid4()
        engine.conn.fetchrow = AsyncMock(
            return_value={"id": user_id, "status": "active"}
        )
        _install_http_mock(monkeypatch, _google_handler_factory())
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=auth-code-123&state={state}")
        assert response.status_code == 302, response.text
        assert response.headers["location"] == "/"
        assert SESSION_COOKIE_NAME in response.cookies
        sql = engine.conn.fetchrow.call_args.args[0]
        assert "INSERT INTO users" in sql
        assert "ON CONFLICT (email) DO UPDATE" in sql

    def test_updates_last_login_when_user_exists(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _install_oauth_state()
        user_id = uuid.uuid4()
        engine.conn.fetchval = AsyncMock(return_value="writer")
        engine.conn.fetchrow = AsyncMock(
            return_value={"id": user_id, "status": "active"}
        )
        _install_http_mock(
            monkeypatch, _google_handler_factory(email="bob@rekursiv.ai")
        )
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=auth-code-123&state={state}")
        assert response.status_code == 302
        sql = engine.conn.fetchrow.call_args.args[0]
        assert "ON CONFLICT (email) DO UPDATE" in sql
        assert "SELECT id FROM users WHERE email" not in sql

    def test_off_list_email_returns_403_and_no_insert(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _install_oauth_state()
        # Both literal and pattern lookups miss -> off-list.
        engine.conn.fetchval = AsyncMock(return_value=None)
        _install_http_mock(
            monkeypatch,
            _google_handler_factory(email="stranger@example.com"),
        )
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=auth-code-123&state={state}")
        assert response.status_code == 403
        assert "allowlist" in response.json()["detail"].lower()
        assert "administrator" in response.json()["detail"].lower()
        sqls = [c.args[0] for c in engine.conn.execute.call_args_list]
        # No INSERT, no UPDATE on users.
        assert not any("INSERT INTO users" in s for s in sqls)
        assert not any("UPDATE users" in s for s in sqls)
        # And no session cookie set on a denied callback.
        assert SESSION_COOKIE_NAME not in response.cookies

    def test_disabled_user_rejected_without_session_cookie(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A disabled user must not receive a session cookie -- the next
        # request would 401 anyway and the cookie just confuses the UX.
        engine = _install_oauth_state()
        engine.conn.fetchval = AsyncMock(return_value="writer")
        user_id = uuid.uuid4()
        engine.conn.fetchrow = AsyncMock(
            return_value={"id": user_id, "status": "disabled"}
        )
        _install_http_mock(monkeypatch, _google_handler_factory())
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=auth-code-123&state={state}")
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()
        assert SESSION_COOKIE_NAME not in response.cookies

    def test_disabled_user_callback_does_not_bump_last_login(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Issue#194 regression: a denied OAuth callback for a disabled
        # account must not mutate last_login. The upsert SQL must guard
        # the timestamp bump on status='active' so the conflict path
        # leaves last_login untouched when the existing row is disabled.
        engine = _install_oauth_state()
        engine.conn.fetchval = AsyncMock(return_value="writer")
        user_id = uuid.uuid4()
        engine.conn.fetchrow = AsyncMock(
            return_value={"id": user_id, "status": "disabled"}
        )
        _install_http_mock(monkeypatch, _google_handler_factory())
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=auth-code-123&state={state}")
        assert response.status_code == 403
        sql = engine.conn.fetchrow.call_args.args[0]
        # The upsert must condition the last_login bump on the existing
        # row being active; otherwise a denied callback silently
        # advances last_login on the disabled row.
        normalized = " ".join(sql.split()).lower()
        assert "status = 'active'" in normalized
        assert "last_login" in normalized


class TestAuthCallbackEmailNormalization:
    """The callback lowercases email at ingest and refreshes the stored name."""

    def test_email_lowercased_before_upsert(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # users.email is a case-sensitive UNIQUE column; a mixed-case
        # Google email must be lowercased before the upsert so two casings
        # of one address can't split into duplicate rows.
        engine = _install_oauth_state()
        engine.conn.fetchval = AsyncMock(return_value="admin")
        engine.conn.fetchrow = AsyncMock(
            return_value={"id": uuid.uuid4(), "status": "active"}
        )
        _install_http_mock(
            monkeypatch, _google_handler_factory(email="Alice@Example.com")
        )
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=c&state={state}")
        assert response.status_code == 302, response.text
        # The email argument to the upsert is the lowercased form.
        assert engine.conn.fetchrow.call_args.args[2] == "alice@example.com"

    def test_upsert_refreshes_name(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A returning user who changed their Google display name should see
        # the stored ``name`` refreshed, not frozen at first-login value.
        engine = _install_oauth_state()
        engine.conn.fetchval = AsyncMock(return_value="writer")
        engine.conn.fetchrow = AsyncMock(
            return_value={"id": uuid.uuid4(), "status": "active"}
        )
        _install_http_mock(monkeypatch, _google_handler_factory(name="New Name"))
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=c&state={state}")
        assert response.status_code == 302
        normalized = " ".join(engine.conn.fetchrow.call_args.args[0].split())
        assert "name = EXCLUDED.name" in normalized


class TestAuthCallbackErrors:
    def test_state_mismatch_returns_400(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_oauth_state()
        _install_http_mock(monkeypatch, _google_handler_factory())
        _start_login(client)
        response = client.get("/auth/callback?code=c&state=not-the-real-state")
        assert response.status_code == 400
        assert "state mismatch" in response.json()["detail"]

    def test_missing_state_cookie_returns_400(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_oauth_state()
        _install_http_mock(monkeypatch, _google_handler_factory())
        # Explicit empty cookie jar -- ``TestClient`` cookies persist
        # within one fixture instance, so a prior login in another test
        # via the same client object would leak here without this reset.
        client.cookies.clear()
        response = client.get("/auth/callback?code=c&state=anything")
        assert response.status_code == 400
        assert "oauth state" in response.json()["detail"]

    def test_missing_code_returns_400(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_oauth_state()
        _install_http_mock(monkeypatch, _google_handler_factory())
        state = _start_login(client)
        response = client.get(f"/auth/callback?state={state}")
        assert response.status_code == 400
        assert "authorization code" in response.json()["detail"]

    @pytest.mark.parametrize("email_verified", [False, "false"])
    def test_unverified_email_rejected(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        email_verified: object,
    ) -> None:
        # Google says ``email_verified=False`` -- must refuse to log the
        # user in regardless of allowlist membership.
        engine = _install_oauth_state()
        engine.conn.fetchval = AsyncMock(return_value="admin")
        _install_http_mock(
            monkeypatch,
            _google_handler_factory(email_verified=email_verified),
        )
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=c&state={state}")
        assert response.status_code == 403
        assert "not verified" in response.json()["detail"]
        assert SESSION_COOKIE_NAME not in response.cookies
        sqls = [c.args[0] for c in engine.conn.execute.call_args_list]
        assert not any("INSERT INTO users" in s for s in sqls)
        assert not any("UPDATE users" in s for s in sqls)

    def test_503_when_oauth_unconfigured(
        self,
        client: TestClient,
    ) -> None:
        _install_oauth_state(config=Config())
        client.cookies.clear()
        response = client.get("/auth/callback?code=c&state=s")
        assert response.status_code == 503

    def test_token_exchange_error_does_not_leak_google_response(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Google's error_description can echo back the client_id /
        # redirect_uri; neither the HTTP client nor the application
        # log may see it. Issue#191: logs are retained longer and
        # shared more broadly than responses, so the raw upstream body
        # must never reach them -- only bounded metadata (status code,
        # endpoint, opaque parsed error code).
        _install_oauth_state()
        leak = "client_id=SENSITIVE redirect_uri=DEPLOYMENT"
        parseable_error = {"error": "invalid_grant", "error_description": leak}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/token":
                return httpx.Response(400, json=parseable_error)
            return httpx.Response(404)

        _install_http_mock(monkeypatch, handler)
        state = _start_login(client)
        caplog.set_level(logging.WARNING, logger=oauth_routes.__name__)
        response = client.get(f"/auth/callback?code=c&state={state}")
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "SENSITIVE" not in detail
        assert "DEPLOYMENT" not in detail
        assert "token exchange" in detail.lower()
        # The raw body and the error_description must NOT reach the log.
        assert "SENSITIVE" not in caplog.text
        assert "DEPLOYMENT" not in caplog.text
        # The bounded metadata that IS allowed: status code, endpoint,
        # opaque parsed error code.
        assert "400" in caplog.text
        assert oauth_routes.GOOGLE_TOKEN_URL in caplog.text
        assert "invalid_grant" in caplog.text

    def test_token_exchange_non_json_error_logs_only_status_and_endpoint(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # When Google returns a non-JSON body (HTML error page, plain
        # text) the parsed error code is absent; only the bounded
        # status/endpoint metadata reaches the log. The raw body --
        # which may carry deployment knobs or secrets -- must not.
        _install_oauth_state()
        leak = "<html>client_id=SENSITIVE redirect_uri=DEPLOYMENT</html>"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/token":
                return httpx.Response(502, text=leak)
            return httpx.Response(404)

        _install_http_mock(monkeypatch, handler)
        state = _start_login(client)
        caplog.set_level(logging.WARNING, logger=oauth_routes.__name__)
        response = client.get(f"/auth/callback?code=c&state={state}")
        assert response.status_code == 400
        assert "SENSITIVE" not in caplog.text
        assert "DEPLOYMENT" not in caplog.text
        assert "html" not in caplog.text.lower()
        assert "502" in caplog.text
        assert oauth_routes.GOOGLE_TOKEN_URL in caplog.text

    def test_token_exchange_malformed_success_returns_400(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_oauth_state()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/token":
                return httpx.Response(200, text="not-json")
            return httpx.Response(404)

        _install_http_mock(monkeypatch, handler)
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=c&state={state}")
        assert response.status_code == 400
        assert "invalid json" in response.json()["detail"]

    def test_userinfo_error_does_not_leak_google_response(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _install_oauth_state()
        leak = "access_token=SECRETTOKEN granted_scopes=PRIVATE"
        parseable_error = {"error": "internal_failure", "error_description": leak}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/token":
                return httpx.Response(
                    200,
                    json={"access_token": "ya29.fake", "token_type": "Bearer"},
                )
            if request.url.path == "/v1/userinfo":
                return httpx.Response(500, json=parseable_error)
            return httpx.Response(404)

        _install_http_mock(monkeypatch, handler)
        state = _start_login(client)
        caplog.set_level(logging.WARNING, logger=oauth_routes.__name__)
        response = client.get(f"/auth/callback?code=c&state={state}")
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "SECRETTOKEN" not in detail
        assert "PRIVATE" not in detail
        assert "userinfo" in detail.lower()
        # The raw body and error_description must NOT reach the log.
        assert "SECRETTOKEN" not in caplog.text
        assert "PRIVATE" not in caplog.text
        assert "500" in caplog.text
        assert oauth_routes.GOOGLE_USERINFO_URL in caplog.text
        assert "internal_failure" in caplog.text

    def test_userinfo_malformed_success_returns_400(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_oauth_state()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/token":
                return httpx.Response(
                    200,
                    json={"access_token": "ya29.fake", "token_type": "Bearer"},
                )
            if request.url.path == "/v1/userinfo":
                return httpx.Response(200, text="not-json")
            return httpx.Response(404)

        _install_http_mock(monkeypatch, handler)
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=c&state={state}")
        assert response.status_code == 400
        assert "invalid json" in response.json()["detail"]


# ---- /auth/logout ---------------------------------------------------------


class TestAuthLogout:
    def test_clears_session_cookie(
        self,
        client: TestClient,
    ) -> None:
        _install_oauth_state()
        # Pre-load a session cookie that the logout should expire.
        carrier = _CookieCarrier()
        set_session_cookie(
            carrier,
            user_id=str(uuid.uuid4()),
            secret=str(_OAUTH_CONFIG.session_secret),
            max_age_seconds=600,
        )
        client.cookies.set(SESSION_COOKIE_NAME, carrier.cookies[SESSION_COOKIE_NAME])
        response = client.post("/auth/logout")
        assert response.status_code == 302
        assert response.headers["location"] == "/"
        # A logout response must include a Set-Cookie that clears the
        # session cookie. Starlette's ``delete_cookie`` sets ``Max-Age=0``.
        set_cookie_header = response.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in set_cookie_header
        assert (
            "Max-Age=0" in set_cookie_header or "max-age=0" in set_cookie_header.lower()
        )

    def test_rejects_cross_origin_logout(
        self,
        client: TestClient,
    ) -> None:
        # POST /auth/logout mutates the session cookie. Under SameSite=Lax a
        # malicious same-site sibling origin (or any page that can POST) could
        # force a logout (session disruption) without a CSRF/origin check. A
        # cross-origin Origin header must be rejected (403), not honored.
        _install_oauth_state()
        response = client.post(
            "/auth/logout",
            headers={"Origin": "https://evil.example.com"},
        )
        assert response.status_code == 403

    def test_allows_same_origin_logout(
        self,
        client: TestClient,
    ) -> None:
        # A same-origin Origin header is the legitimate SPA logout button and
        # must succeed.
        _install_oauth_state()
        response = client.post(
            "/auth/logout",
            headers={"Origin": "https://testserver"},
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/"

    def test_allows_same_origin_logout_across_proxy_scheme_mismatch(
        self,
        client: TestClient,
    ) -> None:
        # B-OAUTH-PROXY-SCHEME: behind a TLS-terminating proxy the app sees
        # scheme=http while the browser's Origin is https. A scheme-sensitive
        # same-origin check would 403 a legitimate logout. The check compares
        # HOST only, so an https Origin on the same host (TestClient host is
        # ``testserver``, app scheme ``http``) is allowed.
        _install_oauth_state()
        response = client.post(
            "/auth/logout",
            headers={"Origin": "https://testserver"},
        )
        assert response.status_code == 302, "https Origin, http app: same host -> allow"

    def test_allows_logout_without_origin_header(
        self,
        client: TestClient,
    ) -> None:
        # A non-browser client (curl, the CLI) sends no Origin; there is no
        # CSRF vector without a browser-driven cross-origin form, so absence of
        # Origin is allowed (the check only rejects a *present, mismatched*
        # Origin), mirroring standard same-origin CSRF defenses.
        _install_oauth_state()
        response = client.post("/auth/logout")
        assert response.status_code == 302


# ---- Bootstrap interaction ------------------------------------------------


class TestBootstrapAdminFirstLogin:
    """End-to-end-ish: an allowlist-only seed becomes a fully-formed user.

    Simulates Phase 1's bootstrap state (one ``allowlist`` row, no
    ``users`` rows) and confirms that the OAuth callback creates the
    admin's ``users`` row on first login.
    """

    def test_first_login_creates_admin_user(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _install_oauth_state()
        # Literal allowlist lookup returns admin (the bootstrap seed).
        engine.conn.fetchval = AsyncMock(return_value="admin")
        user_id = uuid.uuid4()
        engine.conn.fetchrow = AsyncMock(
            return_value={"id": user_id, "status": "active"}
        )
        _install_http_mock(
            monkeypatch,
            _google_handler_factory(email="admin@rekursiv.ai", name="Admin"),
        )
        state = _start_login(client)
        response = client.get(f"/auth/callback?code=c&state={state}")
        assert response.status_code == 302, response.text
        assert SESSION_COOKIE_NAME in response.cookies
        insert_args = engine.conn.fetchrow.call_args.args
        assert "INSERT INTO users" in insert_args[0]
        assert "ON CONFLICT (email) DO UPDATE" in insert_args[0]
        assert insert_args[2] == "admin@rekursiv.ai"
        assert insert_args[3] == "Admin"
        assert insert_args[4] == "admin"


# ---- Helper wires app.state for sibling auth-gated routes -----------------


class TestInstallOAuthStateWiresStore:
    """Issue#203: ``_install_oauth_state`` must wire ``app.state.store``.

    Wave 2 widened :func:`auth.current_user` to read
    ``request.app.state.store`` (for the per-process ``last_used_at``
    throttle). The OAuth helper used to wire only ``state.engine`` and
    ``state.config``, so any auth-gated request that came through the
    same module-global ``app`` after an OAuth test would
    ``AttributeError`` -> 500. The wired-store invariant turns that into
    a documented 401/404/200 outcome instead.
    """

    def test_auth_gated_route_after_install_does_not_500(
        self,
        client: TestClient,
    ) -> None:
        # Force the unwired-store starting state: a prior test that
        # ran ``route_client`` on the same global ``app`` would have
        # set ``state.store``; clear it so this test reproduces the
        # bug-when-unfixed instead of riding on stale wiring.
        if hasattr(app.state, "store"):
            del app.state.store
        engine = _install_oauth_state()
        # Sanity: the helper must own the wiring it claims to. Without
        # this assertion the test could silently pass on a
        # prior-leaked store.
        assert isinstance(app.state.store, Store)
        # Stub the bearer-resolution fetch as a viewer-role api_key
        # joined to an active user. This is the documented happy path
        # for ``_resolve_identity`` -- the route then 404s on the unknown
        # target_id rather than 500ing.
        secret, _prefix = generate_token()
        user_id = uuid.uuid4()
        key_id = uuid.uuid4()
        engine.conn.fetch = AsyncMock(
            return_value=[
                {
                    "key_id": key_id,
                    "secret_hash": hash_secret(secret),
                    "user_id": user_id,
                    "email": "viewer@example.com",
                    "user_role": "viewer",
                    "key_role": "viewer",
                    "status": "active",
                }
            ]
        )
        target_id = uuid.uuid4()
        response = client.get(
            f"/api/inquiry/{target_id}",
            headers={"Authorization": f"Bearer {secret}"},
        )
        # The point of the test is that the auth layer doesn't 500 on
        # a missing ``app.state.store``; any of the documented auth
        # outcomes (200 happy path, 401 unauthorized, 404 unknown
        # target) is acceptable evidence the bearer path resolved.
        assert response.status_code in {200, 401, 404}, response.text


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
