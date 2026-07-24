"""Tests for ``/api/admin/*`` routes (Phase 4 user/allowlist management)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import uuid

import asyncpg
import pytest

from trackinizer.conftest import executed_sql
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_USER_ID,
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.auth import Role, current_user


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


# ---- Helpers --------------------------------------------------------------


def _user_row(
    *,
    user_id: uuid.UUID | None = None,
    role: str = "viewer",
    status: str = "active",
    email: str = "alice@example.com",
) -> dict[str, object]:
    """One ``users``-row shape for ``fetch`` / ``fetchrow`` mocks."""
    return {
        "id": user_id or uuid.uuid4(),
        "email": email,
        "name": email.split("@", maxsplit=1)[0],
        "role": role,
        "status": status,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_login": None,
    }


def _allowlist_row(
    *,
    email_or_pattern: str = "x@example.com",
    role: str = "writer",
) -> dict[str, object]:
    """One ``allowlist``-row shape."""
    return {
        "email_or_pattern": email_or_pattern,
        "role": role,
        "added_by": uuid.uuid4(),
        "added_at": datetime(2026, 1, 1, tzinfo=UTC),
    }


def _executed_sql(engine: FakeEngine) -> list[str]:
    """Return the SQL strings captured on ``engine.conn``, in call order.

    Spans both ``execute`` and ``fetch`` via :func:`executed_sql`: ``tx()``
    issues the error-path ``ROLLBACK`` over the extended protocol (``fetch``) to
    avoid a pglite 0.5 simple-query mis-frame, so a rollback assertion must look
    at both methods.
    """
    return executed_sql(engine.conn)


# ---- Auth gating ---------------------------------------------------------


class TestAuthGating:
    def test_unauthenticated_list_users_401(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        clear_identity_override()
        r = client.get("/api/admin/users")
        assert r.status_code == 401

    def test_unauthenticated_set_role_401(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        clear_identity_override()
        r = client.put(
            f"/api/admin/users/{uuid.uuid4()}/role",
            json={"role": "admin"},
        )
        assert r.status_code == 401

    def test_unauthenticated_disable_401(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        clear_identity_override()
        r = client.post(f"/api/admin/users/{uuid.uuid4()}/disable")
        assert r.status_code == 401

    def test_unauthenticated_allowlist_list_401(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        clear_identity_override()
        r = client.get("/api/admin/allowlist")
        assert r.status_code == 401

    def test_unauthenticated_allowlist_add_401(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        clear_identity_override()
        r = client.post(
            "/api/admin/allowlist",
            json={"email_or_pattern": "x@example.com", "role": "viewer"},
        )
        assert r.status_code == 401

    def test_unauthenticated_allowlist_delete_401(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        clear_identity_override()
        r = client.delete("/api/admin/allowlist/x@example.com")
        assert r.status_code == 401

    @pytest.mark.parametrize("role", ["viewer", "writer"])
    def test_non_admin_list_users_403(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        role: str,
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(role=cast(Role, role)))
        r = client.get("/api/admin/users")
        assert r.status_code == 403
        assert role in r.json()["detail"]

    def test_non_admin_role_change_403(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(role="writer"))
        r = client.put(
            f"/api/admin/users/{uuid.uuid4()}/role",
            json={"role": "admin"},
        )
        assert r.status_code == 403

    def test_non_admin_disable_403(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(role="writer"))
        r = client.post(f"/api/admin/users/{uuid.uuid4()}/disable")
        assert r.status_code == 403

    def test_non_admin_allowlist_post_403(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(role="viewer"))
        r = client.post(
            "/api/admin/allowlist",
            json={"email_or_pattern": "x@example.com", "role": "viewer"},
        )
        assert r.status_code == 403

    def test_non_admin_allowlist_role_403(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(role="writer"))
        r = client.put(
            "/api/admin/allowlist/x@example.com/role",
            json={"role": "admin"},
        )
        assert r.status_code == 403

    def test_unauthenticated_allowlist_role_401(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        clear_identity_override()
        r = client.put(
            "/api/admin/allowlist/x@example.com/role",
            json={"role": "viewer"},
        )
        assert r.status_code == 401


# ---- Happy paths ---------------------------------------------------------


class TestAdminUsers:
    def test_list_returns_rows(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        row = _user_row(email="bob@example.com", role="writer")
        engine.conn.fetch = AsyncMock(return_value=[row])
        r = client.get("/api/admin/users")
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["users"]) == 1
        assert body["users"][0]["email"] == "bob@example.com"
        assert body["users"][0]["role"] == "writer"
        assert body["users"][0]["status"] == "active"
        # ``id`` and ``created_at`` are serialized for the table view.
        assert body["users"][0]["id"] == str(row["id"])
        assert "created_at" in body["users"][0]

    def test_role_change_happy_path(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        target = uuid.uuid4()
        r = client.put(
            f"/api/admin/users/{target}/role",
            json={"role": "admin"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "role": "admin"}
        sqls = _executed_sql(engine)
        assert any("UPDATE users SET role" in s for s in sqls)

    def test_role_change_404_when_no_row(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 0")
        r = client.put(
            f"/api/admin/users/{uuid.uuid4()}/role",
            json={"role": "viewer"},
        )
        assert r.status_code == 404

    def test_role_change_rejects_bogus_role(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(role="admin"))
        r = client.put(
            f"/api/admin/users/{uuid.uuid4()}/role",
            json={"role": "superuser"},
        )
        # Pydantic literal validation rejects the unknown role before
        # the SQL UPDATE ever runs.
        assert r.status_code == 422

    def test_self_demote_refused_409(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        r = client.put(
            f"/api/admin/users/{TEST_USER_ID}/role",
            json={"role": "viewer"},
        )
        assert r.status_code == 409, r.text
        assert not any(
            "UPDATE users SET role" in c.args[0]
            for c in engine.conn.execute.call_args_list
        )

    def test_disable_revokes_tokens(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """The disable path must revoke outstanding tokens in one shot.

        Regression test for the documented Phase 4 invariant: disabling
        a user revokes their tokens so a stolen credential can't keep
        working past the click.
        """
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        target = uuid.uuid4()
        r = client.post(f"/api/admin/users/{target}/disable")
        assert r.status_code == 200, r.text
        sqls = _executed_sql(engine)
        assert any("UPDATE users SET status = 'disabled'" in s for s in sqls)
        # And the per-user api_keys revocation actually fires.
        assert any(
            "UPDATE api_keys SET revoked_at" in s and "WHERE user_id = $1" in s
            for s in sqls
        )

    def test_disable_404_when_no_row(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 0")
        r = client.post(f"/api/admin/users/{uuid.uuid4()}/disable")
        assert r.status_code == 404
        sqls = _executed_sql(engine)
        # Crucially, the api_keys revoke never runs when the user
        # lookup missed -- otherwise an attacker probing UUIDs could
        # blow away foreign users' tokens.
        assert not any("UPDATE api_keys" in s for s in sqls)

    def test_self_disable_refused_409(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        r = client.post(f"/api/admin/users/{TEST_USER_ID}/disable")
        assert r.status_code == 409, r.text
        assert not any(
            "UPDATE users SET status = 'disabled'" in c.args[0]
            for c in engine.conn.execute.call_args_list
        )

    def test_disable_revocation_failure_rolls_back_user_status(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))

        async def execute(sql: str, *args: object) -> str:
            del args
            if "UPDATE api_keys SET revoked_at" in sql:
                raise RuntimeError("revocation failed")
            return "UPDATE 1"

        engine.conn.execute = AsyncMock(side_effect=execute)
        with pytest.raises(RuntimeError, match="revocation failed"):
            client.post(f"/api/admin/users/{uuid.uuid4()}/disable")
        assert _executed_sql(engine) == [
            "BEGIN",
            "SELECT pg_advisory_xact_lock(hashtext('admin_roster'))",
            "UPDATE users SET status = 'disabled' WHERE id = $1",
            (
                "UPDATE api_keys SET revoked_at = clock_timestamp() "
                "WHERE user_id = $1 AND revoked_at IS NULL"
            ),
            "ROLLBACK",
        ]

    def test_enable_happy_path(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        r = client.post(f"/api/admin/users/{uuid.uuid4()}/enable")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active"
        sqls = _executed_sql(engine)
        assert any("UPDATE users SET status = 'active'" in s for s in sqls)


class TestLastAdminGuard:
    """Refuse demote/disable/delete of the org's final active admin.

    Two-admin orgs that lose one admin without a second to escalate to
    are locked out: ``bootstrap_admin`` is a no-op once any user row
    exists, and no in-app role-recovery path exists. Each mutating
    admin route must therefore verify that an active-admin count
    precondition survives the operation: the count of active admins
    (``role='admin' AND status='active'``) excluding the target must
    remain at least one.
    """

    @staticmethod
    def _stub_target_active_admin(engine: FakeEngine, others: int) -> None:
        """Mock the guard row: target is an active admin with ``others`` peers."""
        engine.conn.fetchrow = AsyncMock(
            return_value={
                "target_is_admin_active": True,
                "other_admins": others,
            }
        )

    def test_demote_last_admin_409(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        self._stub_target_active_admin(engine, others=0)
        target = uuid.uuid4()
        r = client.put(
            f"/api/admin/users/{target}/role",
            json={"role": "viewer"},
        )
        assert r.status_code == 409, r.text
        assert "last_admin" in r.json()["detail"]
        assert not any(
            "UPDATE users SET role" in c.args[0]
            for c in engine.conn.execute.call_args_list
        )

    def test_disable_last_admin_409(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        self._stub_target_active_admin(engine, others=0)
        target = uuid.uuid4()
        r = client.post(f"/api/admin/users/{target}/disable")
        assert r.status_code == 409, r.text
        assert "last_admin" in r.json()["detail"]
        assert not any(
            "UPDATE users SET status = 'disabled'" in c.args[0]
            for c in engine.conn.execute.call_args_list
        )

    def test_delete_last_admin_409(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="DELETE 1")
        self._stub_target_active_admin(engine, others=0)
        target = uuid.uuid4()
        r = client.delete(f"/api/admin/users/{target}")
        assert r.status_code == 409, r.text
        assert "last_admin" in r.json()["detail"]
        assert not any(
            "DELETE FROM users" in c.args[0] for c in engine.conn.execute.call_args_list
        )

    def test_demote_non_admin_target_permitted(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Guard only fires when the target itself is an active admin."""
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        engine.conn.fetchrow = AsyncMock(
            return_value={
                "target_is_admin_active": False,
                "other_admins": 0,
            }
        )
        r = client.put(
            f"/api/admin/users/{uuid.uuid4()}/role",
            json={"role": "viewer"},
        )
        assert r.status_code == 200, r.text

    def test_demote_when_peer_admin_exists_permitted(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        self._stub_target_active_admin(engine, others=1)
        r = client.put(
            f"/api/admin/users/{uuid.uuid4()}/role",
            json={"role": "viewer"},
        )
        assert r.status_code == 200, r.text

    def test_role_change_to_admin_skips_guard(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Promotions/lateral-to-admin never reduce the admin count."""
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        # No fetchrow stub: the guard query should not run.
        r = client.put(
            f"/api/admin/users/{uuid.uuid4()}/role",
            json={"role": "admin"},
        )
        assert r.status_code == 200, r.text
        assert engine.conn.fetchrow.call_count == 0

    def test_demote_serializes_under_transaction(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Demote wraps guard+UPDATE in BEGIN/COMMIT with a roster lock.

        Without a transaction and a serializing lock, two concurrent
        demotes of distinct admins under Read Committed each see one
        peer admin and both succeed -- zero active admins remain. The
        route must (1) BEGIN, (2) take a fixed advisory roster lock so
        peer mutators serialize, (3) run the guard, (4) UPDATE,
        (5) COMMIT.
        """
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        self._stub_target_active_admin(engine, others=1)
        r = client.put(
            f"/api/admin/users/{uuid.uuid4()}/role",
            json={"role": "viewer"},
        )
        assert r.status_code == 200, r.text
        sqls = _executed_sql(engine)
        assert sqls[0] == "BEGIN"
        assert sqls[-1] == "COMMIT"
        assert any("pg_advisory_xact_lock" in s for s in sqls)
        lock_idx = next(i for i, s in enumerate(sqls) if "pg_advisory_xact_lock" in s)
        update_idx = next(i for i, s in enumerate(sqls) if "UPDATE users SET role" in s)
        assert lock_idx < update_idx

    def test_delete_serializes_under_transaction(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Hard-delete wraps guard+DELETE in BEGIN/COMMIT with a roster lock."""
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="DELETE 1")
        self._stub_target_active_admin(engine, others=1)
        r = client.delete(f"/api/admin/users/{uuid.uuid4()}")
        assert r.status_code == 204, r.text
        sqls = _executed_sql(engine)
        assert sqls[0] == "BEGIN"
        assert sqls[-1] == "COMMIT"
        assert any("pg_advisory_xact_lock" in s for s in sqls)
        lock_idx = next(i for i, s in enumerate(sqls) if "pg_advisory_xact_lock" in s)
        delete_idx = next(i for i, s in enumerate(sqls) if "DELETE FROM users" in s)
        assert lock_idx < delete_idx

    def test_disable_takes_roster_lock(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Disable's existing tx must also acquire the roster lock."""
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        self._stub_target_active_admin(engine, others=1)
        r = client.post(f"/api/admin/users/{uuid.uuid4()}/disable")
        assert r.status_code == 200, r.text
        sqls = _executed_sql(engine)
        assert any("pg_advisory_xact_lock" in s for s in sqls)
        lock_idx = next(i for i, s in enumerate(sqls) if "pg_advisory_xact_lock" in s)
        update_idx = next(
            i for i, s in enumerate(sqls) if "UPDATE users SET status = 'disabled'" in s
        )
        assert lock_idx < update_idx

    def test_demote_404_rolls_back_transaction(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A 404 on demote must roll back, not leak a half-open tx."""
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 0")
        engine.conn.fetchrow = AsyncMock(
            return_value={"target_is_admin_active": False, "other_admins": 0}
        )
        r = client.put(
            f"/api/admin/users/{uuid.uuid4()}/role",
            json={"role": "viewer"},
        )
        assert r.status_code == 404
        sqls = _executed_sql(engine)
        assert sqls[0] == "BEGIN"
        assert sqls[-1] in {"ROLLBACK", "COMMIT"}

    def test_delete_404_rolls_back_transaction(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A 404 on delete must close the transaction cleanly."""
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="DELETE 0")
        engine.conn.fetchrow = AsyncMock(
            return_value={"target_is_admin_active": False, "other_admins": 0}
        )
        r = client.delete(f"/api/admin/users/{uuid.uuid4()}")
        assert r.status_code == 404
        sqls = _executed_sql(engine)
        assert sqls[0] == "BEGIN"
        assert sqls[-1] in {"ROLLBACK", "COMMIT"}


class TestAdminRemoveUser:
    """``DELETE /api/admin/users/{id}`` -- hard-delete via the admin UI.

    Cascade behaviour (``api_keys`` removed, ``change_log.api_key_id``
    nulled) is enforced by FKs in :mod:`assets/schema.sql` and exercised
    end-to-end in :mod:`integration_test.py`; here we cover the route
    plumbing (auth, self-delete refusal, 404, success status).
    """

    def test_unauthenticated_401(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        clear_identity_override()
        r = client.delete(f"/api/admin/users/{uuid.uuid4()}")
        assert r.status_code == 401

    @pytest.mark.parametrize("role", ["viewer", "writer"])
    def test_non_admin_403(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        role: str,
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(role=cast(Role, role)))
        r = client.delete(f"/api/admin/users/{uuid.uuid4()}")
        assert r.status_code == 403

    def test_admin_delete_returns_204(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="DELETE 1")
        r = client.delete(f"/api/admin/users/{uuid.uuid4()}")
        assert r.status_code == 204, r.text
        # Empty body on 204 -- FastAPI's TestClient returns "".
        assert r.content == b""
        sqls = _executed_sql(engine)
        assert any("DELETE FROM users WHERE id = $1" in s for s in sqls)

    def test_404_when_user_missing(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="DELETE 0")
        r = client.delete(f"/api/admin/users/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_self_delete_refused_409(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Admin cannot DELETE their own row -- 409 with a clear detail.

        Guards against the "I'm the only admin" lockout. Multi-admin
        orgs rotate by having one admin delete the other.
        """
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        # Re-mock so a stray SQL call would surface as an unexpected
        # ``DELETE 1`` and fail the post-condition assertion below.
        engine.conn.execute = AsyncMock(return_value="DELETE 1")
        r = client.delete(f"/api/admin/users/{TEST_USER_ID}")
        assert r.status_code == 409, r.text
        assert "own account" in r.json()["detail"]
        # Crucially: the self-delete guard fires *before* the SQL,
        # otherwise an admin could nuke themselves and rely on a stale
        # 409 message to suggest the row survived.
        assert not any(
            "DELETE FROM users" in c.args[0] for c in engine.conn.execute.call_args_list
        )


class TestAdminAllowlist:
    def test_list_returns_rows(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.fetch = AsyncMock(
            return_value=[_allowlist_row(email_or_pattern="*@example.com")]
        )
        r = client.get("/api/admin/allowlist")
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["entries"]) == 1
        assert body["entries"][0]["email_or_pattern"] == "*@example.com"
        assert body["entries"][0]["role"] == "writer"

    def test_add_happy_path_stamps_added_by(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="INSERT 0 1")
        r = client.post(
            "/api/admin/allowlist",
            json={"email_or_pattern": "new@example.com", "role": "viewer"},
        )
        assert r.status_code == 200, r.text
        # ``added_by`` is server-stamped from the authenticated principal.
        insert = next(
            c
            for c in engine.conn.execute.call_args_list
            if "INSERT INTO allowlist" in c.args[0]
        )
        assert insert.args[1] == "new@example.com"
        assert insert.args[2] == "viewer"
        assert insert.args[3] == TEST_USER_ID

    def test_add_strips_and_lowercases_literal_email(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        r = client.post(
            "/api/admin/allowlist",
            json={"email_or_pattern": " Alice@Example.com ", "role": "viewer"},
        )
        assert r.status_code == 200, r.text
        insert = next(
            c
            for c in engine.conn.execute.call_args_list
            if "INSERT INTO allowlist" in c.args[0]
        )
        assert insert.args[1] == "alice@example.com"
        assert r.json()["email_or_pattern"] == "alice@example.com"

    def test_add_lowercases_wildcard_pattern_domain(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        r = client.post(
            "/api/admin/allowlist",
            json={"email_or_pattern": "*@Example.com", "role": "viewer"},
        )
        assert r.status_code == 200, r.text
        insert = next(
            c
            for c in engine.conn.execute.call_args_list
            if "INSERT INTO allowlist" in c.args[0]
        )
        assert insert.args[1] == "*@example.com"
        assert r.json()["email_or_pattern"] == "*@example.com"

    def test_set_allowlist_role_canonicalizes_path_param(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        r = client.put(
            "/api/admin/allowlist/*@Example.com/role",
            json={"role": "writer"},
        )
        assert r.status_code == 200, r.text
        update = next(
            c
            for c in engine.conn.execute.call_args_list
            if "UPDATE allowlist" in c.args[0]
        )
        assert update.args[1] == "*@example.com"

    def test_remove_allowlist_canonicalizes_path_param(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="DELETE 1")
        r = client.delete("/api/admin/allowlist/*@Example.com")
        assert r.status_code == 200, r.text
        delete = next(
            c
            for c in engine.conn.execute.call_args_list
            if "DELETE FROM allowlist" in c.args[0]
        )
        assert delete.args[1] == "*@example.com"

    def test_add_duplicate_409s(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Re-adding an existing ``email_or_pattern`` surfaces 409.

        The schema's PRIMARY KEY on ``email_or_pattern`` is enforced by
        asyncpg's :class:`UniqueViolationError`; the global handler in
        :mod:`api.app` maps it to 409 without per-route try/except.
        """
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(
            side_effect=asyncpg.UniqueViolationError(
                "duplicate key value violates unique constraint"
            )
        )
        r = client.post(
            "/api/admin/allowlist",
            json={"email_or_pattern": "dup@example.com", "role": "viewer"},
        )
        assert r.status_code == 409

    def test_remove_happy_path(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="DELETE 1")
        r = client.delete("/api/admin/allowlist/x@example.com")
        assert r.status_code == 200, r.text

    def test_remove_404_when_no_row(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="DELETE 0")
        r = client.delete("/api/admin/allowlist/x@example.com")
        assert r.status_code == 404

    def test_remove_url_encoded_pattern(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """``*@example.com`` round-trips through percent-encoded URL.

        FastAPI/Starlette decodes ``%2A%40example.com`` back to
        ``*@example.com`` so the path param matches the stored row.
        """
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="DELETE 1")
        r = client.delete("/api/admin/allowlist/%2A%40example.com")
        assert r.status_code == 200
        delete = next(
            c
            for c in engine.conn.execute.call_args_list
            if "DELETE FROM allowlist" in c.args[0]
        )
        assert delete.args[1] == "*@example.com"

    def test_remove_404_when_missing(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="DELETE 0")
        r = client.delete("/api/admin/allowlist/missing@example.com")
        assert r.status_code == 404

    def test_role_change_happy_path(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        r = client.put(
            "/api/admin/allowlist/x@example.com/role",
            json={"role": "admin"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "role": "admin"}
        update = next(
            c
            for c in engine.conn.execute.call_args_list
            if "UPDATE allowlist SET role" in c.args[0]
        )
        assert update.args[1] == "x@example.com"
        assert update.args[2] == "admin"

    def test_role_change_url_encoded_pattern(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """``*@example.com`` round-trips through percent-encoding.

        Same FastAPI/Starlette decode that the DELETE path relies on:
        ``%2A%40example.com`` arrives at the handler as ``*@example.com``.
        """
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        r = client.put(
            "/api/admin/allowlist/%2A%40example.com/role",
            json={"role": "writer"},
        )
        assert r.status_code == 200, r.text
        update = next(
            c
            for c in engine.conn.execute.call_args_list
            if "UPDATE allowlist SET role" in c.args[0]
        )
        assert update.args[1] == "*@example.com"

    def test_role_change_404_when_missing(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.execute = AsyncMock(return_value="UPDATE 0")
        r = client.put(
            "/api/admin/allowlist/missing@example.com/role",
            json={"role": "viewer"},
        )
        assert r.status_code == 404

    def test_role_change_rejects_bogus_role(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(role="admin"))
        r = client.put(
            "/api/admin/allowlist/x@example.com/role",
            json={"role": "superuser"},
        )
        assert r.status_code == 422


# ---- /api/me/profile -----------------------------------------------------


class TestProfileRoute:
    def test_returns_identity(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="admin"))
        engine.conn.fetchrow = AsyncMock(
            return_value={
                "name": "Alice",
                "last_login": datetime(2026, 1, 1, tzinfo=UTC),
            }
        )
        r = client.get("/api/me/profile")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["email"]
        assert body["role"] == "admin"
        assert body["name"] == "Alice"
        assert body["last_login"].startswith("2026")

    def test_401_without_auth(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        app.dependency_overrides.pop(current_user, None)
        r = client.get("/api/me/profile")
        assert r.status_code == 401


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
