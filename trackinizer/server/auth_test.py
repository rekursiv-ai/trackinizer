"""Tests for :mod:`trackinizer.auth`.

Three layers:

* :class:`TestHashSecret` -- pure ``hash_secret`` / ``verify_secret``
  roundtrip and tampering guarantees. No I/O.
* :class:`TestCurrentUser` -- the FastAPI Bearer-token dependency
  against the :class:`FakeEngine` from ``conftest``. Covers valid,
  missing, malformed, revoked, unknown, and disabled-user paths.
* :class:`TestBootstrapAdmin` -- the empty-``users`` env-driven seed
  path; verifies the env-var gate, the emptiness gate (rerun is a
  no-op), and the SQL it emits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock

import logging
import os
import uuid

from fastapi import HTTPException

import pytest

from trackinizer.conftest import (
    FakeEngine,
    executed_sql,
    make_conn,
)
from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server import auth as auth_mod
from trackinizer.server.auth import (
    BOOTSTRAP_ADMIN_ENV,
    BOOTSTRAP_TOKEN_FILE_ENV,
    TOKEN_PREFIX_LEN,
    AuthIdentity,
    RoleCeilingError,
    _publish_bootstrap_token,
    _stage_bootstrap_token,
    allowlist_match,
    bootstrap_admin,
    create_api_key,
    current_user,
    effective_role,
    generate_token,
    hash_secret,
    set_api_key_role,
    verify_secret,
)
from trackinizer.server.config import Config
from trackinizer.server.session import (
    SESSION_COOKIE_NAME,
    read_session_cookie,
    set_session_cookie,
)
from trackinizer.server.store.core import Store, StubEmbedder


class TestHashSecret:
    """Pure secret-hashing behaviour."""

    def test_roundtrip_matches(self) -> None:
        encoded = hash_secret("hunter2")
        assert verify_secret("hunter2", encoded)

    def test_wrong_secret_rejected(self) -> None:
        encoded = hash_secret("hunter2")
        assert not verify_secret("hunter3", encoded)

    def test_distinct_hashes_for_same_secret(self) -> None:
        # Salts must randomize per call; two hashes of the same input
        # must differ -- otherwise the hash leaks a fingerprint.
        a = hash_secret("hunter2")
        b = hash_secret("hunter2")
        assert a != b
        assert verify_secret("hunter2", a)
        assert verify_secret("hunter2", b)

    def test_encoded_format_is_self_describing(self) -> None:
        encoded = hash_secret("x")
        parts = encoded.split("$")
        assert parts[0] == "scrypt"
        assert len(parts) == 6
        # n, r, p must round-trip as ints so a future cost bump can
        # rotate per-row without orphaning historical rows.
        int(parts[1])
        int(parts[2])
        int(parts[3])

    def test_verify_rejects_malformed_hash(self) -> None:
        # Corrupted rows should surface as ``False``, never as an
        # exception that would 500 the auth middleware.
        assert not verify_secret("x", "not-an-encoded-hash")
        assert not verify_secret("x", "scrypt$bad$8$1$AAAA$BBBB")
        assert not verify_secret("x", "")

    def test_verify_rejects_oversized_scrypt_params(self) -> None:
        encoded = hash_secret("x")
        _prefix, _n, r, p, salt, key = encoded.split("$")
        bad_hash = "$".join(("scrypt", str(2**30), r, p, salt, key))
        assert not verify_secret("x", bad_hash)

    def test_generate_token_shape(self) -> None:
        secret, prefix = generate_token()
        assert secret.startswith("trax_")
        assert prefix == secret[:TOKEN_PREFIX_LEN]
        # Two calls must produce distinct secrets -- a token collision
        # would silently authenticate one user as another.
        secret2, _ = generate_token()
        assert secret != secret2


# ---- current_user tests ---------------------------------------------------


def _row(
    *,
    secret_hash: str,
    user_id: uuid.UUID | None = None,
    status: str = "active",
    role: str = "writer",
    email: str = "u@example.com",
    key_id: uuid.UUID | None = None,
    user_role: str | None = None,
    key_role: str | None = None,
) -> dict[str, Any]:
    """Build one ``api_keys JOIN users`` row in the shape ``_resolve_identity`` reads.

    The legacy ``role`` kwarg sets both ``user_role`` and ``key_role`` so
    tests written before the per-key ceiling landed keep working without
    edits; tests that want a min(user, key) split set them explicitly.
    """
    return {
        "key_id": key_id or uuid.uuid4(),
        "secret_hash": secret_hash,
        "user_id": user_id or uuid.uuid4(),
        "email": email,
        "user_role": role if user_role is None else user_role,
        "key_role": role if key_role is None else key_role,
        "status": status,
    }


def _request_with(
    engine: FakeEngine,
    authorization: str | None,
    *,
    cookies: dict[str, str] | None = None,
    config: Config | None = None,
    store: Store | None = None,
) -> Any:
    """Build a fake :class:`fastapi.Request` carrying one header + the engine.

    ``app.state.config`` defaults to a vanilla :class:`Config` (auth on,
    no session secret) so ``current_user``'s no-auth short-circuit
    doesn't fire on tests that don't care about it. Pass
    ``Config(auth_disabled=True)`` to opt in.

    A fresh :class:`Store` is bound on ``app.state.store`` by default so
    the per-instance ``last_used_at`` throttle is hermetic per call;
    tests that care about throttle behaviour across multiple requests
    pass an explicit ``store`` to reuse one.
    """
    request = MagicMock()
    headers: dict[str, str] = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    request.headers = headers
    request.cookies = cookies or {}
    request.app.state.engine = engine
    request.app.state.config = Config() if config is None else config
    request.app.state.store = (
        Store(cast(Any, engine), embed=StubEmbedder()) if store is None else store
    )
    request.state.request_id = ""
    return request


class TestCurrentUser:
    """Bearer-token middleware behaviour."""

    @pytest.mark.asyncio
    async def test_valid_token_returns_identity(self) -> None:
        secret, _ = generate_token()
        user_id = uuid.uuid4()
        key_id = uuid.uuid4()
        row = _row(
            secret_hash=hash_secret(secret),
            user_id=user_id,
            key_id=key_id,
            email="alice@example.com",
            role="admin",
        )
        engine = FakeEngine()
        engine.conn.fetch.return_value = [row]
        identity = await current_user(_request_with(engine, f"Bearer {secret}"))
        assert identity == AuthIdentity(
            user_id=user_id,
            api_key_id=key_id,
            email="alice@example.com",
            role="admin",
        )
        # ``last_used_at`` must be bumped on every successful auth so
        # the UI can show "this key was used 5 minutes ago".
        sqls = [c.args[0] for c in engine.conn.execute.call_args_list]
        assert any("UPDATE api_keys SET last_used_at" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_bearer_auth_logs_correlated_stage_timings(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret, _ = generate_token()
        engine = FakeEngine()
        engine.conn.fetch.return_value = [_row(secret_hash=hash_secret(secret))]
        request = _request_with(engine, f"Bearer {secret}")
        request.state.request_id = "request-123"

        with caplog.at_level(logging.INFO):
            await current_user(request)

        record = next(
            record
            for record in caplog.records
            if getattr(record, "event", "") == "trackinizer_auth_completed"
        )
        fields = record.__dict__
        assert fields["request_id"] == "request-123"
        assert fields["lookup_mode"] == "prefix"
        assert fields["outcome"] == "success"
        assert fields["query_sec"] >= 0
        assert fields["verify_sec"] >= 0
        assert fields["duration_sec"] >= fields["query_sec"] + fields["verify_sec"]

    @pytest.mark.asyncio
    async def test_effective_role_is_min_of_user_and_key(self) -> None:
        """Admin-user + writer-key resolves as writer.

        Per docs/design.md (Auth) the credential ceiling caps the user's
        standing role; this is the wire-side proof that
        :class:`AuthIdentity.role` carries the floor.
        """
        secret, _ = generate_token()
        engine = FakeEngine()
        engine.conn.fetch.return_value = [
            _row(
                secret_hash=hash_secret(secret),
                user_role="admin",
                key_role="writer",
            ),
        ]
        identity = await current_user(_request_with(engine, f"Bearer {secret}"))
        assert identity.role == "writer"

    @pytest.mark.asyncio
    async def test_effective_role_is_min_when_key_stronger(self) -> None:
        # A writer user holding an admin-roled key (impossible to mint
        # through ``create_api_key``, but a useful invariant against
        # direct-SQL drift) still resolves as writer.
        secret, _ = generate_token()
        engine = FakeEngine()
        engine.conn.fetch.return_value = [
            _row(
                secret_hash=hash_secret(secret),
                user_role="writer",
                key_role="admin",
            ),
        ]
        identity = await current_user(_request_with(engine, f"Bearer {secret}"))
        assert identity.role == "writer"

    @pytest.mark.asyncio
    async def test_missing_header_raises_401(self) -> None:
        engine = FakeEngine()
        with pytest.raises(HTTPException) as exc_info:
            await current_user(_request_with(engine, None))
        # Asserting on status_code directly is fine now that the
        # pytest.raises is typed to HTTPException.
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_non_bearer_scheme_raises_401(self) -> None:
        engine = FakeEngine()
        with pytest.raises(HTTPException) as exc_info:
            await current_user(_request_with(engine, "Basic abc"))
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_bearer_raises_401(self) -> None:
        engine = FakeEngine()
        with pytest.raises(HTTPException) as exc_info:
            await current_user(_request_with(engine, "Bearer    "))
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_token_raises_401(self) -> None:
        secret, _ = generate_token()
        engine = FakeEngine()
        # No matching prefix row in the DB.
        engine.conn.fetch.return_value = []
        with pytest.raises(HTTPException) as exc_info:
            await current_user(_request_with(engine, f"Bearer {secret}"))
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_prefix_miss_runs_dummy_scrypt_for_constant_time(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A prefix that matches no live key must still pay one scrypt
        # verify (against a fixed sentinel hash) so an attacker can't use
        # the response-time gap between "prefix absent" (~one PG round
        # trip) and "prefix present" (~50ms scrypt) to probe which key
        # prefixes exist. The dummy verify is the constant-time floor.
        secret, _ = generate_token()
        engine = FakeEngine()
        engine.conn.fetch.return_value = []  # prefix miss

        calls: list[str] = []
        real_verify = auth_mod.verify_secret

        def _spy(candidate: str, encoded: str) -> bool:
            calls.append(encoded)
            return real_verify(candidate, encoded)

        monkeypatch.setattr(auth_mod, "verify_secret", _spy)
        with pytest.raises(HTTPException):
            await current_user(_request_with(engine, f"Bearer {secret}"))
        # Exactly one verify ran even though no row matched, and it used a
        # valid scrypt-encoded sentinel (so the cost actually applies).
        assert len(calls) == 1
        assert real_verify("anything", calls[0]) is False

    @pytest.mark.asyncio
    async def test_revoked_token_is_unknown(self) -> None:
        # ``_resolve_identity``'s SQL filters ``revoked_at IS NULL`` so
        # a revoked key is invisible -- the test simulates that by
        # returning no rows, mirroring what the live query would do.
        secret, _ = generate_token()
        engine = FakeEngine()
        engine.conn.fetch.return_value = []
        with pytest.raises(HTTPException) as exc_info:
            await current_user(_request_with(engine, f"Bearer {secret}"))
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_secret_with_matching_prefix_rejected(self) -> None:
        # Prefix collision is rare but possible: same first 12 chars,
        # different rest. The scrypt verify is the second gate.
        real_secret, _ = generate_token()
        engine = FakeEngine()
        engine.conn.fetch.return_value = [_row(secret_hash=hash_secret(real_secret))]
        # Submit a different secret with the same prefix.
        bad = real_secret[:TOKEN_PREFIX_LEN] + "DIFFERENT_TAIL"
        with pytest.raises(HTTPException) as exc_info:
            await current_user(_request_with(engine, f"Bearer {bad}"))
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_disabled_user_rejected(self) -> None:
        secret, _ = generate_token()
        engine = FakeEngine()
        engine.conn.fetch.return_value = [
            _row(secret_hash=hash_secret(secret), status="disabled"),
        ]
        with pytest.raises(HTTPException) as exc_info:
            await current_user(_request_with(engine, f"Bearer {secret}"))
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_prefix_collision_disabled_then_active_resolves_active(self) -> None:
        # Two ``api_keys`` rows share the full secret (same prefix AND
        # same plaintext) -- can arise when an operator disables a user
        # via ``status='disabled'`` without revoking the credential and
        # subsequently re-mints the same plaintext for an active user
        # (or in any future scenario where prefix-scoped rows happen to
        # both verify). The resolver iterates in DB order; a
        # disabled-row short-circuit would deny the active user even
        # though the active row's verify also succeeds. The resolver
        # must exhaust every prefix-matching row that verifies and
        # return the first ACTIVE match.
        secret, _ = generate_token()
        shared_hash = hash_secret(secret)
        active_user_id = uuid.uuid4()
        active_key_id = uuid.uuid4()
        engine = FakeEngine()
        engine.conn.fetch.return_value = [
            _row(
                secret_hash=shared_hash,
                status="disabled",
                email="ghost@example.com",
            ),
            _row(
                secret_hash=hash_secret(secret),
                status="active",
                user_id=active_user_id,
                key_id=active_key_id,
                email="alice@example.com",
                role="writer",
            ),
        ]
        identity = await current_user(_request_with(engine, f"Bearer {secret}"))
        assert identity.user_id == active_user_id
        assert identity.api_key_id == active_key_id
        assert identity.email == "alice@example.com"

    @pytest.mark.asyncio
    async def test_last_used_at_coalesces_repeat_hits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A hot key (one CI bot driving the bulk of traffic) would
        # serialize every authenticated request on the same row-level
        # write lock if ``last_used_at`` were UPDATEd per call. The
        # resolver coalesces bumps via a per-``Store`` throttle keyed on
        # ``api_keys.id``: the first hit writes, subsequent hits inside
        # the interval skip the UPDATE, and a hit past the interval
        # writes again.
        secret, _ = generate_token()
        key_id = uuid.uuid4()
        row = _row(secret_hash=hash_secret(secret), key_id=key_id)
        engine = FakeEngine()
        engine.conn.fetch.return_value = [row]
        # One ``Store`` reused across calls so the throttle stays the
        # subject of the test rather than the fixture.
        store = Store(cast(Any, engine), embed=StubEmbedder())

        clock: list[float] = [1_000.0]
        monkeypatch.setattr(auth_mod, "monotonic_clock", lambda: clock[0])

        for _ in range(5):
            await current_user(_request_with(engine, f"Bearer {secret}", store=store))
        bump_sqls = [
            c
            for c in engine.conn.execute.call_args_list
            if "UPDATE api_keys SET last_used_at" in c.args[0]
        ]
        # First hit primes the cache and writes; the next four collapse.
        assert len(bump_sqls) == 1

        # Advance past the interval; the next hit writes again.
        clock[0] += 120.0
        await current_user(_request_with(engine, f"Bearer {secret}", store=store))
        bump_sqls = [
            c
            for c in engine.conn.execute.call_args_list
            if "UPDATE api_keys SET last_used_at" in c.args[0]
        ]
        assert len(bump_sqls) == 2

    @pytest.mark.asyncio
    async def test_last_used_at_throttle_is_per_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The throttle lifecycle matches the ``Store``, not the module:
        # two ``Store`` instances sharing one engine must each get their
        # own first-hit write for the same ``api_keys.id``. A
        # module-level throttle would silently suppress the second
        # store's UPDATE.
        secret, _ = generate_token()
        key_id = uuid.uuid4()
        row = _row(secret_hash=hash_secret(secret), key_id=key_id)
        engine = FakeEngine()
        engine.conn.fetch.return_value = [row]
        store_a = Store(cast(Any, engine), embed=StubEmbedder())
        store_b = Store(cast(Any, engine), embed=StubEmbedder())
        monkeypatch.setattr(auth_mod, "monotonic_clock", lambda: 1_000.0)

        await current_user(_request_with(engine, f"Bearer {secret}", store=store_a))
        await current_user(_request_with(engine, f"Bearer {secret}", store=store_b))

        bump_sqls = [
            c
            for c in engine.conn.execute.call_args_list
            if "UPDATE api_keys SET last_used_at" in c.args[0]
        ]
        assert len(bump_sqls) == 2


class TestCurrentUserNoAuth:
    """``Config.auth_disabled`` short-circuits the resolver."""

    @pytest.mark.asyncio
    async def test_returns_synthetic_admin_without_credentials(self) -> None:
        engine = FakeEngine()
        config = Config(auth_disabled=True)
        identity = await current_user(_request_with(engine, None, config=config))
        assert identity.role == "admin"
        assert identity.api_key_id is None
        assert identity.email == "no-auth@localhost"
        # No DB hit -- the synthetic path skips the engine.
        engine.conn.execute.assert_not_called()
        engine.conn.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_synthetic_user_id_is_stable(self) -> None:
        engine = FakeEngine()
        config = Config(auth_disabled=True)
        first = await current_user(_request_with(engine, None, config=config))
        second = await current_user(_request_with(engine, None, config=config))
        assert first.user_id == second.user_id

    @pytest.mark.asyncio
    async def test_bearer_ignored_in_no_auth_mode(self) -> None:
        # Even a bogus Bearer header collapses to the synthetic identity
        # -- no DB lookup, no 401, no token verification.
        engine = FakeEngine()
        config = Config(auth_disabled=True)
        identity = await current_user(
            _request_with(engine, "Bearer not-a-real-token", config=config),
        )
        assert identity.role == "admin"
        engine.conn.fetch.assert_not_called()


# ---- bootstrap_admin tests ------------------------------------------------


# The id the race-safe ``INSERT INTO users ... RETURNING id`` hands back on the
# winning path; any non-None value drives the seed past the loser short-circuit.
_BOOTSTRAP_WINNER_ID: uuid.UUID = uuid.UUID("44444444-4444-4444-4444-444444444444")


class TestBootstrapAdmin:
    """``TRACKINIZER_BOOTSTRAP_ADMIN`` env-driven seed path."""

    @pytest.mark.asyncio
    async def test_seeds_admin_when_users_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "admin@example.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(tmp_path / "bt"))
        conn = make_conn()
        # Three fetchval calls in order: emptiness probe (``SELECT 1 FROM
        # users``) -> None; the race-safe ``INSERT INTO users ... RETURNING id``
        # -> the winner id; then ``create_api_key`` looks up the just-inserted
        # bootstrap admin's role.
        conn.fetchval = AsyncMock(side_effect=[None, _BOOTSTRAP_WINNER_ID, "admin"])
        await bootstrap_admin(conn)
        inserts = [
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO allowlist" in c.args[0]
        ]
        assert len(inserts) == 1
        # The seeded email must round-trip verbatim.
        assert inserts[0].args[1] == "admin@example.com"

    @pytest.mark.asyncio
    async def test_lost_race_skips_api_key_and_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The empty-users probe cleared, but a concurrent worker committed the
        # admin first: the ``INSERT INTO users ... ON CONFLICT DO NOTHING
        # RETURNING id`` returns None. This worker must NOT mint a second
        # api_key or publish a token for a credential it does not own -- it just
        # rolls its no-op tx forward and returns.
        token_file = tmp_path / "bootstrap_token"
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "admin@example.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(token_file))
        conn = make_conn()
        # Probe -> None (looked empty); users insert RETURNING id -> None (lost).
        conn.fetchval = AsyncMock(side_effect=[None, None])
        await bootstrap_admin(conn)
        sqls = [c.args[0] for c in conn.execute.call_args_list]
        # No api_key minted, and the tx still committed (no rollback).
        assert not any("INSERT INTO api_keys" in s for s in sqls)
        assert any(s.strip().upper().startswith("COMMIT") for s in sqls)
        assert not any(s.strip().upper().startswith("ROLLBACK") for s in sqls)
        # No token published for a credential this worker never created.
        assert not token_file.exists()

    @pytest.mark.asyncio
    async def test_skips_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(BOOTSTRAP_ADMIN_ENV, raising=False)
        conn = make_conn()
        await bootstrap_admin(conn)
        # No SQL at all when the env var is missing -- offline tests
        # and dev bootstraps must not require it.
        assert conn.execute.call_count == 0
        assert conn.fetchval.call_count == 0

    @pytest.mark.asyncio
    async def test_skips_when_env_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "   ")
        conn = make_conn()
        await bootstrap_admin(conn)
        assert conn.execute.call_count == 0

    @pytest.mark.asyncio
    async def test_skips_when_users_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Once any user exists the seed must no-op -- the path is
        # "first run only", not "every startup".
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "admin@example.com")
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value=1)
        await bootstrap_admin(conn)
        inserts = [
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO allowlist" in c.args[0]
        ]
        assert inserts == []

    def test_bootstrap_token_file_is_created_private_before_chmod(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # The staged ``.tmp`` carries the same mode-0600 invariant as the
        # final path: a wide-open umask must not race a permission bump.
        # ``_stage_bootstrap_token`` opens with ``O_CREAT, 0o600`` then
        # chmods to 0o600 explicitly; both steps must land before any
        # readable mode is observable on the file.
        token_path = tmp_path / "bootstrap_token"
        tmp_token_path = tmp_path / "bootstrap_token.tmp"
        original_chmod = Path.chmod
        observed_modes: list[int] = []

        def spy_chmod(path: Path, mode: int) -> None:
            if path == tmp_token_path:
                observed_modes.append(path.stat().st_mode & 0o777)
            original_chmod(path, mode)

        monkeypatch.setattr(Path, "chmod", spy_chmod)
        old_umask = os.umask(0o022)
        try:
            _stage_bootstrap_token(token_path, "secret")
            _publish_bootstrap_token(token_path)
        finally:
            os.umask(old_umask)
        assert observed_modes == [0o600]
        assert token_path.read_text() == "secret\n"
        # The staged sibling must be consumed by the rename -- a
        # lingering ``.tmp`` would confuse the next bootstrap.
        assert not tmp_token_path.exists()

    @pytest.mark.asyncio
    async def test_idempotent_on_rerun(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Two back-to-back calls with the same env var. The second
        # call sees a "user already exists" world (in real life: the
        # bootstrap admin has now logged in) and must not double-insert.
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "admin@example.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(tmp_path / "bt"))
        conn = make_conn()
        # First call: emptiness probe -> None; ``create_api_key`` role
        # lookup -> "admin". Second call: emptiness probe -> 1 (the
        # bootstrap admin is now present) and the function short-circuits
        # before ``create_api_key``.
        conn.fetchval = AsyncMock(side_effect=[None, _BOOTSTRAP_WINNER_ID, "admin", 1])
        await bootstrap_admin(conn)
        await bootstrap_admin(conn)
        inserts = [
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO allowlist" in c.args[0]
        ]
        assert len(inserts) == 1

    @pytest.mark.asyncio
    async def test_seeds_user_and_api_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Beyond the allowlist row, bootstrap must also create the
        # ``users`` row and an ``api_keys`` row so the operator can
        # call the API immediately (no OAuth flow needed).
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "admin@example.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(tmp_path / "bootstrap_token"))
        conn = make_conn()
        conn.fetchval = AsyncMock(side_effect=[None, _BOOTSTRAP_WINNER_ID, "admin"])
        await bootstrap_admin(conn)
        sqls = [c.args[0] for c in conn.execute.call_args_list]
        # The users insert is a ``fetchval`` now (it reads back ``RETURNING id``
        # to tell the race winner from a loser), so it lands among the queries,
        # not the plain executes.
        fetched = [c.args[0] for c in conn.fetchval.call_args_list]
        assert any("INSERT INTO allowlist" in s for s in sqls)
        assert any("INSERT INTO users" in s for s in fetched)
        assert any("INSERT INTO api_keys" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_writes_token_file_mode_0600(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        token_file = tmp_path / "bootstrap_token"
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "admin@example.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(token_file))
        conn = make_conn()
        conn.fetchval = AsyncMock(side_effect=[None, _BOOTSTRAP_WINNER_ID, "admin"])
        await bootstrap_admin(conn)
        assert token_file.exists()
        # Sensitive: secret is auth-equivalent to a password.
        assert (token_file.stat().st_mode & 0o777) == 0o600
        secret = token_file.read_text().strip()
        # The minted secret carries the ``trax_`` brand prefix that
        # ``generate_token`` stamps on every key.
        assert secret.startswith("trax_")

    @pytest.mark.asyncio
    async def test_token_file_write_failure_rolls_back_bootstrap(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # The api_keys row stores ``secret_hash`` only -- the plaintext
        # is never re-derivable. If the token file write fails AFTER
        # the DB tx commits, the operator is locked out: the gate
        # ``SELECT 1 FROM users`` traps retries into a permanent no-op
        # and the only credential the row authorizes is unrecoverable.
        # The token write is therefore part of the bootstrap unit of
        # work: an OSError must propagate and the surrounding
        # ``tx(conn)`` must ROLLBACK so the operator's retry succeeds.
        bad_path = tmp_path / "nonexistent" / "ro" / "token"
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "admin@example.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(bad_path))
        conn = make_conn()
        conn.fetchval = AsyncMock(side_effect=[None, _BOOTSTRAP_WINNER_ID, "admin"])
        original_mkdir = Path.mkdir

        def _failing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
            del self, args, kwargs
            raise OSError("simulated read-only filesystem")

        monkeypatch.setattr(Path, "mkdir", _failing_mkdir)
        try:
            with pytest.raises(OSError, match="simulated read-only filesystem"):
                await bootstrap_admin(conn)
        finally:
            monkeypatch.setattr(Path, "mkdir", original_mkdir)
        sqls = executed_sql(conn)
        assert any(s.strip().upper().startswith("BEGIN") for s in sqls)
        assert any(s.strip().upper().startswith("ROLLBACK") for s in sqls)
        assert not any(s.strip().upper().startswith("COMMIT") for s in sqls)

    @pytest.mark.asyncio
    async def test_commit_failure_leaves_no_orphan_token_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # The bootstrap unit-of-work spans two storage layers (DB +
        # filesystem). If the token file is published at its final path
        # BEFORE COMMIT and the commit then fails (connection loss,
        # deadlock, deferred constraint), the rolled-back ``api_keys``
        # row leaves an orphan plaintext token on disk for a credential
        # the DB doesn't know. The fix: write to a sibling ``<final>.tmp``
        # inside the tx and rename to the final path only after COMMIT
        # has returned cleanly. On COMMIT failure the final path stays
        # absent; only the ``.tmp`` may remain (and is then clearly an
        # orphan that the next bootstrap attempt can clear).
        token_file = tmp_path / "bootstrap_token"
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "admin@example.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(token_file))
        conn = make_conn()
        conn.fetchval = AsyncMock(side_effect=[None, _BOOTSTRAP_WINNER_ID, "admin"])

        async def execute(sql: str, *args: object) -> str:
            del args
            if sql.strip().upper().startswith("COMMIT"):
                raise RuntimeError("simulated commit failure")
            return "UPDATE 1"

        conn.execute = AsyncMock(side_effect=execute)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            await bootstrap_admin(conn)
        # The final path must NOT exist: a rolled-back DB tx and a
        # plaintext credential on disk are an unrecoverable mismatch.
        assert not token_file.exists()
        # Any sibling that survived must wear the ``.tmp`` suffix --
        # operator tooling that reads the final-named file can never
        # surface an orphan secret as a live credential.
        leftovers = [p.name for p in tmp_path.iterdir() if p.is_file()]  # noqa: ASYNC240 - sync iterdir on tmp_path is fine in a test
        assert all(name.endswith(".tmp") for name in leftovers), leftovers

    @pytest.mark.asyncio
    async def test_rolls_back_users_insert_when_api_key_creation_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Idempotency claim in the docstring depends on the multi-INSERT
        # bootstrap being atomic: if ``create_api_key`` raises after the
        # ``users`` row landed, the gate ``SELECT 1 FROM users`` would
        # see that orphan and permanently no-op on retry. Bootstrap must
        # wrap the sequence in ``tx(conn)`` so the failure rolls back.
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "admin@example.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(tmp_path / "bt"))
        conn = make_conn()
        conn.fetchval = AsyncMock(side_effect=[None, _BOOTSTRAP_WINNER_ID, "admin"])

        async def boom(*args: object, **kwargs: object) -> Any:
            del args, kwargs
            raise RuntimeError("simulated api_key insert failure")

        monkeypatch.setattr("trackinizer.server.auth.create_api_key", boom)
        with pytest.raises(RuntimeError, match="simulated api_key insert failure"):
            await bootstrap_admin(conn)
        sqls = executed_sql(conn)
        # Atomic bootstrap: BEGIN must precede the INSERTs and ROLLBACK
        # must follow the failure -- the gate row is undone, retry works.
        assert any(s.strip().upper().startswith("BEGIN") for s in sqls)
        assert any(s.strip().upper().startswith("ROLLBACK") for s in sqls)

    @pytest.mark.asyncio
    async def test_lowercases_email_before_insert(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # ``_canonical_allowlist_entry`` (admin UI) lowercases on insert
        # and ``allowlist_match`` lowercases on read, so the bootstrap
        # path must lowercase too -- otherwise "Foo@Bar.com" from env
        # and "foo@bar.com" from the admin UI become two distinct rows
        # (the column literal is case-sensitive) and the admin listing
        # shows duplicates.
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "Foo@Bar.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(tmp_path / "bt"))
        conn = make_conn()
        conn.fetchval = AsyncMock(side_effect=[None, _BOOTSTRAP_WINNER_ID, "admin"])
        await bootstrap_admin(conn)
        allowlist_inserts = [
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO allowlist" in c.args[0]
        ]
        assert len(allowlist_inserts) == 1
        assert allowlist_inserts[0].args[1] == "foo@bar.com"
        # The users insert reads back ``RETURNING id`` via fetchval, so its
        # lowercased email argument lands on the fetchval call, not an execute.
        users_inserts = [
            c for c in conn.fetchval.call_args_list if "INSERT INTO users" in c.args[0]
        ]
        assert len(users_inserts) == 1
        assert users_inserts[0].args[2] == "foo@bar.com"


class _SeedAfterUsersProbe:
    """Connection wrapper that commits a competing admin before the users insert.

    Delegates every attribute to a real connection, but the first
    ``INSERT INTO users`` it sees first commits an identical-email admin row on
    an independent connection -- reproducing the probe-stale window where a
    second worker has already won. Used to drive ``bootstrap_admin`` through the
    exact interleaving that triggers K3-BOOTSTRAP-RACE.
    """

    def __init__(self, conn: object, engine: PostgresEngine) -> None:
        self._conn = conn
        self._engine = engine
        self._seeded = False

    async def execute(self, sql: str, *args: object) -> object:
        if not self._seeded and "INSERT INTO users" in sql:
            self._seeded = True
            async with self._engine.acquire() as competitor:
                await competitor.execute(
                    "INSERT INTO users (id, email, name, role, status) "
                    "VALUES ($1, 'race@example.com', 'race', 'admin', 'active')",
                    uuid.uuid4(),
                )
        return await cast(Any, self._conn).execute(sql, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
class TestBootstrapAdminRace:
    """Concurrent ``bootstrap_admin`` on empty ``users`` must not crash a worker.

    Under ``--engine pg --workers N`` two workers can both pass the
    empty-``users`` probe before either commits its admin row. Without
    ``ON CONFLICT`` the loser hits the ``users.email`` unique violation and the
    worker dies at startup (K3-BOOTSTRAP-RACE). The seed must instead converge
    on exactly one admin row, no exception, regardless of interleaving.
    """

    async def test_loser_bootstrap_on_committed_admin_does_not_crash(
        self,
        integ_engine: PostgresEngine,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "race@example.com")
        monkeypatch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(tmp_path / "token"))
        async with integ_engine.acquire() as setup_conn:
            await setup_conn.execute("DELETE FROM api_keys")
            await setup_conn.execute("DELETE FROM users")

        # Reproduce the TOCTOU deterministically: between ``bootstrap_admin``'s
        # own empty-``users`` probe and its ``INSERT INTO users``, a competing
        # worker commits the admin row. A connection wrapper interposes on the
        # first users-insert -- rather than racing two coroutines on one event
        # loop -- so the probe-stale window is pinned every run. The insert must
        # converge on that row, not crash on the unique constraint.
        async with integ_engine.acquire() as conn_b:
            # The wrapper is a structural Conn stand-in (delegates every attr);
            # cast at the call site so the test exercises the real signature.
            await bootstrap_admin(cast(Any, _SeedAfterUsersProbe(conn_b, integ_engine)))

        async with integ_engine.acquire() as conn:
            admin_count = await conn.fetchval(
                "SELECT count(*) FROM users WHERE email = 'race@example.com'"
            )
        assert admin_count == 1


# ---- session-cookie tests -------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for :class:`fastapi.Response` that satisfies the
    :class:`session._SetsCookies` Protocol -- captures cookies.

    Signature mirrors the Protocol exactly so structural-typing checkers
    accept it without casts.
    """

    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}

    def set_cookie(
        self,
        key: str,
        value: str = "",
        *,
        max_age: int | None = None,
        httponly: bool = False,
        secure: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
        path: str | None = "/",
    ) -> None:
        del max_age, httponly, secure, samesite, path
        self.cookies[key] = value

    def delete_cookie(
        self,
        key: str,
        *,
        path: str = "/",
        httponly: bool = False,
        secure: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
    ) -> None:
        del path, httponly, secure, samesite
        self.cookies.pop(key, None)


