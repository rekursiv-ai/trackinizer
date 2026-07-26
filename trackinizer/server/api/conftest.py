"""Shared fixtures for FastAPI route tests.

Phase 2 of ``docs/design.md (Auth)`` gated every route behind
:func:`require_role`. To keep the existing per-route behavioural tests
focused on what they actually test (route plumbing, store wiring, body
validation), :func:`route_client` installs a static :class:`AuthIdentity`
override on the dependency surface so every request resolves as a
writer-role principal. Tests that want to exercise the 401 / 403 paths
explicitly pop the override (see ``api/auth_routes_test.py`` for the
pattern).
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import DEFAULT

import uuid

from fastapi.testclient import TestClient

import pytest

from trackinizer.conftest import FakeEngine, make_store
from trackinizer.server.api.app import app
from trackinizer.server.auth import AuthIdentity, Role, current_user
from trackinizer.server.store.core import Store


# Stable test principal injected by ``route_client``. Pinned UUIDs +
# email so assertions about ``change_log.api_key_id`` / ``actor``
# defaulting can refer to known values.
TEST_USER_ID: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TEST_API_KEY_ID: uuid.UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")
TEST_USER_EMAIL: str = (
    "test-user@example.com"  # config-globals: ignore -- test fixture constant
)


def make_test_identity(
    *,
    user_id: uuid.UUID = TEST_USER_ID,
    api_key_id: uuid.UUID | None = TEST_API_KEY_ID,
    email: str = TEST_USER_EMAIL,
    role: Role = "writer",
) -> AuthIdentity:
    """Mint a deterministic :class:`AuthIdentity` for route tests."""
    return AuthIdentity(user_id=user_id, api_key_id=api_key_id, email=email, role=role)


def install_identity(identity: AuthIdentity) -> None:
    """Wire ``identity`` as the result of :func:`current_user` for tests.

    Every role guard (``require_role(...)``) ultimately depends on
    :func:`current_user`, so this single override flows through the whole
    role-enforcement chain. Callers that want to test a 401 path pop the
    override; tests that want a 403 path install a viewer identity here
    before hitting a writer route.
    """

    async def _override() -> AuthIdentity:
        return identity

    app.dependency_overrides[current_user] = _override


def clear_identity_override() -> None:
    """Remove the :func:`current_user` override installed by tests."""
    app.dependency_overrides.pop(current_user, None)


def answer_account_active(engine: FakeEngine) -> None:
    """Make ``auth.assert_account_active`` pass on the fixture's mock conn.

    Route tests inject a static writer identity instead of seeding the
    ``users`` table, so the submit / ``set_account`` account-active gate
    would otherwise 422. Answer just that one ``users`` probe truthy while
    leaving every other ``fetchval`` on whatever the test configures via the
    mock's ``return_value`` / ``side_effect`` -- so a test that sets
    ``conn.fetchval.return_value = "Issue"`` still drives ``lookup_kind``.
    """
    conn = engine.conn

    def fetchval(sql: str, *args: object) -> object:
        del args
        # Answer only the account-active probe; ``DEFAULT`` tells the mock to
        # fall back to its own ``return_value``, so a test setting
        # ``conn.fetchval.return_value = "Issue"`` still drives ``lookup_kind``.
        if "FROM users" in sql and "status = 'active'" in sql:
            return 1
        return DEFAULT

    conn.fetchval.side_effect = fetchval


@pytest.fixture
def route_client() -> Iterator[tuple[TestClient, Store, FakeEngine]]:
    """``TestClient`` + ``Store`` + ``FakeEngine`` with a writer principal injected.

    Yields rather than returns so the dependency override is torn down
    after each test -- otherwise a viewer-identity test in one file
    would leak into an unrelated writer-route test elsewhere.
    """
    store, engine = make_store()
    answer_account_active(engine)
    # Save prior app.state so an integration test that runs before us
    # (and sets a real engine via lifespan) isn't wiped out by ours.
    prev_engine = getattr(app.state, "engine", None)
    prev_store = getattr(app.state, "store", None)
    app.state.engine = engine
    app.state.store = store
    install_identity(make_test_identity())
    try:
        yield TestClient(app), store, engine
    finally:
        clear_identity_override()
        if prev_engine is None:
            del app.state.engine
        else:
            app.state.engine = prev_engine
        if prev_store is None:
            del app.state.store
        else:
            app.state.store = prev_store
