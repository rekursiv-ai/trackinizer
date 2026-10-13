"""The fake server answers as the real one does, where ``trax helper`` relies on it."""

from __future__ import annotations

from datetime import UTC, datetime

import re
import uuid

import pytest

from trackinizer.lib.agent.types.sessions import AgentToAgentMessage
from trackinizer.trax.run.fake_chat_server import FakeChatServer
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_science_chat import CHAT_HELPER_CLI
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody
from trackinizer.wire.wire_sessions import SessionStart


CONVERSATION = uuid.UUID("11111111-1111-4111-8111-111111111111")


def test_records_are_read_after_a_position_from_the_part_asked_for() -> None:
    server = FakeChatServer()
    session = server.seed(CONVERSATION, records=[_said(0), _said(1), _said(2)])

    after_first = server.read_session_records(
        session,
        part=0,
        after_idx=0,
        plaintext_only=True,
    )
    everything = server.read_session_records(
        session,
        part=0,
        after_idx=-1,
        plaintext_only=True,
    )

    assert [record.idx for record in after_first] == [1, 2]
    assert [record.idx for record in everything] == [0, 1, 2]


def test_the_helper_reads_plaintext_only() -> None:
    server = FakeChatServer()
    session = server.seed(CONVERSATION, records=[_said(0)])

    with pytest.raises(ValueError, match=r"^Expected plaintext_only\.$"):
        _ = server.read_session_records(
            session,
            part=0,
            after_idx=-1,
            plaintext_only=False,
        )


@pytest.mark.parametrize(
    ("name", "idx", "records", "error"),
    [
        ("other.jsonl", 0, 1, "Expected manifest.name == name."),
        (
            "turns.jsonl",
            1,
            1,
            "Expected [record.idx for record in records] == [len(part)].",
        ),
        ("turns.jsonl", 0, 2, "Expected manifest.records == len(part) + 1."),
    ],
    ids=["another file", "out of place", "miscounted"],
)
def test_an_append_the_server_would_refuse_is_refused_and_keeps_nothing(
    *,
    name: str,
    idx: int,
    records: int,
    error: str,
) -> None:
    server = FakeChatServer()
    session = server.seed(CONVERSATION, records=[])

    with pytest.raises(ValueError, match=f"^{re.escape(error)}$"):
        server.append_records(
            session,
            name="turns.jsonl",
            manifest=ManifestBody(name=name, ir_id=uuid.UUID(int=1), records=records),
            records=[_said(idx)],
        )

    assert server.records(CONVERSATION) == []


def test_a_session_with_no_cli_session_id_is_opened_anew_each_time() -> None:
    server = FakeChatServer()
    start = SessionStart(cli=CHAT_HELPER_CLI)

    first, second = server.session_start(start), server.session_start(start)

    assert (first.seq, second.seq, len({first.id, second.id})) == (0, 0, 2)
    assert server.service().id == second.id


def test_the_helper_drains_its_own_service_session_and_no_other() -> None:
    server = FakeChatServer(batches=[[("hi", None, None, None)]])
    chat = server.seed(CONVERSATION, records=[])
    service = server.session_start(SessionStart(cli=CHAT_HELPER_CLI)).id

    with pytest.raises(
        ValueError,
        match=r"^Expected session_id == self\.service\(\)\.id\.$",
    ):
        _ = server.drain_inbound(chat)

    assert server.drain_inbound(service) == [("hi", None, None, None)]


def test_the_helper_reads_only_its_profile_and_lists_only_sessions() -> None:
    server = FakeChatServer()

    with pytest.raises(ValueError, match=r'^Expected path == "/api/me/profile"\.$'):
        _ = server.get("/api/me")
    with pytest.raises(ValueError, match=r'^Expected kind == "AgentSession"\.$'):
        _ = server.list_kind("Issue", limit=10, offset=0, filters=[])

    assert server.listings == []


def _said(idx: int) -> RecordBody:
    """Return the record of one line said in a chat, at position ``idx``."""
    return RecordBody.of(
        SessionRecordRow.of(
            session_id=uuid.UUID(int=0),
            part=0,
            idx=idx,
            record=AgentToAgentMessage(
                sender="ada@x",
                content=f"line {idx}",
                timestamp=datetime.now(UTC).isoformat(),
            ),
        ),
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
