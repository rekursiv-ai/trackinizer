"""Variable routes against a mocked connection: the SQL each route sends.

``variables_routes_pglite_test.py`` runs the same routes against a real
database; these pin what the routes ask of the connection and the backend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Final, override

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
    from pathlib import Path

    from httpx2 import Response

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


_HIDDEN: Final = "s3cr3t-value-7f3a"
_REF: Final = SecretRef(layer="org", owner="", name="TOKEN")
_LOCK: Final = "SELECT pg_advisory_xact_lock(hashtext($1))"
_LIST: Final = (
    "SELECT layer, owner, name, secret, value, updated_by, updated "
    "FROM variables WHERE layer = 'org' AND owner = '' ORDER BY name"
)
_UPSERT: Final = (
    "INSERT INTO variables (layer, owner, name, secret, value, updated_by) "
    "VALUES ($1, $2, $3, $4, $5, $6) "
    "ON CONFLICT (layer, owner, name) DO UPDATE SET "
    "secret = EXCLUDED.secret, value = EXCLUDED.value, "
    "updated_by = EXCLUDED.updated_by, updated = now() "
    "WHERE EXCLUDED.secret OR NOT variables.secret "
    "RETURNING name"
)
_SELECT_FLAG: Final = (
    "SELECT secret FROM variables WHERE layer = $1 AND owner = $2 AND name = $3"
)
_DELETE: Final = "DELETE FROM variables WHERE layer = $1 AND owner = $2 AND name = $3"
_Recorded = tuple[str, tuple[object, ...], dict[str, object]]
"""How a mock's ``mock_calls`` records a call: method, arguments, keywords."""

_BEGIN: Final[_Recorded] = ("execute", ("BEGIN",), {})
_COMMIT: Final[_Recorded] = ("execute", ("COMMIT",), {})
_ROLLBACK: Final[_Recorded] = ("fetch", ("ROLLBACK",), {})


@pytest.fixture
def vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FileSecrets:
    """Serve the app's secrets from a temporary directory."""
    backend = FileSecrets(tmp_path / "vault")
    monkeypatch.setattr(app.state, "secrets", backend, raising=False)
    return backend


