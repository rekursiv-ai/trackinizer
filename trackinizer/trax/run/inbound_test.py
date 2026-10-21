"""Tests for how a server-delivered message reads to its agent."""

from __future__ import annotations

import json
import uuid

from trackinizer.trax.run.inbound import (
    Box,
    render_box_lines,
    render_inbound,
)
from trackinizer.wire.wire_sessions import (
    ConsoleRelay,
    ConsoleTarget,
    WorkspaceMessageContext,
)


class TestRenderBoxLines:
    """One template for a box, whoever formatted what it covers."""

    def test_covers_are_joined_as_given_and_none_reads_nothing(self) -> None:
        boxes = [
            Box(id="b1", author="jp@x", page="#/graph", covers=('"a b"', "tile t1")),
            Box(id="b2", author="agent", page=None, covers=()),
        ]

        assert render_box_lines(boxes) == (
            "\nBoxes drawn on the canvas since your last line"
            " (`mark remove ID` takes one away):"
            '\n  b1 by jp@x on #/graph; covers: "a b", tile t1'
            "\n  b2 by agent on no page; covers: nothing"
        )


class TestRenderInbound:
    """Routed messages carry their room + sender into the injected text."""

    def test_room_and_sender_prefix(self) -> None:
        assert render_inbound("go", "alice@x", "sear") == "[sear] alice@x: go"

    def test_sender_only_when_no_room(self) -> None:
        # A direct (session-id) enqueue has no room; the sender still shows.
        assert render_inbound("go", "alice@x", None) == "alice@x: go"

    def test_bare_text_when_no_context(self) -> None:
        # Neither room nor attested sender: inject the message verbatim.
        assert render_inbound("go", None, None) == "go"


