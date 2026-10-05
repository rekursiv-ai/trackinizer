"""Copies of a JSON record with one field swapped to another JSON type."""

from __future__ import annotations

from typing import cast

import copy

from trackinizer.lib.agent.sessions.testdata.mistype import mistyped


def test_every_field_gets_every_other_json_type() -> None:
    record = {"a": "s", "b": {"c": 1}}

    found = {(path, type(_at(copy, path)).__name__) for path, copy in mistyped(record)}

    assert found == {
        *(("a", t) for t in ("int", "float", "bool", "list", "dict")),
        *(("b", t) for t in ("str", "int", "float", "bool", "list")),
        *(("b.c", t) for t in ("str", "float", "bool", "list", "dict")),
    }


def test_each_copy_changes_one_field_and_leaves_the_record_alone() -> None:
    record = {"a": "s", "b": ["x", {"c": True}]}

    for path, changed in mistyped(record):
        assert _at(changed, path) != _at(record, path)
        assert _put(changed, path, _at(record, path)) == record
    assert record == {"a": "s", "b": ["x", {"c": True}]}


def test_list_members_are_reached_by_index() -> None:
    paths = {path for path, _ in mistyped({"b": ["x", {"c": True}]})}

    assert paths == {"b", "b.0", "b.1", "b.1.c"}


def test_a_null_field_is_swapped_like_any_other() -> None:
    kinds = {type(_at(changed, "a")).__name__ for _, changed in mistyped({"a": None})}

    assert kinds == {"str", "int", "float", "bool", "list", "dict"}


def _at(value: object, path: str) -> object:
    for key in path.split("."):
        if isinstance(value, list):
            value = cast(list[object], value)[int(key)]
        else:
            value = cast(dict[str, object], value)[key]
    return value


def _put(value: object, path: str, leaf: object) -> object:
    out = copy.deepcopy(value)
    *parents, last = path.split(".")
    holder = _at(out, ".".join(parents)) if parents else out
    if isinstance(holder, list):
        cast(list[object], holder)[int(last)] = leaf
    else:
        cast(dict[str, object], holder)[last] = leaf
    return out


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
