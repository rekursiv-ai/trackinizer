"""Daemon routes against a mocked connection: the SQL each route sends.

The hand-run ``private/daemon_routes_parity.py`` runs the same routes against a real
database, and ``private/daemon_routes_races_parity.py`` races them on PostgreSQL; these
pin what the routes and the credential check ask of the connection, and that a
refusal sends nothing further.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import asyncio
import uuid

from fastapi import HTTPException
from treekle import from_plain

import pytest

from trackinizer.conftest import executed_sql, make_conn, set_field_row
from trackinizer.server.api import daemon_routes
from trackinizer.server.api._deps import host_changes
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    install_identity,
    make_test_identity,
)
from trackinizer.server.api.daemon_routes import (
    HostOwnedError,
    claim_host,
    issue_enrollment,
    unregister_host,
)
from trackinizer.server.auth import PROCESS_KEY_SQL
from trackinizer.server.config import Assistant, Config, RemoteAgentService
from trackinizer.server.host_auth import (
    MintedSecret,
    mint_secret,
    parse_credential,
    parse_enrollment,
)
from trackinizer.server.session_relay import SessionRelay
from trackinizer.server.terminals import TerminalRelay
from trackinizer.server.tool_relay import ToolRelay
from trackinizer.wire.wire_daemon import (
    CREDENTIAL_PREFIX,
    ENROLL_PATH,
    ENROLL_TTL_SEC,
    ENROLLMENT_PREFIX,
    HEARTBEAT_PATH,
    JOIN_PATH,
    OFFLINE_AFTER_SEC,
    REVOKE_PATH,
)


if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from httpx2 import Response

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


_HOST_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_INSTANCE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_BEAT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def test_enroll_supersedes_open_tokens_then_stores_only_the_digest(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    set_field_row(engine.conn, {"id": _HOST_ID, "owner": TEST_USER_EMAIL})
    engine.conn.fetchval.side_effect = [_BEAT]

    response = client.post(ENROLL_PATH, json={"name": "dev-1"})

    assert response.status_code == 201
    token = from_plain(response.json(), dict[str, str])["token"]
    parsed = parse_enrollment(token)
    assert parsed is not None
    upsert = engine.conn.fetchrow.call_args
    insert = engine.conn.fetchval.call_args
    assert upsert is not None
    assert insert is not None
    # One statement registers the host or locks its row; a separate insert and
    # lock leave a gap in which a delete commits and the lock finds no host.
    sql = upsert.args[0]
    assert isinstance(sql, str)
    assert "INSERT INTO hosts (name, updated_by, owner, role, confirmed_at)" in sql
    assert "VALUES ($1, $2, $2, $3, CASE WHEN $4 THEN now() END)" in sql
    assert "ON CONFLICT (name) DO UPDATE SET" in sql
    assert "COALESCE(hosts.confirmed_at, now())" in sql
    assert "RETURNING id, owner" in sql
    assert upsert.args[1:] == ("dev-1", TEST_USER_EMAIL, "", True)
    sql = insert.args[0]
    assert isinstance(sql, str)
    assert "INSERT INTO host_enrollments" in sql
    assert insert.args[1:] == (
        parsed.id,
        _HOST_ID,
        parsed.digest,
        TEST_USER_EMAIL,
        ENROLL_TTL_SEC,
    )
    sent = executed_sql(engine.conn)
    assert [sql.split(" WHERE")[0] for sql in sent] == [
        "BEGIN",
        "UPDATE host_enrollments SET used_at = now()",
        "COMMIT",
    ]
    assert "used_at IS NULL" in sent[1]
    assert token not in " ".join(sent)


_CLAIM_SQL = (
    "INSERT INTO hosts (name, updated_by, owner, role, confirmed_at) "
    "VALUES ($1, $2, $2, $3, CASE WHEN $4 THEN now() END) "
    "ON CONFLICT (name) DO UPDATE SET "
    "owner = CASE WHEN $4 AND $3 = '' "
    "THEN COALESCE(hosts.owner, EXCLUDED.owner) "
    "ELSE hosts.owner END, "
    "confirmed_at = CASE WHEN $4 THEN COALESCE(hosts.confirmed_at, now()) "
    "ELSE hosts.confirmed_at END RETURNING id, owner"
)


@pytest.mark.parametrize(("admin", "role"), [(True, ""), (False, "personal")])
def test_claim_host_registers_or_locks_the_host_in_one_statement(
    admin: bool,
    role: str,
) -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {"id": str(_HOST_ID), "owner": TEST_USER_EMAIL}

    claimed = asyncio.run(
        claim_host(conn, name="dev-1", email=TEST_USER_EMAIL, admin=admin),
    )

    assert claimed == _HOST_ID
    conn.fetchrow.assert_awaited_once_with(
        _CLAIM_SQL,
        "dev-1",
        TEST_USER_EMAIL,
        role,
        admin,
    )


def test_claim_host_confirms_a_personal_host_when_asked() -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {"id": str(_HOST_ID), "owner": TEST_USER_EMAIL}

    asyncio.run(
        claim_host(
            conn,
            name="dev-1",
            email=TEST_USER_EMAIL,
            admin=False,
            confirmed=True,
        ),
    )

    conn.fetchrow.assert_awaited_once_with(
        _CLAIM_SQL,
        "dev-1",
        TEST_USER_EMAIL,
        "personal",
        True,
    )


def test_claim_host_expects_the_statement_to_return_a_row() -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = None

    with pytest.raises(ValueError, match=r"^Expected host is not None\.$"):
        asyncio.run(
            claim_host(conn, name="dev-1", email=TEST_USER_EMAIL, admin=True),
        )


def test_claim_host_refuses_an_admin_a_name_another_person_owns() -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {"id": _HOST_ID, "owner": "mal@x.y"}

    with pytest.raises(HostOwnedError) as refused:
        asyncio.run(
            claim_host(conn, name="dev-1", email=TEST_USER_EMAIL, admin=True),
        )

    assert str(refused.value) == (
        "host 'dev-1' is owned by mal@x.y; "
        "choose another name, or have them revoke it first"
    )


@pytest.mark.parametrize("owner", ["mal@x.y", None])
def test_claim_host_refuses_anyone_else_a_host_that_is_not_theirs(
    owner: str | None,
) -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {"id": _HOST_ID, "owner": owner}

    with pytest.raises(HTTPException) as refused:
        asyncio.run(
            claim_host(conn, name="dev-1", email=TEST_USER_EMAIL, admin=False),
        )

    assert (refused.value.status_code, refused.value.detail) == (
        403,
        "host belongs to another user",
    )


def test_issue_enrollment_supersedes_the_open_token_and_returns_the_new_one() -> None:
    conn = make_conn()
    conn.fetchval.return_value = _BEAT

    minted, expires_at = asyncio.run(
        issue_enrollment(conn, host_id=_HOST_ID, created_by=TEST_USER_EMAIL),
    )

    parsed = parse_enrollment(minted.token)
    assert parsed is not None
    assert expires_at == _BEAT
    conn.execute.assert_awaited_once_with(
        "UPDATE host_enrollments SET used_at = now() "
        "WHERE host_id = $1 AND used_at IS NULL",
        _HOST_ID,
    )
    conn.fetchval.assert_awaited_once_with(
        "INSERT INTO host_enrollments "
        "(id, host_id, secret_sha256, created_by, expires_at) "
        "VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5)) "
        "RETURNING expires_at",
        parsed.id,
        _HOST_ID,
        parsed.digest,
        TEST_USER_EMAIL,
        ENROLL_TTL_SEC,
    )


def test_enroll_by_a_viewer_sends_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("viewer")

    response = client.post(ENROLL_PATH, json={"name": "dev-1"})

    assert response.status_code == 403
    _assert_untouched(engine)


def test_a_writer_enrolls_a_host_that_is_new_as_their_own_personal_one(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, {"id": _HOST_ID, "owner": TEST_USER_EMAIL})
    engine.conn.fetchval.side_effect = [_BEAT]

    response = client.post(ENROLL_PATH, json={"name": "laptop"})

    assert response.status_code == 201
    upsert = engine.conn.fetchrow.call_args
    assert upsert is not None
    assert upsert.args[1:] == ("laptop", TEST_USER_EMAIL, "personal", False)


def _assistant(monkeypatch: pytest.MonkeyPatch, *, email: str) -> None:
    monkeypatch.setitem(
        app.state._state,
        "config",
        Config(
            session_secret="s" * 32,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email=email),
        ),
    )


def test_the_assistant_enrolls_a_host_for_its_asker_as_their_confirmed_personal_one(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, engine = route_client
    _assistant(monkeypatch, email=TEST_USER_EMAIL)
    asker = {"email": "ada@x.y", "role": "writer", "hosted_domain": None}
    set_field_row(engine.conn, {"id": _HOST_ID, "owner": "ada@x.y"})
    engine.conn.fetchrow.side_effect = [asker, {"id": _HOST_ID, "owner": "ada@x.y"}]
    engine.conn.fetchval.side_effect = [_BEAT]

    response = client.post(
        ENROLL_PATH,
        json={"name": "box-1a2b3c4d", "asker": "ada@x.y"},
    )

    assert response.status_code == 201, response.text
    assert from_plain(response.json(), dict[str, str])["owner"] == "ada@x.y"
    upsert = engine.conn.fetchrow.call_args
    assert upsert is not None
    assert upsert.args[1:] == ("box-1a2b3c4d", "ada@x.y", "personal", True)
    insert = engine.conn.fetchval.call_args
    assert insert is not None
    assert insert.args[4] == "ada@x.y"


@pytest.mark.parametrize(
    "asker",
    [
        None,
        {"email": "vic@x.y", "role": "viewer", "hosted_domain": None},
    ],
    ids=["unknown", "viewer"],
)
def test_an_asker_who_is_unknown_or_cannot_write_gets_no_host(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    asker: dict[str, object] | None,
) -> None:
    client, _, engine = route_client
    _assistant(monkeypatch, email=TEST_USER_EMAIL)
    engine.conn.fetchrow.side_effect = [asker]

    response = client.post(
        ENROLL_PATH,
        json={"name": "box-1a2b3c4d", "asker": "vic@x.y"},
    )

    assert response.status_code == 403
    engine.conn.fetchval.assert_not_called()
    assert engine.conn.fetchrow.call_count == 1


def test_an_asker_named_by_anyone_but_the_assistant_is_refused_unread(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, engine = route_client
    _assistant(monkeypatch, email="kb@x.y")

    response = client.post(
        ENROLL_PATH,
        json={"name": "box-1a2b3c4d", "asker": "ada@x.y"},
    )

    assert response.status_code == 403
    _assert_untouched(engine)


def test_without_auth_no_asker_is_believed_and_none_is_read(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, engine = route_client
    monkeypatch.setitem(
        app.state._state,
        "config",
        Config(
            auth_disabled=True,
            assistant=Assistant(actor="kb", email=TEST_USER_EMAIL),
        ),
    )

    response = client.post(
        ENROLL_PATH,
        json={"name": "box-1a2b3c4d", "asker": "ada@x.y"},
    )

    assert response.status_code == 403
    _assert_untouched(engine)


def test_a_writer_cannot_enroll_a_host_that_belongs_to_someone_else(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, {"id": _HOST_ID, "owner": "other@x.y"})

    response = client.post(ENROLL_PATH, json={"name": "laptop"})

    assert response.status_code == 403
    engine.conn.fetchval.assert_not_called()
    assert executed_sql(engine.conn) == ["BEGIN", "ROLLBACK"]


_SERVICE_ACCOUNT = "remote-agents@service.invalid"


@pytest.mark.parametrize(
    ("name", "email", "confirmed"),
    [
        ("remote-agents", _SERVICE_ACCOUNT, True),
        ("remote-agents", "other@service.invalid", False),
        ("lab", _SERVICE_ACCOUNT, False),
    ],
    ids=[
        "the name and account the config names",
        "that name under another account",
        "another name under that account",
    ],
)
def test_the_remote_agent_service_the_config_names_is_confirmed_as_it_enrolls(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    email: str,
    confirmed: bool,
) -> None:
    """A service account has no web app to confirm a host of its own in."""
    client, _, engine = route_client
    monkeypatch.setitem(
        app.state._state,
        "config",
        Config(
            remote_agent_services=(
                RemoteAgentService(name="remote-agents", account=_SERVICE_ACCOUNT),
            ),
        ),
    )
    install_identity(make_test_identity(role="writer", email=email))
    set_field_row(engine.conn, {"id": _HOST_ID, "owner": email})
    engine.conn.fetchval.side_effect = [_BEAT]

    response = client.post(ENROLL_PATH, json={"name": name})

    assert response.status_code == 201, response.text
    upsert = engine.conn.fetchrow.call_args
    assert upsert is not None
    assert upsert.args[1:] == (name, email, "personal", confirmed)


def test_join_checks_the_token_unlocked_then_locks_the_host_and_claims_it(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    enrollment = mint_secret(ENROLLMENT_PREFIX)
    parsed_enrollment = parse_enrollment(enrollment.token)
    assert parsed_enrollment is not None
    engine.conn.fetchval.side_effect = [enrollment.digest, _HOST_ID]
    engine.conn.fetchrow.side_effect = [{"id": _HOST_ID, "owner": TEST_USER_EMAIL}]

    response = _join(client, enrollment.token, facts={"os": "linux"})

    assert response.status_code == 201
    body = from_plain(response.json(), dict[str, str])
    assert body["host_id"] == str(_HOST_ID)
    credential = parse_credential(body["credential"])
    assert credential is not None
    check, claim = engine.conn.fetchval.call_args_list
    lock = engine.conn.fetchrow.call_args
    assert lock is not None
    # A plain read first: join has no authentication, so a token that cannot succeed
    # must be refused before it queues for the host row.
    sql = check.args[0]
    assert isinstance(sql, str)
    assert sql == (
        "SELECT e.secret_sha256 FROM host_enrollments e "
        "JOIN hosts m ON m.id = e.host_id "
        "WHERE e.id = $1 AND m.name = $2 AND e.used_at IS NULL "
        "AND e.expires_at > now() "
        "AND EXISTS (SELECT 1 FROM users u WHERE u.email = e.created_by "
        "AND u.status = 'active' AND ((u.role = 'admin' "
        "AND (m.owner IS NULL OR m.owner = u.email)) "
        "OR (u.email = m.owner AND m.role = $3)))"
    )
    assert check.args[1:] == (parsed_enrollment.id, "dev-1", "personal")
    # The same first lock as enroll, revoke and delete take, or a join racing one of
    # them deadlocks.
    sql = lock.args[0]
    assert isinstance(sql, str)
    assert "FROM hosts WHERE name = $1 FOR UPDATE" in sql
    assert lock.args[1:] == ("dev-1",)
    sql = claim.args[0]
    assert isinstance(sql, str)
    assert "UPDATE host_enrollments SET used_at = now()" in sql
    assert "used_at IS NULL AND expires_at > now() AND secret_sha256 = $2" in sql
    assert "AND host_id = $3" in sql
    assert (
        "FROM users u JOIN hosts m ON m.id = $3 "
        "WHERE u.email = host_enrollments.created_by AND u.status = 'active' "
        "AND ((u.role = 'admin' AND (m.owner IS NULL OR m.owner = u.email)) "
        "OR (m.owner = u.email AND m.role = $4))"
    ) in sql
    assert claim.args[1:] == (
        parsed_enrollment.id,
        enrollment.digest,
        _HOST_ID,
        "personal",
    )
    sent = executed_sql(engine.conn)
    assert [sql.split(" (")[0].split(" SET")[0] for sql in sent] == [
        "BEGIN",
        "UPDATE host_credentials",
        "INSERT INTO host_credentials",
        "UPDATE hosts",
        "COMMIT",
    ]
    assert sent[1] == (
        "UPDATE host_credentials SET revoked_at = now() "
        "WHERE host_id = $1 AND revoked_at IS NULL"
    )
    insert, update = engine.conn.execute.call_args_list[-3:-1]
    assert insert.args[1:] == (credential.id, _HOST_ID, credential.digest)
    assert update.args[1:] == (
        _HOST_ID,
        {"os": "linux"},
        uuid.UUID(_INSTANCE),
        "0.1",
    )
    assert body["credential"] not in " ".join(sent)


@pytest.mark.parametrize(
    ("host", "claimed"),
    [(None, []), ({"id": _HOST_ID, "owner": None}, [None])],
    ids=["unknown-host", "unclaimable-token"],
)
def test_join_that_finds_no_host_or_no_claimable_token_is_401_and_writes_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
    host: dict[str, object] | None,
    claimed: list[None],
) -> None:
    client, _, engine = route_client
    enrollment = mint_secret(ENROLLMENT_PREFIX)
    engine.conn.fetchval.side_effect = [enrollment.digest, *claimed]
    engine.conn.fetchrow.side_effect = [host]

    response = _join(client, enrollment.token)

    assert response.status_code == 401
    assert from_plain(response.json(), dict[str, str])["detail"] == (
        "invalid enrollment token"
    )
    assert executed_sql(engine.conn) == ["BEGIN", "ROLLBACK"]


@pytest.mark.parametrize(
    "stored",
    [None, bytes(32)],
    ids=["no-open-token", "wrong-secret"],
)
def test_join_refused_by_the_unlocked_check_is_401_takes_no_lock_and_writes_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
    stored: bytes | None,
) -> None:
    client, _, engine = route_client
    engine.conn.fetchval.side_effect = [stored]

    response = _join(client, mint_secret(ENROLLMENT_PREFIX).token)

    assert response.status_code == 401
    assert from_plain(response.json(), dict[str, str])["detail"] == (
        "invalid enrollment token"
    )
    # No BEGIN: the refusal opened no transaction, so it holds no row lock.
    assert executed_sql(engine.conn) == []
    engine.conn.fetchval.assert_called_once()
    check = engine.conn.fetchval.call_args
    assert check is not None
    sql = check.args[0]
    assert isinstance(sql, str)
    assert "FOR UPDATE" not in sql


@pytest.mark.parametrize(
    "token",
    ["", "enr_short", f"{CREDENTIAL_PREFIX}{'0' * 32}_{'A' * 43}"],
    ids=["empty", "short", "credential"],
)
def test_join_with_a_malformed_token_never_reaches_the_database(
    route_client: tuple[TestClient, Store, FakeEngine],
    token: str,
) -> None:
    client, _, engine = route_client

    response = _join(client, token)

    assert response.status_code == 401
    _assert_untouched(engine)


def test_heartbeat_locks_the_host_then_checks_the_credential_then_updates_the_row(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [TEST_USER_EMAIL, True, _BEAT]

    response = _heartbeat(client, credential.token, facts={"cpus": 4})

    assert response.status_code == 200
    assert from_plain(response.json(), dict[str, str]) == {
        "server_time": "2026-10-07T12:00:00Z",
    }
    lookup = engine.conn.fetchrow.call_args
    assert lookup is not None
    sql = lookup.args[0]
    assert isinstance(sql, str)
    assert "FROM host_credentials c JOIN hosts m ON m.id = c.host_id" in sql
    assert "WHERE c.id = $1" in sql
    assert "revoked_at IS NULL" not in sql
    assert lookup.args[1:] == (credential.id,)
    lock, live, beat = engine.conn.fetchval.call_args_list
    # The lock comes first, so a revoke in flight has committed before the check
    # reads the credential, and the update cannot land after it.
    sql = lock.args[0]
    assert isinstance(sql, str)
    assert "FROM hosts WHERE id = $1 FOR UPDATE" in sql
    assert lock.args[1:] == (_HOST_ID,)
    sql = live.args[0]
    assert isinstance(sql, str)
    assert "FROM host_credentials WHERE id = $1 AND revoked_at IS NULL" in sql
    assert live.args[1:] == (credential.id,)
    sql = beat.args[0]
    assert isinstance(sql, str)
    assert "($6::boolean OR daemon_instance IS NULL OR daemon_instance = $2" in sql
    assert "last_heartbeat < now() - make_interval(secs => $5)" in sql
    assert "facts = COALESCE($4, facts)" in sql
    assert beat.args[1:] == (
        _HOST_ID,
        uuid.UUID(_INSTANCE),
        "0.1",
        {"cpus": 4},
        OFFLINE_AFTER_SEC,
        False,
    )


# The oldest supported daemon is raised to 0.2 here, above 0.1.11, the first daemon that
# reads an answer naming a field it does not know.
@pytest.mark.parametrize(
    ("daemon_version", "told"),
    [("0.1.11", True), ("0.1.9", False), ("0.2", False), ("", False)],
)
def test_a_heartbeat_tells_an_outdated_daemon_only_when_it_can_read_that(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    daemon_version: str,
    told: bool,
) -> None:
    client, _, engine = route_client
    monkeypatch.setattr(daemon_routes, "MIN_DAEMON_VERSION", "0.2")
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [TEST_USER_EMAIL, True, _BEAT]

    response = _heartbeat(client, credential.token, daemon_version=daemon_version)

    assert response.status_code == 200
    assert from_plain(response.json(), dict[str, object]) == {
        "server_time": "2026-10-07T12:00:00Z",
        **({"daemon_outdated": True} if told else {}),
    }


def test_a_heartbeat_the_instance_rule_refuses_is_409(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [TEST_USER_EMAIL, True, None]

    response = _heartbeat(client, credential.token)

    assert response.status_code == 409
    assert (
        from_plain(response.json(), dict[str, str])["code"]
        == "another_daemon_connected"
    )


def test_a_take_passes_the_instance_rule_by_and_a_release_only_lets_go(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [TEST_USER_EMAIL, True, _BEAT] * 2

    take = _heartbeat(client, credential.token, claim="take")
    release = _heartbeat(client, credential.token, claim="release")

    assert (take.status_code, release.status_code) == (200, 200)
    took, let_go = engine.conn.fetchval.call_args_list[2::3]
    assert took.args[-1] is True
    sql = let_go.args[0]
    assert isinstance(sql, str)
    assert "CASE WHEN daemon_instance = $2 THEN NULL ELSE daemon_instance END" in sql
    assert "last_heartbeat" not in sql
    assert let_go.args[1:] == (_HOST_ID, uuid.UUID(_INSTANCE))


def test_a_heartbeat_whose_credential_was_revoked_meanwhile_is_410_and_writes_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [TEST_USER_EMAIL, False]

    response = _heartbeat(client, credential.token)

    assert response.status_code == 410
    assert from_plain(response.json(), dict[str, str])["detail"] == "host_revoked"
    assert engine.conn.fetchval.call_count == 2
    assert executed_sql(engine.conn)[-1] == "ROLLBACK"


def test_last_used_is_written_by_the_first_authenticated_request_only(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [TEST_USER_EMAIL, True, _BEAT] * 2

    _heartbeat(client, credential.token)
    _heartbeat(client, credential.token)

    touches: list[tuple[object, ...]] = []
    for recorded in engine.conn.execute.call_args_list:
        sql = recorded.args[0]
        assert isinstance(sql, str)
        if "UPDATE host_credentials SET last_used = now()" in sql:
            touches.append(recorded.args[1:])
    assert touches == [(credential.id,)]


def test_a_revoked_credential_is_410_only_when_its_secret_matches(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    forged = MintedSecret(
        id=credential.id,
        token=f"{CREDENTIAL_PREFIX}{credential.id.hex}_{'A' * 43}",
        digest=credential.digest,
    )
    set_field_row(engine.conn, _credential_row(credential, revoked=True))

    matching = _heartbeat(client, credential.token)
    wrong = _heartbeat(client, forged.token)

    assert (matching.status_code, wrong.status_code) == (410, 401)
    assert from_plain(matching.json(), dict[str, str])["detail"] == "host_revoked"
    engine.conn.fetchval.assert_not_called()
    engine.conn.execute.assert_not_called()


def test_an_unknown_or_missing_credential_is_401_with_one_body(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, None)
    unknown = mint_secret(CREDENTIAL_PREFIX).token

    responses = [
        _heartbeat(client, unknown),
        _heartbeat(client, "not-a-credential"),
        client.post(
            HEARTBEAT_PATH.format(host_id=_HOST_ID),
            json={"instance": _INSTANCE, "daemon_version": "0.1"},
        ),
    ]

    assert [r.status_code for r in responses] == [401, 401, 401]
    assert len({r.text for r in responses}) == 1
    engine.conn.fetchval.assert_not_called()


def test_a_daemon_request_naming_a_field_the_server_does_not_know_is_422(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))

    beat = client.post(
        HEARTBEAT_PATH.format(host_id=_HOST_ID),
        json={"instance": _INSTANCE, "daemon_version": "0.1", "lane": 1},
        headers={"Authorization": f"Bearer {credential.token}"},
    )
    joined = client.post(
        JOIN_PATH,
        json={
            "name": "dev-1",
            "token": mint_secret(ENROLLMENT_PREFIX).token,
            "instance": _INSTANCE,
            "daemon_version": "0.1",
            "lane": 1,
        },
    )

    assert (beat.status_code, joined.status_code) == (422, 422)
    engine.conn.fetchval.assert_not_called()


def test_another_hosts_credential_is_404_and_updates_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(
        engine.conn,
        {**_credential_row(credential), "host_id": uuid.uuid4()},
    )

    response = _heartbeat(client, credential.token)

    assert response.status_code == 404
    engine.conn.fetchval.assert_not_called()
    engine.conn.execute.assert_not_called()


def test_revoke_closes_credentials_enrollments_and_the_daemon_instance(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, engine = route_client
    _as("admin")
    # The row's id as text, as a driver may hand it: the host is named by its UUID.
    set_field_row(engine.conn, {"id": str(_HOST_ID), "owner": "other@x.y"})
    engine.conn.fetch.return_value = [{"id": uuid.uuid4()}]
    forgotten: list[uuid.UUID] = []
    monkeypatch.setattr(store, "forget_bearer_identities_for_host", forgotten.append)

    response = client.post(REVOKE_PATH.format(name="dev-1"))

    assert (response.status_code, response.content) == (204, b"")
    # Each process key revoked stops being honoured now, not when its cache entry ends.
    assert forgotten == [_HOST_ID]
    select = engine.conn.fetchrow.call_args
    assert select is not None
    assert select.args == (
        "SELECT id, owner FROM hosts WHERE name = $1 FOR UPDATE",
        "dev-1",
    )
    sent = executed_sql(engine.conn)
    assert [sql.split(" SET")[0] for sql in sent] == [
        "BEGIN",
        "UPDATE host_credentials",
        "UPDATE api_keys AS credential",
        "UPDATE host_enrollments",
        "UPDATE hosts",
        "COMMIT",
    ]
    assert "revoked_at = now()" in sent[1]
    assert "revoked_at IS NULL" in sent[1]
    assert sent[3] == (
        "UPDATE host_enrollments SET used_at = now() "
        "WHERE host_id = $1 AND used_at IS NULL"
    )
    assert "daemon_instance = NULL" in sent[4]
    assert [c.args[1:] for c in engine.conn.execute.call_args_list[1:4]] == [
        (_HOST_ID,),
        (_HOST_ID,),
        (_HOST_ID,),
    ]
    revoked = engine.conn.fetch.call_args
    assert revoked is not None
    # The host's keys minted for its agent processes, by the one predicate.
    assert PROCESS_KEY_SQL in cast("str", revoked.args[0])
    assert revoked.args[1:] == (_HOST_ID,)


def test_revoke_tells_the_owners_listing_then_ends_what_the_host_served(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, engine = route_client
    _as("admin")
    set_field_row(engine.conn, {"id": _HOST_ID, "owner": "other@x.y"})
    ended: list[tuple[str, uuid.UUID]] = []

    def connections_closed(relay: ToolRelay, host_id: uuid.UUID) -> None:
        del relay
        ended.append(("tool connections", host_id))

    async def terminals_closed(relay: TerminalRelay, host_id: uuid.UUID) -> None:
        del relay
        ended.append(("terminals", host_id))

    async def processes_stopped(
        relay: SessionRelay,
        host_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        del relay
        ended.append(("agent processes", host_id))
        return []

    monkeypatch.setattr(ToolRelay, "close_host", connections_closed)
    monkeypatch.setattr(TerminalRelay, "close_host", terminals_closed)
    monkeypatch.setattr(SessionRelay, "stop_host", processes_stopped)
    listed = host_changes(app).version("other@x.y")

    response = client.post(REVOKE_PATH.format(name="dev-1"))

    assert response.status_code == 204
    assert host_changes(app).version("other@x.y") == listed + 1
    assert ended == [
        ("tool connections", _HOST_ID),
        ("terminals", _HOST_ID),
        ("agent processes", _HOST_ID),
    ]


def test_revoke_of_an_absent_host_is_404_and_changes_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    set_field_row(engine.conn, None)

    response = client.post(REVOKE_PATH.format(name="nope"))

    assert response.status_code == 404
    assert "'nope' is not registered" in response.text
    assert executed_sql(engine.conn) == ["BEGIN", "ROLLBACK"]


def test_revoke_of_a_reserved_name_is_refused_and_sends_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")

    response = client.post(REVOKE_PATH.format(name="enroll"))

    assert response.status_code == 422
    _assert_untouched(engine)


def test_an_admin_revokes_a_host_nobody_owns(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    set_field_row(engine.conn, {"id": _HOST_ID, "owner": None})

    response = client.post(REVOKE_PATH.format(name="dev-1"))

    assert response.status_code == 204
    assert executed_sql(engine.conn)[-1] == "COMMIT"


def test_revoke_by_a_viewer_sends_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("viewer")

    response = client.post(REVOKE_PATH.format(name="dev-1"))

    assert response.status_code == 403
    _assert_untouched(engine)


def test_a_writer_revokes_their_own_host_and_not_another_users(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, {"id": _HOST_ID, "owner": "other@x.y"})

    refused = client.post(REVOKE_PATH.format(name="dev-1"))

    assert (refused.status_code, refused.json()) == (
        403,
        {"detail": "host belongs to another user"},
    )
    assert executed_sql(engine.conn) == ["BEGIN", "ROLLBACK"]

    set_field_row(engine.conn, {"id": _HOST_ID, "owner": TEST_USER_EMAIL})
    allowed = client.post(REVOKE_PATH.format(name="dev-1"))

    assert allowed.status_code == 204


@pytest.mark.asyncio
async def test_unregister_takes_the_owners_host_out_of_service_then_deletes_it(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    _, _, engine = route_client
    # The row's id as text, as a driver may hand it: the host is named by its UUID.
    set_field_row(engine.conn, {"id": str(_HOST_ID)})

    host_id = await unregister_host(engine.conn, name="box-1", owner="ada@x.y")

    assert host_id == _HOST_ID
    assert [call.args[1:] for call in engine.conn.execute.call_args_list] == [
        (_HOST_ID,),
        (_HOST_ID,),
        (_HOST_ID,),
        (_HOST_ID,),
    ]
    revoked = engine.conn.fetch.call_args
    assert revoked is not None
    assert PROCESS_KEY_SQL in cast("str", revoked.args[0])
    assert revoked.args[1:] == (_HOST_ID,)
    assert all(
        isinstance(call.args[1], uuid.UUID)
        for call in [
            *engine.conn.execute.call_args_list,
            *engine.conn.fetch.call_args_list,
        ]
    )
    select = engine.conn.fetchrow.call_args
    assert select is not None
    assert select.args == (
        "SELECT id FROM hosts WHERE name = $1 AND owner = $2 FOR UPDATE",
        "box-1",
        "ada@x.y",
    )
    assert [sql.split(" SET")[0] for sql in executed_sql(engine.conn)] == [
        "UPDATE host_credentials",
        "UPDATE api_keys AS credential",
        "UPDATE host_enrollments",
        "UPDATE hosts",
        "DELETE FROM hosts WHERE id = $1",
    ]


@pytest.mark.asyncio
async def test_unregister_leaves_a_host_that_is_gone_or_another_persons(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    _, _, engine = route_client
    set_field_row(engine.conn, None)

    assert await unregister_host(engine.conn, name="box-1", owner="ada@x.y") is None
    assert executed_sql(engine.conn) == []


def _as(role: Role) -> None:
    install_identity(make_test_identity(role=role))


def _credential_row(
    credential: MintedSecret,
    *,
    revoked: bool = False,
) -> dict[str, object]:
    return {
        "secret_sha256": credential.digest,
        "revoked_at": _BEAT if revoked else None,
        "host_id": _HOST_ID,
        "name": "dev-1",
    }


def _join(
    client: TestClient,
    token: str,
    *,
    facts: dict[str, object] | None = None,
) -> Response:
    return client.post(
        JOIN_PATH,
        json={
            "name": "dev-1",
            "token": token,
            "instance": _INSTANCE,
            "daemon_version": "0.1",
            "facts": facts or {},
        },
    )


def _heartbeat(
    client: TestClient,
    bearer: str,
    *,
    facts: dict[str, object] | None = None,
    claim: str | None = None,
    daemon_version: str = "0.1",
) -> Response:
    body: dict[str, object] = {"instance": _INSTANCE, "daemon_version": daemon_version}
    if facts is not None:
        body["facts"] = facts
    if claim is not None:
        body["claim"] = claim
    return client.post(
        HEARTBEAT_PATH.format(host_id=_HOST_ID),
        json=body,
        headers={"Authorization": f"Bearer {bearer}"},
    )


def _assert_untouched(engine: FakeEngine) -> None:
    """Assert no route sent the connection anything."""
    engine.conn.fetch.assert_not_called()
    engine.conn.fetchrow.assert_not_called()
    engine.conn.fetchval.assert_not_called()
    engine.conn.execute.assert_not_called()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
