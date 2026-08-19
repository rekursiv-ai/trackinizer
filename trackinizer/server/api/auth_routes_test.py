"""Tests for ``/api/me/tokens*`` routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import uuid

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.auth import AuthIdentity, current_user, generate_token


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


@pytest.fixture
def auth_identity() -> AuthIdentity:
    """A stable test principal injected by the dependency override."""
    return AuthIdentity(
        user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        api_key_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        email="alice@example.com",
        role="writer",
    )


@pytest.fixture(autouse=True)
def override_current_user(auth_identity: AuthIdentity) -> object:
    """Replace :func:`current_user` with a static-identity stub for these tests.

    Bearer-header parsing has its own test coverage in ``auth_test.py``;
    here we focus on route behaviour. The override is removed after
    each test so unrelated suites still exercise the real dependency.
    """

    async def fake_current_user() -> AuthIdentity:
        return auth_identity

    app.dependency_overrides[current_user] = fake_current_user
    yield None
    app.dependency_overrides.pop(current_user, None)


class TestCreateToken:
    def test_returns_secret_once(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        # ``create_api_key`` SELECTs ``users.role`` to enforce the
        # ceiling; without a value the call surfaces as LookupError.
        engine.conn.fetchval = AsyncMock(return_value="writer")
        r = client.post("/api/me/tokens", json={"name": "laptop"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "laptop"
        # Defaults to caller's user role when ``role`` is omitted.
        assert body["role"] == "writer"
        # The plaintext secret is the *only* return path; clients
        # cannot re-derive it later.
        assert body["secret"].startswith("trax_")
        # And the INSERT into api_keys actually fired.
        sqls = [c.args[0] for c in engine.conn.execute.call_args_list]
        assert any("INSERT INTO api_keys" in s for s in sqls)

    def test_rejects_blank_name(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        r = client.post("/api/me/tokens", json={"name": ""})
        assert r.status_code == 422

    def test_writer_cannot_mint_admin_token(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A writer asking for ``role=admin`` is refused with 403.

        This is the trust anchor for per-key ceilings: nothing under
        the user's own role is permitted, and exceeding the ceiling
        does not silently downgrade -- it fails loud so a misbehaving
        agent can't claim it got an admin token.
        """
        client, _store, engine = route_client
        engine.conn.fetchval = AsyncMock(return_value="writer")
        r = client.post("/api/me/tokens", json={"name": "x", "role": "admin"})
        assert r.status_code == 403
        # And no INSERT into api_keys -- the ceiling check fires
        # before the write.
        sqls = [c.args[0] for c in engine.conn.execute.call_args_list]
        assert not any("INSERT INTO api_keys" in s for s in sqls)

    def test_writer_can_mint_viewer_token(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A writer can mint a weaker viewer token (read-only agent).

        Demonstrates the dual of the ceiling: capping *down* is the
        whole reason per-key roles exist.
        """
        client, _store, engine = route_client
        engine.conn.fetchval = AsyncMock(return_value="writer")
        r = client.post("/api/me/tokens", json={"name": "ro", "role": "viewer"})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "viewer"

    def test_scoped_key_cannot_mint_above_its_ceiling(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A viewer-scoped key of an admin user cannot mint an admin token.

        ``identity.role`` is the effective role of the *presented* credential
        (the weaker of the user's standing role and the key's ceiling). The
        mint must cap at that effective role, not at the owning user's row --
        otherwise a scoped-down agent key self-escalates to admin, which is
        the whole point of handing agents narrow per-spawn keys.
        """
        client, _store, engine = route_client

        async def viewer_key_of_admin_user() -> AuthIdentity:
            return AuthIdentity(
                user_id=uuid.uuid4(),
                api_key_id=uuid.uuid4(),
                email="admin-through-viewer-key@example.com",
                role="viewer",
            )

        app.dependency_overrides[current_user] = viewer_key_of_admin_user
        # The owning user genuinely is an admin in the DB...
        engine.conn.fetchval = AsyncMock(return_value="admin")
        # ...but the presented credential is viewer-scoped, so admin is refused.
        r = client.post("/api/me/tokens", json={"name": "escalate", "role": "admin"})
        assert r.status_code == 403, r.text
        sqls = [c.args[0] for c in engine.conn.execute.call_args_list]
        assert not any("INSERT INTO api_keys" in s for s in sqls)


class TestListTokens:
    def test_returns_rows_without_hashes(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        auth_identity: AuthIdentity,
    ) -> None:
        client, _store, engine = route_client
        key_id = uuid.uuid4()
        engine.conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": key_id,
                    "name": "laptop",
                    "prefix": "trax_aBcDeFgH",
                    "role": "viewer",
                    "created_at": datetime(2025, 1, 1, tzinfo=UTC),
                    "last_used_at": None,
                    "revoked_at": None,
                }
            ]
        )
        r = client.get("/api/me/tokens")
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["tokens"]) == 1
        tok = body["tokens"][0]
        assert tok["id"] == str(key_id)
        assert tok["prefix"] == "trax_aBcDeFgH"
        # The new ``role`` column lands in the wire shape so the UI can
        # render a per-row dropdown.
        assert tok["role"] == "viewer"
        # The wire shape must not leak ``secret_hash`` or a recoverable
        # plaintext -- this is the central confidentiality invariant.
        assert "secret_hash" not in tok
        assert "secret" not in tok
        # The query must be scoped to the caller's ``user_id``; an
        # accidental "SELECT * FROM api_keys" would leak everyone's
        # rows.
        fetch_call = engine.conn.fetch.call_args
        assert fetch_call.args[1] == auth_identity.user_id