class TestSessionCookieRoundTrip:
    """The signed-session-cookie helpers must round-trip cleanly."""

    def test_set_and_read_recovers_user_id(self) -> None:
        secret = "test-session-secret-32-bytes-or-so"  # noqa: S105 -- test fixture.
        response = _FakeResponse()
        user_id = uuid.uuid4()
        set_session_cookie(
            response,
            user_id=str(user_id),
            secret=secret,
            max_age_seconds=600,
        )
        recovered = read_session_cookie(
            {SESSION_COOKIE_NAME: response.cookies[SESSION_COOKIE_NAME]},
            secret=secret,
            max_age_seconds=600,
        )
        assert recovered == str(user_id)

    def test_expired_cookie_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Forge an expired cookie by lying about the clock during
        # ``set_session_cookie``: itsdangerous stamps the signature time
        # from ``itsdangerous.timed.time.time``, so a backdated clock at
        # sign-time produces a token that the live clock sees as old.
        # ``time`` is monkey-patched by string path -- importing
        # ``itsdangerous.timed.time`` directly trips the private-export
        # type-check warning.
        secret = "another-session-secret"  # noqa: S105 -- test fixture.
        monkeypatch.setattr("itsdangerous.timed.time.time", lambda: 1_000_000.0)
        response = _FakeResponse()
        set_session_cookie(
            response,
            user_id="abc",
            secret=secret,
            max_age_seconds=600,
        )
        monkeypatch.setattr(
            "itsdangerous.timed.time.time", lambda: 1_000_000.0 + 10_000.0
        )
        assert (
            read_session_cookie(
                {SESSION_COOKIE_NAME: response.cookies[SESSION_COOKIE_NAME]},
                secret=secret,
                max_age_seconds=600,
            )
            is None
        )

    def test_tampered_cookie_returns_none(self) -> None:
        secret = "secret-x"  # noqa: S105 -- test fixture.
        response = _FakeResponse()
        set_session_cookie(
            response,
            user_id="abc",
            secret=secret,
            max_age_seconds=600,
        )
        # Flip one character in the signed payload, not the final base64
        # signature character where unused pad bits can decode identically.
        cookie = response.cookies[SESSION_COOKIE_NAME]
        head, sep, tail = cookie.partition(".")
        assert sep
        tampered = head[:-1] + ("Z" if head[-1] != "Z" else "A") + sep + tail
        assert (
            read_session_cookie(
                {SESSION_COOKIE_NAME: tampered},
                secret=secret,
                max_age_seconds=600,
            )
            is None
        )

    def test_wrong_secret_returns_none(self) -> None:
        response = _FakeResponse()
        set_session_cookie(
            response,
            user_id="abc",
            secret="signing-secret",  # noqa: S106 -- test fixture.
            max_age_seconds=600,
        )
        assert (
            read_session_cookie(
                {SESSION_COOKIE_NAME: response.cookies[SESSION_COOKIE_NAME]},
                secret="different-secret",  # noqa: S106 -- test fixture.
                max_age_seconds=600,
            )
            is None
        )

    def test_missing_cookie_returns_none(self) -> None:
        assert (
            read_session_cookie(
                {},
                secret="x",  # noqa: S106 -- test fixture.
                max_age_seconds=600,
            )
            is None
        )


