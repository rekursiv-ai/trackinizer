"""Tests for FastAPI app-level concerns (exception handlers, lifespan)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import asyncio
import json
import logging

from fastapi import FastAPI

import asyncpg

from trackinizer.conftest import make_store
from trackinizer.lib.custom_json import (
    SchemaError,
    dict_val,
    float_val,
    int_val,
    str_val,
)
from trackinizer.server.api import app as app_module
from trackinizer.server.api.app import (
    check_violation_handler,
    conflict_handler,
    fk_violation_handler,
    lifespan,
    not_found_handler,
    schema_handler,
    unique_violation_handler,
    validation_handler,
)
from trackinizer.server.config import Config
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    import pytest

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


class TestCLIHelpers:
    def test_exception_handlers_map_asyncpg_to_409(self) -> None:
        """The three asyncpg-error → 409 handlers each emit ``detail``.

        Hard to trigger via FakeEngine (it doesn't raise these), so
        invoke the handlers directly. Verifies the JSON shape consumers
        rely on.
        """
        req = cast(Any, Mock())
        fk_response = asyncio.run(
            fk_violation_handler(req, asyncpg.ForeignKeyViolationError("boom"))
        )
        check_response = asyncio.run(
            check_violation_handler(req, asyncpg.CheckViolationError("boom"))
        )
        unique_response = asyncio.run(
            unique_violation_handler(req, asyncpg.UniqueViolationError("boom"))
        )
        for response, prefix in [
            (fk_response, "foreign key"),
            (check_response, "check constraint"),
            (unique_response, "unique constraint"),
        ]:
            assert response.status_code == 409
            # ``response.body`` is ``bytes | memoryview``; coerce to
            # ``bytes`` for json.loads's narrower type signature.
            body = json.loads(bytes(response.body))
            assert prefix in body["detail"]

    def test_handlers_do_not_leak_constraint_detail(self) -> None:
        # asyncpg ``detail`` carries internal column / constraint names
        # (e.g. ``Key (from_id)=(...) is not present``); the client-facing
        # body must NOT echo it -- only a generic message (REV-OPUS-03).
        req = cast(Any, Mock())
        leak = 'Key (from_id)=(deadbeef) is not present in table "inquiries"'
        constraint = "edges_from_id_fkey"
        cases = [
            fk_violation_handler,
            check_violation_handler,
            unique_violation_handler,
        ]
        exc_types = [
            asyncpg.ForeignKeyViolationError,
            asyncpg.CheckViolationError,
            asyncpg.UniqueViolationError,
        ]
        for handler, exc_type in zip(cases, exc_types, strict=True):
            exc = exc_type(constraint)
            # asyncpg exposes ``detail`` from the server error fields; set it
            # so the handler sees a realistic leaky value.
            object.__setattr__(exc, "detail", leak)
            response = asyncio.run(handler(req, cast(Any, exc)))
            body = json.loads(bytes(response.body))
            assert response.status_code == 409
            assert leak not in body["detail"]
            assert "from_id" not in body["detail"]
            assert constraint not in body["detail"]

    def test_conflict_handler_emits_error_code(self) -> None:
        req = cast(Any, Mock())
        response = asyncio.run(conflict_handler(req, ConflictError("clash")))
        body = json.loads(bytes(response.body))
        assert response.status_code == 409
        assert body == {"detail": "clash", "code": "conflict"}

    def test_not_found_handler_emits_404_and_code(self) -> None:
        req = cast(Any, Mock())
        response = asyncio.run(not_found_handler(req, NotFoundError("gone")))
        body = json.loads(bytes(response.body))
        assert response.status_code == 404
        assert body == {"detail": "gone", "code": "not_found"}

    def test_validation_handler_emits_422_and_code(self) -> None:
        req = cast(Any, Mock())
        response = asyncio.run(validation_handler(req, ValidationError("bad input")))
        body = json.loads(bytes(response.body))
        assert response.status_code == 422
        assert body == {"detail": "bad input", "code": "validation"}

    def test_schema_handler_emits_422_and_code(self) -> None:
        req = cast(Any, Mock())
        response = asyncio.run(schema_handler(req, SchemaError("stray key")))
        body = json.loads(bytes(response.body))
        assert response.status_code == 422
        assert body == {"detail": "stray key", "code": "schema"}

    def test_schema_error_is_registered_not_merely_defined(self) -> None:
        # The handler function existing is not what stops the 500 -- FastAPI
        # only consults REGISTERED handlers, and a codec ``SchemaError``
        # reaches the app as a plain ``ValueError`` on a client-supplied
        # body. Assert the registration, which is the part that was missing.
        assert app_module.app.exception_handlers.get(SchemaError) is schema_handler


class TestRequestLogging:
    def test_request_completion_is_correlated_and_timed(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, _engine = route_client
        request_id = uuid4()

        with caplog.at_level(logging.INFO):
            response = client.get(
                "/api/version",
                headers={"X-Request-ID": str(request_id)},
            )

        assert response.status_code == 200
        assert UUID(response.headers["X-Request-ID"]) == request_id
        record = next(
            record
            for record in caplog.records
            if getattr(record, "event", "") == "trackinizer_request_completed"
        )
        fields = dict_val(record.__dict__)
        assert str_val(fields.get("request_id")) == str(request_id)
        assert str_val(fields.get("method")) == "GET"
        assert str_val(fields.get("path")) == "/api/version"
        assert str_val(fields.get("outcome")) == "success"
        assert int_val(fields.get("status_code"), 0) == 200
        assert int_val(fields.get("worker_pid"), 0) > 0
        assert float_val(fields.get("response_start_sec"), -1) >= 0
        assert float_val(fields.get("duration_sec"), -1) >= float_val(
            fields.get("response_start_sec"),
            0,
        )

    def test_invalid_request_id_is_replaced_and_rejection_is_classified(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _store, _engine = route_client

        with caplog.at_level(logging.INFO):
            response = client.get(
                "/api/does-not-exist",
                headers={"X-Request-ID": "not-a-uuid"},
            )

        assert response.status_code == 404
        request_id = str(UUID(response.headers["X-Request-ID"]))
        record = next(
            record
            for record in caplog.records
            if getattr(record, "event", "") == "trackinizer_request_completed"
        )
        fields = dict_val(record.__dict__)
        assert str_val(fields.get("request_id")) == request_id
        assert str_val(fields.get("outcome")) == "rejected"
        assert int_val(fields.get("status_code"), 0) == 404


class TestAuthDisabledWarning:
    """The lifespan loudly warns when auth is disabled (synthetic-admin mode)."""

    @staticmethod
    def _run_lifespan(monkeypatch: pytest.MonkeyPatch, *, auth_disabled: bool) -> None:
        """Drive the real ``lifespan`` once with engine/store/embedder stubbed.

        The warning fires inside ``lifespan``; the engine, store, and
        embedder are replaced so the body runs hermetically (no real DB).
        """
        store, engine = make_store()
        monkeypatch.setattr(app_module, "build_engine", Mock(return_value=engine))
        monkeypatch.setattr(app_module, "build_embedder", Mock(return_value=object()))
        monkeypatch.setattr(app_module, "Store", Mock(return_value=store))
        monkeypatch.setattr(store, "bootstrap", AsyncMock(return_value=None))
        app = cast(FastAPI, Mock())
        app.state = Mock()
        app.state.config = Config(auth_disabled=auth_disabled)

        async def _drive() -> None:
            async with lifespan(app):
                pass

        asyncio.run(_drive())

    def test_warns_when_auth_disabled(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``--no-auth`` makes every request a synthetic admin; the operator
        # must be loudly told at startup, never let it slip silently into a
        # reachable deployment (API-47).
        with caplog.at_level(logging.WARNING):
            self._run_lifespan(monkeypatch, auth_disabled=True)
        records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert records, "expected a warning when auth_disabled is set"
        assert "auth" in records[0].getMessage().lower()

    def test_silent_when_auth_enabled(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with caplog.at_level(logging.WARNING):
            self._run_lifespan(monkeypatch, auth_disabled=False)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @staticmethod
    def _seeded_no_auth_user(
        monkeypatch: pytest.MonkeyPatch, *, auth_disabled: bool
    ) -> bool:
        """Drive the lifespan and report whether the no-auth user was seeded."""
        store, engine = make_store()
        monkeypatch.setattr(app_module, "build_engine", Mock(return_value=engine))
        monkeypatch.setattr(app_module, "build_embedder", Mock(return_value=object()))
        monkeypatch.setattr(app_module, "Store", Mock(return_value=store))
        monkeypatch.setattr(store, "bootstrap", AsyncMock(return_value=None))
        app = cast(FastAPI, Mock())
        app.state = Mock()
        app.state.config = Config(auth_disabled=auth_disabled)

        async def _drive() -> None:
            async with lifespan(app):
                pass

        asyncio.run(_drive())
        return any(
            "INSERT INTO users" in c.args[0] and "no-auth@localhost" in c.args
            for c in engine.conn.execute.call_args_list
        )

    def test_seeds_no_auth_user_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The synthetic no-auth principal must exist as an active user so its
        # submits pass the account-attribution gate; demo mode is otherwise
        # unusable (every write 422s).
        assert self._seeded_no_auth_user(monkeypatch, auth_disabled=True)

    def test_no_seed_when_auth_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert not self._seeded_no_auth_user(monkeypatch, auth_disabled=False)


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
