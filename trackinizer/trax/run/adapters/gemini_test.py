"""Tests for the gemini adapter: session-shape fixtures → typed messages."""

from __future__ import annotations

from pathlib import Path

import json

import pytest

from trackinizer.trax.run.adapters import gemini
from trackinizer.trax.run.adapters.base import Event
from trackinizer.trax.run.adapters.gemini import GeminiAdapter
from trackinizer.types.agent_session_events import (
    AssistantMessage,
    Message,
    UnknownMessage,
    UserMessage,
)


def _text(message: Message) -> str:
    """The ``text`` of a user/assistant turn; asserts the member carries one."""
    assert isinstance(message, (UserMessage, AssistantMessage))
    return message.text


def _encode(obj: object) -> bytes:
    return json.dumps(obj).encode()


def _parse_one(raw: bytes) -> Event | None:
    """The single event for a whole-file body, or ``None`` when skipped.

    A fresh adapter per call: the adapter tracks the per-file emitted-message
    count to emit only newly-appended messages (REV-004), so a stale count
    from a prior single-body case must not bleed into the next.
    """
    events = list(GeminiAdapter().parse(raw, whole_file=True))
    assert len(events) <= 1, events
    return events[0] if events else None


class TestGeminiParseLine:
    def test_empty_messages_is_skipped(self) -> None:
        line = _encode({"sessionId": "x", "messages": []})
        assert _parse_one(line) is None

    def test_missing_messages_is_skipped(self) -> None:
        line = _encode({"sessionId": "x"})
        assert _parse_one(line) is None

    def test_user_message(self) -> None:
        line = _encode(
            {
                "sessionId": "x",
                "messages": [{"type": "user", "content": "hi"}],
            }
        )
        event = _parse_one(line)
        assert event is not None
        assert isinstance(event.message, UserMessage)
        assert event.message.text == "hi"

    def test_assistant_message(self) -> None:
        line = _encode(
            {
                "sessionId": "x",
                "messages": [
                    {"type": "user", "content": "hi"},
                    {"type": "gemini", "content": "hi back"},
                ],
            }
        )
        # A fresh body's first parse emits every message in order (REV-004):
        # the user prompt then the gemini reply.
        events = list(GeminiAdapter().parse(line, whole_file=True))
        assert isinstance(events[0].message, UserMessage)
        assert isinstance(events[1].message, AssistantMessage)
        assert events[1].message.text == "hi back"

    def test_assistant_message_with_tool_calls(self) -> None:
        line = _encode(
            {
                "sessionId": "x",
                "messages": [
                    {
                        "type": "gemini",
                        "content": "running",
                        "toolCalls": [
                            {"id": "t1", "name": "read_file", "args": {"path": "x"}}
                        ],
                    },
                ],
            }
        )
        event = _parse_one(line)
        assert event is not None
        assert isinstance(event.message, AssistantMessage)
        assert len(event.message.tool_calls) == 1
        call = event.message.tool_calls[0]
        assert call.id == "t1"
        assert call.name == "read_file"
        assert call.args == {"path": "x"}

    def test_unrecognized_type_is_unknown(self) -> None:
        line = _encode(
            {"sessionId": "x", "messages": [{"type": "system", "content": "x"}]}
        )
        event = _parse_one(line)
        assert event is not None
        assert isinstance(event.message, UnknownMessage)

    def test_malformed_json_returns_none(self) -> None:
        assert _parse_one(b"{not json}") is None

    def test_non_dict_returns_none(self) -> None:
        assert _parse_one(b"[1, 2, 3]") is None

    def test_is_whole_file_adapter(self) -> None:
        """Gemini rewrites its session in place; it must declare whole-file."""
        assert GeminiAdapter().whole_file is True

    def test_watches_the_tmp_root_so_a_new_project_is_covered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tmp ROOT is returned, not each project's ``chats`` leaf.

        Gemini mints ``<project-sha>/chats/`` the first time it runs in a
        workspace, which for the run being captured is after the watch was
        armed. Returning today's leaves leaves tomorrow's sibling unwatched
        and the run captures nothing silently.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tmp = tmp_path / ".gemini" / "tmp"
        (tmp / "sha-one" / "chats").mkdir(parents=True)
        (tmp / "sha-two" / "chats").mkdir(parents=True)
        # One entry (the root), never one per project.
        assert tuple(GeminiAdapter().session_dirs()) == (tmp,)

    def test_parse_whole_file_emits_latest_message(self) -> None:
        line = _encode(
            {"sessionId": "x", "messages": [{"type": "user", "content": "hi"}]}
        )
        events = list(GeminiAdapter().parse(line, whole_file=True))
        assert len(events) == 1
        assert isinstance(events[0].message, UserMessage)
        assert events[0].message.text == "hi"


