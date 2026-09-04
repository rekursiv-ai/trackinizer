"""Tests for the session lifecycle, messaging, and feed wire contract."""

from __future__ import annotations

from datetime import UTC, datetime

import uuid

from pydantic import ValidationError

import pytest

from trackinizer.wire.wire_sessions import (
    FeedCursor,
    FeedEvent,
    InboundDrainItem,
    InboundEnqueueRequest,
    SendMessage,
    SessionEnd,
    SessionStart,
    session_end_path,
    session_records_path,
)


_NOW = datetime(2026, 8, 24, 20, 29, tzinfo=UTC)


class TestSessionStart:
    def test_requires_cli(self) -> None:
        with pytest.raises(ValidationError):
            # Intentionally omit the required ``cli`` to assert the validator.
            SessionStart()  # ty: ignore[missing-argument]  # pyright: ignore[reportCallIssue]

    def test_minimal_start(self) -> None:
        body = SessionStart(cli="codex")
        assert body.cli == "codex"
        assert body.cli_session_id is None

    def test_rejects_blank_cli(self) -> None:
        with pytest.raises(ValidationError):
            SessionStart(cli="")

    def test_rejects_whitespace_only_cli(self) -> None:
        # ``min_length=1`` alone admits "   "; a whitespace CLI is a client
        # bug (no CLI is named "   "). Mirror the rooms blank-rejection rule.
        for bad in ("   ", "\t", "\n"):
            with pytest.raises(ValidationError):
                SessionStart(cli=bad)

    def test_rejects_whitespace_only_cli_session_id(self) -> None:
        # ``cli_session_id`` is a dedup/scoping key matched verbatim; a
        # whitespace-only value is a client bug, rejected like ``cli``.
        for bad in ("   ", "\t", "\n"):
            with pytest.raises(ValidationError):
                SessionStart(cli="codex", cli_session_id=bad)
        # ``None`` (absent) stays valid.
        assert SessionStart(cli="codex", cli_session_id=None).cli_session_id is None

    def test_rejects_whitespace_only_actor(self) -> None:
        # ``actor`` becomes the granted routing name (the AgentSession owner),
        # matched verbatim; a whitespace-only value can never route and is a
        # client bug, rejected like ``cli`` / ``cli_session_id``.
        for bad in ("   ", "\t", "\n"):
            with pytest.raises(ValidationError):
                SessionStart(cli="codex", actor=bad)
        assert SessionStart(cli="codex", actor=None).actor is None


class TestSessionEnd:
    def test_rejects_whitespace_only_cli_session_id(self) -> None:
        for bad in ("   ", "\t", "\n"):
            with pytest.raises(ValidationError):
                SessionEnd(cli_session_id=bad)
        assert SessionEnd(cli_session_id=None).cli_session_id is None

    def test_rejects_whitespace_only_actor(self) -> None:
        for bad in ("   ", "\t", "\n"):
            with pytest.raises(ValidationError):
                SessionEnd(actor=bad)
        assert SessionEnd(actor=None).actor is None


class TestFeedEvent:
    def test_kind_is_open_not_a_closed_vocabulary(self) -> None:
        """The IR has 21 record kinds and gains more.

        A closed Literal here would 422 the console on the first record type
        added upstream, so the feed carries the class name as free text and
        the renderer decides what it can draw.
        """
        event = FeedEvent(
            session_id=uuid.uuid4(),
            actor="scientist",
            part=0,
            seq=0,
            kind="ShellCommandResult",
            created=_NOW,
        )
        assert event.kind == "ShellCommandResult"

    def test_kind_must_be_named(self) -> None:
        with pytest.raises(ValidationError):
            FeedEvent(
                session_id=uuid.uuid4(),
                actor="scientist",
                seq=0,
                kind="",
                created=_NOW,
            )

    def test_a_legacy_backfilled_turn_carries_the_reserved_part(self) -> None:
        """``-1`` namespaces rows that came from no file and cannot collide."""
        event = FeedEvent(
            session_id=uuid.uuid4(),
            actor="scientist",
            part=-1,
            seq=3,
            kind="UserMessage",
            created=_NOW,
        )
        assert event.part == -1


