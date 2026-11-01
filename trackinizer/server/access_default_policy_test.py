"""The access-policy seam: today's rules by default, and every call it makes.

With no addon installed and no access setting, each write the policy guards
answers exactly as it did before the seam existed. A recording policy then shows
that each seam point is called once, inside the right request, about the right
resource.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast

import uuid

from treekle import from_plain, json_loads

import httpx2
import pytest
import pytest_asyncio

from trackinizer.addons.deployment import Deployment
from trackinizer.server.access_policy import (
    AccessDeniedError,
    DefaultPolicy,
    Principal,
    Resource,
)
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    clear_identity_override,
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.api.session_access import request_principal
from trackinizer.server.api.session_test_support import (
    OTHER_EMAIL,
    OTHER_USER_ID,
    seed_accounts,
)
from trackinizer.server.auth import (
    BOOTSTRAP_ADMIN_ENV,
    BOOTSTRAP_TOKEN_FILE_ENV,
    bootstrap_admin,
    seed_no_auth_user,
)
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.types.errors import ConflictError
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import Request

    from trackinizer.lib.postgres import Conn, PGliteEngine
    from trackinizer.server.access_policy import (
        AccessPolicy,
        Action,
        ResourceKind,
    )
    from trackinizer.server.auth import Role


type _Client = tuple[httpx2.AsyncClient, Store]

pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]

ADA: Final = TEST_USER_EMAIL
BOB: Final = OTHER_EMAIL
VIEWER: Final = "viewer@example.com"
VIEWER_ID: Final = uuid.UUID("88888888-8888-8888-8888-888888888888")
VIEWER_KEY: Final = uuid.UUID("99999999-9999-9999-9999-999999999999")
ROOT: Final = "root@example.com"
ROOT_ID: Final = uuid.UUID("77777777-7777-7777-7777-777777777777")
GROUPS: Final = frozenset({"g1"})


@dataclass(slots=True, kw_only=True)
class _RecordingPolicy:
    """Allows everything a default policy allows, and records each call."""

    calls: list[tuple[str, object]] = field(default_factory=list)
    deny_writes: bool = False
    refuse_states: bool = False
    hide_reads: bool = False

    async def groups(self, conn: Conn, principal: Principal) -> frozenset[str]:
        del conn
        self.calls.append(("groups", principal.email))
        return GROUPS

    def read_filter(
        self,
        kind: ResourceKind,
        alias: str,
        *,
        params: list[object],
        principal: Principal,
    ) -> str:
        del kind, alias, params, principal
        self.calls.append(("read_filter", None))
        return "FALSE" if self.hide_reads else "TRUE"

    async def check(
        self,
        conn: Conn,
        *,
        principal: Principal,
        resource: Resource,
        action: Action,
    ) -> None:
        del conn
        self.calls.append(("check", (principal, resource, action)))
        if self.deny_writes:
            raise AccessDeniedError("The row's write grant excludes the caller.")

    async def on_create(
        self,
        conn: Conn,
        *,
        principal: Principal | None,
        resource: Resource,
    ) -> None:
        del conn
        self.calls.append(("on_create", (principal, resource)))

    async def prepare_account_change(self, conn: Conn, *, resource: Resource) -> None:
        del conn
        self.calls.append(("prepare_account_change", resource))

    async def validate(self, conn: Conn, *, resource: Resource) -> None:
        del conn
        self.calls.append(("validate", resource))
        if self.refuse_states:
            raise ConflictError("The new account may not hold the row.")

    async def on_user(self, conn: Conn, *, email: str, role: Role) -> None:
        del conn
        self.calls.append(("on_user", (email, role)))

    def of(self, name: str) -> list[object]:
        """Return the recorded arguments of every call to ``name``, in order."""
        return [args for called, args in self.calls if called == name]


@pytest_asyncio.fixture(loop_scope="session")
async def served(
    pglite_db: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Client]:
    """Serve the whole app over a fresh store built as a site with no addon is."""
    async for client in _serve(
        pglite_db,
        monkeypatch,
        policy=Deployment.Config().make().access_policy,
    ):
        yield client


@pytest_asyncio.fixture(loop_scope="session")
async def recorded(
    pglite_db: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx2.AsyncClient, Store, _RecordingPolicy]]:
    """Serve the whole app over a fresh store whose policy records each call."""
    policy = _RecordingPolicy()
    async for client, store in _serve(pglite_db, monkeypatch, policy=policy):
        yield client, store, policy


# -- Parity: no addon, no env, every case answers as it did before the seam. ---


async def test_a_site_with_no_addon_asks_the_default_policy(served: _Client) -> None:
    _, store = served
    assert store.access_policy == DefaultPolicy()


async def test_a_writer_edits_another_writers_row(served: _Client) -> None:
    client, _ = served
    _as(ADA)
    row = await _issue(client, "Ada's row")

    _as(BOB, user_id=OTHER_USER_ID)
    edited = await client.put(
        f"/api/inquiries/{row}/description",
        json={"value": "Bob was here."},
    )

    assert edited.status_code == 200, edited.text


async def test_an_admin_edits_a_viewers_session(served: _Client) -> None:
    client, _ = served
    session = await _viewer_session(client)

    _as(ROOT, user_id=ROOT_ID, role="admin")
    edited = await client.put(
        f"/api/inquiries/{session}/title",
        json={"value": "Renamed by an admin"},
    )

    assert edited.status_code == 200, edited.text


async def test_a_viewer_starts_a_session(served: _Client) -> None:
    client, _ = served
    _as(VIEWER, user_id=VIEWER_ID, api_key_id=VIEWER_KEY, role="viewer")

    started = await client.post("/api/sessions/start", json={"cli": "codex"})

    assert started.status_code == 201, started.text


async def test_a_writer_reassigns_a_rows_account_to_a_viewer(served: _Client) -> None:
    client, _ = served
    _as(ADA)
    row = await _issue(client, "Ada's row")

    moved = await client.put(f"/api/inquiries/{row}/account", json={"value": VIEWER})
    read = await client.get(f"/api/inquiries/{row}")

    assert moved.status_code == 200, moved.text
    assert json_loads(read.content, dict[str, object])["account"] == VIEWER


# -- Seam calls: each guarded point asks the policy once, about the right row. ---


async def test_each_guarded_write_asks_the_policy_once_about_its_row(
    recorded: tuple[httpx2.AsyncClient, Store, _RecordingPolicy],
) -> None:
    client, _, policy = recorded
    _as(ADA)
    row = await _issue(client, "Ada's row")
    ada = Principal(email=ADA, role="writer", groups=GROUPS)
    resource = Resource(kind="inquiry", id=str(row), account=ADA)

    answers = [
        await client.put(f"/api/inquiries/{row}/description", json={"value": "d"}),
        await client.patch(
            f"/api/inquiries/{row}/labels",
            json={"op": "add", "value": "x"},
        ),
        await client.request("DELETE", f"/api/inquiries/{row}/description", json={}),
    ]
    _as(ROOT, user_id=ROOT_ID, role="admin")
    answers.append(
        await client.put(
            f"/api/admin/inquiries/{row}/lock",
            json={"locked": False},
        ),
    )
    _as(ADA)
    answers.append(await client.request("DELETE", f"/api/inquiries/{row}", json={}))

    root = Principal(email=ROOT, role="admin", groups=GROUPS)
    assert [answer.status_code for answer in answers] == [200] * 5, [
        answer.text for answer in answers
    ]
    assert policy.of("on_create") == [(ada, resource)]
    assert policy.of("check") == [
        (ada, resource, "write"),
        (ada, resource, "write"),
        (ada, resource, "write"),
        (root, resource, "write"),
        (ada, resource, "write"),
    ]
    assert policy.of("groups") == [ADA, ADA, ADA, ADA, ROOT, ADA]
    assert policy.of("validate") == []
    # Each write's gate asks whether its row is readable, and a writer's lock check
    # asks again so that it names only a row the writer may read.
    assert policy.of("read_filter") == [None] * 9


async def test_a_refusal_is_a_403_and_writes_nothing(
    recorded: tuple[httpx2.AsyncClient, Store, _RecordingPolicy],
) -> None:
    client, _, policy = recorded
    _as(ADA)
    row = await _issue(client, "Ada's row")
    policy.deny_writes = True

    refused = await client.put(
        f"/api/inquiries/{row}/description",
        json={"value": "denied"},
    )
    read = await client.get(f"/api/inquiries/{row}")

    assert refused.status_code == 403, refused.text
    assert json_loads(refused.content, dict[str, str]) == {
        "detail": "The row's write grant excludes the caller.",
    }
    assert json_loads(read.content, dict[str, object])["description"] is None


async def test_a_hidden_row_reads_by_seq_as_an_absent_one(
    recorded: tuple[httpx2.AsyncClient, Store, _RecordingPolicy],
) -> None:
    client, store, policy = recorded
    _as(ADA)
    row = await store.get_inquiry(await _issue(client, "Ada's row"))
    assert row is not None
    policy.hide_reads = True

    hidden = await client.get(f"/api/inquiries/Issue/{row.seq}")
    absent = await client.get(f"/api/inquiries/Issue/{row.seq + 1}")

    assert (hidden.status_code, absent.status_code) == (404, 404), hidden.text
    assert json_loads(hidden.content, dict[str, str]) == {
        "detail": f"Issue#{row.seq} not found",
    }
    assert json_loads(absent.content, dict[str, str]) == {
        "detail": f"Issue#{row.seq + 1} not found",
    }


async def test_an_account_move_is_validated_in_its_transaction(
    recorded: tuple[httpx2.AsyncClient, Store, _RecordingPolicy],
) -> None:
    client, _, policy = recorded
    _as(ADA)
    row = await _issue(client, "Ada's row")

    policy.refuse_states = True
    refused = await client.put(
        f"/api/inquiries/{row}/account",
        json={"value": VIEWER},
    )
    kept = await client.get(f"/api/inquiries/{row}")
    policy.refuse_states = False
    moved = await client.put(f"/api/inquiries/{row}/account", json={"value": BOB})
    read = await client.get(f"/api/inquiries/{row}")

    assert (refused.status_code, moved.status_code) == (409, 200), refused.text
    expected = [
        Resource(kind="inquiry", id=str(row), account=VIEWER),
        Resource(kind="inquiry", id=str(row), account=BOB),
    ]
    assert policy.of("prepare_account_change") == expected
    assert policy.of("validate") == expected
    assert json_loads(kept.content, dict[str, object])["account"] == ADA
    assert json_loads(read.content, dict[str, object])["account"] == BOB


async def test_every_new_row_is_created_with_the_policy(
    recorded: tuple[httpx2.AsyncClient, Store, _RecordingPolicy],
) -> None:
    """A route's create names its caller; the system's names no one."""
    client, store, policy = recorded
    _as(ADA)
    batch = await client.post(
        "/api/inquiries/batch",
        json={
            "items": [
                {"kind": "Issue", "title": title, "idempotency_key": str(uuid.uuid4())}
                for title in ("one", "two")
            ],
        },
    )
    _as(VIEWER, user_id=VIEWER_ID, api_key_id=VIEWER_KEY, role="viewer")
    started = await client.post("/api/sessions/start", json={"cli": "codex"})
    system = await store.submit_issue(SubmitIssue(title="system", account=ADA))

    ids = json_loads(batch.content, dict[str, list[str]])["ids"]
    session = _created_id(started)
    ada = Principal(email=ADA, role="writer", groups=GROUPS)
    viewer = Principal(
        email=VIEWER,
        role="viewer",
        groups=GROUPS,
        api_key_id=VIEWER_KEY,
    )
    assert policy.of("on_create") == [
        (ada, Resource(kind="inquiry", id=ids[0], account=ADA)),
        (ada, Resource(kind="inquiry", id=ids[1], account=ADA)),
        (viewer, Resource(kind="inquiry", id=session, account=VIEWER)),
        (None, Resource(kind="inquiry", id=str(system), account=ADA)),
    ]


async def test_every_users_insert_and_role_change_tells_the_policy(
    recorded: tuple[httpx2.AsyncClient, Store, _RecordingPolicy],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, store, policy = recorded
    _as(ROOT, user_id=ROOT_ID, role="admin")
    promoted = await client.put(
        f"/api/admin/users/{VIEWER_ID}/role",
        json={"role": "writer"},
    )
    created = await client.post(
        "/api/admin/service-accounts",
        json={"name": "indexer", "role": "viewer"},
    )
    async with store.engine.acquire() as conn:
        await seed_no_auth_user(conn, policy=policy)
        await seed_no_auth_user(conn, policy=policy)
        # The bootstrap seeds its admin only into a database with no user.
        await conn.execute("TRUNCATE users CASCADE")
    monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "first@example.com")
    monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(tmp_path / "token"))
    await store.bootstrap()
    async with store.engine.acquire() as conn:
        await bootstrap_admin(conn, policy=policy)

    assert (promoted.status_code, created.status_code) == (200, 201), created.text
    assert policy.of("on_user") == [
        (VIEWER, "writer"),
        ("indexer@service.invalid", "viewer"),
        ("no-auth@localhost", "admin"),
        ("first@example.com", "admin"),
    ]


async def test_a_request_asks_the_policy_for_its_groups_once(
    recorded: tuple[httpx2.AsyncClient, Store, _RecordingPolicy],
) -> None:
    _, store, policy = recorded
    request = cast(
        "Request",
        SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(store=store)),
            state=SimpleNamespace(),
        ),
    )
    identity = make_test_identity(email="Ada@Example.com")

    first = await request_principal(request, identity)
    second = await request_principal(request, identity)

    assert first is second
    assert first == Principal(
        email="ada@example.com",
        role="writer",
        groups=GROUPS,
        api_key_id=TEST_API_KEY_ID,
    )
    assert policy.of("groups") == ["ada@example.com"]


async def _serve(
    engine: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: AccessPolicy,
) -> AsyncIterator[_Client]:
    """Serve the app over a fresh store asking ``policy``, with everyone seeded."""
    store = Store(engine, embed=StubEmbedder(), access_policy=policy)
    await _seed_people(store)
    for name, value in (
        ("engine", engine),
        ("store", store),
        ("inbound", InboundQueue()),
    ):
        patch_state(monkeypatch, name, value)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client, store
    finally:
        clear_identity_override()


async def _seed_people(store: Store) -> None:
    """Seed Ada, Bob and the assistant as writers, a viewer with a key, and an admin."""
    await seed_accounts(store)
    async with store.engine.acquire() as conn:
        for user_id, email, role in (
            (VIEWER_ID, VIEWER, "viewer"),
            (ROOT_ID, ROOT, "admin"),
        ):
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, 'Test', $3, 'active')",
                user_id,
                email,
                role,
            )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', 'trax_viewer', 'viewer')",
            VIEWER_KEY,
            VIEWER_ID,
        )


def _as(
    email: str,
    *,
    user_id: uuid.UUID = TEST_USER_ID,
    api_key_id: uuid.UUID | None = None,
    role: Role = "writer",
) -> None:
    """Act as ``email``: a signed-in browser, or its key when one is named."""
    install_identity(
        make_test_identity(
            user_id=user_id,
            api_key_id=api_key_id,
            email=email,
            role=role,
        ),
    )


async def _issue(client: httpx2.AsyncClient, title: str) -> uuid.UUID:
    """File an Issue as the installed caller; return its id."""
    made = await client.post("/api/inquiries/issue", json={"title": title})
    assert made.status_code == 201, made.text
    return uuid.UUID(json_loads(made.content, dict[str, str])["id"])


async def _viewer_session(client: httpx2.AsyncClient) -> str:
    """Open a session with the viewer's own key; return its id."""
    _as(VIEWER, user_id=VIEWER_ID, api_key_id=VIEWER_KEY, role="viewer")
    started = await client.post("/api/sessions/start", json={"cli": "codex"})
    assert started.status_code == 201, started.text
    return _created_id(started)


def _created_id(response: httpx2.Response) -> str:
    """Read the ``id`` a create's response names."""
    return from_plain(json_loads(response.content, dict[str, object])["id"], str)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
