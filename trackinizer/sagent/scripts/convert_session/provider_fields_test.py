"""Tests for :mod:`trackinizer.sagent.scripts.convert_session.provider_fields`."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from typing import cast, get_args

import pytest

from trackinizer.sagent.journal.types import (
    UncategorizedRecord,
    UserMessage,
    record_classes,
)
from trackinizer.sagent.scripts.convert_session.provider_fields import (
    Stamped,
    kept_spelling,
    read_or_default,
    stamped,
)


@pytest.mark.parametrize("value", [None, "failed", ["a"], 3])
def test_a_missing_or_mistyped_object_reads_as_the_default(value: object) -> None:
    assert read_or_default(value, dict[str, object], default=dict[str, object]()) == {}


def test_a_present_value_of_the_target_type_is_read() -> None:
    assert read_or_default(
        {"a": 1},
        dict[str, object],
        default=dict[str, object](),
    ) == {"a": 1}
    assert read_or_default("x", str, default=None) == "x"


def test_a_mistyped_value_reads_as_none_when_the_default_is_none() -> None:
    assert read_or_default(7, str, default=None) is None


def test_a_missing_value_is_the_default_even_where_none_would_fit() -> None:
    """``object`` takes ``None`` as it is, yet a null field still reads as absent."""
    assert read_or_default(None, object, default="absent") == "absent"


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("2026-11-19T10:03:04+00:00", {}),
        ("2026-11-19T10:03:04.000001-08:00", {}),
        ("2026-11-19T10:03:04Z", {"$timestamp_as_written": "2026-11-19T10:03:04Z"}),
        (
            "2026-11-19T10:03:04.000+00:00",
            {"$timestamp_as_written": "2026-11-19T10:03:04.000+00:00"},
        ),
        # ``isoformat`` would add the offset the reader assumed.
        ("2026-11-19T10:03:04", {"$timestamp_as_written": "2026-11-19T10:03:04"}),
        ("soon", {"$timestamp_as_written": "soon"}),
        ("", {"$timestamp_as_written": ""}),
        # No text: each reader keeps such a value its own way.
        (7, {}),
        (None, {}),
    ],
)
def test_a_spelling_is_kept_only_where_isoformat_would_write_another(
    value: object,
    kept: dict[str, str],
) -> None:
    assert kept_spelling(value) == kept


def test_a_record_stamped_takes_the_time_a_spelling_names_and_keeps_the_spelling() -> (
    None
):
    record = UserMessage(
        content="hi",
        timestamp=datetime(2000, 1, 1, tzinfo=UTC),
        extra={"k": 1, "$timestamp_as_written": "an older spelling"},
    )
    assert stamped(record, "2026-11-19T10:03:04Z") == UserMessage(
        content="hi",
        timestamp=datetime(2026, 11, 19, 10, 3, 4, tzinfo=UTC),
        extra={"k": 1, "$timestamp_as_written": "2026-11-19T10:03:04Z"},
    )
    # A spelling ``isoformat`` writes is kept nowhere, and replaces one kept before.
    assert stamped(record, "2026-11-19T10:03:04+00:00") == UserMessage(
        content="hi",
        timestamp=datetime(2026, 11, 19, 10, 3, 4, tzinfo=UTC),
        extra={"k": 1},
    )
    assert stamped(record, "soon") == UserMessage(
        content="hi",
        extra={"k": 1, "$timestamp_as_written": "soon"},
    )


def test_stamped_names_each_session_record_that_holds_a_time() -> None:
    timed = {
        kind
        for kind in record_classes().values()
        if is_dataclass(kind) and any(f.name == "timestamp" for f in fields(kind))
    }
    assert set(get_args(cast(object, Stamped.__value__))) == timed


def test_a_record_of_no_other_type_is_stamped_in_its_payload() -> None:
    record = UncategorizedRecord(
        kind="odd",
        payload={"a": 1, "$timestamp_as_written": "older"},
    )
    assert stamped(record, "2026-11-19T10:03:04Z") == UncategorizedRecord(
        timestamp=datetime(2026, 11, 19, 10, 3, 4, tzinfo=UTC),
        kind="odd",
        payload={"a": 1, "$timestamp_as_written": "2026-11-19T10:03:04Z"},
    )
    assert stamped(record, "2026-11-19T10:03:04+00:00") == UncategorizedRecord(
        timestamp=datetime(2026, 11, 19, 10, 3, 4, tzinfo=UTC),
        kind="odd",
        payload={"a": 1},
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
