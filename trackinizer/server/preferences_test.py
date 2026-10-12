"""Tests for ``server/preferences.py``: reading a user's stored preferences."""

from __future__ import annotations

from pydantic import JsonValue

import pydantic
import pytest

from trackinizer.server.preferences import Preferences, stored_preferences


_DEFAULTS: dict[str, JsonValue] = {
    "theme": "dark",
    "chat_style": "default",
    "chat_side": "left",
    "highlight_mentions": True,
    "stars": [],
    "views": [],
    "aliases": [],
    "people": {},
    "read": {"boundary": None, "marks": []},
    "collapsed": [],
    "lens": None,
    "drafts": {},
    "create_draft": None,
}

_VIEW_REQUEST: dict[str, JsonValue] = {
    "kinds": ["Issue"],
    "filters": [{"field": "owner", "op": "re", "value": "^(ada)$"}],
}

_VIEW: dict[str, JsonValue] = {"id": "v1", "name": "Mine", "request": _VIEW_REQUEST}

_RELATION: dict[str, JsonValue] = {
    "option": "narrows:out",
    "target": {"id": "i1", "kind": "Issue", "seq": 7},
}

_CREATE_DRAFT: dict[str, JsonValue] = {
    "kind": "Issue",
    "title": "T",
    "description": "",
    "fields": {"confidence": "0.5"},
    "chips": {"priority": 20, "labels": ["a"], "owner": None},
    "relations": [_RELATION],
}


def test_nothing_stored_reads_as_the_defaults() -> None:
    assert stored_preferences(None).model_dump() == _DEFAULTS


def test_the_keys_set_override_their_defaults_and_unknown_ones_are_dropped() -> None:
    read = stored_preferences(
        {"theme": "system", "highlight_mentions": False, "retired": "x"},
    )
    assert read.model_dump() == {
        **_DEFAULTS,
        "theme": "system",
        "highlight_mentions": False,
    }


def test_a_value_its_key_does_not_take_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate({"chat_side": "middle"})


def test_the_user_data_keys_round_trip_whole() -> None:
    kept: dict[str, JsonValue] = {
        "stars": ["a", "b"],
        "views": [_VIEW],
        "aliases": ["ada"],
        "people": {"dan": {"name": "Dan", "type": "person"}},
        "read": {"boundary": "2026-10-01T00:00:00Z", "marks": ["c1"]},
        "collapsed": ["open"],
        "lens": "graph",
        "drafts": {"i1:description": {"text": "hi", "base": {"a": [1, None]}}},
        "create_draft": {
            **_CREATE_DRAFT,
            "relations": [
                {
                    "option": "narrows:out",
                    "target": {"id": "i1", "kind": "Issue", "seq": 7, "title": None},
                },
            ],
        },
    }
    assert stored_preferences(kept).model_dump(mode="json", include=set(kept)) == kept


def test_a_draft_names_its_base_even_when_unset() -> None:
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate({"drafts": {"i1:title": {"text": "x"}}})
    read = Preferences.model_validate(
        {"drafts": {"i1:title": {"text": "x", "base": None}}},
    )
    assert read.drafts["i1:title"].base is None


def _key_of(change: dict[str, JsonValue]) -> str:
    return next(iter(change))


@pytest.mark.parametrize(
    "change",
    [
        {"stars": ["x"] * 2_001},
        {"stars": ["x" * 101]},
        {"views": [_VIEW] * 201},
        {"views": [{**_VIEW, "name": "n" * 201}]},
        {
            "views": [
                {
                    **_VIEW,
                    "request": {
                        **_VIEW_REQUEST,
                        "filters": [{"field": "f", "op": "like", "value": ""}],
                    },
                },
            ],
        },
        {"views": [{**_VIEW, "extra": 1}]},
        {"aliases": ["a"] * 51},
        {"aliases": ["a" * 201]},
        {"people": {f"p{n}": {"name": "P", "type": "person"} for n in range(1_001)}},
        {"people": {"p": {"name": "P", "type": "robot"}}},
        {"read": {"boundary": None, "marks": ["m"] * 5_001}},
        {"read": {"boundary": None}},
        {"collapsed": ["c"] * 501},
        {"drafts": {f"d{n}": {"text": "", "base": None} for n in range(201)}},
        {"drafts": {"d": {"text": "x" * 100_001, "base": None}}},
        {"drafts": {"d": {"text": "", "base": "x" * 100_001}}},
        {"create_draft": {**_CREATE_DRAFT, "title": "x" * 100_001}},
        {"create_draft": {**_CREATE_DRAFT, "chips": {"labels": ["x" * 100_001]}}},
        {
            "create_draft": {
                **_CREATE_DRAFT,
                "relations": [_RELATION] * 101,
            },
        },
        {"create_draft": {**_CREATE_DRAFT, "kind": None}},
        {"lens": 3},
    ],
    ids=_key_of,
)
def test_a_value_past_its_bound_or_type_is_refused(
    change: dict[str, JsonValue],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate(change)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
