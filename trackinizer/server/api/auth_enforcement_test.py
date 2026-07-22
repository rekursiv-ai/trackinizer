"""Phase 2 auth enforcement tests: 401 / 403 + server-stamped principal.

These tests cover behaviour the per-route test files don't:

* Mutating routes hit by an unauthenticated request return 401.
* Mutating routes hit by an authenticated *viewer* return 403.
* When the wire body omits ``actor``, the server stamps it from the
  authenticated principal's email; ``change_log.api_key_id``
  always carries the credential id regardless of body shape.

The 401 / 403 tests exercise the real :func:`current_user` and
:func:`require_role` dependencies; the principal-stamping test reuses
the writer identity installed by :func:`route_client` and inspects the
``INSERT INTO change_log`` arguments captured by ``FakeEngine``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    clear_identity_override,
    install_identity,
    make_test_identity,
)


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


def _change_log_column(engine: FakeEngine, name: str) -> object:
    """Read the bound value for one column on the latest change_log insert.

    ``emit_change`` builds the column dict in a stable order; we recover
    the bind position from the SQL by parsing the column list. Avoids
    coupling tests to insertion order.
    """
    for call in reversed(engine.conn.execute.call_args_list):
        sql = call.args[0]
        if "INSERT INTO change_log" in sql:
            columns_segment = sql.split("(", 1)[1].split(")", 1)[0]
            columns = [c.strip() for c in columns_segment.split(",")]
            return call.args[1 + columns.index(name)]
    raise AssertionError("no INSERT INTO change_log in executed SQL")


def _inquiry_column(engine: FakeEngine, name: str) -> object:
    """Read the bound value for one column on the latest inquiries insert.

    Like :func:`_change_log_column`, but ``inquiries`` mints ``seq`` inline
    via ``nextval(...)`` rather than a bind param, so that column consumes no
    positional argument -- discount it when mapping a column to its bind.
    """
    for call in reversed(engine.conn.execute.call_args_list):
        sql = call.args[0]
        if "INSERT INTO inquiries" in sql:
            columns_segment = sql.split("(", 1)[1].split(")", 1)[0]
            columns = [c.strip() for c in columns_segment.split(",")]
            bind_index = columns.index(name)
            if "seq" in columns and columns.index("seq") < bind_index:
                bind_index -= 1
            return call.args[1 + bind_index]
    raise AssertionError("no INSERT INTO inquiries in executed SQL")


class TestUnauthenticatedReturns401:
    def test_submit_without_auth(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        # Pop the override installed by the fixture so the real
        # current_user runs against an empty Authorization header.
        clear_identity_override()
        r = client.post("/api/inquiries/issue", json={"title": "x"})
        assert r.status_code == 401

    def test_read_without_auth(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        clear_identity_override()
        r = client.get("/api/inquiries?kind=Issue")
        assert r.status_code == 401


class TestViewerHitsWriterRoute403:
    def test_viewer_cannot_submit(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(role="viewer"))
        r = client.post("/api/inquiries/issue", json={"title": "x"})
        assert r.status_code == 403
        assert "viewer" in r.json()["detail"]

    def test_viewer_can_read(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        install_identity(make_test_identity(role="viewer"))
        # list_kind hits two fetches: main select + bulk edges. Empty
        # rows are enough -- the assertion is the 200, not the payload.
        engine.conn.fetch.side_effect = [[], []]
        r = client.get("/api/inquiries?kind=Issue")
        assert r.status_code == 200


class TestServerStampsPrincipal:
    def test_submit_stamps_principal_and_defaults_actor_to_email(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        r = client.post("/api/inquiries/issue", json={"title": "no actor in body"})
        assert r.status_code == 201, r.text
        # ``api_key_id`` carries the authenticating credential's id,
        # regardless of what the body contained; ``actor`` falls back to
        # the principal's email when omitted.
        assert _change_log_column(engine, "api_key_id") == TEST_API_KEY_ID
        assert _change_log_column(engine, "actor") == TEST_USER_EMAIL

    def test_explicit_actor_overrides_email_default(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        r = client.post(
            "/api/inquiries/issue",
            json={"title": "with actor", "actor": "claude-opus"},
        )
        assert r.status_code == 201, r.text
        # The body's ``actor`` is recorded as the provenance string; the
        # api_key_id is still server-stamped (clients can't set it).
        assert _change_log_column(engine, "actor") == "claude-opus"
        assert _change_log_column(engine, "api_key_id") == TEST_API_KEY_ID

    def test_client_cannot_spoof_api_key_id(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """The wire body has no ``api_key_id`` slot; an attempted
        override is silently ignored. Only the server stamps it.
        """
        client, _store, engine = route_client
        bogus = "99999999-9999-9999-9999-999999999999"
        r = client.post(
            "/api/inquiries/issue",
            json={"title": "spoof attempt", "api_key_id": bogus},
        )
        assert r.status_code == 201, r.text
        # The recorded credential is the real authenticating one, never
        # the client-supplied value.
        assert _change_log_column(engine, "api_key_id") == TEST_API_KEY_ID


class TestAccountAttribution:
    """``account`` defaults to the creator and must be an active user.

    The ``route_client`` fixture's mock answers the account-active probe
    truthy by default (see ``conftest.answer_account_active``), so the
    happy paths need no extra setup; the inactive-account test re-points
    the probe to a falsy answer.
    """

    def test_submit_defaults_account_to_creator(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        r = client.post("/api/inquiries/issue", json={"title": "no account in body"})
        assert r.status_code == 201, r.text
        # Unspecified account attributes the row to the authenticated creator,
        # never left NULL (unlike ``owner``).
        assert _inquiry_column(engine, "account") == TEST_USER_EMAIL

    def test_submit_account_override_to_active_user(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client
        r = client.post(
            "/api/inquiries/issue",
            json={"title": "explicit account", "account": "dan@example.com"},
        )
        assert r.status_code == 201, r.text
        # The body override wins over the creator default once it passes the
        # active-user gate.
        assert _inquiry_column(engine, "account") == "dan@example.com"

    def test_submit_account_inactive_user_rejected(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client

        # Re-point the account-active probe to "no active row" so the gate
        # fires; every other ``fetchval`` keeps its permissive default.
        async def fetchval(sql: str, *args: object) -> object:
            del args
            if "FROM users" in sql and "status = 'active'" in sql:
                return None
            return None

        engine.conn.fetchval.side_effect = fetchval
        r = client.post(
            "/api/inquiries/issue",
            json={"title": "ghost account", "account": "ghost@example.com"},
        )
        assert r.status_code == 422, r.text
        assert "not an active user" in r.json()["detail"]

    def test_submit_blank_account_rejected_as_malformed(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A whitespace ``account`` is malformed input, not an inactive user.

        It must be rejected by body validation (422) with a blank-input
        message, never reach the active-user probe (whose "not an active
        user" message would mislead).
        """
        client, _store, _engine = route_client
        r = client.post(
            "/api/inquiries/issue",
            json={"title": "blank account", "account": "  "},
        )
        assert r.status_code == 422, r.text
        assert "not an active user" not in str(r.json())
        assert "account" in str(r.json())


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
