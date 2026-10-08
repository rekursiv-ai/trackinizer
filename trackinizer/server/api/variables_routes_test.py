"""Variable routes against a mocked connection: the SQL each route sends.

``variables_routes_pglite_test.py`` runs the same routes against a real
database; these pin what the routes ask of the connection and the backend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Final

from fastapi.testclient import TestClient

import pytest

from trackinizer.lib.codec import from_plain
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    install_identity,
    make_test_identity,
)
from trackinizer.server.secrets import (
    FileSecrets,
    SecretNotFoundError,
    SecretRef,
)
from trackinizer.wire.wire_variables import (
    MAX_VALUE_BYTES,
    VARIABLE_PATH,
    VARIABLES_PATH,
)


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from httpx2 import Response

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


_HIDDEN: Final = "s3cr3t-value-7f3a"
_REF: Final = SecretRef(layer="org", owner="", name="TOKEN")
_LOCK: Final = "SELECT pg_advisory_xact_lock(hashtext($1))"


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[FileSecrets]:
    """Serve the app's secrets from a temporary directory."""
    backend = FileSecrets(tmp_path / "vault")
    app.state.secrets = backend
    yield backend
    del app.state.secrets


@pytest.fixture
def no_vault() -> Iterator[None]:
    """Serve the app with secret storage disabled."""
    app.state.secrets = None
    yield
    del app.state.secrets


def test_listing_asks_for_the_org_layer_by_name(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    updated = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    engine.conn.fetch.return_value = [
        {
            "layer": "org",
            "owner": "",
            "name": "A_SECRET",
            "secret": True,
            "value": None,
            "updated_by": TEST_USER_EMAIL,
            "updated": updated,
        },
        {
            "layer": "org",
            "owner": "",
            "name": "B_PLAIN",
            "secret": False,
            "value": "one",
            "updated_by": TEST_USER_EMAIL,
            "updated": updated,
        },
    ]

    response = client.get(VARIABLES_PATH)

    assert response.status_code == 200
    body = from_plain(response.json(), dict[str, list[dict[str, object]]])
    assert [(v["name"], v["value"]) for v in body["variables"]] == [
        ("A_SECRET", None),
        ("B_PLAIN", "one"),
    ]
    sql = from_plain(engine.conn.fetch.call_args.args[0], str)
    assert "WHERE layer = 'org' AND owner = ''" in sql
    assert sql.endswith("ORDER BY name")


def test_a_viewer_cannot_list_and_a_writer_cannot_change(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client

    writer_put = client.put(_path("KEEP"), json={"value": "v"})
    writer_delete = client.delete(_path("KEEP"))
    _as("viewer")
    viewer_list = client.get(VARIABLES_PATH)

    assert (writer_put.status_code, writer_delete.status_code) == (403, 403)
    assert viewer_list.status_code == 403
    engine.conn.fetchval.assert_not_called()
    engine.conn.execute.assert_not_called()


def test_a_plain_put_upserts_the_value_without_touching_a_secret_row(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, "GREETING")

    response = client.put(_path("GREETING"), json={"value": "hello"})

    assert (response.status_code, response.content) == (204, b"")
    sql, *args = engine.conn.fetchval.call_args.args
    assert args == ["org", "", "GREETING", False, "hello", TEST_USER_EMAIL]
    assert "ON CONFLICT (layer, owner, name) DO UPDATE" in from_plain(sql, str)
    assert "WHERE EXCLUDED.secret OR NOT variables.secret" in from_plain(sql, str)


def test_a_plain_put_on_a_secret_name_conflicts(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, None)

    response = client.put(_path("TOKEN"), json={"value": "exposed"})

    assert response.status_code == 409
    assert _detail(response) == "TOKEN is a secret; delete it first"


def test_a_secret_put_stores_the_value_only_in_the_backend(
    route_client: tuple[TestClient, Store, FakeEngine],
    vault: FileSecrets,
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, "TOKEN")

    response = client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})

    assert (response.status_code, response.content) == (204, b"")
    assert vault.get(_REF) == _HIDDEN
    args = engine.conn.fetchval.call_args.args[1:]
    assert args == ("org", "", "TOKEN", True, None, TEST_USER_EMAIL)
    assert _HIDDEN not in repr(engine.conn.mock_calls)


@pytest.mark.usefixtures("no_vault")
def test_a_secret_put_without_a_backend_never_reaches_the_database(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")

    response = client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})

    assert response.status_code == 503
    assert _detail(response) == "no secret store is configured"
    engine.conn.fetchval.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"value": "x" * (MAX_VALUE_BYTES + 1)},
        {"value": "é" * (MAX_VALUE_BYTES // 2 + 1)},
        {"value": "a\x00b"},
        {"value": ""},
    ],
    ids=["too-long", "too-many-bytes", "nul", "empty"],
)
@pytest.mark.usefixtures("vault")
def test_a_bad_value_never_reaches_the_database(
    route_client: tuple[TestClient, Store, FakeEngine],
    body: dict[str, str],
) -> None:
    client, _, engine = route_client
    _as("admin")

    plain = client.put(_path("V"), json=body)
    secret = client.put(_path("V"), json={**body, "secret": True})

    assert (plain.status_code, secret.status_code) == (422, 422)
    engine.conn.fetchval.assert_not_called()