@pytest.fixture
def no_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the app with secret storage disabled."""
    monkeypatch.setattr(app.state, "secrets", None, raising=False)


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
    assert engine.conn.mock_calls == [_call("fetch", _LIST)]


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
    assert engine.conn.mock_calls == []


def test_a_plain_put_upserts_the_value_without_touching_a_secret_row(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, "GREETING")

    response = client.put(_path("GREETING"), json={"value": "hello"})

    assert (response.status_code, response.content) == (204, b"")
    assert engine.conn.mock_calls == [
        _BEGIN,
        _lock("GREETING"),
        _call(
            "fetchval",
            _UPSERT,
            "org",
            "",
            "GREETING",
            False,
            "hello",
            TEST_USER_EMAIL,
        ),
        _COMMIT,
    ]


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
    assert engine.conn.mock_calls == [
        _BEGIN,
        _lock("TOKEN"),
        _call("fetchval", _UPSERT, "org", "", "TOKEN", True, None, TEST_USER_EMAIL),
        _COMMIT,
    ]
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
    assert engine.conn.mock_calls == []


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
    assert engine.conn.mock_calls == []


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
    assert engine.conn.mock_calls == []


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
    assert engine.conn.mock_calls == []


def test_deleting_an_absent_variable_is_not_found(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, None)

    response = client.delete(_path("NOPE"))

    assert response.status_code == 404
    assert engine.conn.mock_calls == [
        _BEGIN,
        _lock("NOPE"),
        _call("fetchval", _SELECT_FLAG, "org", "", "NOPE"),
        _ROLLBACK,
    ]


@pytest.mark.usefixtures("no_vault")
def test_deleting_a_plain_variable_needs_no_backend(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, False)

    response = client.delete(_path("GREETING"))

    assert (response.status_code, response.content) == (204, b"")
    assert engine.conn.mock_calls == [
        _BEGIN,
        _lock("GREETING"),
        _call("fetchval", _SELECT_FLAG, "org", "", "GREETING"),
        _call("execute", _DELETE, "org", "", "GREETING"),
        _COMMIT,
    ]


def test_deleting_a_secret_deletes_its_row_before_its_value(
    route_client: tuple[TestClient, Store, FakeEngine],
    vault: FileSecrets,
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, True)
    vault.put(_REF, _HIDDEN)
    present_at_delete: list[bool] = []
    engine.conn.execute.side_effect = partial(_note_at_delete, vault, present_at_delete)

    response = client.delete(_path("TOKEN"))

    assert response.status_code == 204
    assert present_at_delete == [True]
    with pytest.raises(SecretNotFoundError):
        vault.get(_REF)


@pytest.mark.usefixtures("no_vault")
def test_deleting_a_secret_without_a_backend_keeps_the_row(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, True)

    response = client.delete(_path("TOKEN"))

    assert response.status_code == 503
    assert engine.conn.mock_calls == [
        _BEGIN,
        _lock("TOKEN"),
        _call("fetchval", _SELECT_FLAG, "org", "", "TOKEN"),
        _ROLLBACK,
    ]


def test_a_secret_put_writes_the_row_before_the_value(
    route_client: tuple[TestClient, Store, FakeEngine],
    vault: FileSecrets,
) -> None:
    client, _, engine = route_client
    _as("admin")
    present_at_upsert: list[bool] = []
    engine.conn.fetchval.side_effect = partial(
        _note_at_upsert,
        vault,
        present_at_upsert,
    )

    response = client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})

    assert response.status_code == 204
    assert present_at_upsert == [False]
    assert vault.get(_REF) == _HIDDEN


def test_a_failed_upsert_leaves_the_stored_secret_as_it_was(
    route_client: tuple[TestClient, Store, FakeEngine],
    vault: FileSecrets,
) -> None:
    client, _, engine = route_client
    _as("admin")
    vault.put(_REF, "old")
    engine.conn.fetchval.side_effect = RuntimeError("connection lost")

    with pytest.raises(RuntimeError, match="connection lost"):
        client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})

    assert vault.get(_REF) == "old"
    assert _COMMIT not in engine.conn.mock_calls


def test_a_failed_row_delete_leaves_the_stored_secret_in_place(
    route_client: tuple[TestClient, Store, FakeEngine],
    vault: FileSecrets,
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, True)
    vault.put(_REF, _HIDDEN)
    engine.conn.execute.side_effect = _fail_at_delete

    with pytest.raises(RuntimeError, match="connection lost"):
        client.delete(_path("TOKEN"))

    assert vault.get(_REF) == _HIDDEN
    assert _COMMIT not in engine.conn.mock_calls


def test_a_backend_that_cannot_store_rolls_the_row_back(
    route_client: tuple[TestClient, Store, FakeEngine],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, "TOKEN")
    monkeypatch.setattr(app.state, "secrets", _FullDisk(tmp_path), raising=False)

    with pytest.raises(OSError, match="no space left"):
        client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})

    assert engine.conn.mock_calls[-1] == _ROLLBACK
    assert _COMMIT not in engine.conn.mock_calls


def test_a_backend_that_cannot_delete_rolls_the_row_back(
    route_client: tuple[TestClient, Store, FakeEngine],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, True)
    monkeypatch.setattr(app.state, "secrets", _FullDisk(tmp_path), raising=False)

    with pytest.raises(OSError, match="no space left"):
        client.delete(_path("TOKEN"))

    assert engine.conn.mock_calls[-1] == _ROLLBACK
    assert _COMMIT not in engine.conn.mock_calls


def test_a_write_takes_its_names_lock_before_touching_the_backend_or_table(
    route_client: tuple[TestClient, Store, FakeEngine],
    vault: FileSecrets,
) -> None:
    client, _, engine = route_client
    _as("admin")
    _answer(engine, "TOKEN")
    present_at_lock: list[bool] = []
    engine.conn.execute.side_effect = partial(_note_at_lock, vault, present_at_lock)

    client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})
    client.put(_path("OTHER"), json={"value": "v"})

    assert engine.conn.mock_calls == [
        _BEGIN,
        _lock("TOKEN"),
        _call("fetchval", _UPSERT, "org", "", "TOKEN", True, None, TEST_USER_EMAIL),
        _COMMIT,
        _BEGIN,
        _lock("OTHER"),
        _call("fetchval", _UPSERT, "org", "", "OTHER", False, "v", TEST_USER_EMAIL),
        _COMMIT,
    ]
    assert present_at_lock[0] is False


class _FullDisk(FileSecrets):
    """A backend whose writes fail, as a full disk does."""

    @override
    def put(self, ref: SecretRef, value: str) -> None:
        raise OSError("no space left on device")

    @override
    def delete(self, ref: SecretRef) -> None:
        raise OSError("no space left on device")


def _call(
    method: str,
    *args: object,
) -> tuple[str, tuple[object, ...], dict[str, object]]:
    """Return a mock's record of ``method(*args)``, which ``mock_calls`` equals."""
    return (method, args, {})


def _lock(name: str) -> _Recorded:
    """Return the call that takes the advisory lock of org variable ``name``."""
    return _call("execute", _LOCK, f"trackinizer.variables.org..{name}")


def _present(vault: FileSecrets) -> bool:
    """Whether the vault holds the test secret right now."""
    try:
        vault.get(_REF)
    except SecretNotFoundError:
        return False
    return True


async def _note_at_lock(
    vault: FileSecrets,
    seen: list[bool],
    sql: str,
    *args: object,
) -> str:
    """Stand in for ``execute``, noting whether the secret exists at each lock."""
    del args
    if sql == _LOCK:
        seen.append(_present(vault))
    return "OK"


async def _note_at_delete(
    vault: FileSecrets,
    seen: list[bool],
    sql: str,
    *args: object,
) -> str:
    """Stand in for ``execute``, noting whether the secret exists at the row delete."""
    del args
    if sql == _DELETE:
        seen.append(_present(vault))
    return "OK"


async def _note_at_upsert(
    vault: FileSecrets,
    seen: list[bool],
    sql: str,
    *args: object,
) -> str:
    """Stand in for ``fetchval``, noting whether the secret exists at the upsert."""
    del args
    assert sql == _UPSERT
    seen.append(_present(vault))
    return "TOKEN"


async def _fail_at_delete(sql: str, *args: object) -> str:
    """Stand in for ``execute``, failing the row ``DELETE`` as a dropped link does."""
    del args
    if sql == _DELETE:
        raise RuntimeError("connection lost")
    return "OK"


def _as(role: Role) -> None:
    install_identity(make_test_identity(role=role))


def _path(name: str) -> str:
    return VARIABLE_PATH.format(name=name)


def _detail(response: Response) -> object:
    return from_plain(response.json(), dict[str, object])["detail"]


def _answer(engine: FakeEngine, value: object) -> None:
    """Make the connection's ``fetchval`` return ``value``."""
    engine.conn.fetchval.side_effect = None
    engine.conn.fetchval.return_value = value


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