# ---- current_user session/bearer precedence -------------------------------


def _session_cookie_value(user_id: uuid.UUID, secret: str) -> str:
    """Build a freshly-signed session cookie for a given ``user_id``."""
    response = _FakeResponse()
    set_session_cookie(
        response,
        user_id=str(user_id),
        secret=secret,
        max_age_seconds=600,
    )
    return response.cookies[SESSION_COOKIE_NAME]


def _config_with_session_secret(secret: str) -> Config:
    """A :class:`Config` carrying just enough fields for the session path."""
    return Config(session_secret=secret, session_max_age_seconds=600)


class TestCurrentUserSession:
    """Session-cookie branch of :func:`current_user`."""

    @pytest.mark.asyncio
    async def test_session_resolves_when_user_active(self) -> None:
        user_id = uuid.uuid4()
        secret = "session-secret-x"  # noqa: S105 -- test fixture.
        engine = FakeEngine()
        engine.conn.fetchrow = AsyncMock(
            return_value={
                "id": user_id,
                "email": "u@example.com",
                "role": "writer",
                "status": "active",
            }
        )
        request = _request_with(
            engine,
            authorization=None,
            cookies={SESSION_COOKIE_NAME: _session_cookie_value(user_id, secret)},
            config=_config_with_session_secret(secret),
        )
        identity = await current_user(request)
        assert identity.user_id == user_id
        assert identity.api_key_id is None
        assert identity.email == "u@example.com"
        assert identity.role == "writer"

    @pytest.mark.asyncio
    async def test_session_rejected_when_user_disabled(self) -> None:
        user_id = uuid.uuid4()
        secret = "session-secret-x"  # noqa: S105 -- test fixture.
        engine = FakeEngine()
        engine.conn.fetchrow = AsyncMock(
            return_value={
                "id": user_id,
                "email": "u@example.com",
                "role": "writer",
                "status": "disabled",
            }
        )
        request = _request_with(
            engine,
            authorization=None,
            cookies={SESSION_COOKIE_NAME: _session_cookie_value(user_id, secret)},
            config=_config_with_session_secret(secret),
        )
        with pytest.raises(HTTPException) as exc_info:
            await current_user(request)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_session_rejected_when_user_missing(self) -> None:
        secret = "session-secret-x"  # noqa: S105 -- test fixture.
        engine = FakeEngine()
        engine.conn.fetchrow = AsyncMock(return_value=None)
        request = _request_with(
            engine,
            authorization=None,
            cookies={SESSION_COOKIE_NAME: _session_cookie_value(uuid.uuid4(), secret)},
            config=_config_with_session_secret(secret),
        )
        with pytest.raises(HTTPException) as exc_info:
            await current_user(request)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_credentials_at_all_is_401(self) -> None:
        # No bearer header, no session cookie -- the canonical 401 path.
        engine = FakeEngine()
        request = _request_with(
            engine, authorization=None, config=_config_with_session_secret("s")
        )
        with pytest.raises(HTTPException) as exc_info:
            await current_user(request)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_bearer_wins_when_both_present(self) -> None:
        # An interactive operator with both a browser session and a
        # token loaded in curl must see the *token's* identity; the
        # session cookie is silently ignored so the audit log records
        # the credential actually presented.
        bearer_user_id = uuid.uuid4()
        bearer_key_id = uuid.uuid4()
        session_user_id = uuid.uuid4()
        secret_token, _ = generate_token()
        bearer_row = _row(
            secret_hash=hash_secret(secret_token),
            user_id=bearer_user_id,
            key_id=bearer_key_id,
            email="bearer@example.com",
            role="writer",
        )
        engine = FakeEngine()
        engine.conn.fetch.return_value = [bearer_row]
        # ``fetchrow`` would be hit if the session path ran; assert it
        # doesn't by leaving it as the default ``None`` and verifying
        # the resolved identity is the bearer one.
        request = _request_with(
            engine,
            authorization=f"Bearer {secret_token}",
            cookies={
                SESSION_COOKIE_NAME: _session_cookie_value(
                    session_user_id, "session-secret"
                ),
            },
            config=_config_with_session_secret("session-secret"),
        )
        identity = await current_user(request)
        assert identity.user_id == bearer_user_id
        assert identity.api_key_id == bearer_key_id
        assert identity.email == "bearer@example.com"


