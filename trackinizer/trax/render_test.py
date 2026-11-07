"""Tests for trackinizer CLI formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from os import terminal_size
from typing import cast, get_args
from uuid import UUID

import json
import os
import re
import shutil
import sys

from treekle import json_loads

import pytest

from trackinizer.trax import render
from trackinizer.trax.context import ERR_STREAM, OUT_STREAM
from trackinizer.trax.render import (
    _relation_title,
    _row_value,
    format_edge,
    print_rows,
)
from trackinizer.trax.verbs import Kind, Next
from trackinizer.types.edges import Edge


@pytest.mark.parametrize("actor_flag", ["--as", "--actor"])
@pytest.mark.parametrize(
    ("command", "context"),
    [
        (Kind, ["17", "title", "to", "revised title"]),
        (Next, ["owner", "to", "claim-owner"]),
    ],
)
def test_public_command_parsers_preserve_write_flags_and_context(
    command: type[Kind | Next],
    context: list[str],
    actor_flag: str,
) -> None:
    parser = command.make_parser()
    defaults = vars(parser.parse_args(context))
    assert defaults["actor"] == ""
    assert defaults["reason"] == ""

    explicit = parser.parse_args(
        [*context, actor_flag, "audit-actor", "--reason", "validation context"],
    )

    assert vars(explicit) == {
        **defaults,
        "actor": "audit-actor",
        "reason": "validation context",
    }
    assert vars(parser.parse_args(context)) == defaults


def test_table_width_explicit_value_overrides_ambient_width() -> None:
    token = render.TERMINAL_WIDTH.set(40)
    try:
        assert render.table_width() == 40
        assert render.table_width(73) == 73
        assert render.table_width() == 40
    finally:
        render.TERMINAL_WIDTH.reset(token)


class TestOutputRouting:
    def test_echo_uses_invocation_streams_and_newline_controls(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        output, errors = StringIO(), StringIO()
        output_token = OUT_STREAM.set(output)
        errors_token = ERR_STREAM.set(errors)
        try:
            render.echo("result")
            render.echo(17, nl=False)
            render.echo("warning", err=True)
            render.echo("tail", err=True, nl=False)
        finally:
            ERR_STREAM.reset(errors_token)
            OUT_STREAM.reset(output_token)

        assert output.getvalue() == "result\n17"
        assert errors.getvalue() == "warning\ntail"
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    @pytest.mark.parametrize("output_format", ["json", "ids", "table"])
    def test_print_rows_preserves_records_and_single_final_newline(
        self,
        output_format: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rows: list[dict[str, object]] = [
            {"id": "first-id", "kind": "Issue", "seq": 1, "title": "alpha"},
            {"id": "second-id", "kind": "Paper", "seq": 2, "title": "beta"},
        ]
        output, errors = StringIO(), StringIO()
        output_token = OUT_STREAM.set(output)
        errors_token = ERR_STREAM.set(errors)
        try:
            print_rows(rows, output_format, width=80)
        finally:
            ERR_STREAM.reset(errors_token)
            OUT_STREAM.reset(output_token)

        text = output.getvalue()
        assert text.endswith("\n")
        assert not text.endswith("\n\n")
        if output_format == "json":
            assert json_loads(text, list[dict[str, object]]) == rows
        elif output_format == "ids":
            assert text == "first-id\nsecond-id\n"
        else:
            for value in ("Issue#1", "Paper#2", "alpha", "beta"):
                assert text.count(value) == 1
        assert errors.getvalue() == ""
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_print_rows_forwards_explicit_width_over_unbounded_context(
        self,
    ) -> None:
        output = StringIO()
        output_token = OUT_STREAM.set(output)
        width_token = render.TERMINAL_WIDTH.set(0)
        try:
            print_rows(
                [{"kind": "Issue", "seq": 1, "title": "summary " * 20}],
                "table",
                width=45,
            )
        finally:
            render.TERMINAL_WIDTH.reset(width_token)
            OUT_STREAM.reset(output_token)

        text = output.getvalue()
        assert text.endswith("\n")
        assert not text.endswith("\n\n")
        assert all(len(line) <= 45 for line in text.splitlines())
        assert "Issue#1" in text
        assert "summary" in text


class TestBasicFormats:
    def test_json_ids_and_empty_tables(self) -> None:
        payload = [{"id": "abc", "kind": "Issue"}]
        assert json.loads(render.format_json(payload)) == payload
        assert render.format_ids(payload) == "abc\n"
        assert render.format_table([]) == "(no rows)\n"
        assert render.format_changes([]) == "(no changes)\n"

    def test_field_value_none_prints_nothing(self) -> None:
        # ``/api/web/get`` sends an unset field as ``null``; it prints as the empty
        # ``""`` and ``[]`` it used to send did, never as ``None``.
        assert render.format_field_value(None) == ""

    def test_projected_lists_and_scalars_preserve_values(self) -> None:
        assert render.format_field_value(["alpha", "beta", 17]) == "alpha\nbeta\n17"
        assert render.format_field_value([]) == ""
        assert render.format_field_value("alpha beta") == "alpha beta"
        assert render.format_field_value(0) == "0"
        assert render.format_field_value(False) == "False"

    def test_table_preserves_nonempty_scalar_matching_a_marker(self) -> None:
        text = render.format_table(
            [{"kind": "Issue", "seq": 1, "title": "scalar value", "owner": "XXXX"}],
            width=0,
        )

        assert "XXXX" in text

    def test_json_stringifies_uuid_and_datetime_without_losing_fields(self) -> None:
        identifier = UUID("c722336d-064c-499b-88c0-5bca7b9bc614")
        created = datetime(2024, 3, 7, 17, 23, 41, tzinfo=UTC)
        text = render.format_json({"id": identifier, "created": created})

        assert json_loads(text, dict[str, str]) == {
            "id": str(identifier),
            "created": str(created),
        }
        assert text.endswith("\n")
        assert not text.endswith("\n\n")

    def test_ids_preserve_multiple_uuid_values_in_order(self) -> None:
        identifiers = (
            UUID("c722336d-064c-499b-88c0-5bca7b9bc614"),
            UUID("b83f3bc9-8ece-451a-a919-7c1ef31da12e"),
        )
        text = render.format_ids({"id": identifier} for identifier in identifiers)

        assert text == f"{identifiers[0]}\n{identifiers[1]}\n"

    def test_show_omits_codechanges_when_unset(self) -> None:
        payload = _show_payload()
        view = cast(dict[str, object], payload["self"])
        view["codechanges"] = None
        text = render.format_show(payload)
        assert "codechanges" not in text

    def test_field_value_dict_renders_indented_json(self) -> None:
        """A dict field (Experiment ``config``) prints as JSON, not repr."""
        assert render.format_field_value({"lr": 0.1}) == '{\n  "lr": 0.1\n}'
        assert render.format_field_value({}) == "{}"

    def test_table_includes_ref_labels_and_cost_fields(self) -> None:
        text = render.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 4,
                    "status": "active",
                    "title": "do work",
                    "labels": ["x", "y"],
                    "marginal_cost": {"agent_usd": 1.25, "resource_usd": 0.75},
                },
            ],
        )
        assert "Issue#4" in text
        assert "x,y" in text
        assert "$AGENT" in text
        assert "$RESOURCE" in text
        assert "$1.25" in text
        assert "$0.75" in text
        assert "  COST" not in text

    def test_table_hides_empty_optional_columns(self) -> None:
        text = render.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 3,
                    "status": "active",
                    "title": "Test issue",
                    "labels": [],
                    "marginal_cost": {"agent_usd": 0, "resource_usd": 0},
                },
            ],
        )
        assert "REF" in text
        assert "STATUS" in text
        assert "TITLE" in text
        assert "LABELS" not in text
        assert "AGENT-COST" not in text
        assert "RESOURCE-COST" not in text

    def test_table_includes_every_populated_schema_column(self) -> None:
        text = render.format_table(
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
                },
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

        text = render.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 123,
                    "status": "active",
                    "title": "summary " * 20,
                    "description": "description " * 20,
                    "validation": "validation " * 20,
                },
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

        text = render.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 123,
                    "status": "active",
                    "title": "summary " * 20,
                    "validation": "validation " * 20,
                },
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

        text = render.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 123,
                    "status": "active",
                    "title": "summary " * 20,
                    "description": "description " * 20,
                    "validation": "validation " * 20,
                },
            ],
            width=80,
        )

        assert all(len(line) <= 80 for line in text.splitlines())
        assert any(len(line) > 20 for line in text.splitlines())

    def test_table_preserves_wide_refs_at_narrow_width(self) -> None:
        text = render.format_table(
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
        text = render.format_table(
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
                },
            ],
            width=80,
        )

        assert "must land first" in text

    def test_table_caps_verbose_columns_on_wide_terminals(self) -> None:
        text = render.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "summary " * 20,
                    "description": "description " * 20,
                    "validation": "validation " * 20,
                },
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
        text = render.format_table(
            [
                {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "summary",
                    "validation": "validation " * 20,
                },
            ],
            width=0,
        )

        assert "validation validation validation" in text

    def test_table_preserves_cells_at_exact_natural_fit(self) -> None:
        text = render.format_table(
            [{"kind": "Issue", "seq": 1, "status": "active", "title": "short"}],
            width=22,
        )

        assert all(len(line) <= 22 for line in text.splitlines())
        for value in ("Issue#1", "active", "short"):
            assert value in text

    def test_table_counts_separators_before_accepting_natural_widths(self) -> None:
        text = render.format_table(
            [
                {
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "abcdefghijklmnopqrstuvwxyzABCD",
                },
            ],
            width=45,
        )

        assert all(len(line) <= 45 for line in text.splitlines())
        assert "Issue#1" in text
        assert "active" in text
        assert "abcdefghijklmnopqrst" in text

    @pytest.mark.parametrize(
        ("value", "width", "expected"),
        [
            ("  alpha\t beta\n", 0, "alpha beta"),
            ("  alpha\t beta\n", -1, "alpha beta"),
            ("  alpha\t beta\n", 1, "…"),
            ("  alpha\t beta\n", 2, "a…"),
            ("  alpha\t beta\n", 3, "al…"),
            ("  alpha\t beta\n", 10, "alpha beta"),
            ("a", 1, "a"),
            ("ab", 2, "ab"),
            ("", 1, ""),
        ],
    )
    def test_table_cell_normalizes_and_respects_boundaries(
        self,
        value: str,
        width: int,
        expected: str,
    ) -> None:
        assert render.table_cell(value, width) == expected

    def test_missing_null_and_zero_costs_do_not_invent_currency(self) -> None:
        text = render.format_table(
            [
                {"kind": "Issue", "seq": 1, "title": "missing cost"},
                {
                    "kind": "Issue",
                    "seq": 2,
                    "title": "null cost",
                    "marginal_cost": {"agent_usd": None, "resource_usd": None},
                },
                {
                    "kind": "Issue",
                    "seq": 3,
                    "title": "zero cost",
                    "marginal_cost": {"agent_usd": 0, "resource_usd": 0},
                },
            ],
            width=0,
        )

        assert "$" not in text
        for title in ("missing cost", "null cost", "zero cost"):
            assert title in text

    def test_terminal_query_failure_uses_real_shutil_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def unavailable(fd: int = 1) -> terminal_size:
            del fd
            raise OSError("No terminal attached")

        monkeypatch.delenv("COLUMNS", raising=False)
        monkeypatch.delenv("LINES", raising=False)
        monkeypatch.setattr(os, "get_terminal_size", unavailable)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        width_token = render.TERMINAL_WIDTH.set(None)
        try:
            width = render.table_width()
            assert width > 0
            text = render.format_table(
                [{"kind": "Issue", "seq": 1, "title": "summary " * width}],
            )
        finally:
            render.TERMINAL_WIDTH.reset(width_token)

        assert all(len(line) <= width for line in text.splitlines())
        assert "Issue#1" in text
        assert "summary" in text


class TestDetailFormats:
    def test_format_show_all_sections_except_changes(self) -> None:
        text = render.format_show(_show_payload(), include_id=True)

        for expected in (
            "Belief#3",
            "owner:       alice",
            "labels:      math",
            "subscribers: bob",
            "judgement:   proven",
            "confidence:  0.9",
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
        text = render.format_show(_show_payload(), changes=True)

        assert "Recent changes:" in text

    def test_format_show_changes_hides_empty_deltas(self) -> None:
        view: dict[str, object] = {
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
                },
            ],
        }
        text = render.format_show(view, changes=True)

        assert "peer_id:" in text
        assert "peer_kind:" in text
        assert "peer_edge_kind:" in text
        assert "edge_note:" not in text
        assert "edge_labels:" not in text

    def test_format_show_uses_semantic_relation_names(self) -> None:
        text = render.format_show(
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
                    "requires": [{"kind": "Issue", "seq": 4, "title": "prerequisite"}],
                },
            },
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
        for kind in cast(
            tuple[Edge.Kind, ...],
            get_args(cast(object, Edge.Kind.__value__)),
        ):
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

    def test_relation_title_titles_a_kind_this_client_does_not_know(self) -> None:
        # A newer server may send an edge kind this client predates.
        assert _relation_title("future_kind", inbound=True) == "Future Kind"

    def test_format_show_uses_belief_facing_citation_names(self) -> None:
        text = render.format_show(
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
            },
        )
        assert "proved_by:" in text
        assert "favored_by:" in text
        assert "proves:" not in text
        assert "favors:" not in text

    def test_format_show_uses_artifact_facing_citation_names(self) -> None:
        text = render.format_show(
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
            },
        )
        assert "proves:" in text
        assert "proved_by:" not in text

    def test_format_show_defaults_owner(self) -> None:
        text = render.format_show(
            {
                "self": {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "x",
                },
            },
        )
        assert "owner:       (unassigned)" in text

    def test_format_show_hides_empty_optional_fields(self) -> None:
        text = render.format_show(
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
                },
            },
        )
        assert "validation" not in text
        assert "labels" not in text
        assert "subscribers" not in text
        assert "agent-cost" not in text
        assert "resource-cost" not in text

    def test_format_show_formats_extra_list_values(self) -> None:
        text = render.format_show(
            {
                "self": {
                    "id": "abc",
                    "kind": "Issue",
                    "seq": 1,
                    "status": "active",
                    "title": "x",
                    "issue_kind": ["task", "bug"],
                },
            },
        )
        assert "kind:        task,bug" in text
        assert "issue_kind" not in text
        assert "['task'" not in text

    def test_format_show_includes_selected_edge_metadata(self) -> None:
        text = render.format_show(
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
            },
        )
        assert "Selected edge:" in text
        assert "priority  : 0" in text
        assert "valence   : 0.7" in text
        assert "labels    : edge" in text
        assert "note      : must land first" in text
        assert "edge_priority" not in text
        assert "edge_labels" not in text

    def test_format_changes(self) -> None:
        text = render.format_changes(
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
                },
            ],
        )
        expected = datetime(2026, 5, 18, 12, 34, 56, tzinfo=UTC).astimezone()
        assert expected.strftime("%Y-%m-%d %H:%M:%S") in text
        assert "Issue#abcdef12" in text
        assert "actor=alice principal=cli@example.com" in text
        assert "status: active -> complete" in text

    @pytest.mark.parametrize(
        "created",
        ["2024-03-07T17:23:41Z", "2024-03-07T11:53:41-05:30"],
    )
    def test_audit_feed_preserves_instants_with_utc_and_negative_offsets(
        self,
        created: str,
    ) -> None:
        text = render.format_changes(
            [
                {
                    "created": created,
                    "kind": "status",
                    "subject_kind": "Issue",
                    "subject_id": "c722336d-064c-499b-88c0-5bca7b9bc614",
                    "actor": "alice",
                },
            ],
        )
        expected = datetime(2024, 3, 7, 17, 23, 41, tzinfo=UTC).astimezone()

        assert expected.strftime("%Y-%m-%d %H:%M:%S") in text
        assert "alice" in text

    @pytest.mark.parametrize("view_kind", ["show", "edge"])
    def test_recent_audit_entries_preserve_created_timestamp(
        self,
        view_kind: str,
    ) -> None:
        change = {"created": "2024-03-07T17:23:41", "kind": "status", "actor": "alice"}
        if view_kind == "show":
            text = render.format_show(
                {
                    "self": {"kind": "Issue", "seq": 1, "title": "audit subject"},
                    "changes": [change],
                },
                changes=True,
            )
        else:
            text = render.format_edge(
                {
                    "title": "requires",
                    "endpoints": [
                        {"label": "from", "kind": "Issue", "seq": 1, "title": "child"},
                        {"label": "to", "kind": "Issue", "seq": 2, "title": "parent"},
                    ],
                    "edge": {},
                    "changes": [change],
                },
                changes=True,
            )
        expected = datetime(2024, 3, 7, 17, 23, 41, tzinfo=UTC).astimezone()

        assert expected.strftime("%Y-%m-%d %H:%M:%S") in text
        assert "alice" in text

    def test_format_changes_prints_local_time(self) -> None:
        text = render.format_changes(
            [
                {
                    "created": datetime(2026, 5, 18, 12, 34, 56, tzinfo=UTC),
                    "kind": "status",
                    "subject_kind": "Issue",
                    "subject_id": "abcdef123456",
                    "actor": "alice",
                },
            ],
        )
        expected = datetime(2026, 5, 18, 12, 34, 56, tzinfo=UTC).astimezone()
        assert expected.strftime("%Y-%m-%d %H:%M:%S") in text
        assert "T12:34:56" not in text
        assert "Issue#abcdef12" in text
        assert "actor=alice" in text


class TestPublicRenderRegressions:
    @pytest.mark.parametrize(
        ("kind", "field", "value", "expected"),
        [
            ("Issue", "owner", "owner-alice", ("owner-alice",)),
            ("Issue", "description", "detailed finding", ("detailed finding",)),
            ("Issue", "subscribers", ["bob", "carol"], ("bob,carol",)),
            ("Issue", "issue_kind", ["task", "bug"], ("task,bug",)),
            ("Issue", "priority", 0, ("0",)),
            ("Issue", "validation", "run validation", ("run validation",)),
            ("Issue", "labels", ["urgent", "review"], ("urgent,review",)),
            ("Belief", "judgement", "disproven", ("disproven",)),
            ("Belief", "confidence", 0.375, ("0.375",)),
            ("Experiment", "outcome", "lift measured", ("lift measured",)),
            (
                "Experiment",
                "codechanges",
                ["change-a", "change-b"],
                ("change-a", "change-b"),
            ),
            ("Paper", "abstract", "paper abstract", ("paper abstract",)),
            ("Paper", "authors", ["Ada", "Grace"], ("Ada,Grace",)),
            ("Paper", "publication_type", "inproceedings", ("inproceedings",)),
            ("Paper", "venue", "Nature", ("Nature",)),
            ("Paper", "subvenue", "main-track", ("main-track",)),
            ("Paper", "publish_date", "2024-03-07", ("2024-03-07",)),
            ("Paper", "source", "doi:10.1234/example", ("doi:10.1234/example",)),
            ("CodeChange", "sha", "abc123def456", ("abc123def456",)),
            (
                "WebResult",
                "url",
                "https://example.org/result",
                ("https://example.org/result",),
            ),
            ("WebSearch", "query", "memory retrieval", ("memory retrieval",)),
            ("WebSearch", "provider", "openalex", ("openalex",)),
            ("AgentSession", "cli", "codex", ("codex",)),
        ],
    )
    def test_public_views_preserve_independently_supplied_kind_fields(
        self,
        kind: str,
        field: str,
        value: object,
        expected: tuple[str, ...],
    ) -> None:
        row = {
            "kind": kind,
            "seq": 17,
            "status": "complete",
            "title": "subject",
            field: value,
        }
        table = render.format_table([row], width=1000)
        detail = render.format_show({"self": row})

        for supplied in expected:
            assert supplied in table.splitlines()[2]
            assert supplied in detail

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("edge_priority", 0, "0"),
            ("edge_valence", -0.625, "-0.625"),
            ("edge_labels", ["release", "blocking"], "release,blocking"),
            ("edge_note", "finish prerequisite", "finish prerequisite"),
        ],
    )
    def test_wide_table_preserves_independently_supplied_edge_metadata(
        self,
        field: str,
        value: object,
        expected: str,
    ) -> None:
        text = render.format_table(
            [{"kind": "Issue", "seq": 17, "title": "subject", field: value}],
            width=1000,
        )

        assert expected in text.splitlines()[2]

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({}, ["Issue#17"]),
            ({"status": None, "title": None}, ["Issue#17"]),
            ({"status": "complete"}, ["Issue#17", "complete"]),
            ({"title": "short-title"}, ["Issue#17", "short-title"]),
        ],
    )
    def test_table_defaults_do_not_invent_values_or_unset_priority(
        self,
        fields: dict[str, object],
        expected: list[str],
    ) -> None:
        text = render.format_table(
            [{"kind": "Issue", "seq": 17, "priority": None, **fields}],
            width=1000,
        )

        assert text.splitlines()[2].split() == expected
        assert len(text.splitlines()[0].split()) == 3

    @pytest.mark.parametrize(
        "field",
        ["owner", "description", "validation", "edge_note"],
    )
    def test_natural_exact_fit_keeps_optional_values_and_full_title(
        self,
        field: str,
    ) -> None:
        text = render.format_table(
            [
                {
                    "kind": "Issue",
                    "seq": 17,
                    "status": "complete",
                    "title": "abcdefghijklmnopqrstuvwx",
                    field: "0123456789abcdefghij",
                },
            ],
            width=66,
        )

        assert "abcdefghijklmnopqrstuvwx" in text.splitlines()[2]
        assert "0123456789abcdefghij" in text.splitlines()[2]
        assert all(len(line) <= 66 for line in text.splitlines())

    @pytest.mark.parametrize("width", [49, 50, 60])
    def test_optional_owner_and_separators_share_a_feasible_width_budget(
        self,
        width: int,
    ) -> None:
        text = render.format_table(
            [
                {
                    "kind": "Issue",
                    "seq": 17,
                    "status": "active",
                    "title": "t" * 80,
                    "owner": "alice",
                },
            ],
            width=width,
        )

        assert all(len(line) <= width for line in text.splitlines())
        assert "Issue#17" in text
        assert "active" in text
        assert "t" * 10 in text
        if width >= 50:
            assert "alice" in text.splitlines()[2]

    def test_long_notes_are_capped_without_displacing_a_fitting_title(self) -> None:
        text = render.format_table(
            [
                {
                    "kind": "Issue",
                    "seq": 17,
                    "status": "complete",
                    "title": "t" * 120,
                    "description": "d" * 100,
                    "validation": "v" * 110,
                    "edge_note": "n" * 140,
                },
            ],
            width=400,
        )
        cells = re.split(r" {2,}", text.splitlines()[2].strip())

        assert len(cells) == 6
        assert cells[2] == "t" * 120
        for cell, supplied in zip(
            cells[3:],
            ("d" * 100, "v" * 110, "n" * 140),
            strict=True,
        ):
            assert cell.startswith(supplied[:20])
            assert len(cell) < len(supplied)
        assert all(len(line) <= 400 for line in text.splitlines())

    @pytest.mark.parametrize("width", [60, 80, 100])
    def test_long_title_and_note_remain_visible_within_constrained_width(
        self,
        width: int,
    ) -> None:
        text = render.format_table(
            [
                {
                    "kind": "Issue",
                    "seq": 17,
                    "status": "active",
                    "title": "t" * 120,
                    "edge_note": "n" * 140,
                },
            ],
            width=width,
        )

        assert all(len(line) <= width for line in text.splitlines())
        assert "Issue#17" in text
        assert "active" in text
        assert "t" * 10 in text.splitlines()[2]
        assert "nn" in text.splitlines()[2]
        assert "t" * 120 not in text
        assert "n" * 140 not in text

    @pytest.mark.parametrize(
        "changes",
        [None, False, True],
        ids=["default", "off", "on"],
    )
    def test_edge_preserves_endpoint_roles_metadata_and_history_opt_in(
        self,
        changes: bool | None,
    ) -> None:
        view: dict[str, object] = {
            "title": "proves",
            "endpoints": [
                {
                    "label": "citing artifact",
                    "kind": "Paper",
                    "seq": 17,
                    "title": "source evidence",
                },
                {
                    "label": "proven claim",
                    "kind": "Belief",
                    "seq": 29,
                    "title": "target proposition",
                },
            ],
            "edge": {
                "edge_valence": -0.625,
                "edge_labels": ["release", "blocking"],
                "edge_note": "weighed evidence",
            },
            "changes": [
                {
                    "kind": "edge_added",
                    "actor": "auditor",
                    "new": {"edge_note": "history-marker"},
                },
            ],
        }
        text = (
            render.format_edge(view)
            if changes is None
            else render.format_edge(view, changes=changes)
        )
        blocks = text.split("\n\n")

        for role, ref, title in (
            ("citing artifact", "Paper#17", "source evidence"),
            ("proven claim", "Belief#29", "target proposition"),
        ):
            assert any(
                role in block and ref in block and title in block for block in blocks
            )
        for supplied in ("-0.625", "release,blocking", "weighed evidence"):
            assert supplied in text
        assert ("history-marker" in text) is (changes is True)
        assert ("auditor" in text) is (changes is True)

    @pytest.mark.parametrize("missing", [True, False], ids=["missing", "null"])
    def test_edge_unset_titles_and_history_do_not_invent_text(
        self,
        missing: bool,
    ) -> None:
        unset: dict[str, object] = {} if missing else {"title": None}
        history: dict[str, object] = {} if missing else {"changes": None}
        text = render.format_edge(
            {
                "title": "requires",
                "endpoints": [
                    {"label": "requirer", "kind": "Issue", "seq": 17, **unset},
                    {"label": "prerequisite", "kind": "Issue", "seq": 29, **unset},
                ],
                "edge": {"edge_priority": 0},
                **history,
            },
            changes=True,
        )

        assert "Issue#17" in text
        assert "Issue#29" in text
        assert "None" not in text
        assert "XXXX" not in text
        assert any(line.partition(":")[2].strip() == "0" for line in text.splitlines())

    @pytest.mark.parametrize(
        "changes",
        [None, False, True],
        ids=["default", "off", "on"],
    )
    @pytest.mark.parametrize("include_id", [False, True])
    def test_show_preserves_metadata_relations_and_explicit_display_flags(
        self,
        changes: bool | None,
        include_id: bool,
    ) -> None:
        view: dict[str, object] = {
            "self": {
                "kind": "Experiment",
                "seq": 17,
                "status": "complete",
                "id": "c722336d-064c-499b-88c0-5bca7b9bc614",
                "title": "measured result",
                "owner": "owner-alice",
                "account": "account-bob",
                "description": "held-out measurement",
                "labels": ["science", "review"],
                "subscribers": ["carol", "dave"],
                "outcome": "improved retrieval",
                "codechanges": ["change-a", "change-b"],
                "marginal_cost": {"agent_usd": 1.25, "resource_usd": 2.75},
            },
            "selected_edge": {
                "edge_labels": ["selected-label"],
                "edge_note": "selected-note",
            },
            "edges": {
                "produced_by": [
                    {
                        "kind": "AgentSession",
                        "seq": 31,
                        "title": "origin session",
                        "note": "origin-note",
                    },
                ],
            },
            "backlinks": {
                "proves": [
                    {
                        "kind": "Paper",
                        "seq": 43,
                        "title": "support paper",
                        "valence": -0.375,
                        "labels": ["support-label"],
                        "note": "support-note",
                    },
                ],
            },
            "changes": [
                {
                    "kind": "outcome",
                    "actor": "auditor",
                    "new": {"outcome": "history-marker"},
                },
            ],
        }
        if changes is None:
            text = (
                render.format_show(view, include_id=True)
                if include_id
                else render.format_show(view)
            )
        else:
            text = render.format_show(view, changes=changes, include_id=include_id)

        for supplied in (
            "Experiment#17",
            "complete",
            "measured result",
            "owner-alice",
            "account-bob",
            "held-out measurement",
            "science,review",
            "carol,dave",
            "improved retrieval",
            "change-a",
            "change-b",
            "$1.2500",
            "$2.7500",
            "selected-label",
            "selected-note",
            "AgentSession#31",
            "origin session",
            "origin-note",
            "Paper#43",
            "support paper",
            "-0.375",
            "support-label",
            "support-note",
        ):
            assert supplied in text
        assert ("c722336d-064c-499b-88c0-5bca7b9bc614" in text) is include_id
        assert ("history-marker" in text) is (changes is True)

    @pytest.mark.parametrize(
        "fields",
        [
            {},
            {"account": None, "title": None, "marginal_cost": None},
            {"marginal_cost": {"agent_usd": None, "resource_usd": None}},
        ],
        ids=["missing", "null-envelope", "null-axes"],
    )
    @pytest.mark.parametrize(
        "changes",
        [{}, {"changes": None}],
        ids=["missing-history", "null-history"],
    )
    def test_show_absent_fields_costs_and_history_invent_nothing(
        self,
        fields: dict[str, object],
        changes: dict[str, object],
    ) -> None:
        text = render.format_show(
            {
                "self": {"kind": "Issue", "seq": 17, "status": "active", **fields},
                **changes,
            },
            changes=True,
        )

        assert "Issue#17" in text
        assert "XXXX" not in text
        assert "$" not in text
        assert "None" not in text

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("cli_session_id", "session-identity"),
            ("started", "2024-03-07T17:23:41Z"),
            ("ended", "2024-03-08T01:02:03Z"),
        ],
    )
    def test_show_preserves_independently_supplied_session_metadata(
        self,
        field: str,
        value: str,
    ) -> None:
        text = render.format_show(
            {
                "self": {
                    "kind": "AgentSession",
                    "seq": 17,
                    "status": "complete",
                    field: value,
                },
            },
        )

        assert value in text


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
                },
            ],
        },
        "backlinks": {"requires": [{"kind": "Issue", "seq": 2, "title": "back"}]},
        "changes": [
            {
                "created": "2026-05-18T00:00:00",
                "kind": "created",
                "actor": "system",
            },
        ],
    }


# Folded in from former crasher_test.py.


def test_format_local_time_handles_none() -> None:
    """``None`` / empty / unparseable timestamps must produce a placeholder."""
    assert render._format_local_time(None) == ""
    assert render._format_local_time("") == ""
    assert render._format_local_time("x") == ""


def test_edge_annotation_includes_peer_priority() -> None:
    """A related peer's edge ``priority`` must appear in the compact annotation.

    The server emits ``priority`` on the peer ref (``web.py:_add_edge_annotation``)
    and ``_format_selected_edge`` shows it, but the compact relation suffix
    ``_edge_annotation`` dropped it. A ``priority=0`` peer must still render its
    priority (zero is a real, set value, not absence).
    """
    text = render.format_show(
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
                    {"kind": "Issue", "seq": 4, "title": "prerequisite", "priority": 0},
                ],
            },
        },
    )
    assert "prio=0" in text


def test_print_rows_json(capsys: pytest.CaptureFixture[str]) -> None:
    print_rows([{"id": "abc", "kind": "Issue"}], "json")
    out = capsys.readouterr().out
    assert '"id": "abc"' in out


def test_show_recent_history_keeps_only_first_ten_entries() -> None:
    changes = [{"kind": "updated", "actor": f"history-{index}"} for index in range(11)]
    text = render.format_show(
        {
            "self": {"kind": "Issue", "seq": 17, "status": "active"},
            "changes": changes,
        },
        changes=True,
    )

    assert re.findall(r"actor=(history-\d+)", text) == [
        f"history-{index}" for index in range(10)
    ]


def test_edge_recent_history_keeps_only_first_ten_entries() -> None:
    changes = [
        {"kind": "edge_added", "actor": f"history-{index}"} for index in range(11)
    ]
    text = render.format_edge(
        {
            "title": "requires",
            "endpoints": [
                {"label": "dependent", "kind": "Issue", "seq": 17},
                {"label": "prerequisite", "kind": "Issue", "seq": 18},
            ],
            "edge": {},
            "changes": changes,
        },
        changes=True,
    )

    assert re.findall(r"actor=(history-\d+)", text) == [
        f"history-{index}" for index in range(10)
    ]


def test_format_edge_includes_changes_section() -> None:
    view: dict[str, object] = {
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
            },
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


def test_show_extra_labels_share_the_fixed_value_column() -> None:
    text = render.format_show({"self": {"title": "VALUE", "judgement": "VALUE"}})
    rows = [line for line in text.splitlines() if "VALUE" in line]
    assert len(rows) == 2
    assert rows[0].index("VALUE") == rows[1].index("VALUE")
    assert "judgement:" in rows[1]


def test_session_hit_rendering_all_branches() -> None:
    assert render.format_session_hits({}) == "(no matches)\n"
    assert render.format_session_hits({"hits": None}) == "(no matches)\n"
    assert render.format_session_hits({"hits": []}) == "(no matches)\n"
    assert (
        render.format_session_hits({"degraded": True})
        == "(semantic search unavailable; showing full-text results only)\n(no matches)\n"
    )
    assert (
        render.format_session_hits(
            {
                "hits": [
                    {
                        "session_id": "123456789",
                        "part": 2,
                        "idx": 3,
                        "score": 0.125,
                        "source": "fts",
                        "title": "title",
                        "snippet": "  excerpt  ",
                    },
                ],
            },
        )
        == "0.1250  fts       12345678#2/3  title\n  excerpt\n"
    )
    empty_hit: dict[str, object] = {}
    assert (
        render.format_session_hits({"hits": [empty_hit]})
        == "0.0000            #0/0  \n"
    )
    assert (
        render.format_session_hits({"hits": [{"snippet": "   "}]})
        == "0.0000            #0/0  \n"
    )


def test_label_resolution_splits_and_trims() -> None:
    assert render.resolve_labels(None) == []
    empty: list[str] = []
    assert render.resolve_labels(empty) == []
    assert render.resolve_labels([" a, b ,,", "c", " "]) == ["a", "b", "c"]


def test_actor_display_uses_system_and_omits_redundant_principal() -> None:
    assert render._format_actor({}) == "actor=system"
    assert (
        render._format_actor({"actor": "alice", "principal": "alice"}) == "actor=alice"
    )
    assert (
        render._format_actor({"actor": "alice", "principal": "bob"})
        == "actor=alice principal=bob"
    )


def test_nothing_echoed_can_drive_a_terminal_or_hide_from_a_reader() -> None:
    for unsafe in (
        "\x1b[8mhidden",
        "\u009b8mhidden",
        "\u009d0;title\x07",
        "\x1b]52;c;aGk=\x07",
        "over\rwrite",
        "a\u202eb",
        "tag\U000e0049",
    ):
        cleaned = render.terminal_safe(unsafe)
        assert not set(cleaned) & {
            "\x1b",
            "\u009b",
            "\u009d",
            "\x07",
            "\r",
            "\u202e",
            "\U000e0049",
        }


def test_what_a_reader_needs_is_left_as_it_was() -> None:
    plain = "Issue#1\tZoë 蛋白质 🧬 -- a\u200db\nnext line\n"
    assert render.terminal_safe(plain) == plain


def test_ids_show_only_where_the_flag_set_them() -> None:
    assert not render.show_ids()
    token = render.SHOW_IDS.set(True)
    try:
        assert render.show_ids()
    finally:
        render.SHOW_IDS.reset(token)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