@pytest.mark.usefixtures("vault")
def test_a_lone_surrogate_is_a_validation_error(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    """JSON can spell a surrogate UTF-8 cannot encode; the body model refuses it."""
    client, _, engine = route_client
    _as("admin")
    headers = {"content-type": "application/json"}

    plain = client.put(_path("V"), content=b'{"value": "\\ud800"}', headers=headers)
    secret = client.put(
        _path("V"),
        content=b'{"value": "\\ud800", "secret": true}',
        headers=headers,
    )

    assert (plain.status_code, secret.status_code) == (422, 422)
    engine.conn.fetchval.assert_not_called()


@pytest.mark.usefixtures("route_client")
def test_a_prefixed_deployment_still_redacts_a_validation_error() -> None:
    """Behind a path prefix a 422 still drops the rejected input."""
    _as("admin")
    prefixed = TestClient(app, root_path="/trax")

    response = prefixed.put(
        "/trax" + _path("TOKEN"),
        json={"value": _HIDDEN, "secret": _HIDDEN},
    )

    assert response.status_code == 422
    assert _HIDDEN not in response.text


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_a_name_with_a_trailing_newline_is_not_another_name(
    route_client: tuple[TestClient, Store, FakeEngine],
    method: str,
) -> None:
    """Starlette's ``$`` matches before a final newline: ``A%0A`` reaches ``A``."""
    client, _, engine = route_client
    _as("admin")

    response = client.request(method, url=_path("A%0A"), json={"value": "v"})

    assert response.status_code == 422
    engine.conn.fetchval.assert_not_called()
    engine.conn.execute.assert_not_called()


def test_deleting_an_absent_variable_is_not_found(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, None)

    response = client.delete(_path("NOPE"))

    assert response.status_code == 404
    assert _sent(engine) == ["BEGIN", _LOCK, "SELECT secret FROM", "ROLLBACK"]


@pytest.mark.usefixtures("no_vault")
def test_deleting_a_plain_variable_needs_no_backend(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, False)

    response = client.delete(_path("GREETING"))

    assert (response.status_code, response.content) == (204, b"")
    assert _sent(engine) == [
        "BEGIN",
        _LOCK,
        "SELECT secret FROM",
        "DELETE FROM variables",
        "COMMIT",
    ]
    delete = engine.conn.execute.call_args_list[2]
    assert delete.args[1:] == ("org", "", "GREETING")


def test_deleting_a_secret_removes_its_value_before_its_row(
    route_client: tuple[TestClient, Store, FakeEngine],
    vault: FileSecrets,
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, True)
    vault.put(_REF, _HIDDEN)
    present_at_delete: list[bool] = []
    engine.conn.execute.side_effect = partial(_delete_row, vault, present_at_delete)

    response = client.delete(_path("TOKEN"))

    assert response.status_code == 204
    assert present_at_delete == [False]


@pytest.mark.usefixtures("no_vault")
def test_deleting_a_secret_without_a_backend_keeps_the_row(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, True)

    response = client.delete(_path("TOKEN"))

    assert response.status_code == 503
    assert "DELETE FROM variables" not in _sent(engine)


def test_a_write_takes_its_names_lock_before_touching_the_backend_or_table(
    route_client: tuple[TestClient, Store, FakeEngine],
    vault: FileSecrets,
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, "TOKEN")
    present_at_lock: list[bool] = []
    engine.conn.execute.side_effect = partial(_note_value, vault, present_at_lock)

    client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})
    client.put(_path("OTHER"), json={"value": "v"})

    assert _sent(engine) == [
        "BEGIN",
        _LOCK,
        "INSERT INTO variables",
        "COMMIT",
        "BEGIN",
        _LOCK,
        "INSERT INTO variables",
        "COMMIT",
    ]
    assert present_at_lock[0] is False
    locks = [
        call.args[1]
        for call in engine.conn.execute.call_args_list
        if _LOCK in from_plain(call.args[0], str)
    ]
    assert locks == [
        "trackinizer.variables.org..TOKEN",
        "trackinizer.variables.org..OTHER",
    ]


async def _note_value(
    vault: FileSecrets,
    seen: list[bool],
    sql: str,
    *args: object,
) -> str:
    """Stand in for ``execute``, noting whether the secret's file exists at the lock."""
    del args
    if _LOCK in sql:
        try:
            vault.get(_REF)
        except SecretNotFoundError:
            seen.append(False)
        else:
            seen.append(True)
    return "OK"


async def _delete_row(
    vault: FileSecrets,
    seen: list[bool],
    sql: str,
    *args: object,
) -> str:
    """Stand in for the row ``DELETE``, noting whether the secret's file is left."""
    del args
    if sql.startswith("DELETE FROM variables"):
        try:
            vault.get(_REF)
        except SecretNotFoundError:
            seen.append(False)
        else:
            seen.append(True)
    return "OK"


def _as(role: Role) -> None:
    install_identity(make_test_identity(role=role))


def _path(name: str) -> str:
    return VARIABLE_PATH.format(name=name)


def _detail(response: Response) -> object:
    return from_plain(response.json(), dict[str, object])["detail"]


def _sent(engine: FakeEngine) -> list[str]:
    """Return the first words of each statement the routes sent, in order."""
    return [
        " ".join(from_plain(call.args[0], str).split()[:3])
        for call in engine.conn.mock_calls
    ]


def _answer(engine: FakeEngine, value: object) -> None:
    """Make the connection's ``fetchval`` return ``value``."""
    engine.conn.fetchval.side_effect = None
    engine.conn.fetchval.return_value = value


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
