"""Code-store routes, end to end over PGlite: who files code, and who reads it.

The whole app answers each request, so the role checks and the domain-error handlers
are on the path. The store's own rules are tested in ``store/code_store_test.py``;
these pin what each route adds: the owner rule, the 404 that hides a private
snapshot from every other caller, and each refusal's status and code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import hashlib

from treekle import from_plain

import pytest

from trackinizer.server.api.conftest import install_identity
from trackinizer.server.auth import AuthIdentity
from trackinizer.wire.audience import AUDIENCES, OPEN, PRIVATE
from trackinizer.wire.wire_code import (
    CODE_BLOBS_PATH,
    CODE_DIFF_PATH,
    CODE_FILE_PATH,
    CODE_MISSING_PATH,
    CODE_SNAPSHOT_PATH,
    CodeDiff,
    CodeFile,
    CodeSnapshot,
    CodeSnapshotHead,
)


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]

_ADA: Final = "ada@example.com"
_BOB: Final = "bob@example.com"
_CAROL: Final = "carol@example.com"


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


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
