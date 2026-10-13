"""Tests for ``/api/admin/service-accounts*`` against a scripted connection.

The security properties are proved end to end over PGlite in
``service_account_routes_pglite_test.py``; these pin the route plumbing: which
statements run, in which order, and what is answered and logged.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import logging
import uuid

from treekle import from_plain

import pytest

from trackinizer.conftest import executed_sql
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.auth import AuthIdentity, Role, generate_token
from trackinizer.server.config import Config, parse_assistant


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


type _Client = tuple[TestClient, Store, FakeEngine]

_LOGGER = "trackinizer.server.api.service_account_routes"
_BOTH = frozenset({"open", "staff"})
_WHEN = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
_ACCOUNT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_KEY_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_ACCOUNTS = "/api/admin/service-accounts"
_TOKENS = f"{_ACCOUNTS}/{_ACCOUNT_ID}/tokens"


def _account_row(
    *,
    name: str = "campaign",
    role: str = "writer",
    status: str = "active",
) -> dict[str, object]:
    """One service ``users`` row, as the account statements select it."""
    return {
        "id": _ACCOUNT_ID,
        "email": f"{name}@service.invalid",
        "name": name,
        "role": role,
        "status": status,
        "created_at": _WHEN,
        "last_login": None,
        "hosted_domain": None,
    }


def _key_row(*, revoked: bool = False) -> dict[str, object]:
    """One ``api_keys`` row, as ``list_api_keys`` returns it."""
    return {
        "id": _KEY_ID,
        "name": "host",
        "prefix": "trax_aBcDeFgH",
        "role": "writer",
        "open_into": "open",
        "created_at": _WHEN,
        "last_used_at": None,
        "revoked_at": _WHEN if revoked else None,
    }


def _admin(*, staff: bool = False, key: bool = True) -> AuthIdentity:
    """Serve the next requests as an admin who does, or does not, hold ``staff``."""
    identity = make_test_identity(
        role="admin",
        api_key_id=uuid.uuid4() if key else None,
    )
    if staff:
        identity = replace(identity, read_audiences=_BOTH, open_audiences=_BOTH)
    install_identity(identity)
    return identity


def _staff_setting(monkeypatch: pytest.MonkeyPatch, *patterns: str) -> None:
    """Serve the app with this staff setting."""
    monkeypatch.setattr(app.state, "config", Config(staff=patterns), raising=False)


def _touched_nothing(engine: FakeEngine) -> bool:
    """Whether the request reached no statement at all."""
    conn = engine.conn
    return not (
        executed_sql(conn)
        or conn.fetchrow.called
        or conn.fetchval.called
        or conn.fetch.called
    )


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the admin-action records the routes logged."""
    return [r for r in caplog.records if r.name == _LOGGER]


