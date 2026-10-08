"""Variable routes, end to end over PGlite and a file secret backend.

The whole app answers each request, so the exception handlers and the request
logger are on the path. What the tests guard hardest is that a secret's value
leaves no trace: in a response, a database column, or a log record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, override
from urllib.parse import quote

import asyncio
import logging
import stat
import threading

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
    VariableList,
)


if TYPE_CHECKING:
    from pathlib import Path

    import httpx2

    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store
    from trackinizer.wire.wire_variables import Layer


_HIDDEN: Final = "s3cr3t-value-7f3a"
_ORG: Final[Layer] = "org"
_LOOP_TURNS: Final = 50
"""Event-loop turns that carry a started request up to its first blocking wait."""
_RELEASE_TIMEOUT_SEC: Final = 5.0
"""Bound on a held delete, so a broken test fails instead of hanging."""

pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileSecrets:
    """Serve the app's secrets from a temporary directory."""
    backend = FileSecrets(tmp_path / "vault")
    monkeypatch.setattr(app.state, "secrets", backend, raising=False)
    return backend


@pytest.fixture
def no_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the app with secret storage disabled."""
    monkeypatch.setattr(app.state, "secrets", None, raising=False)


@pytest.mark.usefixtures("vault")
async def test_a_writer_lists_the_org_layer_sorted_with_secrets_blank(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    assert (
        await client.put(_path("B_PLAIN"), json={"value": "one"})
    ).status_code == 204
    assert (
        await client.put(_path("A_HIDDEN"), json={"value": _HIDDEN, "secret": True})
    ).status_code == 204
    _as("writer")

    listed = await _listed(client)

    assert [v.name for v in listed.variables] == ["A_HIDDEN", "B_PLAIN"]
    secret, plain = listed.variables
    assert (secret.layer, secret.owner, secret.secret, secret.value) == (
        _ORG,
        "",
        True,
        None,
    )
    assert (plain.secret, plain.value, plain.updated_by) == (
        False,
        "one",
        TEST_USER_EMAIL,
    )
    assert _HIDDEN not in (await client.get(VARIABLES_PATH)).text


async def test_a_viewer_cannot_list(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("viewer")

    assert (await client.get(VARIABLES_PATH)).status_code == 403


@pytest.mark.usefixtures("vault")
async def test_only_an_admin_can_write_or_delete(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("KEEP"), json={"value": "v"})
    _as("writer")

    put = await client.put(_path("KEEP"), json={"value": "other"})
    delete = await client.delete(_path("KEEP"))

    assert (put.status_code, delete.status_code) == (403, 403)
    assert [v.value for v in (await _listed(client)).variables] == ["v"]


async def test_a_plain_variable_round_trips(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    put = await client.put(_path("GREETING"), json={"value": "hello"})
    again = await client.put(_path("GREETING"), json={"value": "café"})
    after_put = await _listed(client)
    deleted = await client.delete(_path("GREETING"))
    after_delete = await _listed(client)

    assert (put.status_code, put.content) == (204, b"")
    assert again.status_code == 204
    assert [v.value for v in after_put.variables] == ["café"]
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert after_delete.variables == []


async def test_a_value_of_exactly_the_limit_is_stored(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    value = "é" * (MAX_VALUE_BYTES // 2)

    put = await client.put(_path("BIG"), json={"value": value})

    assert put.status_code == 204
    assert [v.value for v in (await _listed(client)).variables] == [value]


async def test_a_secret_leaves_its_value_only_in_the_backend(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    vault: FileSecrets,
    tmp_path: Path,
) -> None:
    client, store = pglite_route_client
    _as("admin")

    put = await client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})

    assert (put.status_code, put.content) == (204, b"")
    assert _HIDDEN not in (await client.get(VARIABLES_PATH)).text
    async with store.engine.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM variables")
    (row,) = rows
    assert row["secret"] is True
    assert row["value"] is None
    assert all(_HIDDEN not in str(field) for field in row.values())
    assert vault.get(SecretRef(layer=_ORG, owner="", name="TOKEN")) == _HIDDEN
    root = tmp_path / "vault"
    file = root / "org" / "_" / "TOKEN"
    assert stat.S_IMODE(file.stat().st_mode) == 0o640
    assert [
        stat.S_IMODE(d.stat().st_mode) for d in (root, file.parent.parent, file.parent)
    ] == [0o750] * 3


async def test_a_secret_overwrites_and_replaces_a_plain_value(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    vault: FileSecrets,
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("TOKEN"), json={"value": "plain-first"})

    await client.put(_path("TOKEN"), json={"value": "one", "secret": True})
    await client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})

    (listed,) = (await _listed(client)).variables
    assert (listed.secret, listed.value) == (True, None)
    assert vault.get(SecretRef(layer=_ORG, owner="", name="TOKEN")) == _HIDDEN


async def test_a_plain_put_cannot_turn_a_secret_into_a_plain_value(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    vault: FileSecrets,
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})

    response = await client.put(_path("TOKEN"), json={"value": "exposed"})

    assert response.status_code == 409
    assert _detail(response) == "TOKEN is a secret; delete it first"
    (listed,) = (await _listed(client)).variables
    assert (listed.secret, listed.value) == (True, None)
    assert vault.get(SecretRef(layer=_ORG, owner="", name="TOKEN")) == _HIDDEN


@pytest.mark.usefixtures("no_vault")
async def test_a_secret_without_a_backend_is_refused(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    response = await client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})

    assert response.status_code == 503
    assert _detail(response) == "no secret store is configured"
    assert (await _listed(client)).variables == []


async def test_deleting_a_secret_removes_row_and_file(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    vault: FileSecrets,
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})

    deleted = await client.delete(_path("TOKEN"))

    assert deleted.status_code == 204
    assert (await _listed(client)).variables == []
    with pytest.raises(SecretNotFoundError):
        vault.get(SecretRef(layer=_ORG, owner="", name="TOKEN"))


async def test_deleting_an_absent_variable_is_not_found(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    assert (await client.delete(_path("NOPE"))).status_code == 404


async def test_deleting_a_secret_without_a_backend_keeps_the_row(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    vault: FileSecrets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})
    monkeypatch.setattr(app.state, "secrets", None, raising=False)

    response = await client.delete(_path("TOKEN"))

    assert response.status_code == 503
    assert [v.name for v in (await _listed(client)).variables] == ["TOKEN"]
    assert vault.get(SecretRef(layer=_ORG, owner="", name="TOKEN")) == _HIDDEN


async def test_a_secret_put_racing_a_delete_leaves_the_row_and_its_value_together(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = pglite_route_client
    slow = _SlowDelete(tmp_path / "vault", loop=asyncio.get_running_loop())
    monkeypatch.setattr(app.state, "secrets", slow, raising=False)
    _as("admin")
    await client.put(_path("TOKEN"), json={"value": "old", "secret": True})

    deleting = asyncio.create_task(client.delete(_path("TOKEN")))
    await slow.entered.wait()
    putting = asyncio.create_task(
        client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True}),
    )
    # Let the put run until it blocks behind the delete, then let the delete end.
    for _ in range(_LOOP_TURNS):
        await asyncio.sleep(0)
    slow.release.set()
    responses = await asyncio.gather(deleting, putting)

    assert [r.status_code for r in responses] == [204, 204]
    assert [v.name for v in (await _listed(client)).variables] == ["TOKEN"]
    assert slow.get(SecretRef(layer=_ORG, owner="", name="TOKEN")) == _HIDDEN


@pytest.mark.parametrize("name", ["../x", "a/b", "1A", "", "A" * 129, "a b"])
@pytest.mark.usefixtures("vault")
async def test_a_bad_name_is_a_validation_error(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    tmp_path: Path,
    name: str,
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    put = await client.put(_path(name), json={"value": "v"})
    delete = await client.delete(_path(name))

    assert (put.status_code, delete.status_code) == (422, 422)
    assert not (tmp_path / "vault").exists()
    assert (await _listed(client)).variables == []


@pytest.mark.parametrize("secret", [False, True])
@pytest.mark.parametrize(
    "value",
    ["x" * (MAX_VALUE_BYTES + 1), "é" * (MAX_VALUE_BYTES // 2 + 1), "", "a\x00b"],
    ids=["too-long", "too-many-bytes", "empty", "nul"],
)
@pytest.mark.usefixtures("vault")
async def test_a_bad_value_is_a_validation_error(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    tmp_path: Path,
    value: str,
    secret: bool,
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    response = await client.put(_path("V"), json={"value": value, "secret": secret})

    assert response.status_code == 422
    assert not (tmp_path / "vault").exists()
    assert (await _listed(client)).variables == []


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (f'"{_HIDDEN}"', "application/json"),
        (f'{{"value": ["{_HIDDEN}"]}}', "application/json"),
        (f'{{"value": "{_HIDDEN}", "secret": "{_HIDDEN}"}}', "application/json"),
        (f'{{"value": "{_HIDDEN}", "extra": 1}}', "application/json"),
        (f'{{"value": "{_HIDDEN}", ', "application/json"),
        (f'{{"value": "{_HIDDEN}"}}', "text/plain"),
        (_HIDDEN, "application/json"),
        (f'{{"{_HIDDEN}": 1, "value": "x"}}', "application/json"),
    ],
    ids=["string", "list", "bad-flag", "extra", "truncated", "text", "bare", "key"],
)
async def test_a_malformed_body_never_echoes_the_value(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    content: str,
    content_type: str,
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    response = await client.put(
        _path("TOKEN"),
        content=content,
        headers={"content-type": content_type},
    )

    assert response.status_code == 422
    assert _HIDDEN not in response.text
    assert from_plain(_detail(response), list[dict[str, object]])


@pytest.mark.usefixtures("vault")
async def test_a_secret_appears_in_no_log_record(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    with caplog.at_level(logging.DEBUG):
        await client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": True})
        await client.put(_path("TOKEN"), json={"value": _HIDDEN})
        await client.put(_path("TOKEN"), json={"value": _HIDDEN, "secret": 3})
        await client.put(_path("TOKEN"), content=_HIDDEN)
        await client.get(VARIABLES_PATH)
        await client.delete(_path("TOKEN"))

    assert caplog.records
    assert _HIDDEN not in caplog.text
    assert all(_HIDDEN not in repr(record.__dict__) for record in caplog.records)


class _SlowDelete(FileSecrets):
    """A backend whose delete waits for ``release``, so a request can arrive meanwhile."""

    def __init__(self, root: Path, *, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(root)
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self._loop = loop

    @override
    def delete(self, ref: SecretRef) -> None:
        # Runs on a worker thread, where an ``asyncio.Event`` may not be set directly.
        self._loop.call_soon_threadsafe(self.entered.set)
        assert self.release.wait(timeout=_RELEASE_TIMEOUT_SEC)
        super().delete(ref)


def _path(name: str) -> str:
    return VARIABLE_PATH.format(name=quote(name, safe=""))


def _as(role: Role) -> None:
    install_identity(make_test_identity(role=role, api_key_id=None))


def _detail(response: httpx2.Response) -> object:
    return from_plain(response.json(), dict[str, object])["detail"]


async def _listed(client: httpx2.AsyncClient) -> VariableList:
    response = await client.get(VARIABLES_PATH)
    assert response.status_code == 200
    return VariableList.model_validate_json(response.text)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