# ---- allowlist_match tests ------------------------------------------------


class TestAllowlistMatch:
    """Literal-first, pattern-fallback allowlist resolution."""

    @pytest.mark.asyncio
    async def test_literal_match_returns_role(self) -> None:
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value="admin")
        role = await allowlist_match(conn, email="alice@rekursiv.ai")
        assert role == "admin"
        # The first SQL probe must be the literal lookup.
        sql, *args = conn.fetchval.call_args_list[0].args
        assert "lower(email_or_pattern) = lower($1)" in sql
        assert args == ["alice@rekursiv.ai"]

    @pytest.mark.asyncio
    async def test_literal_match_is_case_insensitive(self) -> None:
        conn = make_conn()

        async def fetchval(sql: str, *args: object) -> str | None:
            del args
            if "lower(email_or_pattern) = lower($1)" in sql:
                return "admin"
            return None

        conn.fetchval = AsyncMock(side_effect=fetchval)
        role = await allowlist_match(conn, email="alice@example.com")
        assert role == "admin"

    @pytest.mark.asyncio
    async def test_pattern_match_returns_role(self) -> None:
        # Literal probe misses (returns None), pattern probe wins.
        conn = make_conn()
        conn.fetchval = AsyncMock(side_effect=[None, "writer"])
        role = await allowlist_match(conn, email="bob@rekursiv.ai")
        assert role == "writer"
        # Second probe queries the LIKE-anchored pattern table.
        second_sql = conn.fetchval.call_args_list[1].args[0]
        assert "LIKE '*@%'" in second_sql

    @pytest.mark.asyncio
    async def test_miss_returns_none(self) -> None:
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value=None)
        role = await allowlist_match(conn, email="evil@example.com")
        assert role is None

    @pytest.mark.asyncio
    async def test_email_without_at_returns_none(self) -> None:
        # A malformed email shouldn't crash the pattern lookup; the
        # function must short-circuit on a missing ``@`` rather than
        # passing an empty domain to the SQL.
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value=None)
        role = await allowlist_match(conn, email="no-at-symbol")
        assert role is None


