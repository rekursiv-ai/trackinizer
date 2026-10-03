"""Tests for ``POST /auth/logout``."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from trackinizer.server.api import logout_routes
from trackinizer.server.session import SESSION_COOKIE_NAME


@pytest.fixture
def client() -> TestClient:
    """``TestClient`` over the logout router, with redirect-following off.

    ``base_url`` is HTTPS so the ``Secure`` session cookie survives in the
    cookie jar; httpx2 drops Secure cookies on plain ``http://testserver``.
    """
    app = FastAPI()
    app.include_router(logout_routes.router)
    return TestClient(app, follow_redirects=False, base_url="https://testserver")


class TestAuthLogout:
    def test_clears_session_cookie(self, client: TestClient) -> None:
        client.cookies.set(SESSION_COOKIE_NAME, "a-session")
        response = client.post("/auth/logout")
        assert response.status_code == 302
        assert response.headers["location"] == "/"
        # Starlette's ``delete_cookie`` clears it with ``Max-Age=0``.
        set_cookie_header = response.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in set_cookie_header
        assert "max-age=0" in set_cookie_header.lower()

    def test_rejects_cross_origin_logout(self, client: TestClient) -> None:
        # POST /auth/logout mutates the session cookie. Under SameSite=Lax a
        # malicious same-site sibling origin (or any page that can POST) could
        # force a logout (session disruption) without a CSRF/origin check. A
        # cross-origin Origin header must be rejected (403), not honored.
        response = client.post(
            "/auth/logout",
            headers={"Origin": "https://evil.example.com"},
        )
        assert response.status_code == 403

    def test_allows_same_origin_logout(self, client: TestClient) -> None:
        # A same-origin Origin header is the web app's sign-out and must succeed.
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
        # Behind a TLS-terminating proxy the app sees scheme=http while the
        # browser's Origin is https. The check compares HOST only, so an http
        # Origin on the same host as this https client is allowed too.
        response = client.post(
            "/auth/logout",
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 302, "same host, other scheme -> allow"

    def test_allows_logout_without_origin_header(self, client: TestClient) -> None:
        # A non-browser client (curl, the CLI) sends no Origin; there is no
        # CSRF vector without a browser-driven cross-origin form, so absence of
        # Origin is allowed (the check only rejects a *present, mismatched*
        # Origin), mirroring standard same-origin CSRF defenses.
        response = client.post("/auth/logout")
        assert response.status_code == 302

    def test_falls_back_to_referer_without_origin(self, client: TestClient) -> None:
        response = client.post(
            "/auth/logout",
            headers={"Referer": "https://evil.example.com/page"},
        )
        assert response.status_code == 403

    @pytest.mark.parametrize("header", ["Origin", "Referer"])
    def test_rejects_an_unparsable_origin(
        self,
        client: TestClient,
        header: str,
    ) -> None:
        # ``https://[`` opens an IPv6 host it never closes, so it names no
        # origin, and so not this server's.
        client.cookies.set(SESSION_COOKIE_NAME, "a-session")
        response = client.post("/auth/logout", headers={header: "https://["})
        assert response.status_code == 403
        assert "set-cookie" not in response.headers


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
