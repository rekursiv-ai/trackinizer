"""Tests for the commands Chat sends a run's CLI, and the goal the CLI follows."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO

import json

import pytest

from trackinizer.lib.agent.sessions import claude, codex
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    ContextState,
    SessionRecord,
)
from trackinizer.trax.run.commands import (
    GoalWatch,
    command_record,
    parse_command,
    replaces_goal,
)
from trackinizer.trax.run.slash import SlashCommand


class TestParseCommand:
    """A command's verb is its first word after the slash, lowercased."""

    @pytest.mark.parametrize(
        ("text", "verb", "args"),
        [
            ("/goal ship it", "goal", "ship it"),
            ("/Goal  Ship It ", "goal", "Ship It"),
            ("/compact", "compact", ""),
            ("/goal clear", "goal", "clear"),
        ],
    )
    def test_the_verb_and_its_arguments(self, text: str, verb: str, args: str) -> None:
        assert parse_command(text) == SlashCommand(command=verb, args=args)

    @pytest.mark.parametrize(
        ("text", "replaces"),
        [
            ("/goal ship it", True),
            ("/goal clear", False),
            ("/goal pause", False),
            ("/goal Resume", False),
            ("/goal edit", False),
            ("/goal", False),
            ("/compact ship it", False),
        ],
    )
    def test_only_an_objective_replaces_a_goal(self, text: str, replaces: bool) -> None:
        assert replaces_goal(parse_command(text)) is replaces


class TestCommandRecord:
    """The run's record of a command says what was sent, by whom, and what came of it."""

    def test_a_typed_command(self) -> None:
        at = datetime(2026, 10, 20, tzinfo=UTC)

        record = command_record(
            "/goal ship it",
            source="ann@x",
            room="lab",
            state="typed",
            at=at,
        )

        assert (record.subtype, record.content, record.timestamp) == (
            "command",
            "/goal ship it",
            at.isoformat(),
        )
        assert record.extra == {
            "$command": {
                "text": "/goal ship it",
                "verb": "goal",
                "args": "ship it",
                "source": "ann@x",
                "room": "lab",
                "state": "typed",
                "reason": None,
            },
        }

    def test_a_refused_command_says_why(self) -> None:
        record = command_record(
            "/goal x",
            source=None,
            room=None,
            state="refused",
            reason="no",
        )

        assert record.extra["$command"] == {
            "text": "/goal x",
            "verb": "goal",
            "args": "x",
            "source": None,
            "room": None,
            "state": "refused",
            "reason": "no",
        }
        assert record.timestamp is not None


def _claude_goal(**state: object) -> ContextState:
    """Return the record claude's reader makes of one ``goal_status`` attachment."""
    line = {
        "type": "attachment",
        "uuid": "u1",
        "attachment": {"type": "goal_status", "condition": "ship it", **state},
    }
    [record] = [
        each
        for each in claude.normalize(StringIO(json.dumps(line) + "\n"))
        if isinstance(each, ContextState)
    ]
    return record


def _codex_goal(status: str) -> SessionRecord:
    """Return the record codex's reader makes of one ``thread_goal_updated`` event."""
    event = {
        "type": "event_msg",
        "timestamp": "2026-10-20T00:00:00Z",
        "payload": {
            "type": "thread_goal_updated",
            "threadId": "t1",
            "goal": {"objective": "ship it", "status": status},
        },
    }
    meta = {"type": "session_meta", "payload": {"id": "s1", "cwd": "/w"}}
    *_, record = codex.normalize(
        StringIO(f"{json.dumps(meta)}\n{json.dumps(event)}\n"),
    )
    return record


class TestGoalWatch:
    """The watch follows each CLI's own log of its goal."""

    def test_claude_sets_checks_and_meets_a_goal(self) -> None:
        watch = GoalWatch()
        seen: list[bool] = []
        for record in (
            _claude_goal(met=False, sentinel=True),
            _claude_goal(met=False, reason="not yet"),
            _claude_goal(met=True, reason="done", iterations=2),
        ):
            watch.observe(record)
            seen.append(watch.open)

        assert seen == [True, True, False]

    @pytest.mark.parametrize(
        "end",
        [{"met": False, "failed": True}, {"met": True, "sentinel": True}],
        ids=["failed", "cleared"],
    )
    def test_a_failed_or_cleared_claude_goal_closes(self, end: dict[str, bool]) -> None:
        watch = GoalWatch()
        watch.observe(_claude_goal(met=False, sentinel=True))
        watch.observe(_claude_goal(**end))

        assert not watch.open

    def test_codex_statuses_open_and_close_the_goal(self) -> None:
        watch = GoalWatch()
        seen: list[bool] = []
        for status in ("active", "paused", "blocked", "complete", "usage_limited"):
            watch.observe(_codex_goal(status))
            seen.append(watch.open)

        assert seen == [True, True, True, False, True]

    def test_a_budget_limit_ends_a_codex_goal_and_a_typed_clear_does_too(self) -> None:
        watch = GoalWatch()
        watch.observe(_codex_goal("active"))
        watch.observe(_codex_goal("budget_limited"))
        closed_by_budget = not watch.open
        watch.observe(_codex_goal("active"))
        watch.cleared()

        assert (closed_by_budget, watch.open) == (True, False)

    def test_other_records_leave_it_alone(self) -> None:
        watch = GoalWatch()
        watch.observe(_claude_goal(met=False, sentinel=True))
        watch.observe(AssistantMessage(content="goal_status"))
        watch.observe(ContextState(kind="todo"))

        assert watch.open


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
