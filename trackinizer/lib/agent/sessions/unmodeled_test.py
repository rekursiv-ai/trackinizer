"""Tests for trackinizer.lib.agent.sessions.unmodeled."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import inspect
import json
import math

import pytest

from trackinizer.lib.absent import ABSENT
from trackinizer.lib.agent.sessions.unmodeled import (
    extract_unmodeled_fields,
    read_field_keeping_invalid,
    restore_unmodeled_fields,
    same_json_value,
)
from trackinizer.lib.codec import Invalid, loads


class TestLosslessFields:
    def test_read_field_keeping_invalid_distinguishes_every_field_state(self) -> None:
        source = {"null": None, "value": "", "invalid": 7}

        missing = read_field_keeping_invalid(source, "missing", str)
        null = read_field_keeping_invalid(source, "null", str)
        value = read_field_keeping_invalid(source, "value", str)
        invalid = read_field_keeping_invalid(source, "invalid", str)

        assert missing is ABSENT
        assert null is None
        assert value == ""
        assert invalid == Invalid(raw=7)
        assert isinstance(invalid, Invalid)

    def test_restore_keeps_the_original_spelling_of_an_equal_number(self) -> None:
        source = {"value": 1}
        state = read_field_keeping_invalid(source, "value", float)
        stored = extract_unmodeled_fields(source, fields={"value": state})
        assert isinstance(state, float)

        restored = restore_unmodeled_fields(stored, {"value": state})
        assert restored == source
        assert type(restored["value"]) is int
        assert restore_unmodeled_fields(stored, {"value": 2.0}) == {"value": 2.0}

    def test_extract_and_restore_preserve_presence_order_and_invalid_values(
        self,
    ) -> None:
        source = {
            "before": 1,
            "null": None,
            "value": "old",
            "invalid": True,
            "after": 2,
        }
        fields = {
            key: read_field_keeping_invalid(source, key, target)
            for key, target in {
                "missing": str,
                "null": str,
                "value": str,
                "invalid": int,
            }.items()
        }

        stored = extract_unmodeled_fields(source, fields=fields)
        encoded = loads(json.dumps(stored))
        restored = restore_unmodeled_fields(
            cast(Mapping[str, object], encoded),
            {"missing": "default", "null": "now set", "value": "new", "invalid": 9},
        )

        assert restored == {
            "before": 1,
            "null": "now set",
            "value": "new",
            "invalid": True,
            "after": 2,
        }
        assert list(restored) == list(source)

    def test_stateful_residual_also_drops_consumed_non_field_keys(self) -> None:
        source = {"value": 1, "derived": 2, "other": 3}
        stored = extract_unmodeled_fields(
            source,
            {"derived"},
            fields={"value": read_field_keeping_invalid(source, "value", int)},
        )

        assert restore_unmodeled_fields(stored, {"value": 4}) == {
            "value": 4,
            "other": 3,
        }

    def test_provider_key_matching_the_metadata_tag_survives(self) -> None:
        source = {"$__custom_json_fields__": "provider", "value": 1}
        stored = extract_unmodeled_fields(
            source,
            fields={"value": read_field_keeping_invalid(source, "value", int)},
        )

        assert restore_unmodeled_fields(stored, {"value": 2}) == {
            "$__custom_json_fields__": "provider",
            "value": 2,
        }

    def test_plain_residual_drops_consumed_keys_without_metadata(self) -> None:
        assert extract_unmodeled_fields({"a": 1, "b": 2}, {"a"}) == {"b": 2}

    def test_plain_residual_escapes_a_provider_replay_marker(self) -> None:
        source = {
            "$__custom_json_fields__": {
                "version": 1,
                "order": ["x"],
                "states": {"x": "value"},
                "residual": {},
            },
            "x": "provider",
        }

        assert restore_unmodeled_fields(extract_unmodeled_fields(source), {}) == source

    def test_restore_requires_a_value_for_every_modeled_field(self) -> None:
        source = {"value": 1}
        stored = extract_unmodeled_fields(
            source,
            fields={"value": read_field_keeping_invalid(source, "value", int)},
        )

        with pytest.raises(KeyError, match="value"):
            restore_unmodeled_fields(stored, {})

    def test_provider_values_must_be_json_safe(self) -> None:
        source = {"x": object()}
        with pytest.raises(TypeError, match="x"):
            extract_unmodeled_fields(source)
        with pytest.raises(TypeError, match="x"):
            read_field_keeping_invalid(source, "x", int)

    def test_restore_rejects_unknown_field_state_labels(self) -> None:
        stored = {
            "$__custom_json_fields__": {
                "version": 1,
                "order": ["value"],
                "states": {"value": "garbage"},
                "raw": {"value": 1},
                "residual": {},
            },
        }
        assert restore_unmodeled_fields(stored, {"value": 2}) == stored

    def test_plain_residual_rejects_runtime_non_string_keys(self) -> None:
        with pytest.raises(TypeError, match="key"):
            extract_unmodeled_fields({1: "value"})  # ty: ignore[invalid-argument-type] -- These negative tests deliberately pass invalid field types to verify rejection.  # pyright: ignore[reportArgumentType] -- The test deliberately passes a non-string key to verify rejection.

    def test_stateful_residual_rejects_runtime_non_string_keys(self) -> None:
        with pytest.raises(TypeError, match="key"):
            extract_unmodeled_fields({1: "value"}, fields={})  # ty: ignore[invalid-argument-type] -- These negative tests deliberately pass invalid field types to verify rejection.  # pyright: ignore[reportArgumentType] -- The test deliberately passes a non-string key to verify rejection.

    def test_plain_residual_escape_preserves_numeric_spelling(self) -> None:
        source = {
            "$__custom_json_fields__": {
                "version": 1,
                "order": ["x"],
                "states": {"x": "value"},
                "residual": {},
            },
            "x": 1.0,
        }

        restored = restore_unmodeled_fields(
            _d(loads(json.dumps(extract_unmodeled_fields(source)))),
            {},
        )

        assert restored == source
        assert type(restored["x"]) is float

    def test_malformed_replay_raw_is_not_treated_as_an_envelope(self) -> None:
        stored = {
            "$__custom_json_fields__": {
                "version": 1,
                "order": ["x"],
                "states": {"x": "value"},
                "raw": "garbage",
                "residual": {},
            },
        }

        assert restore_unmodeled_fields(stored, {"x": 2}) == stored

    def test_unknown_replay_label_fallback_is_documented(self) -> None:
        doc = inspect.getdoc(restore_unmodeled_fields)
        assert doc is not None
        assert "unknown field-state labels" in doc

    @pytest.mark.parametrize(
        "fields",
        [
            {"version": 2, "order": [], "states": {}, "residual": {}},
            {"version": 1, "order": "x", "states": {}, "residual": {}},
            {"version": 1, "order": [], "states": "x", "residual": {}},
            {"version": 1, "order": [], "states": {}, "residual": "x"},
        ],
    )
    def test_restore_passes_a_malformed_envelope_through(
        self,
        fields: dict[str, object],
    ) -> None:
        stored = {"$__custom_json_fields__": fields}
        assert restore_unmodeled_fields(stored, {}) == stored


class TestSameJsonValue:
    def test_a_bool_never_equals_the_integer_it_would_coerce_to(self) -> None:
        assert not same_json_value(True, 1)
        assert not same_json_value(0, False)
        assert same_json_value(True, True)

    def test_two_nans_are_the_same_value(self) -> None:
        assert same_json_value(math.nan, math.nan)
        assert not same_json_value(math.nan, 1.0)

    def test_mappings_compare_by_keys_and_values_recursively(self) -> None:
        assert same_json_value({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]})
        assert not same_json_value({"a": 1}, {"b": 1})
        assert not same_json_value({"a": 1}, {"a": 1, "b": 2})
        assert not same_json_value({"a": [True]}, {"a": [1]})

    def test_sequences_compare_by_length_and_items_but_strings_stay_atoms(
        self,
    ) -> None:
        assert same_json_value([1, [2.5, "x"]], (1, (2.5, "x")))
        assert not same_json_value([1, 2], [1, 2, 3])
        assert not same_json_value([1, 2], [1, 3])
        assert not same_json_value("ab", ["a", "b"])


class TestUnmodeledEnvelopeVersion:
    @pytest.mark.parametrize("version", [1, 1.0, "1", " 1 "])
    def test_a_spelling_of_version_one_is_recognized(self, version: object) -> None:
        source = {"value": 1}
        state = read_field_keeping_invalid(source, "value", float)
        stored = _with_envelope_version(
            extract_unmodeled_fields(source, fields={"value": state}),
            version=version,
        )

        assert restore_unmodeled_fields(stored, {"value": 2.0}) == {"value": 2.0}

    @pytest.mark.parametrize("version", [True, 2, "2", "one", None])
    def test_any_other_version_leaves_the_stored_mapping_unchanged(
        self,
        version: object,
    ) -> None:
        source = {"value": 1}
        state = read_field_keeping_invalid(source, "value", float)
        stored = _with_envelope_version(
            extract_unmodeled_fields(source, fields={"value": state}),
            version=version,
        )

        assert restore_unmodeled_fields(stored, {"value": 2.0}) == stored


def _with_envelope_version(
    stored: Mapping[str, object],
    *,
    version: object,
) -> dict[str, object]:
    result = dict(stored)
    for key, envelope in stored.items():
        if isinstance(envelope, Mapping) and "version" in envelope:
            result[key] = {**cast(Mapping[str, object], envelope), "version": version}
    return result


def _d(value: object) -> dict[str, object]:
    """Narrow a decoded JSON object for a test assertion."""
    assert isinstance(value, dict)
    return {
        str(key): member for key, member in cast(dict[object, object], value).items()
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
