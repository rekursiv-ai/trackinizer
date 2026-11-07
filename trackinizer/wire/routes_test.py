from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.types.columns import ColumnSpec, FlatColumn
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire import routes
from trackinizer.wire.routes import (
    edge_field_path,
    edge_field_routes,
    field_owner_kind,
    inquiry_field_path,
    inquiry_field_routes,
    inquiry_relation_fields,
    inquiry_row_fields,
)


if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def fresh_tables() -> Iterator[None]:
    """Derive every table in the test, not from a cache an import already filled."""
    cached = (
        inquiry_field_routes,
        field_owner_kind,
        edge_field_routes,
        inquiry_row_fields,
        inquiry_relation_fields,
    )
    for table in cached:
        table.cache_clear()
    yield
    for table in cached:
        table.cache_clear()


def _route(column: str) -> routes.InquiryFieldRoute:
    (route,) = (r for r in inquiry_field_routes() if r.column == column)
    return route


def test_list_column_gets_add_and_remove_verbs() -> None:
    labels = _route("labels")
    assert (labels.put, labels.delete, labels.patch) == (True, True, True)
    assert (labels.add_method, labels.sub_method) == ("add_label", "remove_label")
    assert labels.set_method == "set_labels"
    assert labels.element_type is str
    assert labels.owner_kind is None


def test_cost_axis_routes_through_add_cost() -> None:
    cost = _route("marginal_cost_agent_usd")
    assert cost.cost_axis == "agent_usd"
    assert (cost.put, cost.delete, cost.patch) == (True, True, True)
    assert (cost.set_method, cost.add_method, cost.sub_method) == (None, None, None)
    assert cost.compare_and_set is False
    assert cost.element_type == cost.value_type


def test_required_and_immutable_columns() -> None:
    account = _route("account")
    assert account.put is True
    assert account.delete is False
    assert account.patch is False
    assert account.element_type is None
    assert (account.add_method, account.sub_method) == (None, None)
    assert account.compare_and_set is False
    assert account.value_type is Inquiry.Actor
    opened = _route("opened_by_api_key_id")
    assert (opened.put, opened.delete, opened.set_method) == (False, False, None)
    assert opened.owner_kind == "agentsession"


def test_compare_and_set_column_accepts_expected() -> None:
    assert _route("owner").compare_and_set is True


def test_tuple_column_without_list_verbs_has_no_patch() -> None:
    """Only a ``list_verb_stem`` makes a column PATCH-able, whatever its type."""
    flat = FlatColumn(spec=ColumnSpec(sql_type="TEXT[]"), value_type=tuple[str, ...])
    route = routes._column_route("tags", flat)
    assert route.value_type == tuple[str, ...]
    assert (route.patch, route.element_type) == (False, None)
    assert (route.add_method, route.sub_method) == (None, None)


def test_non_route_editable_column_has_no_route() -> None:
    columns = [r.column for r in inquiry_field_routes()]
    assert "ended" not in columns
    assert len(columns) == len(set(columns))


def test_kind_specific_fields_route_under_their_kind() -> None:
    owners = field_owner_kind()
    assert owners["authors"] == "paper"
    assert "labels" not in owners
    assert inquiry_field_path("authors") == "/api/paper/{target_id}/authors"
    assert inquiry_field_path("labels") == "/api/inquiries/{target_id}/labels"


def test_edge_routes_patch_only_labels() -> None:
    by_column = {r.column: r for r in edge_field_routes()}
    assert set(by_column) == {"priority", "note", "valence", "labels"}
    assert by_column["labels"].patch is True
    assert by_column["labels"].element_type is str
    assert by_column["note"].patch is False
    assert edge_field_path("note") == "/api/edges/{from_id}/{edge_kind}/{to_id}/note"


def test_row_and_relation_fields() -> None:
    rows = inquiry_row_fields()
    relations = inquiry_relation_fields()
    assert {"kind", "title", "authors"} <= rows
    assert {"narrows", "proved_by", "produces"} <= relations
    assert "title" not in relations
    assert relations <= rows


def test_element_type_reads_homogeneous_tuples_only() -> None:
    assert routes._element_type(tuple[int, ...] | None) is int
    assert routes._element_type(tuple[int, ...]) is int
    assert routes._element_type(tuple[int, str]) is None
    assert routes._element_type(str) is None


def test_shared_column_has_no_single_owner() -> None:
    shared = FlatColumn(
        spec=ColumnSpec(
            sql_type="TEXT",
            applies_to_inquiry_kinds=frozenset({"Issue", "Paper"}),
        ),
        value_type=str,
    )
    assert routes._owner_kind(shared) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