class TestRevokeToken:
    def test_revokes_when_owned(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        # asyncpg returns ``"UPDATE 1"`` when the predicate matched.
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        r = client.post(f"/api/me/tokens/{uuid.uuid4()}/revoke")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}

    def test_404_when_not_owned(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        # ``UPDATE 0`` -- the WHERE clause didn't match any row, which
        # covers unknown id, foreign owner, and already-revoked alike.
        engine.conn.execute = AsyncMock(return_value="UPDATE 0")
        r = client.post(f"/api/me/tokens/{uuid.uuid4()}/revoke")
        assert r.status_code == 404

    def test_revoke_evicts_only_the_revoked_key(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        auth_identity: AuthIdentity,
    ) -> None:
        # Revoking one key must not flush the caller's OTHER keys: they are
        # still valid credentials, and evicting them buys nothing while
        # costing each a fresh 30ms scrypt on its next request. Eviction is
        # scoped to the credential that actually changed.
        client, store, engine = route_client
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        revoked_key = uuid.uuid4()
        revoked_secret, _ = generate_token()
        kept_secret, _ = generate_token()
        store.remember_bearer_identity(
            revoked_secret,
            AuthIdentity(
                user_id=auth_identity.user_id,
                api_key_id=revoked_key,
                email=auth_identity.email,
                role=auth_identity.role,
            ),
        )
        store.remember_bearer_identity(
            kept_secret,
            AuthIdentity(
                user_id=auth_identity.user_id,
                api_key_id=uuid.uuid4(),
                email=auth_identity.email,
                role=auth_identity.role,
            ),
        )
        r = client.post(f"/api/me/tokens/{revoked_key}/revoke")
        assert r.status_code == 200, r.text
        assert store.cached_bearer_identity(revoked_secret) is None
        assert store.cached_bearer_identity(kept_secret) is not None

    def test_404_revoke_leaves_the_cache_intact(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        auth_identity: AuthIdentity,
    ) -> None:
        # A revoke that matched no row changed no credential; evicting on
        # that path would let an unauthenticated UUID probe flush a live
        # principal's cache and force a scrypt round per guess.
        client, store, engine = route_client
        engine.conn.execute = AsyncMock(return_value="UPDATE 0")
        secret, _ = generate_token()
        store.remember_bearer_identity(secret, auth_identity)
        r = client.post(f"/api/me/tokens/{uuid.uuid4()}/revoke")
        assert r.status_code == 404
        assert store.cached_bearer_identity(secret) is not None


class TestSetTokenRole:
    def test_promote_within_ceiling_succeeds(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # User is writer; promoting one of their viewer keys to writer
        # is at-ceiling and allowed.
        client, _store, engine = route_client
        engine.conn.fetchval = AsyncMock(return_value="writer")
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        r = client.put(
            f"/api/me/tokens/{uuid.uuid4()}/role",
            json={"role": "writer"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "role": "writer"}
        sqls = [c.args[0] for c in engine.conn.execute.call_args_list]
        assert any("UPDATE api_keys SET role" in s for s in sqls)

    def test_above_ceiling_is_403(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # User is writer; asking for admin must fail. The UPDATE never
        # runs -- the ceiling check is the trust boundary.
        client, _store, engine = route_client
        engine.conn.fetchval = AsyncMock(return_value="writer")
        r = client.put(
            f"/api/me/tokens/{uuid.uuid4()}/role",
            json={"role": "admin"},
        )
        assert r.status_code == 403
        sqls = [c.args[0] for c in engine.conn.execute.call_args_list]
        assert not any("UPDATE api_keys SET role" in s for s in sqls)

    def test_404_when_not_owned(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # Ceiling is fine, but the row doesn't match (foreign owner /
        # unknown id / revoked) -- 404 to avoid leaking ownership.
        client, _store, engine = route_client
        engine.conn.fetchval = AsyncMock(return_value="writer")
        engine.conn.execute = AsyncMock(return_value="UPDATE 0")
        r = client.put(
            f"/api/me/tokens/{uuid.uuid4()}/role",
            json={"role": "viewer"},
        )
        assert r.status_code == 404

    def test_scoped_key_cannot_retier_above_its_ceiling(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Re-tiering caps at the presented credential's effective role too.

        The sibling of the create-token escalation: an admin acting through
        a viewer-scoped key must not promote an owned key to admin. The row
        exists (so it is not a 404 masking the ceiling), yet the ceiling
        refuses it -- 403, no UPDATE.
        """
        client, _store, engine = route_client

        async def viewer_key_of_admin_user() -> AuthIdentity:
            return AuthIdentity(
                user_id=uuid.uuid4(),
                api_key_id=uuid.uuid4(),
                email="admin-through-viewer-key@example.com",
                role="viewer",
            )

        app.dependency_overrides[current_user] = viewer_key_of_admin_user
        # User role "admin"; existence probe finds the owned live key.
        engine.conn.fetchval = AsyncMock(side_effect=["admin", 1])
        r = client.put(
            f"/api/me/tokens/{uuid.uuid4()}/role",
            json={"role": "admin"},
        )
        assert r.status_code == 403, r.text
        sqls = [c.args[0] for c in engine.conn.execute.call_args_list]
        assert not any("UPDATE api_keys SET role" in s for s in sqls)

    def test_unknown_key_above_ceiling_is_404_not_403(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # The key doesn't exist (no row matches the existence probe), yet
        # the requested role exceeds the caller's ceiling. Existence must
        # be checked first: a 403 here would leak that the key never
        # existed (an attacker probing UUIDs could tell revoked from
        # absent). 404. The role probe returns the user role; the
        # key-existence probe returns no row.
        client, _store, engine = route_client

        async def _fetchval(sql: str, *args: object) -> object:
            del args
            return "writer" if "FROM users" in sql else None

        engine.conn.fetchval = AsyncMock(side_effect=_fetchval)
        engine.conn.execute = AsyncMock(return_value="UPDATE 0")
        r = client.put(
            f"/api/me/tokens/{uuid.uuid4()}/role",
            json={"role": "admin"},
        )
        assert r.status_code == 404

    def test_rejects_bogus_role(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        r = client.put(
            f"/api/me/tokens/{uuid.uuid4()}/role",
            json={"role": "superuser"},
        )
        assert r.status_code == 422


class TestRouteRequiresAuth:
    """Without an override, the routes must demand a bearer token."""

    def test_create_without_auth_is_401(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        # Drop the auto-applied override so the real ``current_user``
        # runs against an empty Authorization header.
        client, _store, _engine = route_client
        app.dependency_overrides.pop(current_user, None)
        r = client.post("/api/me/tokens", json={"name": "x"})
        assert r.status_code == 401


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
