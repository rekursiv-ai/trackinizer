"""Tests for trackinizer CLI formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from os import terminal_size
from typing import TYPE_CHECKING, get_args

import json
import shutil
import sys

from trackinizer.trax import render as fmt
from trackinizer.trax.render import (
    _relation_title,
    _row_value,
    format_edge,
    print_rows,
)
from trackinizer.types.edges import Edge


if TYPE_CHECKING:
    import pytest


class TestBasicFormats:
    def test_json_ids_and_empty_tables(self) -> None:
        payload = [{"id": "abc", "kind": "Issue"}]
        assert json.loads(fmt.format_json(payload)) == payload
        assert fmt.format_ids(payload) == "abc\n"
        assert fmt.format_table([]) == "(no rows)\n"
        assert fmt.format_changes([]) == "(no changes)\n"

    def test_field_value_dict_renders_indented_json(self) -> None:
        """A dict field (Experiment ``config``) prints as JSON, not repr."""
        assert fmt.format_field_value({"lr": 0.1}) == '{\n  "lr": 0.1\n}'
        assert fmt.format_field_value({}) == "{}"

    def test_table_includes_ref_labels_and_cost_fields(self) -> None:
        text = fmt.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 4,
                    "status": "active",
                    "title": "do work",
                    "labels": ["x", "y"],
                    "marginal_cost": {"agent_usd": 1.25, "resource_usd": 0.75},
                }
            ]
        )
        assert "Issue#4" in text
        assert "x,y" in text
        assert "$AGENT" in text
        assert "$RESOURCE" in text
        assert "$1.25" in text
        assert "$0.75" in text
        assert "  COST" not in text

    def test_table_hides_empty_optional_columns(self) -> None:
        text = fmt.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 3,
                    "status": "active",
                    "title": "Test issue",
                    "labels": [],
                    "marginal_cost": {"agent_usd": 0, "resource_usd": 0},
                }
            ]
        )
        assert "REF" in text
        assert "STATUS" in text
        assert "TITLE" in text
        assert "LABELS" not in text
        assert "AGENT-COST" not in text
        assert "RESOURCE-COST" not in text

    def test_table_includes_every_populated_schema_column(self) -> None:
        text = fmt.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 3,
                    "status": "active",
                    "title": "Test issue",
                    "issue_kind": ["task"],
                    "priority": 10,
                    "validation": "pytest",
                    "labels": ["cli"],
                    "edge_priority": 0,
                    "edge_valence": 0.7,
                    "edge_labels": ["edge"],
                    "edge_note": "context",
                    "marginal_cost": {"agent_usd": 1.25, "resource_usd": 0},
                }
            ],
            width=200,
        )
        assert "KIND" in text
        assert "ISSUE_KIND" not in text
        assert "PRI" in text
        assert "EDGE-PRI" in text
        assert "VALENCE" in text
        assert "EDGE-LABEL" in text
        assert "NOTE" in text
        assert "VALID" in text
        assert "LABELS" in text
        assert "$AGENT" in text
        assert "task" in text
        assert "10" in text
        assert "pytest" in text
        assert "cli" in text
        assert "$1.25" in text

    def test_table_truncates_to_terminal_width(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def terminal_80(fallback: tuple[int, int]) -> terminal_size:
            del fallback
            return terminal_size((80, 24))

        monkeypatch.setattr(shutil, "get_terminal_size", terminal_80)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        text = fmt.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 123,
                    "status": "active",
                    "title": "summary " * 20,
                    "description": "description " * 20,
                    "validation": "validation " * 20,
                }
            ],
        )

        lines = text.splitlines()
        assert lines
        assert all(len(line) <= 80 for line in lines)
        assert "…" in text

    def test_table_does_not_truncate_when_stdout_is_piped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def terminal_20(fallback: tuple[int, int]) -> terminal_size:
            del fallback
            return terminal_size((20, 24))

        monkeypatch.setattr(shutil, "get_terminal_size", terminal_20)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

        text = fmt.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 123,
                    "status": "active",
                    "title": "summary " * 20,
                    "validation": "validation " * 20,
                }
            ],
        )

        assert "summary summary summary" in text
        assert "validation validation validation" in text

    def test_table_explicit_width_overrides_terminal_width(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def terminal_20(fallback: tuple[int, int]) -> terminal_size:
            del fallback
            return terminal_size((20, 24))

        monkeypatch.setattr(shutil, "get_terminal_size", terminal_20)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

        text = fmt.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 123,
                    "status": "active",
                    "title": "summary " * 20,
                    "description": "description " * 20,
                    "validation": "validation " * 20,
                }
            ],
            width=80,
        )

        assert all(len(line) <= 80 for line in text.splitlines())
        assert any(len(line) > 20 for line in text.splitlines())

    def test_table_preserves_wide_refs_at_narrow_width(self) -> None:
        text = fmt.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Experiment",
                    "seq": 2,
                    "status": "active",
                    "title": "x" * 100,
                },
                {
                    "id": "def",
                    "kind": "CodeChange",
                    "seq": 123_456_789,
                    "status": "active",
                    "title": "y" * 100,
                },
            ],
            width=40,
        )

        assert "Experiment#2" in text
        assert "CodeChange#123456789" in text

    def test_table_preserves_edge_note_before_generic_columns(self) -> None:
        text = fmt.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 6,
                    "status": "active",
                    "title": "task child",
                    "issue_kind": ["task"],
                    "edge_priority": 10,
                    "edge_valence": 0.7,
                    "edge_labels": ["edge"],
                    "edge_note": "must land first",
                }
            ],
            width=80,
        )

        assert "must land first" in text

    def test_table_caps_verbose_columns_on_wide_terminals(self) -> None:
        text = fmt.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "summary " * 20,
                    "description": "description " * 20,
                    "validation": "validation " * 20,
                }
            ],
            width=180,
        )

        lines = text.splitlines()
        assert lines
        assert max(len(line) for line in lines) <= 180
        assert "summary summary summary summary summary" in text
        assert (
            "description description description description description description"
            not in text
        )
        assert (
            "validation validation validation validation validation validation"
            not in text
        )

    def test_table_width_zero_keeps_full_cells(self) -> None:
        text = fmt.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "summary",
                    "validation": "validation " * 20,
                }
            ],
            width=0,
        )

        assert "validation validation validation" in text


class TestDetailFormats:
    def test_format_show_all_sections_except_changes(self) -> None:
        text = fmt.format_show(_show_payload(), include_id=True)

        for expected in (
            "Belief#3",
            "owner:       alice",
            "labels:      math",
            "subscribers: bob",
            "judgement  : proven",
            "confidence : 0.9",
            "codechanges: 1 entries",
            "    - c1",
            "agent-cost:  $1.0000",
            "resource-cost: $2.0000",
            "created:     "
            + datetime(2026, 5, 18, 12, 34, 56, tzinfo=UTC)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S"),
            "modified:    "
            + datetime(2026, 5, 19, 1, 2, 3, tzinfo=UTC)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S"),
            "val=0.5; labels=edge; context",
            "narrows:",
            "required_by:",
        ):
            assert expected in text
        assert "Recent changes:" not in text

    def test_format_show_changes_includes_recent_changes(self) -> None:
        text = fmt.format_show(_show_payload(), changes=True)

        assert "Recent changes:" in text

    def test_format_show_changes_hides_empty_deltas(self) -> None:
        text = fmt.format_show(
            {
                "self": {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "x",
                },
                "changes": [
                    {
                        "created": "2026-05-18T00:00:00",
                        "kind": "edge_added",
                        "actor": "system",
                        "old": {
                            "peer_id": None,
                            "peer_kind": None,
                            "peer_edge_kind": None,
                            "edge_note": None,
                            "edge_labels": [],
                        },
                        "new": {
                            "peer_id": "b83f3bc9-8ece-451a-a919-7c1ef31da12e",
                            "peer_kind": "Issue",
                            "peer_edge_kind": "requires",
                            "edge_note": None,
                            "edge_labels": [],
                        },
                    }
                ],
            },
            changes=True,
        )

        assert "peer_id:" in text
        assert "peer_kind:" in text
        assert "peer_edge_kind:" in text
        assert "edge_note:" not in text
        assert "edge_labels:" not in text

    def test_format_show_uses_semantic_relation_names(self) -> None:
        text = fmt.format_show(
            {
                "self": {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 5,
                    "status": "active",
                    "title": "requirer",
                },
                # ``requires`` is stored from=requirer, so on the requirer this
                # edge is OUTBOUND and renders as "requires".
                "edges": {
                    "requires": [{"kind": "Issue", "seq": 4, "title": "prerequisite"}]
                },
            }
        )
        assert "requires:" in text
        assert "Issue#4" in text
        assert "Inbound" not in text
        assert "backlinks" not in text

    def test_relation_title_covers_every_edge_kind_both_directions(self) -> None:
        """Every ``Edge.Kind`` has an explicit relation title in both directions.

        ``_relation_title`` falls back to ``edge_kind.replace("_"," ").title()``
        for any kind it does not name -- which leaks the storage kind (e.g.
        ``cites_paper`` -> ``Cites Paper``) instead of the CLI alias
        (``cites``/``cited_by``). Pin the table to the closed ``Edge.Kind`` so a
        new kind cannot silently render its storage name.
        """
        for kind in get_args(Edge.Kind.__value__):
            for inbound in (False, True):
                title = _relation_title(kind, inbound=inbound)
                fallback = kind.replace("_", " ").title()
                assert title != fallback, (
                    f"_relation_title({kind!r}, inbound={inbound}) falls back to "
                    f"the storage name {fallback!r}; add an explicit alias"
                )

    def test_relation_title_renders_paper_citation_aliases(self) -> None:
        assert _relation_title("cites_paper", inbound=False) == "cites"
        assert _relation_title("cites_paper", inbound=True) == "cited_by"

    def test_format_show_uses_belief_facing_citation_names(self) -> None:
        text = fmt.format_show(
            {
                "self": {
                    "id": "abc",
                    "kind": "Belief",
                    "seq": 5,
                    "status": "active",
                    "title": "finding",
                },
                # proves/favors store Artifact -> claim, so on the cited Belief
                # they are INBOUND (backlinks) and read proved_by / favored_by.
                "backlinks": {
                    "proves": [{"kind": "Experiment", "seq": 4, "title": "evidence"}],
                    "favors": [{"kind": "Paper", "seq": 1, "title": "context"}],
                },
            }
        )
        assert "proved_by:" in text
        assert "favored_by:" in text
        assert "proves:" not in text
        assert "favors:" not in text

    def test_format_show_uses_artifact_facing_citation_names(self) -> None:
        text = fmt.format_show(
            {
                "self": {
                    "id": "abc",
                    "kind": "Experiment",
                    "seq": 4,
                    "status": "active",
                    "title": "evidence",
                },
                # On the citing Artifact the proves edge is OUTBOUND, read proves.
                "edges": {"proves": [{"kind": "Belief", "seq": 5, "title": "finding"}]},
            }
        )
        assert "proves:" in text
        assert "proved_by:" not in text

    def test_format_show_defaults_owner(self) -> None:
        text = fmt.format_show(
            {
                "self": {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "x",
                }
            }
        )
        assert "owner:       (unassigned)" in text

    def test_format_show_hides_empty_optional_fields(self) -> None:
        text = fmt.format_show(
            {
                "self": {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "x",
                    "validation": "",
                    "labels": [],
                    "subscribers": [],
                    "marginal_cost": {"agent_usd": 0, "resource_usd": 0},
                }
            }
        )
        assert "validation" not in text
        assert "labels" not in text
        assert "subscribers" not in text
        assert "agent-cost" not in text
        assert "resource-cost" not in text

    def test_format_show_formats_extra_list_values(self) -> None:
        text = fmt.format_show(
            {
                "self": {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "x",
                    "issue_kind": ["task", "bug"],
                }
            }
        )
        assert "kind       : task,bug" in text
        assert "issue_kind" not in text
        assert "['task'" not in text

    def test_format_show_includes_selected_edge_metadata(self) -> None:
        text = fmt.format_show(
            {
                "self": {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "x",
                },
                "selected_edge": {
                    "edge_priority": 0,
                    "edge_valence": 0.7,
                    "edge_labels": ["edge"],
                    "edge_note": "must land first",
                },
            }
        )
        assert "Selected edge:" in text
        assert "priority  : 0" in text
        assert "valence   : 0.7" in text
        assert "labels    : edge" in text
        assert "note      : must land first" in text
        assert "edge_priority" not in text
        assert "edge_labels" not in text

    def test_format_changes(self) -> None:
        text = fmt.format_changes(
            [
                {
                    "created": "2026-05-18T12:34:56",
                    "kind": "status",
                    "subject_kind": "Issue",
                    "subject_id": "abcdef123456",
                    "actor": "alice",
                    "principal": "cli@example.com",
                    "old": {"status": "active"},
                    "new": {"status": "complete"},
                }
            ]
        )
        expected = datetime(2026, 5, 18, 12, 34, 56, tzinfo=UTC).astimezone()
        assert expected.strftime("%Y-%m-%d %H:%M:%S") in text
        assert "Issue#abcdef12" in text
        assert "actor=alice principal=cli@example.com" in text
        assert "status: active -> complete" in text

    def test_format_changes_prints_local_time(self) -> None:
        text = fmt.format_changes(
            [
                {
                    "created": datetime(2026, 5, 18, 12, 34, 56, tzinfo=UTC),
                    "kind": "status",
                    "subject_kind": "Issue",
                    "subject_id": "abcdef123456",
                    "actor": "alice",
                }
            ]
        )
        expected = datetime(2026, 5, 18, 12, 34, 56, tzinfo=UTC).astimezone()
        assert expected.strftime("%Y-%m-%d %H:%M:%S") in text
        assert "T12:34:56" not in text
        assert "Issue#abcdef12" in text
        assert "actor=alice" in text


def _show_payload() -> dict[str, object]:
    return {
        "self": {
            "id": "abc",
            "kind": "Belief",
            "seq": 3,
            "status": "active",
            "owner": "alice",
            "title": "belief summary",
            "description": "longer",
            "labels": ["math"],
            "subscribers": ["bob"],
            "judgement": "proven",
            "confidence": 0.9,
            "codechanges": ["c1"],
            "marginal_cost": {"agent_usd": 1.0, "resource_usd": 2.0},
            "created": "2026-05-18T12:34:56+00:00",
            "modified": "2026-05-19T01:02:03+00:00",
        },
        "edges": {
            "narrows": [
                {
                    "kind": "Issue",
                    "seq": 1,
                    "title": "narrows",
                    "note": "context",
                    "valence": 0.5,
                    "labels": ["edge"],
                }
            ]
        },
        "backlinks": {"requires": [{"kind": "Issue", "seq": 2, "title": "back"}]},
        "changes": [
            {
                "created": "2026-05-18T00:00:00",
                "kind": "created",
                "actor": "system",
            }
        ],
    }


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)


# Folded in from former crasher_test.py.


def test_format_local_time_handles_none() -> None:
    """``None`` / empty / unparseable timestamps must produce a placeholder."""
    assert fmt._format_local_time(None) == ""
    assert fmt._format_local_time("") == ""
    assert fmt._format_local_time("x") == ""


def test_edge_annotation_includes_peer_priority() -> None:
    """A related peer's edge ``priority`` must appear in the compact annotation.

    The server emits ``priority`` on the peer ref (``web.py:_add_edge_annotation``)
    and ``_format_selected_edge`` shows it, but the compact relation suffix
    ``_edge_annotation`` dropped it. A ``priority=0`` peer must still render its
    priority (zero is a real, set value, not absence).
    """
    text = fmt.format_show(
        {
            "self": {
                "id": "abc",
                "kind": "Issue",
                "seq": 5,
                "status": "active",
                "title": "blocker",
            },
            "edges": {
                "requires": [
                    {"kind": "Issue", "seq": 4, "title": "prerequisite", "priority": 0}
                ]
            },
        }
    )
    assert "prio=0" in text


def test_print_rows_json(capsys: pytest.CaptureFixture[str]) -> None:
    print_rows([{"id": "abc", "kind": "Issue"}], "json")
    out = capsys.readouterr().out
    assert '"id": "abc"' in out


def test_format_edge_includes_changes_section() -> None:
    view = {
        "title": "blocked_by",
        "endpoints": (
            {"label": "blocker", "kind": "Issue", "seq": 1, "title": "a"},
            {"label": "blocked", "kind": "Issue", "seq": 2, "title": "b"},
        ),
        "edge": {"edge_priority": 10},
        "changes": [
            {
                "created": "2024-01-01T00:00:00",
                "kind": "edge_added",
                "actor": "alice",
                "old": {},
                "new": {"priority": 10},
            }
        ],
    }
    text = format_edge(view, changes=True)
    assert "Recent changes:" in text
    assert "edge_added" in text


def test_row_value_depth_cap() -> None:
    deep: list[object] = []
    cursor: list[object] = deep
    for _ in range(20):
        nxt: list[object] = []
        cursor.append(nxt)
        cursor = nxt
    cursor.append("leaf")
    assert "..." in _row_value(deep)
