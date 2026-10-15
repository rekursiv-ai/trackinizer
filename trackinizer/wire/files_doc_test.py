"""Tests for the files document's shape and labels."""

from __future__ import annotations

from dataclasses import replace

import json

from treekle import from_plain, json_loads

import pytest

from trackinizer.wire.files_doc import (
    FILES_LABEL,
    FILES_OF,
    ChangedFile,
    FilesDoc,
    dump_files,
    load_files,
)


def _doc() -> FilesDoc:
    return FilesDoc(
        base="origin/main",
        files=("a.py", "b/c.py"),
        more_files=True,
        changed=(
            ChangedFile(path="a.py", change="modified", diff="+x\n", note="clipped"),
            ChangedFile(path="old.py", change="deleted", diff="", note="omitted"),
        ),
        more_changes=False,
        hidden=3,
    )


@pytest.mark.parametrize("hidden", [0, 3])
def test_a_files_document_reads_back_as_written(hidden: int) -> None:
    doc = replace(_doc(), hidden=hidden)

    assert load_files(dump_files(doc)) == doc


def test_the_json_holds_exactly_the_contract_keys() -> None:
    text = dump_files(_doc())
    tree = json_loads(dump_files(_doc()), dict[str, object])

    assert list(tree) == [
        "base",
        "files",
        "more_files",
        "changed",
        "more_changes",
        "hidden",
        "version",
        "kind",
    ]
    changed = from_plain(tree["changed"], list[dict[str, object]])
    assert [list(c) for c in changed] == [["path", "change", "diff", "note"]] * 2
    assert (tree["base"], tree["version"], tree["kind"]) == ("origin/main", 1, "files")
    assert ", " not in text
    assert ": " not in text


@pytest.mark.parametrize(
    ("change", "dropped", "named"),
    [
        ({"kind": "campaign"}, "", "'kind'"),
        ({"session": "x"}, "", "unknown field"),
        ({}, "base", "missing"),
        ({"version": 2}, "", "cannot read 2"),
        ({"hidden": -1}, "", "hidden must not be negative"),
    ],
    ids=["kind-not-files", "unknown-field", "no-base", "version-2", "hidden-negative"],
)
def test_a_document_of_another_shape_is_not_read_as_files(
    change: dict[str, object],
    dropped: str,
    named: str,
) -> None:
    tree = {
        k: v
        for k, v in json_loads(dump_files(_doc()), dict[str, object]).items()
        if k != dropped
    } | change

    with pytest.raises(ValueError, match=named):
        _ = load_files(json.dumps(tree))


def test_the_labels_are_the_contracts() -> None:
    assert (FILES_LABEL, FILES_OF) == ("files", "files:of:")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
