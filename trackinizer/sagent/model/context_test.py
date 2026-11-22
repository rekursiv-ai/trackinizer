"""Defaults, immutability, and names of ``ContextSize``."""

from __future__ import annotations

import dataclasses

import pytest

from trackinizer.sagent.model.capability import literal_members
from trackinizer.sagent.model.context import ContextSize, ContextSizeName
from trackinizer.sagent.model.cost import PriceCatalog


def test_the_default_is_the_whole_window_a_catalog_of_unknown_window_names() -> None:
    assert ContextSize() == PriceCatalog()["fullcontext"]


def test_fields_are_keyword_only_and_frozen() -> None:
    size = ContextSize(context_name="fullcontext", tokens=1_000_000, display_name="1M")
    assert size.context_name == "fullcontext"
    assert size.tokens == 1_000_000
    assert size.display_name == "1M"
    assert all(field.kw_only for field in dataclasses.fields(size))
    with pytest.raises(dataclasses.FrozenInstanceError):
        dataclasses.replace(size, tokens=2).__setattr__("tokens", 1)


def test_the_size_names_say_they_size_the_context() -> None:
    assert literal_members(ContextSizeName) == (
        "smallcontext",
        "midcontext",
        "fullcontext",
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
