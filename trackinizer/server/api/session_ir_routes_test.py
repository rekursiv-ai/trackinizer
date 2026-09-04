"""The IR read routes: what they return, and what they refuse.

Route-level behavior only -- the storage properties (idempotent ``idx``,
ciphertext isolation) are proven in ``store/session_ir_test.py`` against real
Postgres. What matters here is that a non-session id 404s, an out-of-range
window is rejected before it reaches the database, and ``plaintext_only``
actually reaches the store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import uuid

import pytest

from trackinizer.lib.agent.types.sessions import (
    SessionRecord,
    Thinking,
    UserMessage,
)
from trackinizer.lib.custom_json import json_freeze
from trackinizer.server.store.session_ir import SessionManifest
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
            store, "get_inquiry", AsyncMock(return_value=AgentSession(cli="claude"))
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
                ]
            ),
        )

        response = client.get(f"/api/sessions/{session_id}/parts")

        assert response.status_code == 200, response.text
        assert [p["name"] for p in response.json()["parts"]] == ["a.jsonl", "b.jsonl"]

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
            store, "get_inquiry", AsyncMock(return_value=AgentSession(cli="sh"))
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
                    )
                ]
            ),
        )

        response = client.get(f"/api/sessions/{uuid.uuid4()}/parts")

        assert response.json()["parts"][0]["format"] == ""

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
            store, "get_inquiry", AsyncMock(return_value=AgentSession(cli="claude"))
        )
        monkeypatch.setattr(
            store,
            "read_session_records",
            AsyncMock(return_value=[_row(0), _row(1)]),
        )

        response = client.get(f"/api/sessions/{uuid.uuid4()}/records?part=0")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["part"] == 0
        assert [r["idx"] for r in body["records"]] == [0, 1]

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
            store, "get_inquiry", AsyncMock(return_value=AgentSession(cli="codex"))
        )
        monkeypatch.setattr(
            store,
            "read_session_records",
            AsyncMock(
                return_value=[
                    _row(0, record=Thinking(content="visible", encrypted=_CIPHERTEXT))
                ]
            ),
        )

        record = client.get(f"/api/sessions/{uuid.uuid4()}/records").json()["records"][
            0
        ]

        assert record["ciphertext"] == _CIPHERTEXT
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
            store, "get_inquiry", AsyncMock(return_value=AgentSession(cli="claude"))
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
            store, "get_inquiry", AsyncMock(return_value=AgentSession(cli="claude"))
        )
        monkeypatch.setattr(store, "read_session_records", read)

        _ = client.get(f"/api/sessions/{uuid.uuid4()}/records?after_idx=41")

        assert read.await_args is not None
        assert read.await_args.kwargs["after_idx"] == 41

    @pytest.mark.parametrize("query", ["limit=0", "limit=100000", "part=-1"])
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
            store, "get_inquiry", AsyncMock(return_value=AgentSession(cli="claude"))
        )
        monkeypatch.setattr(store, "read_session_records", read)

        response = client.get(f"/api/sessions/{uuid.uuid4()}/records?{query}")

        assert response.status_code == 400
        assert not read.await_count


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
