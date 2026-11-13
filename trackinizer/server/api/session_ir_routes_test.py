"""The IR read routes: what they return, and what they refuse.

Route-level behavior only -- the storage properties (idempotent ``idx``,
ciphertext isolation) are proven in ``store/session_ir_test.py`` against real
Postgres. What matters here is that a non-session id 404s, an out-of-range
window is rejected before it reaches the database, ``plaintext_only``
actually reaches the store, and every part the listing names is one the
records route reads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, TypedDict, cast
from unittest.mock import AsyncMock, Mock, call

import asyncio
import uuid

from fastapi import FastAPI
from treekle import from_plain, immutable

import httpx2
import pytest

from trackinizer.sagent.session.types import (
    SessionRecord,
    Thinking,
    TokenUsage,
    UncategorizedRecord,
    UserMessage,
)
from trackinizer.server.api import session_ir_routes
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.server.session_relay import SessionRelay
from trackinizer.server.store.session_ir import (
    AppendedPart,
    PartFile,
    RecentSessionTurn,
    SessionManifest,
    StalePartError,
)
from trackinizer.types.inquiries import AgentSession, Issue
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.wire_session_ir import (
    CLIENT_OUTDATED,
    ManifestBody,
    ReadRecordsResponse,
    RecordBody,
)
from trackinizer.wire.wire_session_relay import STALE_PART, Opened, RecordLine


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


class _Indexed(TypedDict):
    idx: int


class _Page(TypedDict):
    part: int
    records: list[_Indexed]


class _Body(TypedDict):
    kind: str
    payload: dict[str, object]
    text: str


class _BodyPage(TypedDict):
    records: list[_Body]


class _Part(TypedDict):
    part: int


class _Parts(TypedDict):
    parts: list[_Part]


_CIPHERTEXT = "gAAAAABqPBiCY9-vjMraAiiOTNS8xKmaodTJ4D2l6XR2pMszVFyz"
_NOTHING: Final = AppendedPart(part=None, written=(), skipped=0)
"""What an upload that named no file stored."""


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
            AsyncMock(
                return_value=AgentSession(
                    cli="claude",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
        )
        monkeypatch.setattr(
            store,
            "read_session_manifests",
            AsyncMock(
                return_value=[
                    SessionManifest(
                        part=0,
                        name="a.jsonl",
                        metadata=immutable({}),
                        ir_id=uuid.uuid4(),
                        format="claude",
                        records=3,
                    ),
                    SessionManifest(
                        part=1,
                        name="b.jsonl",
                        metadata=immutable({}),
                        ir_id=uuid.uuid4(),
                        format="",
                        records=1,
                    ),
                ],
            ),
        )

        response = client.get(f"/api/sessions/{session_id}/parts")

        assert response.status_code == 200, response.text
        body = from_plain(response.json(), dict[str, object])
        parts = from_plain(body["parts"], list[object])
        assert [
            from_plain(from_plain(p, dict[str, object])["name"], str) for p in parts
        ] == [
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
            AsyncMock(
                return_value=AgentSession(
                    cli="sh",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
        )
        monkeypatch.setattr(
            store,
            "read_session_manifests",
            AsyncMock(
                return_value=[
                    SessionManifest(
                        part=0,
                        name="pty",
                        metadata=immutable({}),
                        ir_id=uuid.uuid4(),
                        format="",
                        records=2,
                    ),
                ],
            ),
        )

        response = client.get(f"/api/sessions/{uuid.uuid4()}/parts")

        body = from_plain(response.json(), dict[str, object])
        parts = from_plain(body["parts"], list[object])
        assert from_plain(parts[0], dict[str, object])["format"] == ""

    def test_a_non_session_id_is_a_404(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An Issue is not a session, even though both are inquiries."""
        client, store, _engine = route_client
        read = AsyncMock(return_value=Issue())
        monkeypatch.setattr(store, "get_inquiry", read)
        asked = uuid.uuid4()

        response = client.get(f"/api/sessions/{asked}/parts")

        assert response.status_code == 404
        assert response.json() == {"detail": f"unknown session {asked}"}
        read.assert_awaited_once_with(asked)


