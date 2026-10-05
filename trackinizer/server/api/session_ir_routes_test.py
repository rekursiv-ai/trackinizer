"""The IR read routes: what they return, and what they refuse.

Route-level behavior only -- the storage properties (idempotent ``idx``,
ciphertext isolation) are proven in ``store/session_ir_test.py`` against real
Postgres. What matters here is that a non-session id 404s, an out-of-range
window is rejected before it reaches the database, ``plaintext_only``
actually reaches the store, and every part the listing names is one the
records route reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import uuid

from fastapi import FastAPI

import httpx2
import pytest

from trackinizer.lib.agent.types.sessions import (
    SessionRecord,
    Thinking,
    UncategorizedRecord,
    UserMessage,
)
from trackinizer.lib.custom_json import convert, json_freeze
from trackinizer.server.api import session_ir_routes
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.server.store.session_ir import RecentSessionTurn, SessionManifest
from trackinizer.types.inquiries import AgentSession, Issue
from trackinizer.types.session_records import SessionRecordRow


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


_CIPHERTEXT = "gAAAAABqPBiCY9-vjMraAiiOTNS8xKmaodTJ4D2l6XR2pMszVFyz"


def _row(idx: int, record: SessionRecord | None = None) -> SessionRecordRow:
    """One stored row, for a store stub to return."""
    return SessionRecordRow.of(
        session_id=uuid.uuid4(),
        part=0,
        idx=idx,
        record=UserMessage(content=f"line {idx}") if record is None else record,
    )


class TestReadParts:
    def test_lists_every_part_in_order(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=AgentSession(cli="claude")),
        )
        monkeypatch.setattr(
            store,
            "read_session_manifests",
            AsyncMock(
                return_value=[
                    SessionManifest(
                        part=0,
                        name="a.jsonl",
                        metadata=json_freeze({}),
                        ir_id=uuid.uuid4(),
                        format="claude",
                        records=3,
                    ),
                    SessionManifest(
                        part=1,
                        name="b.jsonl",
                        metadata=json_freeze({}),
                        ir_id=uuid.uuid4(),
                        format="",
                        records=1,
                    ),
                ],
            ),
        )

        response = client.get(f"/api/sessions/{session_id}/parts")

        assert response.status_code == 200, response.text
        body = convert(response.json(), dict[str, object])
        parts = convert(body["parts"], list[object])
        assert [convert(convert(p, dict[str, object])["name"], str) for p in parts] == [
            "a.jsonl",
            "b.jsonl",
        ]

    def test_an_empty_format_is_reported_not_hidden(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``format=""`` is the signal a part can never be resumed.

        A caller decides resumability from this field, so an empty one must
        reach it verbatim rather than being omitted as falsy.
        """
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=AgentSession(cli="sh")),
        )
        monkeypatch.setattr(
            store,
            "read_session_manifests",
            AsyncMock(
                return_value=[
                    SessionManifest(
                        part=0,
                        name="pty",
                        metadata=json_freeze({}),
                        ir_id=uuid.uuid4(),
                        format="",
                        records=2,
                    ),
                ],
            ),
        )

        response = client.get(f"/api/sessions/{uuid.uuid4()}/parts")

        body = convert(response.json(), dict[str, object])
        parts = convert(body["parts"], list[object])
        assert convert(parts[0], dict[str, object])["format"] == ""

    def test_a_non_session_id_is_a_404(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An Issue is not a session, even though both are inquiries."""
        client, store, _engine = route_client
        monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=Issue()))

        response = client.get(f"/api/sessions/{uuid.uuid4()}/parts")

        assert response.status_code == 404


class TestReadRecords:
    def test_returns_a_parts_records(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=AgentSession(cli="claude")),
        )
        monkeypatch.setattr(
            store,
            "read_session_records",
            AsyncMock(return_value=[_row(0), _row(1)]),
        )

        response = client.get(f"/api/sessions/{uuid.uuid4()}/records?part=0")

        assert response.status_code == 200, response.text
        body = convert(response.json(), dict[str, object])
        assert convert(body["part"], int) == 0
        records = convert(body["records"], list[object])
        assert [
            convert(convert(r, dict[str, object])["idx"], int) for r in records
        ] == [0, 1]

    def test_ciphertext_rides_beside_the_payload(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The encrypted half is its own field, never inside ``payload``.

        A replay needs it back; search must never have seen it. Keeping it
        beside the payload is what lets one reader take both and another take
        neither.
        """
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=AgentSession(cli="codex")),
        )
        monkeypatch.setattr(
            store,
            "read_session_records",
            AsyncMock(
                return_value=[
                    _row(0, record=Thinking(content="visible", encrypted=_CIPHERTEXT)),
                ],
            ),
        )

        body = convert(
            client.get(f"/api/sessions/{uuid.uuid4()}/records").json(),
            dict[str, object],
        )
        record = convert(convert(body["records"], list[object])[0], dict[str, object])

        assert convert(record["ciphertext"], str) == _CIPHERTEXT
        assert _CIPHERTEXT not in str(record["payload"])

    def test_plaintext_only_reaches_the_store(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The flag is the caller's way to skip the largest column."""
        client, store, _engine = route_client
        read = AsyncMock(return_value=[])
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=AgentSession(cli="claude")),
        )
        monkeypatch.setattr(store, "read_session_records", read)

        _ = client.get(f"/api/sessions/{uuid.uuid4()}/records?plaintext_only=true")

        assert read.await_args is not None
        assert read.await_args.kwargs["plaintext_only"] is True

    def test_after_idx_pages_without_an_offset(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Paging is a cursor, not an offset.

        A capture appends while a reader pages, so an offset would re-window
        on every growth; an exclusive ``idx`` bound is stable.
        """
        client, store, _engine = route_client
        read = AsyncMock(return_value=[])
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=AgentSession(cli="claude")),
        )
        monkeypatch.setattr(store, "read_session_records", read)

        _ = client.get(f"/api/sessions/{uuid.uuid4()}/records?after_idx=41")

        assert read.await_args is not None
        assert read.await_args.kwargs["after_idx"] == 41

    @pytest.mark.parametrize("query", ["limit=0", "limit=100000"])
    def test_an_out_of_range_window_is_rejected(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        query: str,
    ) -> None:
        """Bad bounds fail at the boundary, before any query runs."""
        client, store, _engine = route_client
        read = AsyncMock(return_value=[])
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=AgentSession(cli="claude")),
        )
        monkeypatch.setattr(store, "read_session_records", read)

        response = client.get(f"/api/sessions/{uuid.uuid4()}/records?{query}")

        assert response.status_code == 400
        assert not read.await_count


class TestRecentTurns:
    def test_returns_recent_messages_with_part_and_position(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=AgentSession(cli="codex")),
        )
        read = AsyncMock(
            return_value=[
                RecentSessionTurn(
                    part=0,
                    idx=4,
                    kind="UserMessage",
                    content="question",
                ),
                RecentSessionTurn(
                    part=1,
                    idx=2,
                    kind="AssistantMessage",
                    content="answer",
                ),
            ],
        )
        monkeypatch.setattr(store, "read_recent_session_turns", read)

        response = client.get(f"/api/sessions/{session_id}/turns?limit=2")

        assert response.status_code == 200, response.text
        assert response.json() == {
            "turns": [
                {"part": 0, "idx": 4, "kind": "UserMessage", "content": "question"},
                {"part": 1, "idx": 2, "kind": "AssistantMessage", "content": "answer"},
            ],
        }
        read.assert_awaited_once_with(session_id, limit=2)

    @pytest.mark.parametrize("limit", [0, 101])
    def test_rejects_invalid_limit(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        limit: int,
    ) -> None:
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=AgentSession(cli="codex")),
        )
        read = AsyncMock(return_value=[])
        monkeypatch.setattr(store, "read_recent_session_turns", read)

        response = client.get(f"/api/sessions/{uuid.uuid4()}/turns?limit={limit}")

        assert response.status_code == 400
        read.assert_not_awaited()


class TestViewerOwnedCapture:
    @pytest.mark.parametrize("owns_session", [True, False])
    def test_viewer_may_append_only_to_own_agent_session(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        owns_session: bool,
    ) -> None:
        client, store, _engine = route_client
        install_identity(make_test_identity(role="viewer"))
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(
                return_value=AgentSession(
                    cli="codex",
                    opened_by_api_key_id=TEST_API_KEY_ID
                    if owns_session
                    else uuid.uuid4(),
                ),
            ),
        )
        append = AsyncMock(return_value=(0, 0, 0))
        monkeypatch.setattr(store, "append_session_records", append)

        response = client.post(
            f"/api/sessions/{uuid.uuid4()}/records",
            json={"records": []},
        )

        assert response.status_code == (200 if owns_session else 403), response.text
        assert append.await_count == int(owns_session)


class TestLegacyPart:
    """Migration 020 backfilled the retired event log at the reserved ``part = -1``."""

    def test_every_listed_part_is_readable(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The listing and the records route agree on which parts exist.

        A reader walks the listing and fetches each part it names, so a part
        the listing returns and the records route refuses fails the whole
        transcript: the old UI awaits every part together, and one refusal
        blanks all of them.
        """
        client, store, _engine = route_client
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=AgentSession(cli="claude")),
        )
        monkeypatch.setattr(
            store,
            "read_session_manifests",
            AsyncMock(
                return_value=[
                    SessionManifest(
                        part=-1,
                        name="legacy",
                        metadata=json_freeze({}),
                        ir_id=session_id,
                        format="",
                        records=2,
                    ),
                    SessionManifest(
                        part=0,
                        name="s.jsonl",
                        metadata=json_freeze({}),
                        ir_id=uuid.uuid4(),
                        format="claude",
                        records=1,
                    ),
                ],
            ),
        )
        read = AsyncMock(return_value=[])
        monkeypatch.setattr(store, "read_session_records", read)

        parts = _listed_parts(client.get(f"/api/sessions/{session_id}/parts"))
        statuses = [
            client.get(f"/api/sessions/{session_id}/records?part={part}").status_code
            for part in parts
        ]

        assert parts == [-1, 0]
        assert statuses == [200, 200]
        assert [call.kwargs["part"] for call in read.await_args_list] == [-1, 0]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_legacy_transcript_reads_every_listed_part(integ_store: Store) -> None:
    """A session captured before the IR and resumed after reads in full, over HTTP.

    It holds both numbering schemes: the backfilled turns at ``part = -1``
    under the manifest 020 wrote, and the resumed run's file at ``part = 0``.
    Walking the listing and reading each part it names must return every
    record of both.
    """
    session_id = await _legacy_session(integ_store, turns=2)
    part = await integ_store.upsert_session_manifest(
        session_id,
        name="s.jsonl",
        metadata=json_freeze({}),
        ir_id=uuid.uuid4(),
        format="claude",
        records=1,
    )
    await integ_store.append_session_records(
        session_id,
        [
            SessionRecordRow.of(
                session_id=session_id,
                part=part,
                idx=0,
                record=UserMessage(content="resumed"),
            ),
        ],
    )
    app = FastAPI()
    app.include_router(session_ir_routes.router)
    app.state.store = integ_store
    app.dependency_overrides[current_user] = _viewer
    # Not raising app exceptions, so a refusal reads as the status a browser gets.
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as http:
        listing = await http.get(f"/api/sessions/{session_id}/parts")
        assert listing.status_code == 200, listing.text
        parts = _listed_parts(listing)
        pages = [
            await http.get(
                f"/api/sessions/{session_id}/records",
                params={"part": listed},
            )
            for listed in parts
        ]

    assert [page.status_code for page in pages] == [200, 200], [
        page.text for page in pages
    ]
    assert [
        (
            convert(body["part"], int),
            convert(convert(record, dict[str, object])["idx"], int),
        )
        for body in (convert(page.json(), dict[str, object]) for page in pages)
        for record in convert(body["records"], list[object])
    ] == [(-1, 0), (-1, 1), (0, 0)]