class TestFeedCursor:
    def test_the_cursor_carries_every_order_key_component(self) -> None:
        """A bare-``created`` resume skips every row tied at the boundary.

        ``part`` is a component because ``seq`` restarts within each source
        file, so ``(created, session_id, seq)`` alone is not a total order.
        """
        cursor = FeedCursor(created=_NOW, session_id=uuid.uuid4(), part=2, seq=7)
        assert (cursor.part, cursor.seq) == (2, 7)

    def test_part_defaults_for_a_client_that_predates_it(self) -> None:
        assert FeedCursor(created=_NOW, session_id=uuid.uuid4(), seq=0).part == 0

    def test_seq_must_not_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            FeedCursor(created=_NOW, session_id=uuid.uuid4(), seq=-1)


class TestRoomValidation:
    """Room scope rejects blank/whitespace consistently across bodies.

    Matches ``SessionStart.rooms``' element validator so all three carriers
    agree (a whitespace room can never match ``agentsession_rooms``).
    """

    def test_send_message_rejects_blank_room(self) -> None:
        for bad in ("", "   ", "\t"):
            with pytest.raises(ValidationError):
                SendMessage(actor="scientist", room=bad, text="hi")
        # A real room and an absent room both pass.
        assert SendMessage(actor="scientist", room="sear", text="hi").room == "sear"
        assert SendMessage(actor="scientist", text="hi").room is None

    def test_enqueue_request_forbids_room(self) -> None:
        # The enqueue route has no room semantics (it discards body.room), so a
        # client-sent ``room`` is a 422 (extra="forbid"), not an accepted-then-
        # dropped field -- the request shape carries only what the server uses.
        with pytest.raises(ValidationError):
            InboundEnqueueRequest(text="hi", room="lab")  # ty: ignore[unknown-argument] -- test proves extra="forbid" rejects room; the arg is unknown by design  # pyright: ignore[reportCallIssue]
        # The bare request still constructs.
        assert InboundEnqueueRequest(text="hi").text == "hi"

    def test_drain_item_rejects_blank_room(self) -> None:
        # The drain RESPONSE item keeps ``room`` (the poller renders the
        # ``[room] sender:`` prefix); a blank one is still a bug.
        for bad in ("", "  ", "\n"):
            with pytest.raises(ValidationError):
                InboundDrainItem(text="hi", room=bad)
        assert InboundDrainItem(text="hi", room="lab").room == "lab"
        assert InboundDrainItem(text="hi").room is None

    def test_session_start_rejects_comma_in_room(self) -> None:
        # A room name carrying ',' is ambiguous once serialized: ``trax run``
        # exports rooms comma-joined into ``TRAX_ROOMS`` (session.py), so a
        # room ``'a,b'`` is indistinguishable from two rooms ``'a'`` and
        # ``'b'`` to an agent inside the session. Reject the comma at the wire
        # boundary where rooms enter the system, alongside the blank rule.
        for bad in (["a,b"], ["ok", "x,y"], [","]):
            with pytest.raises(ValidationError):
                SessionStart(cli="codex", rooms=bad)
        # Comma-free rooms (single and several) still pass.
        assert SessionStart(cli="codex", rooms=["lab", "sear"]).rooms == ["lab", "sear"]
        assert SessionStart(cli="codex", rooms=None).rooms is None

    def test_send_message_rejects_whitespace_only_actor(self) -> None:
        # ``actor`` is the routing target (an AgentSession owner) resolved
        # verbatim; a whitespace-only value can never match, so reject it like
        # ``cli`` / ``room`` rather than enqueue an undeliverable message.
        for bad in ("   ", "\t", "\n"):
            with pytest.raises(ValidationError):
                SendMessage(actor=bad, text="hi")
        assert SendMessage(actor="scientist", text="hi").actor == "scientist"


class TestPaths:
    def test_path_helpers_interpolate_session_id(self) -> None:
        sid = uuid.uuid4()
        assert str(sid) in session_records_path(sid)
        assert session_records_path(sid).endswith("/records")
        assert session_end_path(sid).endswith("/end")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
