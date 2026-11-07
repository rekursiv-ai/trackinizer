"""Tests for the semantic-mapping contract: index units and the mapper protocol."""

from __future__ import annotations

import dataclasses

import pytest

from trackinizer.server.semantic_mapper import IndexUnit, SemanticMapper


class _ContentOnly:
    """Index each message's text whole; every other kind is not indexed."""

    name = "content-only"

    @property
    def indexed_kinds(self) -> frozenset[str]:
        return frozenset({"UserMessage"})

    def units(self, *, kind: str, text: str) -> tuple[IndexUnit, ...]:
        if kind not in self.indexed_kinds or not text:
            return ()
        return (IndexUnit(text=text),)


def test_a_unit_defaults_to_a_whole_content_span_searched_both_ways() -> None:
    unit = IndexUnit(text="hello")

    assert (unit.field, unit.chunk, unit.fts, unit.embed) == ("content", 0, True, True)


def test_a_unit_is_frozen_and_keyword_only() -> None:
    unit = IndexUnit(text="t", field="description", chunk=2, fts=False)

    assert (unit.field, unit.chunk, unit.fts, unit.embed) == (
        "description",
        2,
        False,
        True,
    )
    assert all(field.kw_only for field in dataclasses.fields(unit))
    with pytest.raises(dataclasses.FrozenInstanceError):
        unit.__setattr__("text", "u")


def test_a_policy_with_name_kinds_and_units_is_a_mapper() -> None:
    mapper = _ContentOnly()

    assert mapper.units(kind="UserMessage", text="hi") == (IndexUnit(text="hi"),)
    assert mapper.units(kind="ToolResult", text="hi") == ()


def test_an_object_without_units_is_not_a_mapper() -> None:
    assert not isinstance(IndexUnit(text="x"), SemanticMapper)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
