"""Tests for the session-ingest wire contract."""

from __future__ import annotations

from typing import cast

import uuid

from pydantic import ValidationError

import pytest

from trackinizer.lib.custom_json import DataclassCodec, SchemaError
from trackinizer.types.agent_session_events import (
    AgentSendMessage,
    AgentSessionEvent,
    AssistantMessage,
    Compaction,
    Kind,
    Message,
    SlashCommand,
    SystemMessage,
    ToolResult,
    UnknownMessage,
    UserMessage,
)
from trackinizer.wire.wire_sessions import (
    KINDS,
    AppendEventsRequest,
    EventBody,
    InboundDrainItem,
    InboundEnqueueRequest,
    SendMessage,
    SessionEnd,
    SessionStart,
    session_end_path,
    session_events_path,
)


# One non-default instance of every ``Message`` member, so the round-trip
# drift test exercises the whole Kind vocabulary (not just AssistantMessage).
_ROUND_TRIP_MESSAGES: list[Message] = [
    UserMessage(text="hi"),
    AgentSendMessage(text="go", source="agent7"),
    SystemMessage(text="<permissions>", role="developer"),
    AssistantMessage(text="ok", thinking="hmm"),
    ToolResult(call_id="t1", content="out", is_error=True),
    Compaction(text="summary", token_before=100, token_after=20),
    SlashCommand(command="exit", args="now"),
    UnknownMessage(raw={"weird": [1, 2, 3]}),
]


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


class TestEventBody:
    def test_defaults_message_to_empty(self) -> None:
        ev = EventBody(seq=0, kind="UserMessage")
        assert ev.message == {}
        assert ev.timestamp is None

    def test_rejects_negative_seq(self) -> None:
        with pytest.raises(ValidationError):
            EventBody(seq=-1, kind="UserMessage")

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValidationError):
            # Intentionally invalid kind to assert the Literal validation.
            EventBody(seq=0, kind="not_a_kind")  # ty: ignore[invalid-argument-type]  # pyright: ignore[reportArgumentType]

    def test_every_event_kind_accepted(self) -> None:
        for kind in KINDS:
            assert EventBody(seq=0, kind=kind).kind == kind

    def test_untagged_wrong_shape_message_rejected(self) -> None:
        # A ToolResult body whose JSON carries no type tag and only
        # foreign keys ({"text": "x"}) must not silently decode to a default
        # ToolResult with the unknown keys dropped; the kind/message shape
        # disagreement is a 422, not a lossy default.
        with pytest.raises(ValueError, match=r"omits the py/object|disagrees"):
            EventBody(kind="ToolResult", seq=0, message={"text": "x"}).to_event(
                uuid.uuid4()
            )

    def test_tagged_message_with_a_foreign_key_is_a_client_error(self) -> None:
        # A correctly-tagged body carrying one stray key reaches the codec's
        # unknown-field check. That check raises, and this route runs on
        # client-supplied input (``store.append_events`` -> ``to_event``), so
        # the exception must be one the API maps to 4xx: a bare ``ValueError``
        # matched no registered handler and surfaced as a 500.
        with pytest.raises(SchemaError, match="bogus"):
            EventBody(
                kind="ToolResult",
                seq=0,
                message={"py/object": "ToolResult", "bogus": 1},
            ).to_event(uuid.uuid4())

    def test_round_trip_through_event(self) -> None:
        sid = uuid.uuid4()
        event = AgentSessionEvent(
            session_id=sid,
            seq=3,
            kind="AssistantMessage",
            message=AssistantMessage(text="hi"),
        )
        body = EventBody.from_event(event)
        assert body.seq == 3
        assert body.kind == "AssistantMessage"
        rebuilt = body.to_event(sid)
        assert rebuilt == event

    @pytest.mark.parametrize(
        "msg", _ROUND_TRIP_MESSAGES, ids=lambda m: type(m).__name__
    )
    def test_from_event_to_event_round_trips_every_kind(self, msg: Message) -> None:
        # ``from_event`` (trusted typed source) and ``to_event`` (untrusted
        # wire decode) must be exact inverses for EVERY message kind, not just
        # AssistantMessage -- a member that serialized lossily (a dropped field,
        # an unset type tag) would silently corrupt one captured turn. Pins
        # the symmetry as a drift guard across the whole Kind vocabulary.
        sid = uuid.uuid4()
        # ``kind`` equals the member class name by construction (the
        # ``AgentSessionEvent.__post_init__`` invariant); cast for the checker.
        event = AgentSessionEvent(
            session_id=sid,
            seq=7,
            kind=cast(Kind, type(msg).__name__),
            message=msg,
        )
        assert EventBody.from_event(event).to_event(sid) == event


class TestAppendEventsRequest:
    def test_rejects_empty_batch(self) -> None:
        with pytest.raises(ValidationError):
            AppendEventsRequest(events=[])

    def test_accepts_events(self) -> None:
        req = AppendEventsRequest(
            events=[
                EventBody(
                    seq=0,
                    kind="UserMessage",
                    message=DataclassCodec.to_json(UserMessage(text="hi")),
                ),
                EventBody(seq=1, kind="AssistantMessage", model="gpt-5.5"),
            ]
        )
        assert len(req.events) == 2
        assert req.events[1].model == "gpt-5.5"


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
        assert str(sid) in session_events_path(sid)
        assert session_events_path(sid).endswith("/events")
        assert session_end_path(sid).endswith("/end")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
