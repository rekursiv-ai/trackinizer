"""Tests for the session lifecycle, messaging, and feed wire contract."""

from __future__ import annotations

from datetime import UTC, datetime

import uuid

from pydantic import ValidationError
from treekle import immutable

import pytest

from trackinizer.wire.wire_sessions import (
    CONSOLE_RELAY_MARK,
    CanvasMark,
    ConsoleRelay,
    ConsoleTarget,
    FeedCursor,
    FeedEvent,
    SendMessage,
    SessionAgent,
    SessionEnd,
    SessionStart,
    campaign_asker_label,
    held_read_timeout,
    session_audience_path,
    session_end_path,
    session_place_path,
    session_records_path,
)


_NOW = datetime(2026, 8, 24, 20, 29, tzinfo=UTC)


class TestSessionStart:
    def test_requires_cli(self) -> None:
        with pytest.raises(ValidationError):
            # Intentionally omit the required ``cli`` to assert the validator.
            SessionStart()  # ty: ignore[missing-argument] -- The test omits the required field to verify validation.  # pyright: ignore[reportCallIssue] -- The test omits the required field to verify validation.  # pyrefly: ignore[missing-argument] -- The test omits the required field to verify validation.

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
            with pytest.raises(
                ValidationError,
                match=r"Value error, value must be non-empty \[type=value_error",
            ):
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

    def test_attach_defaults_to_a_new_session(self) -> None:
        assert SessionStart(cli="claude").attach is None

    def test_attach_re_opens_a_session_without_a_cli_session_id(self) -> None:
        session_id = uuid.UUID(int=21_056)
        assert SessionStart(cli="claude", attach=session_id).attach == session_id

    def test_attach_and_cli_session_id_are_exclusive(self) -> None:
        # A resumed run keeps the cli_session_id the session already holds; naming
        # a new one alongside the session to re-open would fork the identity.
        with pytest.raises(ValidationError):
            SessionStart(
                cli="claude",
                attach=uuid.UUID(int=1),
                cli_session_id="chat:21056",
            )


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
            kind="BashResult",
            created=_NOW,
        )
        assert event.kind == "BashResult"

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

    def test_a_frozen_message_serializes_as_plain_json(self) -> None:
        """The store builds ``message`` with ``json_freeze``; the feed must dump it."""
        event = FeedEvent(
            session_id=uuid.uuid4(),
            actor="scientist",
            seq=0,
            kind="UserMessage",
            created=_NOW,
            message=immutable({"attachments": [{"mime": "image/png"}]}),
        )
        assert event.model_dump(mode="json")["message"] == {
            "attachments": [{"mime": "image/png"}],
        }

    def test_a_non_json_message_is_a_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            FeedEvent.model_validate(
                {
                    "session_id": uuid.uuid4(),
                    "actor": "scientist",
                    "seq": 0,
                    "kind": "UserMessage",
                    "created": _NOW,
                    "message": {"when": object()},
                },
            )


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
            FeedCursor(created=_NOW, session_id=uuid.uuid4(), seq=-1)  # pyrefly: ignore[bad-argument-type] -- The negative test passes the out-of-range value under test.


class TestRoomValidation:
    """Room scope rejects blank/whitespace consistently across bodies.

    Matches ``SessionStart.rooms``' element validator so the carriers
    agree (a whitespace room can never match ``agentsession_rooms``).
    """

    def test_send_message_rejects_blank_room(self) -> None:
        for bad in ("", "   ", "\t"):
            with pytest.raises(ValidationError):
                SendMessage(actor="scientist", room=bad, text="hi")
        # A real room and an absent room both pass.
        assert SendMessage(actor="scientist", room="sear", text="hi").room == "sear"
        assert SendMessage(actor="scientist", text="hi").room is None

    def test_session_start_rejects_comma_in_room(self) -> None:
        # A room name carrying ',' is ambiguous once serialized: the runner
        # exports rooms comma-joined into ``TRAX_ROOMS`` (session.py), so a
        # room ``'a,b'`` is indistinguishable from two rooms ``'a'`` and
        # ``'b'`` to an agent inside the session. Reject the comma at the wire
        # boundary where rooms enter the system, alongside the blank rule.
        for bad in (["a,b"], ["ok", "x,y"], [","]):
            with pytest.raises(ValidationError):
                SessionStart(cli="codex", rooms=bad)
        for blank in ([""], ["ok", "  "]):
            with pytest.raises(ValidationError, match="rooms must be non-empty"):
                SessionStart(cli="codex", rooms=blank)
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


_UUID = uuid.UUID("0b1c2d3e-0000-4000-8000-000000000412")


@pytest.mark.parametrize(
    ("named", "label", "session"),
    [
        ("lead#412", "lead", 412),
        (f"lead#{_UUID}", "lead", _UUID),
        ("lead#a#412", "lead#a", 412),
    ],
    ids=["a number", "a UUID", "a label that holds #"],
)
def test_an_agent_of_a_session_is_its_label_then_the_session_after_the_last_hash(
    named: str,
    label: str,
    session: int | uuid.UUID,
) -> None:
    agent = SessionAgent.parse(named)

    assert (agent.label, agent.session, str(agent)) == (label, session, named)


@pytest.mark.parametrize(
    "named",
    ["#412", " #412", "lead", "lead#", "lead#abc", "lead#0", "lead#-1", "lead#٤١٢"],
    ids=[
        "an empty label",
        "a blank label",
        "no session",
        "an empty session",
        "a session neither number nor UUID",
        "session zero",
        "a negative session",
        "digits that are not ASCII",
    ],
)
def test_a_name_that_is_no_agent_of_a_session_is_refused(named: str) -> None:
    with pytest.raises(ValueError, match=r"label|session|greater|names no agent"):
        _ = SessionAgent.parse(named)


