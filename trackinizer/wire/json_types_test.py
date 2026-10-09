"""``UtcDatetime`` reads a naive request instant as UTC, whatever the host zone."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import time

from pydantic import TypeAdapter

import pytest

from trackinizer.wire.bodies import SubmitAgentSession, SubmitPaper
from trackinizer.wire.json_types import UtcDatetime
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_session_ir import RecordBody, SlashCommandBody
from trackinizer.wire.wire_sessions import SessionEnd, SessionStart


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


_ZONES: Final = ("Pacific/Kiritimati", "America/Los_Angeles", "Europe/Berlin")
_MIDNIGHT: Final = datetime(year=2024, month=12, day=10, tzinfo=UTC)

_NAIVE_FIELDS: Final[dict[str, Callable[[str], datetime | None]]] = {
    "SubmitPaper.publish_date": lambda v: (
        SubmitPaper.model_validate({"title": "x", "publish_date": v}).publish_date
    ),
    "SubmitAgentSession.started": lambda v: (
        SubmitAgentSession.model_validate({"title": "x", "started": v}).started
    ),
    "SessionStart.started": lambda v: (
        SessionStart.model_validate({"cli": "claude", "started": v}).started
    ),
    "SessionEnd.ended": lambda v: SessionEnd.model_validate({"ended": v}).ended,
    "MetricPoint.timestamp": lambda v: (
        MetricPoint.model_validate(
            {"key": "k", "step": 0, "value": 1.0, "timestamp": v},
        ).timestamp
    ),
    "RecordBody.timestamp": lambda v: (
        RecordBody.model_validate({"idx": 0, "kind": "K", "timestamp": v}).timestamp
    ),
    "SlashCommandBody.timestamp": lambda v: (
        SlashCommandBody.model_validate(
            {"timestamp": v, "command": "c", "idx": 0},
        ).timestamp
    ),
}


@pytest.fixture
def in_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Return a function that makes the process's local zone ``name``."""

    def set_zone(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield set_zone
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("zone", _ZONES)
@pytest.mark.parametrize("name", _NAIVE_FIELDS)
def test_naive_input_is_utc_in_every_zone(
    in_zone: Callable[[str], None],
    zone: str,
    name: str,
) -> None:
    """Read a date-only string as midnight UTC, the same instant in any zone."""
    in_zone(zone)
    value = _NAIVE_FIELDS[name]("2024-12-10")

    assert value is not None
    assert value.tzinfo is not None
    # What asyncpg does to a naive value bound to TIMESTAMPTZ, host zone included.
    assert value.astimezone(UTC) == _MIDNIGHT, zone


@pytest.mark.parametrize("zone", _ZONES)
def test_aware_input_is_kept(in_zone: Callable[[str], None], zone: str) -> None:
    """Keep the offset an aware value carries."""
    in_zone(zone)
    adapter: TypeAdapter[datetime] = TypeAdapter(UtcDatetime)
    value = adapter.validate_python("2024-12-10T05:00:00+05:00")

    assert value.utcoffset() == timedelta(hours=5)
    assert value == _MIDNIGHT


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