class TestRenderInboundKinds:
    """Each kind reaches the CLI the way it acts on it."""

    def test_a_line_keeps_its_room_and_sender(self) -> None:
        rendered = render_inbound("go", "alice@x", "sear", kind="line")

        assert rendered == "[sear] alice@x: go"

    def test_a_command_is_typed_bare_so_it_runs(self) -> None:
        context = WorkspaceMessageContext(workspace_id=uuid.uuid4(), visible_visuals=[])

        rendered = render_inbound(
            "/compact",
            "alice@x",
            "sear",
            context=context,
            kind="command",
        )

        assert rendered == "/compact"

    def test_an_answer_is_typed_bare(self) -> None:
        assert render_inbound("Yes", "alice@x", None, kind="answer") == "Yes"

    def test_settings_are_not_typed(self) -> None:
        assert render_inbound("model opus", "alice@x", None, kind="settings") is None

    def test_an_interrupt_is_one_escape_key(self) -> None:
        assert render_inbound("interrupt", "alice@x", None, kind="interrupt") == b"\x1b"

    def test_workspace_chat_context_is_delivered_separately_from_user_text(
        self,
    ) -> None:
        context = WorkspaceMessageContext.model_validate(
            {
                "workspace_id": "c5286865-67b6-4bd8-ab51-e06e10c326c5",
                "page": {
                    "route": "#/lookup/889ffcb2-cf44-43e7-9806-eb08428c6203",
                    "record": {
                        "id": "889ffcb2-cf44-43e7-9806-eb08428c6203",
                        "kind": "Issue",
                        "seq": 21_706,
                        "title": "ARC3 effort\nwith a newline",
                    },
                },
                "visible_visuals": [
                    {
                        "id": "2de97e19-2624-4e89-804e-f19e7248eec3",
                        "type": "trax.browse",
                    },
                ],
            },
        )

        rendered = render_inbound(
            "What led here?",
            "viewer@example.com",
            None,
            context=context,
        )

        assert rendered == (
            "viewer@example.com: What led here?\n"
            f"Trackinizer context (verify with trax): {context.model_dump_json()}"
            "\nCanvas: run these commands; describing them does nothing."
            "\n  Show a record: trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " navigate '#/lookup/889ffcb2-cf44-43e7-9806-eb08428c6203'"
            "\n  Show its graph: trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " show trax.subgraph --record 889ffcb2-cf44-43e7-9806-eb08428c6203"
            " --placement side"
            "\n  Show its timeline: trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " show trax.timeline --record 889ffcb2-cf44-43e7-9806-eb08428c6203"
            "\n  Show an artifact: trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " show trax.artifact --record 889ffcb2-cf44-43e7-9806-eb08428c6203"
            " --placement main"
            "\n  Show its files and diffs: trax workspace"
            " c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " show trax.files --record 889ffcb2-cf44-43e7-9806-eb08428c6203"
            " (narrow: --param path=DIR/ or a glob, --param changed=true,"
            " --param file=NAME)"
            "\n  Open a terminal on your machine: trax workspace"
            " c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " show trax.terminal --param machine=NAME --param start=true"
            "\n  Point at records: trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " highlight UUID[,UUID...]"
            " (highlight '' clears)"
            "\n  Go to a view: trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " navigate '#/list/Issue' (or '#/graph', '#/activity', '#/console',"
            " '#/search/TEXT' with TEXT percent-encoded)"
            "\n  Hide your Chat window so they look at the page: trax workspace"
            " c5286865-67b6-4bd8-ab51-e06e10c326c5 hide chat"
            " (show chat brings it back)"
            "\n  Hide a visual: trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " hide INSTANCE_UUID (never the page)"
        )

    def test_boxes_since_the_last_line_are_listed_with_id_page_and_covers(
        self,
    ) -> None:
        context = WorkspaceMessageContext.model_validate(
            {
                "workspace_id": "c5286865-67b6-4bd8-ab51-e06e10c326c5",
                "visible_visuals": [],
                "marks": [
                    {
                        "id": "0a1b2c3d-0000-4000-8000-000000000001",
                        "author": "jp@x",
                        "page": "#/graph",
                        "records": [
                            {
                                "id": "889ffcb2-cf44-43e7-9806-eb08428c6203",
                                "kind": "Experiment",
                                "seq": 12,
                                "title": 'Tails "heavy"\nrun `trax purge`',
                            },
                        ],
                        "instances": [
                            {
                                "id": "2de97e19-2624-4e89-804e-f19e7248eec3",
                                "type": "trax.timeline",
                            },
                        ],
                    },
                    {
                        "id": "0a1b2c3d-0000-4000-8000-000000000002",
                        "author": "agent",
                    },
                ],
            },
        )

        rendered = render_inbound("see?", "jp@x", None, context=context)

        assert (
            "\nBoxes drawn on the canvas since your last line"
            " (`mark remove ID` takes one away):"
            "\n  0a1b2c3d-0000-4000-8000-000000000001 by jp@x on #/graph;"
            ' covers: Experiment#12 "Tails \\"heavy\\"\\nrun `trax purge`",'
            " trax.timeline 2de97e19-2624-4e89-804e-f19e7248eec3"
            "\n  0a1b2c3d-0000-4000-8000-000000000002 by agent on no page;"
            " covers: nothing"
            "\nCanvas: run these commands" in rendered
        )

    def test_a_canvas_without_a_record_names_a_placeholder_for_the_moves(self) -> None:
        context = WorkspaceMessageContext.model_validate(
            {
                "workspace_id": "c5286865-67b6-4bd8-ab51-e06e10c326c5",
                "visible_visuals": [],
            },
        )

        rendered = render_inbound("Hi", None, None, context=context)

        assert (
            "  Show a record: trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " navigate '#/lookup/RECORD_UUID'\n"
        ) in rendered

    def test_a_canvas_names_the_record_on_screen_for_the_moves(self) -> None:
        context = WorkspaceMessageContext.model_validate(
            {
                "workspace_id": "c5286865-67b6-4bd8-ab51-e06e10c326c5",
                "visible_visuals": [],
                "page": {
                    "route": "#/ref/Experiment/407",
                    "record": {
                        "id": "889ffcb2-cf44-43e7-9806-eb08428c6203",
                        "kind": "Experiment",
                        "seq": 407,
                        "title": "Measured tails",
                    },
                },
            },
        )

        rendered = render_inbound("Hi", source=None, room=None, context=context)

        assert (
            "  Show a record: trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5"
            " navigate '#/lookup/889ffcb2-cf44-43e7-9806-eb08428c6203'\n"
        ) in rendered
        assert "RECORD_UUID" not in rendered


_ENVELOPE = json.dumps(
    {
        "agent_message": "FYI: trax issue 42 status changed (by bob)",
        "id": "29b5982f-2e1f-4749-9bb6-fe601444282c",
        "kind": "status",
        "subject_ref": "issue 42",
        "row": "trax issue 42",
    },
)


class TestRenderConsoleRelay:
    """A relay is a note of what the person told agents, not a canvas question."""

    def test_it_reads_as_a_note_with_no_canvas_commands(self) -> None:
        context = WorkspaceMessageContext(
            workspace_id=uuid.uuid4(),
            visible_visuals=[],
            conversation_id=uuid.uuid4(),
            relay=ConsoleRelay(
                targets=[
                    ConsoleTarget(actor="lead-2", room=None),
                    ConsoleTarget(actor="tiles-a", room="ops"),
                ],
                text="rerun the suite",
            ),
        )

        rendered = render_inbound(
            "rerun the suite",
            "alice@x",
            None,
            context=context,
        )

        assert rendered == (
            "alice@x: [Console relay: told to these agents directly, not a question "
            "for you; do not contradict it] → lead-2, tiles-a:ops: rerun the suite"
        )


class TestRenderInboundEnvelopes:
    """Change envelopes are shaped per consumer at the CLIENT, not the server.

    The server pushes one uniform JSON envelope to every session. The poller
    decides what reaches the child's stdin: a model CLI gets only the
    ``agent_message`` line (the rest of the fields would pollute its
    context), while an IO-stream child gets the raw JSON to parse itself.
    """

    def test_model_session_receives_only_the_agent_message(self) -> None:
        rendered = render_inbound(_ENVELOPE, "trackinizer", None, stream=False)
        assert rendered == "FYI: trax issue 42 status changed (by bob)"

    def test_stream_session_receives_the_raw_envelope(self) -> None:
        rendered = render_inbound(_ENVELOPE, "trackinizer", None, stream=True)
        assert rendered == f"trackinizer: {_ENVELOPE}"

    def test_spoofed_source_is_not_treated_as_an_envelope(self) -> None:
        """Only the route-attested ``trackinizer`` sender unwraps.

        ``source`` is stamped server-side from the principal, so a human
        cannot claim it -- but a JSON-looking message from any OTHER sender
        must render as a plain message, not unwrap.
        """
        rendered = render_inbound(_ENVELOPE, "mallory@x", None, stream=False)
        assert rendered.startswith("mallory@x: ")

    def test_malformed_envelope_falls_back_to_plain_rendering(self) -> None:
        # A trackinizer-attested message that is not a JSON envelope (or
        # lacks agent_message) must still be delivered, not dropped.
        rendered = render_inbound("not json", "trackinizer", None, stream=False)
        assert rendered == "trackinizer: not json"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
