"""Tests for the ``Adapter`` protocol's value type."""

from __future__ import annotations

import dataclasses

import pytest

from trackinizer.runs.adapters.custom_types import Subagent


def test_a_subagent_names_its_child_and_parent_and_defaults_no_note() -> None:
    subagent = Subagent(child="c1", parent="p1")

    assert (subagent.child, subagent.parent, subagent.note) == ("c1", "p1", "")
    assert all(field.kw_only for field in dataclasses.fields(subagent))
    with pytest.raises(dataclasses.FrozenInstanceError):
        subagent.__setattr__("note", "changed")


def test_a_subagent_keeps_what_the_cli_says_spawned_it() -> None:
    assert Subagent(child="c", parent="p", note="Task: review").note == "Task: review"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
