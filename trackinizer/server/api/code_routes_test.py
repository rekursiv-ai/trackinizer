"""Code-store routes, end to end over PGlite: who files code, and who reads it.

The whole app answers each request, so the role checks and the domain-error handlers
are on the path. The store's own rules are tested in ``store/code_store_test.py``;
these pin what each route adds: the owner rule, the 404 that hides a private
snapshot from every other caller, and each refusal's status and code.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from unittest.mock import AsyncMock
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import hashlib

from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from treekle import from_plain

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.access_policy import Principal
from trackinizer.server.api import code_routes
from trackinizer.server.api.conftest import install_identity, make_test_identity
from trackinizer.server.api.session_access import session_reader
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.store.code_store import SnapshotOwnerError
from trackinizer.wire.audience import AUDIENCES, OPEN, PRIVATE
from trackinizer.wire.wire_code import (
    CODE_BLOBS_PATH,
    CODE_DIFF_PATH,
    CODE_FILE_PATH,
    CODE_MISSING_PATH,
    CODE_SNAPSHOT_PATH,
    CodeBlobsPut,
    CodeDiff,
    CodeFile,
    CodeMissingRequest,
    CodeSnapshot,
    CodeSnapshotHead,
    CodeSnapshotPut,
)


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


pytestmark = pytest.mark.asyncio(loop_scope="session")

_ADA: Final = "ada@example.com"
_BOB: Final = "bob@example.com"
_CAROL: Final = "carol@example.com"


@pytest.mark.db_pglite
async def test_a_writer_files_code_that_a_viewer_reads_without_its_owner(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    old, new = await _record(store), await _record(store)
    await _as(store, _ADA)

    asked = await client.post(
        CODE_MISSING_PATH,
        json={"owner": _ADA, "sha256": [_sha("1\n"), _sha("2\n")]},
    )
    sent = await client.post(
        CODE_BLOBS_PATH,
        json={"owner": _ADA, "blobs": [_blob("1\n"), _blob("2\n")]},
    )
    filed = await client.put(_snapshot(old), json=_put({"a.py": "1\n"}))
    await client.put(_snapshot(new), json=_put({"a.py": "2\n"}, parent=old))
    await _as(store, _BOB, role="viewer")
    read = await client.get(_snapshot(new))
    text = await client.get(_file(new), params={"path": "a.py"})
    diff = await client.get(CODE_DIFF_PATH, params={"from": str(old), "to": str(new)})

    assert asked.json() == {"missing": [_sha("1\n"), _sha("2\n")]}
    assert sent.json() == {"stored": 2}
    assert CodeSnapshotHead.model_validate_json(filed.text).file_count == 1
    snapshot = CodeSnapshot.model_validate_json(read.text)
    assert (snapshot.parent_record_id, [f.path for f in snapshot.files]) == (
        old,
        ["a.py"],
    )
    assert CodeFile.model_validate_json(text.text).text == "2\n"
    assert [f.change for f in CodeDiff.model_validate_json(diff.text).files] == [
        "modified",
    ]
    for response in (filed, read, text, diff):
        assert _ADA not in response.text


@pytest.mark.db_pglite
async def test_a_viewer_files_no_code(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    record = await _record(store)
    await _as(store, _ADA, role="viewer")

    answers = [
        await client.post(CODE_MISSING_PATH, json={"owner": _ADA, "sha256": []}),
        await client.post(CODE_BLOBS_PATH, json={"owner": _ADA, "blobs": []}),
        await client.put(_snapshot(record), json=_put({})),
    ]

    assert [a.status_code for a in answers] == [403, 403, 403]


@pytest.mark.db_pglite
async def test_code_is_filed_under_the_callers_own_email_unless_staff(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    record = await _record(store)
    await _as(store, _ADA)

    refused = [
        await client.post(CODE_MISSING_PATH, json={"owner": _BOB, "sha256": []}),
        await client.post(CODE_BLOBS_PATH, json={"owner": _BOB, "blobs": [_blob("")]}),
        await client.put(_snapshot(record), json=_put({}, owner=_BOB)),
    ]
    own = await client.post(
        CODE_MISSING_PATH,
        json={"owner": _ADA.upper(), "sha256": [_sha("")]},
    )
    await _as(store, _CAROL, staff=True)
    stored = await client.post(
        CODE_BLOBS_PATH,
        json={"owner": _BOB, "blobs": [_blob("")]},
    )
    filed = await client.put(_snapshot(record), json=_put({"a.py": ""}, owner=_BOB))

    assert [r.status_code for r in refused] == [403, 403, 403]
    assert (own.status_code, own.json()) == (200, {"missing": [_sha("")]})
    assert (stored.status_code, stored.json()) == (200, {"stored": 1})
    assert filed.status_code == 200


@pytest.mark.db_pglite
async def test_only_its_owner_or_staff_replaces_a_records_snapshot(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    record = await _record(store)
    await _as(store, _ADA)
    await _upload(client, _ADA, "ada\n")
    await client.put(_snapshot(record), json=_put({"a.py": "ada\n"}))
    await _as(store, _BOB)
    await _upload(client, _BOB, "bob\n")

    taken = await client.put(
        _snapshot(record),
        json=_put({"a.py": "bob\n"}, owner=_BOB),
    )
    await _as(store, _CAROL, staff=True)
    replaced = await client.put(
        _snapshot(record),
        json=_put({"a.py": "bob\n"}, owner=_BOB),
    )

    assert (taken.status_code, replaced.status_code) == (403, 200)


@pytest.mark.db_pglite
async def test_a_private_snapshot_is_404_to_everyone_but_its_owner(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    record, other = await _record(store), await _record(store)
    await _as(store, _ADA)
    await _upload(client, _ADA, "secret\n")
    await client.put(_snapshot(record), json=_put({"a.py": "secret\n"}, private=True))
    await client.put(_snapshot(other), json=_put({"a.py": "secret\n"}))
    diff = {"from": str(other), "to": str(record)}

    owner_reads = await client.get(_snapshot(record))
    answers: list[int] = []
    overseers: tuple[tuple[str, Role, bool], ...] = (
        (_BOB, "admin", False),
        (_CAROL, "writer", True),
    )
    for email, role, staff in overseers:
        await _as(store, email, role=role, staff=staff)
        answers += [
            (await client.get(_snapshot(record))).status_code,
            (await client.get(_file(record), params={"path": "a.py"})).status_code,
            (await client.get(CODE_DIFF_PATH, params=diff)).status_code,
        ]
    unknown = await client.get(_snapshot(uuid4()))

    assert owner_reads.status_code == 200
    assert answers == [404] * 6
    assert unknown.status_code == 404


@pytest.mark.db_pglite
async def test_each_refusal_answers_its_status_and_code(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    record, parent = await _record(store), await _record(store)
    await _as(store, _ADA)
    await _upload(client, _ADA, "held\n")

    mismatch = await client.post(
        CODE_BLOBS_PATH,
        json={"owner": _ADA, "blobs": [{"sha256": _sha("x"), "text": "y"}]},
    )
    unstored = await client.put(_snapshot(record), json=_put({"a.py": "never\n"}))
    orphan = await client.put(
        _snapshot(record),
        json=_put({"a.py": "held\n"}, parent=parent),
    )
    unknown = await client.put(_snapshot(uuid4()), json=_put({"a.py": "held\n"}))
    climbs = await client.put(_snapshot(record), json=_put({"../a.py": "held\n"}))
    wide = await client.get(
        CODE_DIFF_PATH,
        params={"from": str(record), "to": str(parent), "context": "51"},
    )

    assert mismatch.status_code == 422
    assert (unstored.status_code, _code(unstored)) == (409, "code_blobs_missing")
    assert (orphan.status_code, _code(orphan)) == (409, "code_parent_missing")
    assert unknown.status_code == 404
    assert climbs.status_code == 422
    assert wide.status_code == 422


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _blob(text: str) -> dict[str, str]:
    return {"sha256": _sha(text), "text": text}


def _put(
    texts: dict[str, str],
    *,
    owner: str = _ADA,
    private: bool = False,
    parent: UUID | None = None,
) -> dict[str, object]:
    return {
        "owner": owner,
        "private": private,
        "parent_record_id": None if parent is None else str(parent),
        "files": {path: _sha(text) for path, text in texts.items()},
    }


def _snapshot(record: UUID) -> str:
    return CODE_SNAPSHOT_PATH.format(record_id=record)


def _file(record: UUID) -> str:
    return CODE_FILE_PATH.format(record_id=record)


def _code(response: httpx2.Response) -> object:
    return from_plain(response.json(), dict[str, object])["code"]


async def _as(
    store: Store,
    email: str,
    *,
    role: Role = "writer",
    staff: bool = False,
) -> None:
    """Answer each request as ``email``, an active user, holding ``staff`` if asked."""
    user_id = uuid5(NAMESPACE_DNS, email)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, $2, $3, 'active') ON CONFLICT (id) DO NOTHING",
            user_id,
            email,
            role,
        )
    install_identity(
        AuthIdentity(
            user_id=user_id,
            api_key_id=None,
            email=email,
            role=role,
            read_audiences=AUDIENCES if staff else frozenset({OPEN, PRIVATE}),
        ),
    )


async def _upload(client: httpx2.AsyncClient, owner: str, text: str) -> None:
    response = await client.post(
        CODE_BLOBS_PATH,
        json={"owner": owner, "blobs": [_blob(text)]},
    )
    assert response.status_code == 200


async def _record(store: Store) -> UUID:
    """Insert one Experiment; return its id."""
    record = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'Experiment', nextval('seq_experiment'), 'active', $2, 'run')",
            record,
            _ADA,
        )
    return record


async def test_blob_routes_forward_the_normalized_owner_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = make_store()
    app = FastAPI()
    app.state.store = store
    request = Request({"type": "http", "app": app})
    identity = make_test_identity(email=_ADA.upper())
    missing = AsyncMock(return_value=[_sha("x")])
    put = AsyncMock(return_value=1)
    monkeypatch.setattr(code_routes, "missing_blobs", missing)
    monkeypatch.setattr(code_routes, "put_blobs", put)
    body = CodeMissingRequest(owner=_ADA.upper(), sha256=[_sha("x")])
    blobs = CodeBlobsPut.model_validate({"owner": _ADA, "blobs": [_blob("x")]})

    asked = await code_routes.code_missing_route(body, request, identity)
    stored = await code_routes.code_put_blobs_route(blobs, request, identity)

    assert asked.missing == [_sha("x")]
    assert stored.stored == 1
    missing.assert_awaited_once_with(engine, owner=_ADA, hashes=body.sha256)
    put.assert_awaited_once_with(engine, owner=_ADA, blobs=blobs.blobs)
    with pytest.raises(HTTPException) as refused:
        await code_routes.code_missing_route(
            CodeMissingRequest(owner=_BOB, sha256=[]),
            request,
            identity,
        )
    assert refused.value.status_code == 403
    assert missing.await_count == 1


@pytest.mark.parametrize(("staff", "parent"), [(False, None), (True, UUID(int=2))])
async def test_snapshot_write_checks_both_records_and_staff_replacement(
    monkeypatch: pytest.MonkeyPatch,
    staff: bool,
    parent: UUID | None,
) -> None:
    store, engine = make_store()
    app = FastAPI()
    app.state.store = store
    request = Request({"type": "http", "app": app})
    identity = replace(
        make_test_identity(email=_ADA),
        read_audiences=AUDIENCES if staff else frozenset({OPEN, PRIVATE}),
    )
    record = UUID(int=1)
    body = CodeSnapshotPut(
        owner=_BOB if staff else _ADA,
        private=False,
        parent_record_id=parent,
        files={},
    )
    head = _unit_snapshot(record)
    readable = AsyncMock()
    put = AsyncMock(return_value=head)
    monkeypatch.setattr(code_routes, "require_readable", readable)
    monkeypatch.setattr(code_routes, "put_snapshot", put)

    assert (
        await code_routes.code_put_snapshot_route(record, body, request, identity)
        is head
    )
    readable.assert_awaited_once_with(
        request,
        identity,
        [record] if parent is None else [record, parent],
    )
    put.assert_awaited_once_with(
        engine,
        record_id=record,
        body=body,
        created_by=identity.user_id,
        replace_others=staff,
    )
    put.side_effect = SnapshotOwnerError("Owned by another caller")
    with pytest.raises(HTTPException) as refused:
        await code_routes.code_put_snapshot_route(record, body, request, identity)
    assert (refused.value.status_code, refused.value.detail) == (
        403,
        "Owned by another caller",
    )


@pytest.mark.parametrize("found", [False, True])
async def test_code_reads_forward_access_context_and_hide_missing_resources(
    monkeypatch: pytest.MonkeyPatch,
    found: bool,
) -> None:
    store, engine = make_store()
    app = FastAPI()
    app.state.store = store
    request = Request({"type": "http", "app": app})
    identity = make_test_identity(email=_ADA)
    principal = Principal(email=_ADA, role="writer")
    request.state.access_principal = principal
    record, other = UUID(int=1), UUID(int=2)
    snapshot = _unit_snapshot(record)
    file = CodeFile(path="a.py", sha256=_sha("x"), text="x")
    diff = CodeDiff(files=[], more=True)
    read_snapshot = AsyncMock(return_value=snapshot if found else None)
    read_file = AsyncMock(return_value=file if found else None)
    read_diff = AsyncMock(return_value=diff if found else None)
    monkeypatch.setattr(code_routes, "read_snapshot", read_snapshot)
    monkeypatch.setattr(code_routes, "read_file", read_file)
    monkeypatch.setattr(code_routes, "diff_snapshots", read_diff)
    if found:
        assert (
            await code_routes.code_snapshot_route(record, request, identity) is snapshot
        )
        assert (
            await code_routes.code_file_route(record, "a.py", request, identity) is file
        )
        assert (
            await code_routes.code_diff_route(record, other, request, identity, 2)
            is diff
        )
    else:
        with pytest.raises(HTTPException) as absent:
            await code_routes.code_snapshot_route(record, request, identity)
        assert (absent.value.status_code, absent.value.detail) == (
            404,
            "No code snapshot for this record.",
        )
        with pytest.raises(HTTPException) as absent:
            await code_routes.code_file_route(record, "a.py", request, identity)
        assert (absent.value.status_code, absent.value.detail) == (
            404,
            "No such file in a code snapshot.",
        )
        with pytest.raises(HTTPException) as absent:
            await code_routes.code_diff_route(record, other, request, identity, 2)
        assert (absent.value.status_code, absent.value.detail) == (
            404,
            "No code snapshot for a record.",
        )
    context = {
        "reader": session_reader(request, identity),
        "policy": store.access_policy,
        "principal": principal,
    }
    read_snapshot.assert_awaited_once_with(engine, record_id=record, **context)
    read_file.assert_awaited_once_with(engine, record_id=record, path="a.py", **context)
    read_diff.assert_awaited_once_with(
        engine,
        from_id=record,
        to_id=other,
        context=2,
        **context,
    )


def _unit_snapshot(record: UUID) -> CodeSnapshot:
    return CodeSnapshot(
        record_id=record,
        parent_record_id=None,
        private=False,
        file_count=0,
        total_bytes=0,
        partial="",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        files=[],
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
