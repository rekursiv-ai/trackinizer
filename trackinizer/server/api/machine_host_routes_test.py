"""Machine host routes against a mocked connection: the SQL each route sends.

``machine_host_routes_pglite_test.py`` runs the same routes against a real database;
these pin what the routes and the credential check ask of the connection, and that
a refusal sends nothing further.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncio
import uuid

from fastapi import HTTPException
from treekle import from_plain

import pytest

from trackinizer.conftest import executed_sql, make_conn, set_field_row
from trackinizer.server.api import machine_host_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    install_identity,
    make_test_identity,
)
from trackinizer.server.api.machine_host_routes import (
    MachineOwnedError,
    claim_machine,
    issue_enrollment,
)
from trackinizer.server.config import Assistant, Config
from trackinizer.server.machine_auth import (
    MintedSecret,
    mint_secret,
    parse_credential,
    parse_enrollment,
)
from trackinizer.wire.wire_machine_host import (
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


_MACHINE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_INSTANCE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_BEAT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def test_enroll_supersedes_open_tokens_then_stores_only_the_digest(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    set_field_row(engine.conn, {"id": _MACHINE_ID, "owner": TEST_USER_EMAIL})
    engine.conn.fetchval.side_effect = [_BEAT]

    response = client.post(ENROLL_PATH, json={"name": "dev-1"})

    assert response.status_code == 201
    token = from_plain(response.json(), dict[str, str])["token"]
    parsed = parse_enrollment(token)
    assert parsed is not None
    upsert = engine.conn.fetchrow.call_args
    insert = engine.conn.fetchval.call_args
    # One statement registers the machine or locks its row; a separate insert and
    # lock leave a gap in which a delete commits and the lock finds no machine.
    sql = from_plain(upsert.args[0], str)
    assert "INSERT INTO machines (name, updated_by, owner, role, confirmed_at)" in sql
    assert "VALUES ($1, $2, $2, $3, CASE WHEN $4 THEN now() END)" in sql
    assert "ON CONFLICT (name) DO UPDATE SET" in sql
    assert "COALESCE(machines.confirmed_at, now())" in sql
    assert "RETURNING id, owner" in sql
    assert upsert.args[1:] == ("dev-1", TEST_USER_EMAIL, "", True)
    assert "INSERT INTO machine_enrollments" in from_plain(insert.args[0], str)
    assert insert.args[1:] == (
        parsed.id,
        _MACHINE_ID,
        parsed.digest,
        TEST_USER_EMAIL,
        ENROLL_TTL_SEC,
    )
    sent = executed_sql(engine.conn)
    assert [sql.split(" WHERE")[0] for sql in sent] == [
        "BEGIN",
        "UPDATE machine_enrollments SET used_at = now()",
        "COMMIT",
    ]
    assert "used_at IS NULL" in sent[1]
    assert token not in " ".join(sent)


_CLAIM_SQL = (
    "INSERT INTO machines (name, updated_by, owner, role, confirmed_at) "
    "VALUES ($1, $2, $2, $3, CASE WHEN $4 THEN now() END) "
    "ON CONFLICT (name) DO UPDATE SET "
    "owner = CASE WHEN $4 AND $3 = '' "
    "THEN COALESCE(machines.owner, EXCLUDED.owner) "
    "ELSE machines.owner END, "
    "confirmed_at = CASE WHEN $4 THEN COALESCE(machines.confirmed_at, now()) "
    "ELSE machines.confirmed_at END RETURNING id, owner"
)


@pytest.mark.parametrize(("admin", "role"), [(True, ""), (False, "personal")])
def test_claim_machine_registers_or_locks_the_machine_in_one_statement(
    admin: bool,
    role: str,
) -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {"id": str(_MACHINE_ID), "owner": TEST_USER_EMAIL}

    claimed = asyncio.run(
        claim_machine(conn, name="dev-1", email=TEST_USER_EMAIL, admin=admin),
    )

    assert claimed == _MACHINE_ID
    conn.fetchrow.assert_awaited_once_with(
        _CLAIM_SQL,
        "dev-1",
        TEST_USER_EMAIL,
        role,
        admin,
    )


def test_claim_machine_confirms_a_personal_machine_when_asked() -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {"id": str(_MACHINE_ID), "owner": TEST_USER_EMAIL}

    asyncio.run(
        claim_machine(
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


def test_claim_machine_expects_the_statement_to_return_a_row() -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = None

    with pytest.raises(ValueError, match=r"^Expected machine is not None\.$"):
        asyncio.run(
            claim_machine(conn, name="dev-1", email=TEST_USER_EMAIL, admin=True),
        )


def test_claim_machine_refuses_an_admin_a_name_another_person_owns() -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {"id": _MACHINE_ID, "owner": "mal@x.y"}

    with pytest.raises(MachineOwnedError) as refused:
        asyncio.run(
            claim_machine(conn, name="dev-1", email=TEST_USER_EMAIL, admin=True),
        )

    assert str(refused.value) == (
        "machine 'dev-1' is owned by mal@x.y; "
        "choose another name, or have them revoke it first"
    )


@pytest.mark.parametrize("owner", ["mal@x.y", None])
def test_claim_machine_refuses_anyone_else_a_machine_that_is_not_theirs(
    owner: str | None,
) -> None:
    conn = make_conn()
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {"id": _MACHINE_ID, "owner": owner}

    with pytest.raises(HTTPException) as refused:
        asyncio.run(
            claim_machine(conn, name="dev-1", email=TEST_USER_EMAIL, admin=False),
        )

    assert (refused.value.status_code, refused.value.detail) == (
        403,
        "machine belongs to another user",
    )


def test_issue_enrollment_supersedes_the_open_token_and_returns_the_new_one() -> None:
    conn = make_conn()
    conn.fetchval.return_value = _BEAT

    minted, expires_at = asyncio.run(
        issue_enrollment(conn, machine_id=_MACHINE_ID, created_by=TEST_USER_EMAIL),
    )

    parsed = parse_enrollment(minted.token)
    assert parsed is not None
    assert expires_at == _BEAT
    conn.execute.assert_awaited_once_with(
        "UPDATE machine_enrollments SET used_at = now() "
        "WHERE machine_id = $1 AND used_at IS NULL",
        _MACHINE_ID,
    )
    conn.fetchval.assert_awaited_once_with(
        "INSERT INTO machine_enrollments "
        "(id, machine_id, secret_sha256, created_by, expires_at) "
        "VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5)) "
        "RETURNING expires_at",
        parsed.id,
        _MACHINE_ID,
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


def test_a_writer_enrolls_a_machine_that_is_new_as_their_own_personal_one(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, {"id": _MACHINE_ID, "owner": TEST_USER_EMAIL})
    engine.conn.fetchval.side_effect = [_BEAT]

    response = client.post(ENROLL_PATH, json={"name": "laptop"})

    assert response.status_code == 201
    upsert = engine.conn.fetchrow.call_args
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


def test_the_assistant_enrolls_a_machine_for_its_asker_as_their_confirmed_personal_one(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, engine = route_client
    _assistant(monkeypatch, email=TEST_USER_EMAIL)
    asker = {"email": "ada@x.y", "role": "writer", "hosted_domain": None}
    set_field_row(engine.conn, {"id": _MACHINE_ID, "owner": "ada@x.y"})
    engine.conn.fetchrow.side_effect = [asker, {"id": _MACHINE_ID, "owner": "ada@x.y"}]
    engine.conn.fetchval.side_effect = [_BEAT]

    response = client.post(
        ENROLL_PATH,
        json={"name": "box-1a2b3c4d", "asker": "ada@x.y"},
    )

    assert response.status_code == 201, response.text
    assert from_plain(response.json(), dict[str, str])["owner"] == "ada@x.y"
    upsert = engine.conn.fetchrow.call_args
    assert upsert.args[1:] == ("box-1a2b3c4d", "ada@x.y", "personal", True)
    assert engine.conn.fetchval.call_args.args[4] == "ada@x.y"


@pytest.mark.parametrize(
    "asker",
    [
        None,
        {"email": "vic@x.y", "role": "viewer", "hosted_domain": None},
    ],
    ids=["unknown", "viewer"],
)
def test_an_asker_who_is_unknown_or_cannot_write_gets_no_machine(
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


def test_a_writer_cannot_enroll_a_machine_that_belongs_to_someone_else(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, {"id": _MACHINE_ID, "owner": "other@x.y"})

    response = client.post(ENROLL_PATH, json={"name": "laptop"})

    assert response.status_code == 403
    engine.conn.fetchval.assert_not_called()
    assert executed_sql(engine.conn) == ["BEGIN", "ROLLBACK"]


def test_join_checks_the_token_unlocked_then_locks_the_machine_and_claims_it(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    enrollment = mint_secret(ENROLLMENT_PREFIX)
    parsed_enrollment = parse_enrollment(enrollment.token)
    assert parsed_enrollment is not None
    engine.conn.fetchval.side_effect = [enrollment.digest, _MACHINE_ID, _MACHINE_ID]

    response = _join(client, enrollment.token, facts={"os": "linux"})

    assert response.status_code == 201
    body = from_plain(response.json(), dict[str, str])
    assert body["machine_id"] == str(_MACHINE_ID)
    credential = parse_credential(body["credential"])
    assert credential is not None
    check, lock, claim = engine.conn.fetchval.call_args_list
    # A plain read first: join has no authentication, so a token that cannot succeed
    # must be refused before it queues for the machine row.
    sql = from_plain(check.args[0], str)
    assert "FOR UPDATE" not in sql
    assert "FROM machine_enrollments e JOIN machines m ON m.id = e.machine_id" in sql
    assert "e.id = $1 AND m.name = $2 AND e.used_at IS NULL" in sql
    assert "AND e.expires_at > now()" in sql
    assert (
        "FROM users u WHERE u.email = e.created_by "
        "AND u.status = 'active' AND ((u.role = 'admin' "
        "AND (m.owner IS NULL OR m.owner = u.email)) "
        "OR (u.email = m.owner AND m.role = $3))"
    ) in sql
    assert check.args[1:] == (parsed_enrollment.id, "dev-1", "personal")
    # The same first lock as enroll, revoke and delete take, or a join racing one of
    # them deadlocks.
    assert "FROM machines WHERE name = $1 FOR UPDATE" in from_plain(lock.args[0], str)
    assert lock.args[1:] == ("dev-1",)
    sql = from_plain(claim.args[0], str)
    assert "UPDATE machine_enrollments SET used_at = now()" in sql
    assert "used_at IS NULL AND expires_at > now() AND secret_sha256 = $2" in sql
    assert "AND machine_id = $3" in sql
    assert (
        "FROM users u JOIN machines m ON m.id = $3 "
        "WHERE u.email = machine_enrollments.created_by AND u.status = 'active' "
        "AND ((u.role = 'admin' AND (m.owner IS NULL OR m.owner = u.email)) "
        "OR (m.owner = u.email AND m.role = $4))"
    ) in sql
    assert claim.args[1:] == (
        parsed_enrollment.id,
        enrollment.digest,
        _MACHINE_ID,
        "personal",
    )
    sent = executed_sql(engine.conn)
    assert [sql.split(" (")[0].split(" SET")[0] for sql in sent] == [
        "BEGIN",
        "UPDATE machine_credentials",
        "INSERT INTO machine_credentials",
        "UPDATE machines",
        "COMMIT",
    ]
    assert "revoked_at = now()" in sent[1]
    assert "revoked_at IS NULL" in sent[1]
    insert, update = engine.conn.execute.call_args_list[-3:-1]
    assert insert.args[1:] == (credential.id, _MACHINE_ID, credential.digest)
    assert update.args[1:] == (
        _MACHINE_ID,
        {"os": "linux"},
        uuid.UUID(_INSTANCE),
        "0.1",
    )
    assert body["credential"] not in " ".join(sent)


@pytest.mark.parametrize(
    "answers",
    [[None], [_MACHINE_ID, None]],
    ids=["unknown-machine", "unclaimable-token"],
)
def test_join_that_finds_no_machine_or_no_claimable_token_is_401_and_writes_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
    answers: list[uuid.UUID | None],
) -> None:
    client, _, engine = route_client
    enrollment = mint_secret(ENROLLMENT_PREFIX)
    engine.conn.fetchval.side_effect = [enrollment.digest, *answers]

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
    assert "FOR UPDATE" not in from_plain(engine.conn.fetchval.call_args.args[0], str)


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


def test_heartbeat_locks_the_machine_then_checks_the_credential_then_updates_the_row(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [_MACHINE_ID, True, _BEAT]

    response = _heartbeat(client, credential.token, facts={"cpus": 4})

    assert response.status_code == 200
    assert from_plain(response.json(), dict[str, str]) == {
        "server_time": "2026-10-07T12:00:00Z",
    }
    lookup = engine.conn.fetchrow.call_args
    sql = from_plain(lookup.args[0], str)
    assert "FROM machine_credentials c JOIN machines m ON m.id = c.machine_id" in sql
    assert "WHERE c.id = $1" in sql
    assert "revoked_at IS NULL" not in sql
    assert lookup.args[1:] == (credential.id,)
    lock, live, beat = engine.conn.fetchval.call_args_list
    # The lock comes first, so a revoke in flight has committed before the check
    # reads the credential, and the update cannot land after it.
    assert "FROM machines WHERE id = $1 FOR UPDATE" in from_plain(lock.args[0], str)
    assert lock.args[1:] == (_MACHINE_ID,)
    assert (
        "FROM machine_credentials WHERE id = $1 AND revoked_at IS NULL"
        in from_plain(
            live.args[0],
            str,
        )
    )
    assert live.args[1:] == (credential.id,)
    sql = from_plain(beat.args[0], str)
    assert "($6::boolean OR host_instance IS NULL OR host_instance = $2" in sql
    assert "last_heartbeat < now() - make_interval(secs => $5)" in sql
    assert "facts = COALESCE($4, facts)" in sql
    assert beat.args[1:] == (
        _MACHINE_ID,
        uuid.UUID(_INSTANCE),
        "0.1",
        {"cpus": 4},
        OFFLINE_AFTER_SEC,
        False,
    )


# The oldest supported host is raised to 0.2 here, above 0.1.11, the first host that
# reads an answer naming a field it does not know.
@pytest.mark.parametrize(
    ("host_version", "told"),
    [("0.1.11", True), ("0.1.9", False), ("0.2", False), ("", False)],
)
def test_a_heartbeat_tells_an_outdated_host_only_when_it_can_read_that(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    host_version: str,
    told: bool,
) -> None:
    client, _, engine = route_client
    monkeypatch.setattr(machine_host_routes, "MIN_HOST_VERSION", "0.2")
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [_MACHINE_ID, True, _BEAT]

    response = _heartbeat(client, credential.token, host_version=host_version)

    assert response.status_code == 200
    assert from_plain(response.json(), dict[str, object]) == {
        "server_time": "2026-10-07T12:00:00Z",
        **({"host_outdated": True} if told else {}),
    }


def test_a_heartbeat_the_instance_rule_refuses_is_409(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [_MACHINE_ID, True, None]

    response = _heartbeat(client, credential.token)

    assert response.status_code == 409
    assert (
        from_plain(response.json(), dict[str, str])["code"] == "another_host_connected"
    )


def test_a_take_passes_the_instance_rule_by_and_a_release_only_lets_go(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [_MACHINE_ID, True, _BEAT] * 2

    take = _heartbeat(client, credential.token, claim="take")
    release = _heartbeat(client, credential.token, claim="release")

    assert (take.status_code, release.status_code) == (200, 200)
    took, let_go = engine.conn.fetchval.call_args_list[2::3]
    assert took.args[-1] is True
    sql = from_plain(let_go.args[0], str)
    assert "CASE WHEN host_instance = $2 THEN NULL ELSE host_instance END" in sql
    assert "last_heartbeat" not in sql
    assert let_go.args[1:] == (_MACHINE_ID, uuid.UUID(_INSTANCE))


def test_a_heartbeat_whose_credential_was_revoked_meanwhile_is_410_and_writes_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.side_effect = [_MACHINE_ID, False]

    response = _heartbeat(client, credential.token)

    assert response.status_code == 410
    assert from_plain(response.json(), dict[str, str])["detail"] == "machine_revoked"
    assert engine.conn.fetchval.call_count == 2
    assert executed_sql(engine.conn)[-1] == "ROLLBACK"


def test_last_used_is_written_by_the_first_authenticated_request_only(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))
    engine.conn.fetchval.return_value = _BEAT

    _heartbeat(client, credential.token)
    _heartbeat(client, credential.token)

    touches = [
        c
        for c in engine.conn.execute.call_args_list
        if "UPDATE machine_credentials SET last_used = now()"
        in from_plain(c.args[0], str)
    ]
    assert [t.args[1:] for t in touches] == [(credential.id,)]


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
    assert from_plain(matching.json(), dict[str, str])["detail"] == "machine_revoked"
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
            HEARTBEAT_PATH.format(machine_id=_MACHINE_ID),
            json={"instance": _INSTANCE, "host_version": "0.1"},
        ),
    ]

    assert [r.status_code for r in responses] == [401, 401, 401]
    assert len({r.text for r in responses}) == 1
    engine.conn.fetchval.assert_not_called()


def test_a_host_request_naming_a_field_the_server_does_not_know_is_422(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(engine.conn, _credential_row(credential))

    beat = client.post(
        HEARTBEAT_PATH.format(machine_id=_MACHINE_ID),
        json={"instance": _INSTANCE, "host_version": "0.1", "lane": 1},
        headers={"Authorization": f"Bearer {credential.token}"},
    )
    joined = client.post(
        JOIN_PATH,
        json={
            "name": "dev-1",
            "token": mint_secret(ENROLLMENT_PREFIX).token,
            "instance": _INSTANCE,
            "host_version": "0.1",
            "lane": 1,
        },
    )

    assert (beat.status_code, joined.status_code) == (422, 422)
    engine.conn.fetchval.assert_not_called()


def test_another_machines_credential_is_404_and_updates_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    credential = mint_secret(CREDENTIAL_PREFIX)
    set_field_row(
        engine.conn,
        {**_credential_row(credential), "machine_id": uuid.uuid4()},
    )

    response = _heartbeat(client, credential.token)

    assert response.status_code == 404
    engine.conn.fetchval.assert_not_called()
    engine.conn.execute.assert_not_called()


def test_revoke_closes_credentials_enrollments_and_the_host_instance(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    set_field_row(engine.conn, {"id": _MACHINE_ID, "owner": "other@x.y"})

    response = client.post(REVOKE_PATH.format(name="dev-1"))

    assert (response.status_code, response.content) == (204, b"")
    select = engine.conn.fetchrow.call_args
    assert "FROM machines WHERE name = $1 FOR UPDATE" in from_plain(select.args[0], str)
    assert select.args[1:] == ("dev-1",)
    sent = executed_sql(engine.conn)
    assert [sql.split(" SET")[0] for sql in sent] == [
        "BEGIN",
        "UPDATE machine_credentials",
        "UPDATE machine_enrollments",
        "UPDATE machines",
        "COMMIT",
    ]
    assert "revoked_at = now()" in sent[1]
    assert "revoked_at IS NULL" in sent[1]
    assert "host_instance = NULL" in sent[3]
    assert [c.args[1:] for c in engine.conn.execute.call_args_list[1:4]] == [
        (_MACHINE_ID,),
    ] * 3


def test_revoke_of_an_absent_machine_is_404_and_changes_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    set_field_row(engine.conn, None)

    response = client.post(REVOKE_PATH.format(name="nope"))

    assert response.status_code == 404
    assert executed_sql(engine.conn) == ["BEGIN", "ROLLBACK"]


def test_revoke_by_a_viewer_sends_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("viewer")

    response = client.post(REVOKE_PATH.format(name="dev-1"))

    assert response.status_code == 403
    _assert_untouched(engine)


def test_a_writer_revokes_their_own_machine_and_not_another_users(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, {"id": _MACHINE_ID, "owner": "other@x.y"})

    refused = client.post(REVOKE_PATH.format(name="dev-1"))

    assert refused.status_code == 403
    assert executed_sql(engine.conn) == ["BEGIN", "ROLLBACK"]

    set_field_row(engine.conn, {"id": _MACHINE_ID, "owner": TEST_USER_EMAIL})
    allowed = client.post(REVOKE_PATH.format(name="dev-1"))

    assert allowed.status_code == 204


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
        "machine_id": _MACHINE_ID,
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
            "host_version": "0.1",
            "facts": facts or {},
        },
    )


def _heartbeat(
    client: TestClient,
    bearer: str,
    *,
    facts: dict[str, object] | None = None,
    claim: str | None = None,
    host_version: str = "0.1",
) -> Response:
    body: dict[str, object] = {"instance": _INSTANCE, "host_version": host_version}
    if facts is not None:
        body["facts"] = facts
    if claim is not None:
        body["claim"] = claim
    return client.post(
        HEARTBEAT_PATH.format(machine_id=_MACHINE_ID),
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