class TestRefusedBelowAdmin:
    """P1: no caller below admin reaches any statement of the four routes."""

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("post", _ACCOUNTS, {"name": "campaign", "role": "writer"}),
            ("post", _TOKENS, {"name": "host"}),
            ("get", _TOKENS, None),
            ("post", f"{_TOKENS}/{_KEY_ID}/revoke", None),
        ],
        ids=["create", "mint", "list", "revoke"],
    )
    @pytest.mark.parametrize("role", ["viewer", "writer"])
    def test_a_viewer_or_a_writer_is_403(
        self,
        route_client: _Client,
        method: str,
        path: str,
        body: dict[str, object] | None,
        role: Role,
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role=role))
        response = client.request(method, path, json=body)
        assert response.status_code == 403, response.text
        assert _touched_nothing(engine)

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("post", _ACCOUNTS, {"name": "campaign", "role": "writer"}),
            ("post", _TOKENS, {"name": "host"}),
            ("get", _TOKENS, None),
            ("post", f"{_TOKENS}/{_KEY_ID}/revoke", None),
        ],
        ids=["create", "mint", "list", "revoke"],
    )
    def test_no_credential_is_401(
        self,
        route_client: _Client,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> None:
        client, _store, engine = route_client
        clear_identity_override()
        response = client.request(method, path, json=body)
        assert response.status_code == 401, response.text
        assert _touched_nothing(engine)

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("post", _ACCOUNTS, {"name": "campaign", "role": "writer"}),
            ("post", _TOKENS, {"name": "host"}),
            ("get", _TOKENS, None),
            ("post", f"{_TOKENS}/{_KEY_ID}/revoke", None),
        ],
        ids=["create", "mint", "list", "revoke"],
    )
    def test_a_service_accounts_own_writer_key_is_403(
        self,
        route_client: _Client,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> None:
        client, _store, engine = route_client
        install_identity(
            replace(
                make_test_identity(role="writer"),
                email="campaign@service.invalid",
            ),
        )
        response = client.request(method, path, json=body)
        assert response.status_code == 403, response.text
        assert _touched_nothing(engine)


class TestCreate:
    def test_it_stores_a_service_row_and_answers_201_with_the_user(
        self,
        route_client: _Client,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        response = client.post(_ACCOUNTS, json={"name": "campaign", "role": "writer"})
        assert response.status_code == 201, response.text
        assert from_plain(response.json(), dict[str, object]) == {
            "id": str(_ACCOUNT_ID),
            "email": "campaign@service.invalid",
            "name": "campaign",
            "role": "writer",
            "status": "active",
            "created_at": _WHEN.isoformat(),
            "last_login": None,
            "staff": False,
        }
        call = engine.conn.fetchrow.call_args
        sql = " ".join(str(call.args[0]).split())
        assert "INSERT INTO users" in sql
        assert "ON CONFLICT (email) DO NOTHING" in sql
        assert call.args[2:] == ("campaign@service.invalid", "campaign", "writer")
        assert isinstance(call.args[1], uuid.UUID)

    def test_a_name_that_exists_is_409_whatever_its_status(
        self,
        route_client: _Client,
    ) -> None:
        """A disabled row keeps its name; the insert does not revive it."""
        client, _store, engine = route_client
        _admin()
        engine.conn.fetchrow = AsyncMock(return_value=None)
        response = client.post(_ACCOUNTS, json={"name": "campaign", "role": "viewer"})
        assert response.status_code == 409, response.text
        assert "campaign" in response.text

    @pytest.mark.parametrize(
        "body",
        [
            {"name": "Campaign", "role": "writer"},
            {"name": "c", "role": "writer"},
            {"name": "a" * 32, "role": "writer"},
            {"name": "campaign", "role": "admin"},
            {"name": "campaign", "role": "superuser"},
            {"name": "campaign"},
            {"role": "writer"},
            {"name": "campaign", "role": "writer", "email": "boss@example.com"},
        ],
    )
    def test_a_body_outside_the_contract_is_422_and_writes_nothing(
        self,
        route_client: _Client,
        body: dict[str, object],
    ) -> None:
        client, _store, engine = route_client
        _admin()
        response = client.post(_ACCOUNTS, json=body)
        assert response.status_code == 422, response.text
        assert _touched_nothing(engine)

    def test_the_answer_marks_an_account_the_staff_setting_names(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, engine = route_client
        _admin(staff=True)
        _staff_setting(monkeypatch, "campaign@service.invalid")
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        response = client.post(_ACCOUNTS, json={"name": "campaign", "role": "writer"})
        assert response.status_code == 201, response.text
        assert from_plain(response.json(), dict[str, object])["staff"] is True

    def test_a_domain_pattern_does_not_mark_a_service_account_staff(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, engine = route_client
        _admin(staff=True)
        _staff_setting(monkeypatch, "*@service.invalid")
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        response = client.post(_ACCOUNTS, json={"name": "campaign", "role": "writer"})
        assert response.status_code == 201, response.text
        assert from_plain(response.json(), dict[str, object])["staff"] is False

    def test_a_caller_without_staff_cannot_create_an_account_that_holds_it(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A key never grants more than it holds, an account included."""
        client, _store, engine = route_client
        _admin(staff=False)
        _staff_setting(monkeypatch, "campaign@service.invalid")
        response = client.post(_ACCOUNTS, json={"name": "campaign", "role": "writer"})
        assert response.status_code == 403, response.text
        assert "staff" in response.text
        assert not any("INSERT INTO users" in s for s in executed_sql(engine.conn))
        assert not engine.conn.fetchrow.called

    def test_it_logs_who_acted_and_on_whom(
        self,
        route_client: _Client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, engine = route_client
        admin = _admin()
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            client.post(_ACCOUNTS, json={"name": "campaign", "role": "writer"})
        (record,) = _audit_records(caplog)
        assert record.levelno == logging.WARNING
        assert record.__dict__["action"] == "service_account_created"
        assert record.__dict__["admin_user_id"] == str(TEST_USER_ID)
        assert record.__dict__["admin_email"] == TEST_USER_EMAIL
        assert record.__dict__["admin_api_key_id"] == str(admin.api_key_id)
        assert record.__dict__["account_user_id"] == str(_ACCOUNT_ID)
        assert record.__dict__["account_email"] == "campaign@service.invalid"
        message = record.getMessage()
        assert TEST_USER_EMAIL in message
        assert "campaign@service.invalid" in message

    def test_a_refused_create_logs_nothing(
        self,
        route_client: _Client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        engine.conn.fetchrow = AsyncMock(return_value=None)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            client.post(_ACCOUNTS, json={"name": "campaign", "role": "writer"})
        assert _audit_records(caplog) == []


def _script_mint(
    engine: FakeEngine,
    *,
    account: dict[str, object] | None,
    role: str = "writer",
) -> None:
    """Answer the account read and the role read ``create_api_key`` makes."""
    engine.conn.fetchrow = AsyncMock(return_value=account)
    engine.conn.fetchval = AsyncMock(return_value=role)


def _api_key_insert(engine: FakeEngine) -> tuple[object, ...]:
    """Return the bound values of the one ``INSERT INTO api_keys``."""
    (insert,) = [
        c
        for c in engine.conn.execute.call_args_list
        if "INSERT INTO api_keys" in str(c.args[0])
    ]
    return tuple(insert.args[1:])


class TestMint:
    def test_it_mints_one_token_for_the_account_and_shows_the_secret_once(
        self,
        route_client: _Client,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        _script_mint(engine, account=_account_row(role="writer"))
        response = client.post(_TOKENS, json={"name": "host"})
        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "no-store"
        body = from_plain(response.json(), dict[str, object])
        secret = from_plain(body["secret"], str)
        assert secret.startswith("trax_")
        assert body["role"] == "writer"
        assert body["name"] == "host"
        assert body["open_into"] is None
        key_id, user_id, name, secret_hash, prefix, role, pin = _api_key_insert(engine)
        assert body["id"] == str(key_id)
        assert user_id == _ACCOUNT_ID
        assert (name, role, pin) == ("host", "writer", None)
        assert prefix == secret[: len(str(prefix))]
        assert secret not in str(secret_hash)
        assert secret not in response.text.replace(secret, "", 1)

    def test_the_account_read_locks_the_row_so_a_disable_cannot_slip_past(
        self,
        route_client: _Client,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        _script_mint(engine, account=_account_row())
        client.post(_TOKENS, json={"name": "host"})
        sql = " ".join(str(engine.conn.fetchrow.call_args.args[0]).split())
        assert sql.endswith("FROM users WHERE id = $1 FOR SHARE")
        assert engine.conn.fetchrow.call_args.args[1] == _ACCOUNT_ID
        sqls = executed_sql(engine.conn)
        assert sqls[0] == "BEGIN"
        assert sqls[-1] == "COMMIT"

    def test_an_id_that_is_no_service_account_is_404_and_mints_nothing(
        self,
        route_client: _Client,
    ) -> None:
        """A person's id, or no row at all, answers alike."""
        client, _store, engine = route_client
        _admin()
        _script_mint(engine, account=None)
        response = client.post(_TOKENS, json={"name": "host"})
        assert response.status_code == 404, response.text
        assert from_plain(response.json(), dict[str, object]) == {
            "detail": "service account not found",
        }
        assert not any("INSERT INTO api_keys" in s for s in executed_sql(engine.conn))

    def test_a_disabled_account_is_409_and_mints_nothing(
        self,
        route_client: _Client,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        _script_mint(engine, account=_account_row(status="disabled"))
        response = client.post(_TOKENS, json={"name": "host"})
        assert response.status_code == 409, response.text
        assert "disabled" in response.text
        assert not any("INSERT INTO api_keys" in s for s in executed_sql(engine.conn))

    @pytest.mark.parametrize(
        ("account_role", "asked"),
        [("viewer", "writer"), ("viewer", "admin"), ("writer", "admin")],
    )
    def test_a_token_role_above_the_accounts_is_403(
        self,
        route_client: _Client,
        account_role: str,
        asked: str,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        _script_mint(engine, account=_account_row(role=account_role), role=account_role)
        response = client.post(_TOKENS, json={"name": "host", "role": asked})
        assert response.status_code == 403, response.text
        assert not any("INSERT INTO api_keys" in s for s in executed_sql(engine.conn))

    @pytest.mark.parametrize(
        ("account_role", "asked"),
        [("viewer", "viewer"), ("writer", "viewer"), ("writer", "writer")],
    )
    def test_a_token_role_up_to_the_accounts_is_stored(
        self,
        route_client: _Client,
        account_role: str,
        asked: str,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        _script_mint(engine, account=_account_row(role=account_role), role=account_role)
        response = client.post(_TOKENS, json={"name": "host", "role": asked})
        assert response.status_code == 201, response.text
        assert _api_key_insert(engine)[5] == asked

    def test_a_pin_the_account_does_not_hold_is_403(
        self,
        route_client: _Client,
    ) -> None:
        client, _store, engine = route_client
        _admin(staff=True)
        _script_mint(engine, account=_account_row())
        response = client.post(_TOKENS, json={"name": "host", "open_into": "staff"})
        assert response.status_code == 403, response.text
        assert "account" in response.text
        assert not any("INSERT INTO api_keys" in s for s in executed_sql(engine.conn))

    def test_staff_is_held_only_through_an_exact_entry_for_the_account(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, engine = route_client
        _admin(staff=True)
        _staff_setting(monkeypatch, "campaign@service.invalid")
        _script_mint(engine, account=_account_row())
        response = client.post(_TOKENS, json={"name": "host", "open_into": "staff"})
        assert response.status_code == 201, response.text
        body = from_plain(response.json(), dict[str, object])
        assert body["open_into"] == "staff"
        assert _api_key_insert(engine)[6:] == ("staff",)

    def test_a_domain_pattern_gives_the_account_no_staff_to_hand_a_token(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, engine = route_client
        _admin(staff=True)
        _staff_setting(monkeypatch, "*@service.invalid")
        _script_mint(engine, account=_account_row())
        refused = client.post(_TOKENS, json={"name": "host", "open_into": "staff"})
        assert refused.status_code == 403, refused.text
        assert not any("INSERT INTO api_keys" in s for s in executed_sql(engine.conn))

    def test_a_caller_cannot_hand_a_token_staff_it_does_not_hold(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """P5: with no pin named, the token would open into what the account opens."""
        client, _store, engine = route_client
        _admin(staff=False)
        _staff_setting(monkeypatch, "campaign@service.invalid")
        _script_mint(engine, account=_account_row())
        response = client.post(_TOKENS, json={"name": "host"})
        assert response.status_code == 403, response.text
        assert "staff" in response.text
        assert not any("INSERT INTO api_keys" in s for s in executed_sql(engine.conn))

    def test_a_pinned_key_hands_its_pin_to_the_token_it_mints(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, engine = route_client
        pinned = replace(
            make_test_identity(role="admin"),
            api_key_id=uuid.uuid4(),
            read_audiences=_BOTH,
            open_audiences=frozenset({"open"}),
            pinned_audience="open",
        )
        install_identity(pinned)
        _staff_setting(monkeypatch, "campaign@service.invalid")
        _script_mint(engine, account=_account_row())
        response = client.post(_TOKENS, json={"name": "host"})
        assert response.status_code == 201, response.text
        assert _api_key_insert(engine)[6:] == ("open",)
        wider = client.post(_TOKENS, json={"name": "wider", "open_into": "staff"})
        assert wider.status_code == 403, wider.text

    @pytest.mark.parametrize(
        "body",
        [
            {"name": ""},
            {"name": "host", "read_audiences": ["open"]},
            {"name": "host", "open_into": "Staff"},
            {"name": "host", "role": "superuser"},
            {},
        ],
    )
    def test_a_body_outside_the_contract_is_422_and_writes_nothing(
        self,
        route_client: _Client,
        body: dict[str, object],
    ) -> None:
        client, _store, engine = route_client
        _admin()
        response = client.post(_TOKENS, json=body)
        assert response.status_code == 422, response.text
        assert _touched_nothing(engine)

    def test_it_logs_both_identities_and_the_token_but_never_the_secret(
        self,
        route_client: _Client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, engine = route_client
        admin = _admin()
        _script_mint(engine, account=_account_row())
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            response = client.post(_TOKENS, json={"name": "host"})
        secret = from_plain(response.json(), dict[str, object])["secret"]
        (record,) = _audit_records(caplog)
        fields = record.__dict__
        assert fields["action"] == "service_token_minted"
        assert fields["admin_user_id"] == str(TEST_USER_ID)
        assert fields["admin_email"] == TEST_USER_EMAIL
        assert fields["admin_api_key_id"] == str(admin.api_key_id)
        assert fields["account_user_id"] == str(_ACCOUNT_ID)
        assert fields["account_email"] == "campaign@service.invalid"
        assert fields["key_id"] == from_plain(response.json(), dict[str, object])["id"]
        assert isinstance(secret, str)
        assert secret not in caplog.text
        assert secret not in str(fields)

    def test_a_refused_mint_logs_nothing(
        self,
        route_client: _Client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        _script_mint(engine, account=_account_row(role="viewer"), role="viewer")
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            client.post(_TOKENS, json={"name": "host", "role": "writer"})
        assert _audit_records(caplog) == []


class TestList:
    def test_it_lists_the_accounts_keys_without_hash_or_secret(
        self,
        route_client: _Client,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        engine.conn.fetch = AsyncMock(return_value=[_key_row(), _key_row(revoked=True)])
        response = client.get(_TOKENS)
        assert response.status_code == 200, response.text
        body = from_plain(response.json(), dict[str, object])
        tokens = from_plain(body["tokens"], list[dict[str, object]])
        assert [t["revoked_at"] is None for t in tokens] == [True, False]
        assert tokens[0] == {
            "id": str(_KEY_ID),
            "name": "host",
            "prefix": "trax_aBcDeFgH",
            "role": "writer",
            "open_into": "open",
            "created_at": _WHEN.isoformat(),
            "last_used_at": None,
            "revoked_at": None,
        }
        assert engine.conn.fetch.call_args.args[1] == _ACCOUNT_ID

    def test_an_id_that_is_no_service_account_is_404_and_lists_nothing(
        self,
        route_client: _Client,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        engine.conn.fetchrow = AsyncMock(return_value=None)
        engine.conn.fetch = AsyncMock(return_value=[_key_row()])
        response = client.get(_TOKENS)
        assert response.status_code == 404, response.text
        assert not engine.conn.fetch.called


class TestRevoke:
    def test_it_revokes_one_key_of_the_account_and_forgets_it(
        self,
        route_client: _Client,
    ) -> None:
        client, store, engine = route_client
        _admin()
        secret, _prefix = generate_token()
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        store.remember_bearer_identity(
            secret,
            AuthIdentity(
                user_id=_ACCOUNT_ID,
                api_key_id=_KEY_ID,
                email="campaign@service.invalid",
                role="writer",
            ),
        )
        response = client.post(f"{_TOKENS}/{_KEY_ID}/revoke")
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}
        update = next(
            c
            for c in engine.conn.execute.call_args_list
            if "UPDATE api_keys SET revoked_at" in str(c.args[0])
        )
        assert update.args[1:] == (_KEY_ID, _ACCOUNT_ID)
        assert store.cached_bearer_identity(secret) is None

    def test_a_key_that_is_not_the_accounts_is_404_and_the_cache_is_kept(
        self,
        route_client: _Client,
    ) -> None:
        client, store, engine = route_client
        _admin()
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        engine.conn.execute = AsyncMock(return_value="UPDATE 0")
        secret, _prefix = generate_token()
        remembered = AuthIdentity(
            user_id=_ACCOUNT_ID,
            api_key_id=_KEY_ID,
            email="campaign@service.invalid",
            role="writer",
        )
        store.remember_bearer_identity(secret, remembered)
        response = client.post(f"{_TOKENS}/{uuid.uuid4()}/revoke")
        assert response.status_code == 404, response.text
        assert store.cached_bearer_identity(secret) == remembered

    def test_an_id_that_is_no_service_account_is_404_and_revokes_nothing(
        self,
        route_client: _Client,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        engine.conn.fetchrow = AsyncMock(return_value=None)
        response = client.post(f"{_TOKENS}/{_KEY_ID}/revoke")
        assert response.status_code == 404, response.text
        assert not any("UPDATE api_keys" in s for s in executed_sql(engine.conn))

    def test_it_logs_both_identities_and_the_key(
        self,
        route_client: _Client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, engine = route_client
        admin = _admin()
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            client.post(f"{_TOKENS}/{_KEY_ID}/revoke")
        (record,) = _audit_records(caplog)
        fields = record.__dict__
        assert fields["action"] == "service_token_revoked"
        assert fields["admin_user_id"] == str(TEST_USER_ID)
        assert fields["admin_api_key_id"] == str(admin.api_key_id)
        assert fields["account_user_id"] == str(_ACCOUNT_ID)
        assert fields["account_email"] == "campaign@service.invalid"
        assert fields["key_id"] == str(_KEY_ID)

    def test_a_revoke_that_matched_nothing_logs_nothing(
        self,
        route_client: _Client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        engine.conn.execute = AsyncMock(return_value="UPDATE 0")
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            client.post(f"{_TOKENS}/{_KEY_ID}/revoke")
        assert _audit_records(caplog) == []


class TestAPersonIsNoServiceAccount:
    """The address decides: a row at any other domain is answered as an absent one."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", _TOKENS),
            ("get", _TOKENS),
            ("post", f"{_TOKENS}/{_KEY_ID}/revoke"),
        ],
        ids=["mint", "list", "revoke"],
    )
    def test_a_persons_id_is_404_and_reaches_no_key(
        self,
        route_client: _Client,
        method: str,
        path: str,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        person = {**_account_row(), "email": "ada@example.com"}
        _script_mint(engine, account=person)
        response = client.request(method, path, json={"name": "host"})
        assert response.status_code == 404, response.text
        statements = [
            *executed_sql(engine.conn),
            *map(str, engine.conn.fetch.call_args_list),
        ]
        assert not any("api_keys" in s for s in statements)


class TestAuditSurvivesTheDefaultLogLevel:
    """The deployment default drops INFO, so the audit lines are WARNING."""

    def test_a_created_account_is_logged_at_warning(
        self,
        route_client: _Client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            client.post(_ACCOUNTS, json={"name": "campaign", "role": "writer"})
        (record,) = _audit_records(caplog)
        assert record.__dict__["action"] == "service_account_created"

    def test_a_minted_key_is_logged_at_warning(
        self,
        route_client: _Client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        _script_mint(engine, account=_account_row())
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            client.post(_TOKENS, json={"name": "host"})
        (record,) = _audit_records(caplog)
        assert record.__dict__["action"] == "service_token_minted"

    def test_a_revoked_key_is_logged_at_warning(
        self,
        route_client: _Client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, engine = route_client
        _admin()
        engine.conn.fetchrow = AsyncMock(return_value=_account_row())
        engine.conn.execute = AsyncMock(return_value="UPDATE 1")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            client.post(f"{_TOKENS}/{_KEY_ID}/revoke")
        (record,) = _audit_records(caplog)
        assert record.__dict__["action"] == "service_token_revoked"


class TestAssistantAccount:
    """The assistant is known by address alone, so its key reaches both audiences."""

    @classmethod
    def _serve_assistant(cls, monkeypatch: pytest.MonkeyPatch) -> None:
        """Name ``chat@service.invalid`` the assistant; staff is the example domain."""
        config = Config(
            staff=("*@example.com",),
            assistant=parse_assistant("chat=chat@service.invalid"),
        )
        monkeypatch.setattr(app.state, "config", config, raising=False)

    def test_a_mint_for_the_assistant_by_a_non_staff_admin_is_403_and_stores_nothing(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, engine = route_client
        self._serve_assistant(monkeypatch)
        _admin(staff=False)
        _script_mint(engine, account=_account_row(name="chat"))
        response = client.post(
            _TOKENS,
            json={"name": "t", "open_into": "open"},
        )
        assert response.status_code == 403, response.text
        assert not any("INSERT INTO api_keys" in s for s in executed_sql(engine.conn))

    def test_a_mint_for_the_assistant_by_a_staff_admin_is_201(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, engine = route_client
        self._serve_assistant(monkeypatch)
        _admin(staff=True)
        _script_mint(engine, account=_account_row(name="chat"))
        response = client.post(_TOKENS, json={"name": "t"})
        assert response.status_code == 201, response.text

    def test_creating_the_assistant_by_a_non_staff_admin_is_403_and_stores_nothing(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, engine = route_client
        self._serve_assistant(monkeypatch)
        _admin(staff=False)
        response = client.post(_ACCOUNTS, json={"name": "chat", "role": "writer"})
        assert response.status_code == 403, response.text
        assert _touched_nothing(engine)

    def test_creating_the_assistant_by_a_staff_admin_is_201(
        self,
        route_client: _Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, engine = route_client
        self._serve_assistant(monkeypatch)
        _admin(staff=True)
        engine.conn.fetchrow = AsyncMock(return_value=_account_row(name="chat"))
        response = client.post(_ACCOUNTS, json={"name": "chat", "role": "writer"})
        assert response.status_code == 201, response.text


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