class TestReadUsage:
    def test_totals_every_part_paged_by_its_own_format(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(
                return_value=AgentSession(
                    cli="sagent",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
        )
        monkeypatch.setattr(
            store,
            "read_session_manifests",
            AsyncMock(
                return_value=[
                    SessionManifest(
                        part=part,
                        name=f"p{part}",
                        metadata=immutable({}),
                        ir_id=uuid.uuid4(),
                        format="sagent",
                        records=1,
                    )
                    for part in (0, 1)
                ],
            ),
        )
        spent = _row(0, TokenUsage(info=immutable({"spend_usd": 0.5})))
        read = AsyncMock(side_effect=[[spent], [spent]])
        monkeypatch.setattr(store, "read_session_records", read)

        response = client.get(f"/api/sessions/{uuid.uuid4()}/usage")

        assert response.status_code == 200, response.text
        assert from_plain(response.json(), dict[str, object])["usd"] == 1.0
        assert [call.kwargs["part"] for call in read.await_args_list] == [0, 1]

    def test_a_full_page_reads_on_after_its_last_record(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(
                return_value=AgentSession(
                    cli="sagent",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
        )
        monkeypatch.setattr(
            store,
            "read_session_manifests",
            AsyncMock(
                return_value=[
                    SessionManifest(
                        part=0,
                        name="p0",
                        metadata=immutable({}),
                        ir_id=uuid.uuid4(),
                        format="sagent",
                        records=MAX_LIST_LIMIT + 1,
                    ),
                ],
            ),
        )
        full = [_row(MAX_LIST_LIMIT - 1)] * MAX_LIST_LIMIT
        spent = _row(MAX_LIST_LIMIT, TokenUsage(info=immutable({"spend_usd": 0.5})))
        read = AsyncMock(side_effect=[full, [spent]])
        monkeypatch.setattr(store, "read_session_records", read)

        response = client.get(f"/api/sessions/{uuid.uuid4()}/usage")

        assert response.status_code == 200, response.text
        assert from_plain(response.json(), dict[str, object])["usd"] == 0.5
        assert [call.kwargs["after_idx"] for call in read.await_args_list] == [
            -1,
            MAX_LIST_LIMIT - 1,
        ]


class TestAppendRevives:
    @pytest.mark.parametrize("ended", [True, False])
    def test_an_ended_session_that_uploads_again_is_reopened_first(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        *,
        ended: bool,
    ) -> None:
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(
                return_value=AgentSession(
                    cli="claude",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                    ended=datetime(2026, 1, 2, tzinfo=UTC) if ended else None,
                ),
            ),
        )
        revive = AsyncMock(return_value=True)
        monkeypatch.setattr(session_ir_routes, "revive_if_reaped", revive)
        monkeypatch.setattr(
            store,
            "append_session_part",
            AsyncMock(return_value=_NOTHING),
        )
        session_id = uuid.uuid4()

        response = client.post(
            f"/api/sessions/{session_id}/records",
            json={"records": []},
        )

        assert response.status_code == 200, response.text
        assert revive.await_args_list == (
            [call(store, session_id=session_id)] if ended else []
        )


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
            AsyncMock(
                return_value=AgentSession(
                    cli="claude",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
        )
        monkeypatch.setattr(
            store,
            "read_session_records",
            AsyncMock(return_value=[_row(0), _row(1)]),
        )

        response = client.get(f"/api/sessions/{uuid.uuid4()}/records?part=0")

        assert response.status_code == 200, response.text
        body = from_plain(response.json(), dict[str, object])
        assert from_plain(body["part"], int) == 0
        records = from_plain(body["records"], list[object])
        assert [
            from_plain(from_plain(r, dict[str, object])["idx"], int) for r in records
        ] == [0, 1]

    def test_a_legacy_tool_call_row_is_returned_in_the_current_shape(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The row keeps its stored ``kind``; only its payload is converted."""
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(
                return_value=AgentSession(
                    cli="claude",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
        )
        legacy = SessionRecordRow(
            session_id=uuid.uuid4(),
            idx=0,
            kind="UncategorizedToolResult",
            payload=immutable(
                {
                    "py/object": "UncategorizedToolResult",
                    "call_id": "c",
                    "content": "found it",
                },
            ),
            text="found it",
        )
        offloaded = SessionRecordRow(
            session_id=uuid.uuid4(),
            idx=1,
            kind="ReadResult",
            payload=immutable({"$body": "offloaded"}),
            text="a.py",
        )
        monkeypatch.setattr(
            store,
            "read_session_records",
            AsyncMock(return_value=[legacy, offloaded]),
        )

        body = from_plain(
            client.get(f"/api/sessions/{uuid.uuid4()}/records").json(),
            _BodyPage,
        )

        assert [
            (record["kind"], record["payload"], record["text"])
            for record in body["records"]
        ] == [
            (
                "UncategorizedToolResult",
                {
                    "py/object": "ToolResult",
                    "call_id": "c",
                    "state": "done",
                    "stdout": ["found it"],
                    "result": {"py/object": "UncategorizedToolResult"},
                    "extra": {},
                },
                "found it",
            ),
            ("ReadResult", {"$body": "offloaded"}, "a.py"),
        ]

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
            AsyncMock(
                return_value=AgentSession(
                    cli="codex",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
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

        body = from_plain(
            client.get(f"/api/sessions/{uuid.uuid4()}/records").json(),
            dict[str, object],
        )
        record = from_plain(
            from_plain(body["records"], list[object])[0],
            dict[str, object],
        )

        assert from_plain(record["ciphertext"], str) == _CIPHERTEXT
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
            AsyncMock(
                return_value=AgentSession(
                    cli="claude",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
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
            AsyncMock(
                return_value=AgentSession(
                    cli="claude",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
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
            AsyncMock(
                return_value=AgentSession(
                    cli="claude",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
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
            AsyncMock(
                return_value=AgentSession(
                    cli="codex",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
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
            AsyncMock(
                return_value=AgentSession(
                    cli="codex",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
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
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID
                    if owns_session
                    else uuid.uuid4(),
                ),
            ),
        )
        append = AsyncMock(return_value=_NOTHING)
        monkeypatch.setattr(store, "append_session_part", append)

        response = client.post(
            f"/api/sessions/{uuid.uuid4()}/records",
            json={"records": []},
        )

        assert response.status_code == (200 if owns_session else 403), response.text
        assert append.await_count == int(owns_session)


def _user_message(module: str) -> dict[str, object]:
    """Return a stored ``UserMessage`` payload naming its class in ``module``."""
    return {"py/object": f"{module}UserMessage", "content": "hi", "extra": {}}


def _append_one(client: TestClient, *, kind: str, payload: object) -> httpx2.Response:
    """Post one record of ``kind`` holding ``payload`` to a fresh part."""
    return client.post(
        f"/api/sessions/{uuid.uuid4()}/records",
        json={
            "name": "s.jsonl",
            "manifest": {"name": "s.jsonl", "ir_id": str(uuid.uuid4())},
            "records": [{"idx": 0, "kind": kind, "payload": payload}],
        },
    )


class TestRetiredRecordFormat:
    """A client older than short class tags is told to update."""

    # The export renames ``loop.`` modules under ``trackinizer.``, so a case per
    # package root would be two identical cases in the public tree.
    @pytest.mark.parametrize(
        "module",
        ["trackinizer.sagent.session.types.", "trackinizer.lib.agent.types.sessions."],
        ids=["import-path", "retired-module"],
    )
    def test_a_record_naming_its_class_by_import_path_is_refused_before_any_write(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        module: str,
    ) -> None:
        """426 ``client_outdated``: an old client's sink shows the message and keeps the run."""
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(
                return_value=AgentSession(
                    cli="claude",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
        )
        append = AsyncMock(return_value=_NOTHING)
        monkeypatch.setattr(store, "append_session_part", append)

        response = _append_one(
            client,
            kind="UserMessage",
            payload=_user_message(module),
        )

        assert response.status_code == 426, response.text
        detail = from_plain(response.json(), dict[str, dict[str, str]])["detail"]
        assert detail["code"] == CLIENT_OUTDATED
        assert detail["message"] == (
            "Update this client and trax (uv tool upgrade --all): it writes "
            "session records in a retired format"
        )
        append.assert_not_awaited()

    def test_a_record_in_the_current_vocabulary_is_appended(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Neither text naming a module nor provider JSON spelling a tag is a class tag."""
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(
                return_value=AgentSession(
                    cli="claude",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
        )
        append = AsyncMock(return_value=_NOTHING)
        monkeypatch.setattr(store, "append_session_part", append)
        payload = {
            **_user_message(""),
            "content": "see trackinizer.lib.agent.types.sessions.UserMessage",
            "extra": {"provider": {"py/object": "a.B"}},
        }

        response = _append_one(client, kind="UserMessage", payload=payload)

        assert response.status_code == 200, response.text
        append.assert_awaited_once()


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
            AsyncMock(
                return_value=AgentSession(
                    cli="claude",
                    audience="open",
                    opened_by_api_key_id=TEST_API_KEY_ID,
                ),
            ),
        )
        monkeypatch.setattr(
            store,
            "read_session_manifests",
            AsyncMock(
                return_value=[
                    SessionManifest(
                        part=-1,
                        name="legacy",
                        metadata=immutable({}),
                        ir_id=session_id,
                        format="",
                        records=2,
                    ),
                    SessionManifest(
                        part=0,
                        name="s.jsonl",
                        metadata=immutable({}),
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
        metadata=immutable({}),
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
        (body["part"], record["idx"])
        for body in (from_plain(page.json(), _Page) for page in pages)
        for record in body["records"]
    ] == [(-1, 0), (-1, 1), (0, 0)]


def _listed_parts(response: httpx2.Response) -> list[int]:
    """Return the ``part`` of every entry a ``GET .../parts`` response lists."""
    return [entry["part"] for entry in from_plain(response.json(), _Parts)["parts"]]


# The rows migration 020 writes for a session captured before the IR: its turns at the
# reserved ``part = -1`` and one manifest bounding them, named ``legacy`` with no format.
# Written directly because the table 020 reads no longer exists (021 dropped it);
# ``schema_backfill_test.py`` runs 020 itself and pins this shape.
async def _legacy_session(store: Store, *, turns: int) -> uuid.UUID:
    """Return the viewer's AgentSession holding ``turns`` records at part ``-1``."""
    session_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "$2, 'legacy', 'open')",
            session_id,
            TEST_USER_EMAIL,
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


# -- The four changes of Part 3: one transaction, stale parts, any key, the push ---------


def _sagent_body(*indices: int, records: int, ir_id: uuid.UUID) -> dict[str, object]:
    """Return a sagent journal's upload of the records at ``indices``."""
    return {
        "name": "a.jsonl",
        "manifest": ManifestBody(
            name="a.jsonl",
            ir_id=ir_id,
            format="sagent",
            records=records,
        ).model_dump(mode="json"),
        "records": [
            RecordBody.of(_row(idx)).model_dump(mode="json") for idx in indices
        ],
    }


@pytest.mark.parametrize("watched", [False, True], ids=["unwatched", "watched"])
def test_an_upload_is_one_store_call_whose_records_reach_only_a_watching_viewer(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    watched: bool,
) -> None:
    client, store, _engine = route_client
    session_id, ir_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(
        store,
        "get_inquiry",
        AsyncMock(
            return_value=AgentSession(
                cli="sagent",
                opened_by_api_key_id=TEST_API_KEY_ID,
            ),
        ),
    )
    append = AsyncMock(
        return_value=AppendedPart(part=4, written=(1, 2), skipped=1),
    )
    monkeypatch.setattr(store, "append_session_part", append)
    # A read returns what the part's bound lets through: here 0 and 1 of 0..2.
    read = AsyncMock(return_value=[_row(0), _row(1)])
    monkeypatch.setattr(store, "read_session_records", read)
    relay = SessionRelay()
    publish = Mock()
    monkeypatch.setattr(relay, "publish_records", publish)
    monkeypatch.setattr(relay, "watched", Mock(return_value=watched))
    patch_state(monkeypatch, "session_relay", relay)

    response = client.post(
        f"/api/sessions/{session_id}/records",
        json=_sagent_body(0, 1, 2, records=2, ir_id=ir_id),
    )

    assert response.status_code == 200, response.text
    assert from_plain(response.json(), dict[str, object]) == {
        "part": 4,
        "written": 2,
        "skipped": 1,
    }
    assert append.await_args is not None
    assert append.await_args.kwargs["file"] == PartFile(
        name="a.jsonl",
        metadata=immutable({}),
        ir_id=ir_id,
        format="sagent",
        records=2,
    )
    assert append.await_args.kwargs["api_key_id"] == TEST_API_KEY_ID
    if not watched:
        read.assert_not_awaited()
        publish.assert_not_called()
        return
    # Read back from the first record written: one the read returns but this write
    # did not insert is not pushed again.
    read.assert_awaited_once_with(session_id, part=4, after_idx=0, limit=2)
    publish.assert_called_once_with(
        session_id,
        part=4,
        records=[RecordBody.of(_row(1))],
    )


def test_a_legacy_tool_call_an_upload_stored_reaches_a_viewer_in_the_current_shape(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A viewer is pushed a record as ``GET .../records`` returns it, kind kept."""
    client, store, _engine = route_client
    monkeypatch.setattr(
        store,
        "get_inquiry",
        AsyncMock(
            return_value=AgentSession(
                cli="sagent",
                opened_by_api_key_id=TEST_API_KEY_ID,
            ),
        ),
    )
    monkeypatch.setattr(
        store,
        "append_session_part",
        AsyncMock(return_value=AppendedPart(part=0, written=(0,), skipped=0)),
    )
    # A client older than ``ToolResult`` still sends its former record's payload.
    legacy = SessionRecordRow(
        session_id=uuid.uuid4(),
        idx=0,
        kind="BashResult",
        payload=immutable(
            {"py/object": "BashResult", "call_id": "c", "stdout": "hi\n"},
        ),
        text="hi\n",
    )
    monkeypatch.setattr(store, "read_session_records", AsyncMock(return_value=[legacy]))
    relay = SessionRelay()
    publish = Mock()
    monkeypatch.setattr(relay, "publish_records", publish)
    monkeypatch.setattr(relay, "watched", Mock(return_value=True))
    patch_state(monkeypatch, "session_relay", relay)

    response = client.post(
        f"/api/sessions/{uuid.uuid4()}/records",
        json=_sagent_body(0, records=1, ir_id=uuid.uuid4()),
    )

    assert response.status_code == 200, response.text
    assert publish.call_args is not None
    [pushed] = cast(list[RecordBody], publish.call_args.kwargs["records"])
    assert (pushed.kind, pushed.payload) == (
        "BashResult",
        immutable(
            {
                "py/object": "ToolResult",
                "call_id": "c",
                "state": "done",
                "stdout": ["hi\n"],
                "result": {"py/object": "BashResult"},
                "extra": {},
            },
        ),
    )


def test_a_write_to_a_part_another_process_moved_on_from_is_refused_as_stale(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _engine = route_client
    monkeypatch.setattr(
        store,
        "get_inquiry",
        AsyncMock(
            return_value=AgentSession(
                cli="sagent",
                opened_by_api_key_id=TEST_API_KEY_ID,
            ),
        ),
    )
    monkeypatch.setattr(
        store,
        "append_session_part",
        AsyncMock(side_effect=StalePartError("part 0 is not its newest, 1")),
    )

    response = client.post(
        f"/api/sessions/{uuid.uuid4()}/records",
        json=_sagent_body(0, records=1, ir_id=uuid.uuid4()),
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {"code": STALE_PART, "message": "part 0 is not its newest, 1"},
    }


async def _sagent_session(store: Store, *, cli_session_id: str = "") -> uuid.UUID:
    """Return the test user's open session, under ``cli_session_id`` if any."""
    session_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience, agentsession_cli, agentsession_cli_session_id) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "$2, 's', 'open', 'sagent', NULLIF($3, ''))",
            session_id,
            TEST_USER_EMAIL,
            cli_session_id,
        )
    return session_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_each_stored_record_is_pushed_to_open_viewers_once(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    relay = SessionRelay()
    patch_state(monkeypatch, "session_relay", relay)
    session_id, ir_id = await _sagent_session(store), uuid.uuid4()
    viewer = relay.lines(session_id, allowed=AsyncMock(return_value=True))
    assert isinstance(await anext(viewer), Opened)

    for indices in ((0, 1), (0, 1, 2)):
        response = await client.post(
            f"/api/sessions/{session_id}/records",
            json=_sagent_body(*indices, records=len(indices), ir_id=ir_id),
        )
        assert response.status_code == 200, response.text
    read = await client.get(f"/api/sessions/{session_id}/records", params={"part": 0})
    stored = ReadRecordsResponse.model_validate(read.json()).records

    pushed = [await asyncio.wait_for(anext(viewer), 1) for _ in stored]
    assert pushed == [RecordLine(part=0, record=record) for record in stored]
    await viewer.aclose()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
