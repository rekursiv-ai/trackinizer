"""Machine host routes against a mocked connection: the SQL each route sends.

``machine_host_routes_pglite_test.py`` runs the same routes against a real database;
these pin what the routes and the credential check ask of the connection, and that
a refusal sends nothing further.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import uuid

import pytest

from trackinizer.conftest import executed_sql, set_field_row
from trackinizer.lib.codec import from_plain
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    install_identity,
    make_test_identity,
)
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
    engine.conn.fetchval.side_effect = [_MACHINE_ID, _BEAT]

    response = client.post(ENROLL_PATH, json={"name": "dev-1"})

    assert response.status_code == 201
    token = from_plain(response.json(), dict[str, str])["token"]
    parsed = parse_enrollment(token)
    assert parsed is not None
    upsert, insert = engine.conn.fetchval.call_args_list
    # One statement registers the machine or locks its row; a separate insert and
    # lock leave a gap in which a delete commits and the lock finds no machine.
    sql = from_plain(upsert.args[0], str)
    assert "INSERT INTO machines (name, updated_by) VALUES ($1, $2)" in sql
    assert "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id" in sql
    assert upsert.args[1:] == ("dev-1", TEST_USER_EMAIL)
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


def test_enroll_by_a_non_admin_sends_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client

    response = client.post(ENROLL_PATH, json={"name": "dev-1"})

    assert response.status_code == 403
    _assert_untouched(engine)


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
        "AND u.status = 'active' AND u.role = 'admin'"
    ) in sql
    assert check.args[1:] == (parsed_enrollment.id, "dev-1")
    # The same first lock as enroll, revoke and delete take, or a join racing one of
    # them deadlocks.
    assert "FROM machines WHERE name = $1 FOR UPDATE" in from_plain(lock.args[0], str)
    assert lock.args[1:] == ("dev-1",)
    sql = from_plain(claim.args[0], str)
    assert "UPDATE machine_enrollments SET used_at = now()" in sql
    assert "used_at IS NULL AND expires_at > now() AND secret_sha256 = $2" in sql
    assert "AND machine_id = $3" in sql
    assert (
        "FROM users WHERE email = created_by AND status = 'active' AND role = 'admin'"
    ) in sql
    assert claim.args[1:] == (
        parsed_enrollment.id,
        enrollment.digest,
        _MACHINE_ID,
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
    assert "host_instance IS NULL OR host_instance = $2" in sql
    assert "last_heartbeat < now() - make_interval(secs => $5)" in sql
    assert "facts = COALESCE($4, facts)" in sql
    assert beat.args[1:] == (
        _MACHINE_ID,
        uuid.UUID(_INSTANCE),
        "0.1",
        {"cpus": 4},
        OFFLINE_AFTER_SEC,
    )


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
    engine.conn.fetchval.return_value = _MACHINE_ID

    response = client.post(REVOKE_PATH.format(name="dev-1"))

    assert (response.status_code, response.content) == (204, b"")
    select = engine.conn.fetchval.call_args
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
    engine.conn.fetchval.return_value = None

    response = client.post(REVOKE_PATH.format(name="nope"))

    assert response.status_code == 404
    assert executed_sql(engine.conn) == ["BEGIN", "ROLLBACK"]


def test_revoke_by_a_non_admin_sends_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client

    response = client.post(REVOKE_PATH.format(name="dev-1"))

    assert response.status_code == 403
    _assert_untouched(engine)


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
) -> Response:
    body: dict[str, object] = {"instance": _INSTANCE, "host_version": "0.1"}
    if facts is not None:
        body["facts"] = facts
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
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