class TestGeminiEmitsAppendedSlice:
    """Multiple messages appended between polls must all emit, in order.

    REV-004/R-20: gemini rewrites its whole session JSON in place. The adapter
    emitted only ``messages[-1]`` per parse, so when N messages appeared
    between two polls (a burst, or a tool call + reply landing together) the
    N-1 earlier ones were dropped. The adapter must track the prior message
    count per file and emit only the newly-appended slice, in order.
    """

    def test_burst_of_new_messages_all_emit_in_order(self) -> None:
        # A fresh adapter (per-run state must not leak across runs).
        fresh = GeminiAdapter()
        first = _encode(
            {"sessionId": "x", "messages": [{"type": "user", "content": "q1"}]}
        )
        events = list(fresh.parse(first, whole_file=True))
        assert [_text(e.message) for e in events] == ["q1"]

        # Three messages appear before the next poll: a reply, a follow-up
        # question, and a second reply. All three must surface, in order.
        second = _encode(
            {
                "sessionId": "x",
                "messages": [
                    {"type": "user", "content": "q1"},
                    {"type": "gemini", "content": "a1"},
                    {"type": "user", "content": "q2"},
                    {"type": "gemini", "content": "a2"},
                ],
            }
        )
        events = list(fresh.parse(second, whole_file=True))
        assert [_text(e.message) for e in events] == ["a1", "q2", "a2"]

    def test_unchanged_message_list_emits_nothing(self) -> None:
        # A re-parse of an unchanged body (the runner can re-feed) emits no
        # duplicate: the prior count already covers every message.
        fresh = GeminiAdapter()
        body = _encode(
            {"sessionId": "x", "messages": [{"type": "user", "content": "only"}]}
        )
        assert [_text(e.message) for e in fresh.parse(body, whole_file=True)] == [
            "only"
        ]
        assert list(fresh.parse(body, whole_file=True)) == []

    def test_cursor_is_per_session_file_not_per_adapter(self) -> None:
        """One adapter draining several session files must not cross their cursors.

        #498: the runner reuses ONE ``GeminiAdapter`` across every matching
        session file (``_scan_and_read``). A single ``_emitted`` counter then
        carried file A's count into file B: parsing B (2 msgs) yielded
        ``messages[2:] == []`` and B's turns were dropped. The cursor must be
        keyed per session file, so each file's appended slice is independent.
        """
        adapter = GeminiAdapter()
        file_a = _encode(
            {
                "sessionId": "sess-A",
                "messages": [
                    {"type": "user", "content": "a-q"},
                    {"type": "gemini", "content": "a-r"},
                ],
            }
        )
        file_b = _encode(
            {
                "sessionId": "sess-B",
                "messages": [
                    {"type": "user", "content": "b-q"},
                    {"type": "gemini", "content": "b-r"},
                ],
            }
        )
        # Same adapter parses A then B (the runner's per-poll order over files).
        events_a = [_text(e.message) for e in adapter.parse(file_a, whole_file=True)]
        events_b = [_text(e.message) for e in adapter.parse(file_b, whole_file=True)]
        assert events_a == ["a-q", "a-r"]
        # File B's cursor is independent of A's: both its messages emit.
        assert events_b == ["b-q", "b-r"]

    def test_interleaved_files_each_advance_independently(self) -> None:
        """Polling two growing files in turn advances each cursor on its own.

        The runner re-reads every file each poll, so A and B are parsed
        alternately as both grow. Each file must emit only its own newly-
        appended messages, never re-emit and never skip across the other file.
        """
        adapter = GeminiAdapter()

        def body(session: str, contents: list[str]) -> bytes:
            return _encode(
                {
                    "sessionId": session,
                    "messages": [{"type": "user", "content": c} for c in contents],
                }
            )

        def texts(session: str, contents: list[str]) -> list[str]:
            events = adapter.parse(body(session, contents), whole_file=True)
            return [_text(e.message) for e in events]

        # Poll 1: A has one message, B has one.
        assert texts("A", ["a1"]) == ["a1"]
        assert texts("B", ["b1"]) == ["b1"]
        # Poll 2: A grew by one; B unchanged.
        assert texts("A", ["a1", "a2"]) == ["a2"]
        assert texts("B", ["b1"]) == []
        # Poll 3: B grew by one; A unchanged.
        assert texts("A", ["a1", "a2"]) == []
        assert texts("B", ["b1", "b2"]) == ["b2"]

    def test_a_raise_mid_slice_does_not_consume_the_slice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A parse that dies partway must leave its messages still pending.

        The cursor advanced BEFORE the messages were normalized, so any raise
        during normalization -- an ``AssistantMessage`` invariant, a malformed
        block -- consumed the slice anyway. ``_process_chunk`` swallows the
        exception, the runner re-feeds the same body on the next wake, and the
        adapter reports nothing new: those turns are gone for the run.
        """
        adapter = GeminiAdapter()
        body = _encode(
            {
                "sessionId": "x",
                "messages": [
                    {"type": "user", "content": "q1"},
                    {"type": "gemini", "content": "a1"},
                ],
            }
        )

        def explode(msg: object) -> Message:
            del msg
            raise RuntimeError("malformed turn")

        monkeypatch.setattr(gemini, "_assistant_message", explode)
        with pytest.raises(RuntimeError):
            _ = list(adapter.parse(body, whole_file=True))
        monkeypatch.undo()

        # The failed parse consumed nothing: a re-feed still yields both turns.
        assert [_text(e.message) for e in adapter.parse(body, whole_file=True)] == [
            "q1",
            "a1",
        ]

    def test_keyless_files_do_not_share_a_cursor(self) -> None:
        """Two bodies with no ``sessionId`` must not collide on a shared cursor.

        K6-002: keyless bodies all mapped to the ``""`` key, so a second
        no-sessionId file's first message was treated as already-emitted by the
        first file's cursor and dropped. A keyless body must emit every message
        it carries rather than silently dropping turns.
        """
        adapter = GeminiAdapter()
        # Neither body carries a ``sessionId`` (malformed / pre-id gemini file).
        file_a = _encode({"messages": [{"type": "user", "content": "a-only"}]})
        file_b = _encode({"messages": [{"type": "user", "content": "b-only"}]})
        events_a = [_text(e.message) for e in adapter.parse(file_a, whole_file=True)]
        events_b = [_text(e.message) for e in adapter.parse(file_b, whole_file=True)]
        assert events_a == ["a-only"]
        # File B's first message must NOT be swallowed by file A's cursor.
        assert events_b == ["b-only"]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
