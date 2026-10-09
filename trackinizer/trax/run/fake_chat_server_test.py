"""The fake server answers as the real one does, where ``trax helper`` relies on it."""

from __future__ import annotations

from datetime import UTC, datetime

import uuid

import pytest

from trackinizer.lib.agent.types.sessions import AgentToAgentMessage
from trackinizer.trax.run.fake_chat_server import FakeChatServer
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_session_ir import RecordBody


CONVERSATION = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _said(idx: int) -> RecordBody:
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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