def test_a_session_neither_number_nor_uuid_is_refused_saying_how_to_name_one() -> None:
    with pytest.raises(ValueError, match="names no agent") as refused:
        _ = SessionAgent.parse("lead#next")

    assert str(refused.value) == (
        "'lead#next' names no agent of a session: write label#number, such as "
        "lead#412, or label#UUID"
    )


def test_a_session_target_reads_from_json_as_a_number_or_a_uuid() -> None:
    by_number = SendMessage.model_validate_json(
        '{"to": {"label": "lead", "session": 412}, "text": "status?"}',
    )
    by_uuid = SendMessage.model_validate_json(
        f'{{"to": {{"label": "lead", "session": "{_UUID}"}}, "text": "status?"}}',
    )

    assert (by_number.to, by_uuid.to) == (
        SessionAgent(label="lead", session=412),
        SessionAgent(label="lead", session=_UUID),
    )


@pytest.mark.parametrize(
    "body",
    [
        {"text": "hi"},
        {"to": {"label": "lead", "session": 412}, "actor": "lead", "text": "hi"},
        {"to": {"label": "lead", "session": 412}, "room": "sear", "text": "hi"},
        {"actor": "lead", "text": "hi", "from_label": "Agent", "booked": "ask"},
        {"actor": "lead", "text": "hi", "from_session": str(_UUID)},
        {"actor": "lead", "text": "hi", "sender": "Agent#415"},
        {"actor": "lead", "text": "hi", "source": "ada@x.y"},
        {"actor": "lead", "text": "hi", "author": "ada@x.y"},
    ],
    ids=[
        "no target",
        "two targets",
        "a room with no actor",
        "a label with no session",
        "a session with no label",
        "a sender",
        "a source",
        "an author",
    ],
)
def test_a_send_names_one_target_and_a_sender_only_whole_and_checked(
    body: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _ = SendMessage.model_validate(body)


def test_an_agent_s_send_names_its_session_its_label_and_its_booking() -> None:
    sent = SendMessage(
        to=SessionAgent(label="lead", session=412),
        text="status?",
        from_session=_UUID,
        from_label="Agent",
        booked="ask",
    )

    assert SendMessage.model_validate_json(sent.model_dump_json()) == sent


class TestPaths:
    def test_path_helpers_interpolate_session_id(self) -> None:
        sid = uuid.uuid4()
        assert str(sid) in session_records_path(sid)
        assert session_records_path(sid).endswith("/records")
        assert session_end_path(sid) == f"/api/sessions/{sid}/end"
        assert session_audience_path(sid) == f"/api/sessions/{sid}/audience"


class TestCanvasMark:
    def test_a_box_stands_inside_the_stage(self) -> None:
        box = CanvasMark(
            id=uuid.uuid4(),
            author="jp",
            left=0.1,
            top=0.2,
            right=0.4,
            bottom=1.0,
        )

        assert (box.left, box.top, box.right, box.bottom) == (0.1, 0.2, 0.4, 1.0)

    @pytest.mark.parametrize("corner", ["left", "top", "right", "bottom"])
    @pytest.mark.parametrize("beyond", [-0.01, 1.01])
    def test_a_corner_beyond_the_stage_is_refused(
        self,
        corner: str,
        beyond: float,
    ) -> None:
        with pytest.raises(ValidationError):
            CanvasMark.model_validate(
                {"id": str(uuid.uuid4()), "author": "jp", corner: beyond},
            )


class TestConsoleRelay:
    def test_a_relay_reads_as_one_marked_line_naming_each_target(self) -> None:
        relay = ConsoleRelay(
            targets=[
                ConsoleTarget(actor="lead-2", room=None),
                ConsoleTarget(actor="tiles-a", room="ops"),
            ],
            text="rerun the suite",
        )

        assert CONSOLE_RELAY_MARK == "\u2192 "
        assert relay.line() == "\u2192 lead-2, tiles-a:ops: rerun the suite"

    def test_a_relay_names_a_target_and_has_words(self) -> None:
        target = ConsoleTarget(actor="lead-2", room=None)
        bad_bodies: list[dict[str, object]] = [
            {"targets": [], "text": "go"},
            {"targets": [target], "text": ""},
            {"targets": [target], "text": "x" * 16_385},
        ]
        for bad in bad_bodies:
            with pytest.raises(ValidationError):
                ConsoleRelay.model_validate(bad)

    def test_a_target_is_a_name_the_console_could_have_sent_to(self) -> None:
        for bad in ({"actor": " "}, {"actor": "a", "room": " "}):
            with pytest.raises(ValidationError):
                ConsoleTarget.model_validate(bad)


class TestHeldReadTimeout:
    @pytest.mark.parametrize("wait_sec", [0.5, 5.0, 30.0, 120.0])
    def test_a_held_request_gets_a_read_timeout_beyond_the_hold(
        self,
        wait_sec: float,
    ) -> None:
        timeout = held_read_timeout(wait_sec)

        assert timeout is not None
        assert timeout > wait_sec

    def test_a_request_that_does_not_wait_keeps_the_transport_timeout(self) -> None:
        assert held_read_timeout(0.0) is None


def test_a_placement_has_a_path_of_the_session() -> None:
    session = uuid.UUID(int=7)

    assert session_place_path(session) == f"/api/sessions/{session}/place"


def test_a_campaigns_asker_label_joins_its_slug_and_lowercased_email() -> None:
    assert campaign_asker_label("lab", "Ann@Lab.Example") == "lab:asker:ann@lab.example"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
