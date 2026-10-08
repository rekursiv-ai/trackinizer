"""Machine routes against a mocked connection: the SQL each route sends.

``machines_routes_pglite_test.py`` runs the same routes against a real
database; these pin what the routes ask of the connection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import uuid

import pytest

from trackinizer.conftest import executed_sql
from trackinizer.lib.codec import from_plain
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    install_identity,
    make_test_identity,
)
from trackinizer.wire.wire_machine_host import OFFLINE_AFTER_SEC
from trackinizer.wire.wire_machines import (
    MACHINE_LABELS_PATH,
    MACHINE_PATH,
    MACHINES_PATH,
    MAX_HOW_CHARS,
    RESERVED_NAMES,
)


if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from httpx2 import Response

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


_UPDATED = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
_MACHINE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _row(name: str, *, role: str = "", how: str = "") -> dict[str, object]:
    return {
        "name": name,
        "role": role,
        "how": how,
        "labels": ["gpu"],
        "updated_by": TEST_USER_EMAIL,
        "updated": _UPDATED,
        "last_heartbeat": None,
        "status": "never",
        "host_version": "",
        "facts": {},
    }


def test_listing_asks_for_every_machine_in_byte_order_of_name(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    engine.conn.fetch.return_value = [
        _row("a-box", role="dev", how="ssh a-box"),
        _row("b-box"),
    ]

    response = client.get(MACHINES_PATH)

    assert response.status_code == 200
    body = from_plain(response.json(), dict[str, list[dict[str, object]]])
    assert [(m["name"], m["role"], m["labels"]) for m in body["machines"]] == [
        ("a-box", "dev", ["gpu"]),
        ("b-box", "", ["gpu"]),
    ]
    sql, *args = engine.conn.fetch.call_args.args
    assert 'OR name = $2 ORDER BY name COLLATE "C"' in from_plain(sql, str)
    assert args == [OFFLINE_AFTER_SEC, None]


def test_listing_reports_the_status_the_database_derived(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    engine.conn.fetch.return_value = [
        {**_row("a-box"), "status": "online", "last_heartbeat": _UPDATED},
        {**_row("b-box"), "status": "revoked"},
    ]

    body = from_plain(
        client.get(MACHINES_PATH).json(),
        dict[str, list[dict[str, object]]],
    )

    assert [
        (m["name"], m["status"], m["last_heartbeat"]) for m in body["machines"]
    ] == [
        ("a-box", "online", "2026-10-06T12:00:00Z"),
        ("b-box", "revoked", None),
    ]
    sql = from_plain(engine.conn.fetch.call_args.args[0], str)
    assert "last_heartbeat > now() - make_interval(secs => $1)" in sql
    assert "revoked_at IS NULL" in sql


def test_an_empty_registry_lists_no_machines(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    engine.conn.fetch.return_value = []

    assert client.get(MACHINES_PATH).json() == {"machines": []}


def test_getting_one_machine_asks_for_it_by_name(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    engine.conn.fetch.return_value = [_row("dev-1", role="dev", how="ssh dev-1")]

    response = client.get(_path("dev-1"))

    assert response.status_code == 200
    assert from_plain(response.json(), dict[str, object])["how"] == "ssh dev-1"
    sql, *args = engine.conn.fetch.call_args.args
    assert "WHERE $2::text IS NULL OR name = $2" in from_plain(sql, str)
    assert args == [OFFLINE_AFTER_SEC, "dev-1"]


def test_getting_an_absent_machine_is_not_found(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    engine.conn.fetch.return_value = []

    response = client.get(_path("nope"))

    assert response.status_code == 404
    assert _detail(response) == "machine 'nope' is not registered"


def test_a_viewer_cannot_read_and_a_writer_cannot_change(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client

    writes = [
        client.put(_path("dev-1"), json={"role": "dev"}),
        client.patch(_labels_path("dev-1"), json={"add": ["gpu"]}),
        client.delete(_path("dev-1")),
    ]
    _as("viewer")
    reads = [client.get(MACHINES_PATH), client.get(_path("dev-1"))]

    assert [r.status_code for r in writes] == [403, 403, 403]
    assert [r.status_code for r in reads] == [403, 403]
    _assert_untouched(engine)


def test_a_put_upserts_and_keeps_a_field_left_out(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")

    only_role = client.put(_path("dev-1"), json={"role": "dev"})
    only_how = client.put(_path("dev-1"), json={"how": "ssh dev-1"})
    cleared = client.put(_path("dev-1"), json={"role": "", "how": ""})

    assert [(r.status_code, r.content) for r in (only_role, only_how, cleared)] == [
        (204, b""),
    ] * 3
    sql = from_plain(engine.conn.execute.call_args.args[0], str)
    assert "ON CONFLICT (name) DO UPDATE" in sql
    assert "role = COALESCE($2, machines.role)" in sql
    assert "how = COALESCE($3, machines.how)" in sql
    assert [c.args[1:] for c in engine.conn.execute.call_args_list] == [
        ("dev-1", "dev", None, TEST_USER_EMAIL),
        ("dev-1", None, "ssh dev-1", TEST_USER_EMAIL),
        ("dev-1", "", "", TEST_USER_EMAIL),
    ]


def test_a_put_with_no_fields_registers_a_bare_machine(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")

    response = client.put(_path("dev-1"), json={})

    assert response.status_code == 204
    assert engine.conn.execute.call_args.args[1:] == (
        "dev-1",
        None,
        None,
        TEST_USER_EMAIL,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"role": "Dev"},
        {"role": "1st"},
        {"role": "r" * 33},
        {"how": "h" * (MAX_HOW_CHARS + 1)},
        {"how": "a\x00b"},
        {"role": None, "extra": "x"},
        {"role": 3},
    ],
    ids=["upper", "digit-first", "long-role", "long-how", "nul", "extra", "type"],
)
def test_a_bad_body_never_reaches_the_database(
    route_client: tuple[TestClient, Store, FakeEngine],
    body: dict[str, object],
) -> None:
    client, _, engine = route_client
    _as("admin")

    response = client.put(_path("dev-1"), json=body)

    assert response.status_code == 422
    engine.conn.execute.assert_not_called()


def test_the_longest_how_and_role_are_accepted(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")

    response = client.put(
        _path("a" * 63),
        json={"role": "r" * 32, "how": "é" * MAX_HOW_CHARS},
    )

    assert response.status_code == 204
    engine.conn.execute.assert_called_once()


@pytest.mark.parametrize(
    "name",
    ["Upper", "-lead", "a" * 64, "a_b", "a.b", "dev%0A", "dev%20x"],
)
def test_a_malformed_name_is_refused_on_every_route(
    route_client: tuple[TestClient, Store, FakeEngine],
    name: str,
) -> None:
    client, _, engine = route_client
    _as("admin")

    responses = [
        client.get(_path(name)),
        client.put(_path(name), json={}),
        client.patch(_labels_path(name), json={"add": ["x"]}),
        client.delete(_path(name)),
    ]

    assert [r.status_code for r in responses] == [422] * 4
    _assert_untouched(engine)


@pytest.mark.parametrize("name", sorted(RESERVED_NAMES))
def test_a_reserved_name_is_refused_on_every_route(
    route_client: tuple[TestClient, Store, FakeEngine],
    name: str,
) -> None:
    client, _, engine = route_client
    _as("admin")

    responses = [
        client.get(_path(name)),
        client.put(_path(name), json={}),
        client.patch(_labels_path(name), json={"add": ["x"]}),
        client.delete(_path(name)),
    ]

    assert [r.status_code for r in responses] == [422] * 4
    assert _detail(responses[1]) == f"{name!r} is a reserved machine name"
    _assert_untouched(engine)


@pytest.mark.parametrize("name", [*sorted(RESERVED_NAMES), "Upper"])
def test_a_caller_without_the_role_gets_403_before_the_name_is_judged(
    route_client: tuple[TestClient, Store, FakeEngine],
    name: str,
) -> None:
    client, _, engine = route_client

    writes = [
        client.put(_path(name), json={}),
        client.patch(_labels_path(name), json={"add": ["x"]}),
        client.delete(_path(name)),
    ]
    _as("viewer")
    reads = [client.get(_path(name))]

    assert [r.status_code for r in writes + reads] == [403] * 4
    _assert_untouched(engine)


def test_labels_are_added_then_removed_in_one_statement(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    engine.conn.fetchval.return_value = "dev-1"

    response = client.patch(
        _labels_path("dev-1"),
        json={"add": [" gpu ", "gpu", "a100"], "remove": ["old"]},
    )

    assert (response.status_code, response.content) == (204, b"")
    sql, *args = engine.conn.fetchval.call_args.args
    assert args == ["dev-1", ["gpu", "a100"], ["old"], TEST_USER_EMAIL]
    assert "WHERE name = $1 RETURNING name" in " ".join(from_plain(sql, str).split())
    assert "labels || $2::text[]" in from_plain(sql, str)
    assert "<> ALL($3::text[])" in from_plain(sql, str)


def test_labels_of_an_absent_machine_are_not_found(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    engine.conn.fetchval.return_value = None

    response = client.patch(_labels_path("nope"), json={"add": ["gpu"]})

    assert response.status_code == 404
    assert _detail(response) == "machine 'nope' is not registered"


@pytest.mark.parametrize(
    "body",
    [
        {"add": [""]},
        {"add": ["  "]},
        {"remove": ["\t"]},
        {"add": ["a\x00b"]},
        {"add": "gpu"},
        {"add": ["x"], "other": []},
    ],
    ids=["empty", "blank", "blank-remove", "nul", "type", "extra"],
)
def test_a_bad_label_never_reaches_the_database(
    route_client: tuple[TestClient, Store, FakeEngine],
    body: dict[str, object],
) -> None:
    client, _, engine = route_client
    _as("admin")

    response = client.patch(_labels_path("dev-1"), json=body)

    assert response.status_code == 422
    engine.conn.fetchval.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"add": [""]},
        {"add": ["  "]},
        {"remove": ["\t"]},
        {"add": ["a\x00b"]},
    ],
    ids=["empty", "blank", "blank-remove", "nul"],
)
def test_a_blank_or_nul_label_is_refused_with_its_reason(
    route_client: tuple[TestClient, Store, FakeEngine],
    body: dict[str, object],
) -> None:
    client, _, _ = route_client
    _as("admin")

    response = client.patch(_labels_path("dev-1"), json=body)

    assert response.status_code == 422
    assert _detail(response) == "a label is blank or contains a NUL character"


def test_deleting_removes_the_row_by_name_when_no_credential_is_live(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    engine.conn.fetchval.side_effect = [_MACHINE_ID, False]

    response = client.delete(_path("dev-1"))

    assert (response.status_code, response.content) == (204, b"")
    lock, check = engine.conn.fetchval.call_args_list
    # The credential check is its own statement: one that shared the lock's would
    # read the snapshot taken before the lock was granted, and miss a join that
    # committed while it waited.
    assert "FROM machines WHERE name = $1 FOR UPDATE" in from_plain(lock.args[0], str)
    assert lock.args[1:] == ("dev-1",)
    assert "revoked_at IS NULL" in from_plain(check.args[0], str)
    assert check.args[1:] == (_MACHINE_ID,)
    assert executed_sql(engine.conn)[-2:] == [
        "DELETE FROM machines WHERE name = $1",
        "COMMIT",
    ]


def test_deleting_a_machine_in_service_is_refused_and_deletes_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    engine.conn.fetchval.side_effect = [_MACHINE_ID, True]

    response = client.delete(_path("dev-1"))

    assert response.status_code == 409
    assert from_plain(response.json(), dict[str, str])["code"] == "machine_in_service"
    sent = executed_sql(engine.conn)
    assert not any("DELETE" in sql for sql in sent)
    assert sent[-1] == "ROLLBACK"


def test_deleting_an_absent_machine_is_not_found(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    engine.conn.fetchval.return_value = None

    response = client.delete(_path("nope"))

    assert response.status_code == 404
    assert _detail(response) == "machine 'nope' is not registered"


def _as(role: Role) -> None:
    install_identity(make_test_identity(role=role))


def _path(name: str) -> str:
    return MACHINE_PATH.format(name=name)


def _labels_path(name: str) -> str:
    return MACHINE_LABELS_PATH.format(name=name)


def _assert_untouched(engine: FakeEngine) -> None:
    """Assert no route sent the connection anything."""
    engine.conn.fetch.assert_not_called()
    engine.conn.fetchrow.assert_not_called()
    engine.conn.fetchval.assert_not_called()
    engine.conn.execute.assert_not_called()


def _detail(response: Response) -> object:
    return from_plain(response.json(), dict[str, object])["detail"]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