def _listed_parts(response: httpx2.Response) -> list[int]:
    """Return the ``part`` of every entry a ``GET .../parts`` response lists."""
    body = convert(response.json(), dict[str, object])
    return [
        convert(convert(entry, dict[str, object])["part"], int)
        for entry in convert(body["parts"], list[object])
    ]


# The rows migration 020 writes for a session captured before the IR: its turns at the
# reserved ``part = -1`` and one manifest bounding them, named ``legacy`` with no format.
# Written directly because the table 020 reads no longer exists (021 dropped it);
# ``schema_backfill_test.py`` runs 020 itself and pins this shape.
async def _legacy_session(store: Store, *, turns: int) -> uuid.UUID:
    """Return an AgentSession holding ``turns`` backfilled records at part ``-1``."""
    session_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'legacy')",
            session_id,
        )
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, -1, 'legacy', '{}'::json, $1, '', $2)",
            session_id,
            turns,
        )
    await store.append_session_records(
        session_id,
        [
            SessionRecordRow.of(
                session_id=session_id,
                part=-1,
                idx=idx,
                record=UncategorizedRecord(
                    kind="legacy/UserMessage",
                    payload={"text": f"turn {idx}"},
                ),
            )
            for idx in range(turns)
        ],
    )
    return session_id


async def _viewer() -> AuthIdentity:
    """Resolve every request to a read-only principal, as a browser viewer is."""
    return make_test_identity(role="viewer")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
