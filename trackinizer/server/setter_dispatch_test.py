"""Tests for the per-column hooks and merged specs ``Store._set_field`` dispatches on."""

from __future__ import annotations

from dataclasses import dataclass, field

import uuid

import pytest

from trackinizer.server.setter_dispatch import (
    COLUMN_SPECS,
    NO_HOOKS,
    RUNTIME_HOOKS,
    _column_specs,
)
from trackinizer.types.columns import ColumnSpec, column_specs
from trackinizer.types.inquiries import Inquiry, Issue, Paper


_LIST_COLUMNS = ("issue_kind", "labels", "agentsession_rooms", "subscribers")


def test_the_default_hooks_pass_every_value_through() -> None:
    value = ["b", "a"]

    assert NO_HOOKS.normalize(value) is value
    assert NO_HOOKS.encode(value) is value
    assert NO_HOOKS.decode_old(value) is value
    assert (NO_HOOKS.validate, NO_HOOKS.notify_old_subscribers) == (None, False)


@pytest.mark.parametrize("column", _LIST_COLUMNS)
def test_a_string_list_column_canonicalizes_and_stores_a_list(column: str) -> None:
    hooks = RUNTIME_HOOKS[column]

    assert hooks.normalize([" b", "a", "b ", ""]) == ("b", "a")
    assert hooks.encode(("b", "a")) == ["b", "a"]
    assert hooks.encode(None) is None
    assert hooks.decode_old(["b", "a"]) == ("b", "a")
    assert hooks.decode_old(None) is None


@pytest.mark.parametrize("column", _LIST_COLUMNS)
def test_a_string_list_column_refuses_a_non_string_item(column: str) -> None:
    with pytest.raises(ValueError, match="isinstance"):
        _ = RUNTIME_HOOKS[column].normalize(["a", 1])


def test_only_subscribers_route_the_audit_to_the_old_subscribers() -> None:
    assert [
        column
        for column, hooks in RUNTIME_HOOKS.items()
        if hooks.notify_old_subscribers
    ] == ["subscribers"]


def test_codechanges_keep_order_as_a_tuple_of_uuids() -> None:
    first, second = uuid.UUID(int=2), uuid.UUID(int=1)
    hooks = RUNTIME_HOOKS["experiment_codechanges"]

    assert hooks.normalize([first, second]) == (first, second)
    assert hooks.normalize(None) is None
    assert hooks.decode_old([first]) == (first,)


def test_a_byline_strips_and_drops_blanks_but_keeps_order_and_repeats() -> None:
    hooks = RUNTIME_HOOKS["paper_authors"]

    assert hooks.normalize(["Smith ", "", "Lee", "Smith"]) == ("Smith", "Lee", "Smith")
    assert hooks.normalize(None) is None
    assert hooks.decode_old(["Lee"]) == ("Lee",)


def test_column_specs_key_kind_columns_by_their_storage_name() -> None:
    assert {"status", "labels", "paper_authors", "experiment_codechanges"} <= set(
        COLUMN_SPECS,
    )
    assert "authors" not in COLUMN_SPECS


def test_merging_keys_each_class_spec_by_storage_name_and_shares_base_columns() -> None:
    merged = _column_specs(Inquiry, Issue, Paper)

    assert merged["paper_authors"] == column_specs(Paper)["authors"]
    assert merged["status"] == column_specs(Inquiry)["status"]
    assert set(_column_specs(Inquiry)) < set(merged)
    assert _column_specs(Paper, Paper) == _column_specs(Paper)
    assert _column_specs() == {}


@dataclass(frozen=True, slots=True, kw_only=True)
class _TextNote:
    note: str = field(default="", metadata=ColumnSpec(sql_type="TEXT"))


@dataclass(frozen=True, slots=True, kw_only=True)
class _JsonNote:
    note: str = field(default="", metadata=ColumnSpec(sql_type="JSONB"))


def test_two_classes_disagreeing_on_one_column_are_refused() -> None:
    with pytest.raises(AssertionError) as caught:
        _ = _column_specs(_TextNote, _JsonNote)

    assert str(caught.value) == (
        f"inconsistent ColumnSpec for 'note': {ColumnSpec(sql_type='TEXT')!r} "
        f"vs {ColumnSpec(sql_type='JSONB')!r}"
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