class TestEffectiveRole:
    """Pure :func:`effective_role` semantics: returns the floor under
    :data:`ROLE_ORDER`.
    """

    def test_returns_min_when_user_stronger(self) -> None:
        assert effective_role("admin", "writer") == "writer"
        assert effective_role("admin", "viewer") == "viewer"
        assert effective_role("writer", "viewer") == "viewer"

    def test_returns_min_when_key_stronger(self) -> None:
        assert effective_role("writer", "admin") == "writer"
        assert effective_role("viewer", "admin") == "viewer"
        assert effective_role("viewer", "writer") == "viewer"

    def test_equal_roles_pass_through(self) -> None:
        for r in ("viewer", "writer", "admin"):
            assert effective_role(cast(Any, r), cast(Any, r)) == r


# ---- create_api_key + set_api_key_role tests ------------------------------


class TestCreateApiKey:
    """Unit-level ceiling enforcement for :func:`create_api_key`."""

    @pytest.mark.asyncio
    async def test_defaults_to_user_role(self) -> None:
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value="writer")
        user_id = uuid.uuid4()
        # A non-binding admin ceiling: the default still caps at the user role.
        _key_id, _secret, _prefix, role = await create_api_key(
            conn, user_id=user_id, name="laptop", ceiling="admin"
        )
        assert role == "writer"
        # The INSERT must carry the inferred role as the 6th bind.
        insert = next(
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO api_keys" in c.args[0]
        )
        assert insert.args[6] == "writer"

    @pytest.mark.asyncio
    async def test_accepts_role_at_or_below_ceiling(self) -> None:
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value="admin")
        _key_id, _secret, _prefix, role = await create_api_key(
            conn, user_id=uuid.uuid4(), name="ro", role="viewer", ceiling="admin"
        )
        assert role == "viewer"

    @pytest.mark.asyncio
    async def test_rejects_role_above_ceiling(self) -> None:
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value="writer")
        with pytest.raises(RoleCeilingError):
            await create_api_key(
                conn, user_id=uuid.uuid4(), name="bad", role="admin", ceiling="writer"
            )
        # And no INSERT fires when the ceiling check rejects.
        sqls = [c.args[0] for c in conn.execute.call_args_list]
        assert not any("INSERT INTO api_keys" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_lookup_error_when_user_missing(self) -> None:
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value=None)
        with pytest.raises(LookupError):
            await create_api_key(conn, user_id=uuid.uuid4(), name="x", ceiling="writer")

    @pytest.mark.asyncio
    async def test_ceiling_caps_below_user_role(self) -> None:
        # The owning user is an admin, but the presented credential is
        # viewer-scoped: minting admin must fail on the ceiling, not the
        # user's standing role. This is the privilege-escalation guard.
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value="admin")
        with pytest.raises(RoleCeilingError):
            await create_api_key(
                conn, user_id=uuid.uuid4(), name="bad", role="admin", ceiling="viewer"
            )
        sqls = [c.args[0] for c in conn.execute.call_args_list]
        assert not any("INSERT INTO api_keys" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_ceiling_defaults_the_minted_role(self) -> None:
        # With no explicit role, the mint defaults to the effective ceiling
        # (the weaker of user role and ceiling), not the user's role.
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value="admin")
        _key_id, _secret, _prefix, role = await create_api_key(
            conn, user_id=uuid.uuid4(), name="scoped", ceiling="viewer"
        )
        assert role == "viewer"


class TestSetApiKeyRole:
    """Unit-level ceiling enforcement for :func:`set_api_key_role`."""

    @pytest.mark.asyncio
    async def test_updates_when_at_or_below_ceiling(self) -> None:
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value="writer")
        conn.execute = AsyncMock(return_value="UPDATE 1")
        updated = await set_api_key_role(
            conn,
            key_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            role="writer",
            ceiling="admin",
        )
        assert updated is True

    @pytest.mark.asyncio
    async def test_rejects_above_ceiling(self) -> None:
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value="writer")
        with pytest.raises(RoleCeilingError):
            await set_api_key_role(
                conn,
                key_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                role="admin",
                ceiling="writer",
            )
        # The UPDATE must not have run.
        sqls = [c.args[0] for c in conn.execute.call_args_list]
        assert not any("UPDATE api_keys" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_returns_false_when_row_not_owned(self) -> None:
        conn = make_conn()
        conn.fetchval = AsyncMock(return_value="writer")
        conn.execute = AsyncMock(return_value="UPDATE 0")
        updated = await set_api_key_role(
            conn,
            key_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            role="viewer",
            ceiling="writer",
        )
        assert updated is False

    @pytest.mark.asyncio
    async def test_ceiling_caps_retier_below_user_role(self) -> None:
        # Admin user, viewer-scoped presenting credential, owned live key:
        # promoting to admin must fail on the ceiling, not the user's role.
        conn = make_conn()
        conn.fetchval = AsyncMock(side_effect=["admin", 1])
        with pytest.raises(RoleCeilingError):
            await set_api_key_role(
                conn,
                key_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                role="admin",
                ceiling="viewer",
            )
        sqls = [c.args[0] for c in conn.execute.call_args_list]
        assert not any("UPDATE api_keys SET role" in s for s in sqls)


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
