"""Tests for ``writer.py``: each command's writes, its answer, and the pacing of runs of them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import json
import uuid

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.web.scripts.writer import Clock, answer


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.types.inquiries import Inquiry


_A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")


def test_each_command_writes_through_the_client_and_answers_when_the_write_returned() -> (
    None
):
    client = _Client()
    clock = _FakeClock(start=1_000.0)
    assert _ask(client, clock, op="create", title="Hello", labels=["x"]) == {
        "id": str(_A),
        "at": 1_000_000,
    }
    assert _ask(
        client,
        clock,
        op="edit",
        id=str(_B),
        field="title",
        value="Renamed",
    ) == {"at": 1_000_000}
    assert _ask(
        client,
        clock,
        op="edge",
        **{"from": str(_A), "to": str(_B), "kind": "requires"},
    ) == {"at": 1_000_000}
    assert _ask(
        client,
        clock,
        op="edge",
        remove=True,
        **{"from": str(_A), "to": str(_B), "kind": "requires"},
    ) == {"at": 1_000_000}
    assert client.calls == [
        ("submit", "Issue", {"title": "Hello", "labels": ["x"]}),
        ("edit", _B, "title", "Renamed", "e2e-writer"),
        ("add_edge", _A, _B, "requires", "e2e-writer"),
        ("remove_edge", _A, _B, "requires", "e2e-writer"),
    ]


def test_a_burst_keeps_its_slots_from_the_start_so_a_slow_write_is_caught_up() -> None:
    client = _Client(write_seconds=[0.0, 0.4, 0.0, 0.0])
    clock = _FakeClock(start=0.0)
    client.clock = clock
    result = _ask(
        client,
        clock,
        op="burst",
        rate=4,
        seconds=1,
        title="Burst",
        labels=["b"],
    )
    # Slots at 0, 0.25, 0.5 and 0.75 s. The second write took 0.4 s, so the
    # third starts at once, late, and the fourth is back on its slot.
    assert result == {"ats": [0, 650, 650, 750], "ids": [str(_A)] * 4}
    assert clock.slept == [0.0, 0.25, 0.0, 0.1]
    assert [call[2] for call in client.calls] == [
        {"title": f"Burst {n}", "labels": ["b"]} for n in range(4)
    ]


def test_steady_edits_take_the_ids_in_turn() -> None:
    client = _Client()
    assert _ask(
        client,
        _FakeClock(start=0.0),
        op="steady",
        ids=[str(_A), str(_B)],
        rate=2,
        seconds=1.5,
    ) == {"ats": [0, 500, 1000]}
    assert [call[1] for call in client.calls] == [_A, _B, _A]
    assert [call[3] for call in client.calls] == [
        "Steady edit 0",
        "Steady edit 1",
        "Steady edit 2",
    ]


def test_a_failed_command_answers_with_its_error() -> None:
    client = _Client(fail=True)
    clock = _FakeClock(start=0.0)
    assert _ask(client, clock, op="create", title="x", labels=[]) == {
        "error": "ClientError: 503",
    }
    assert _ask(client, clock, op="teleport") == {
        "error": "ValueError: Unknown op 'teleport'.",
    }
    assert _ask(client, clock, op="edit", field="title", value="x") == {
        "error": "KeyError: 'id'",
    }
    assert str(answer(client, "[1, 2]", clock=clock.clock())["error"]).startswith(
        "ReadError: cannot read [1, 2] as dict[str, object]",
    )


def test_steady_edits_with_no_ids_answer_an_error_and_the_writer_carries_on() -> None:
    client = _Client()
    clock = _FakeClock(start=0.0)
    assert _ask(client, clock, op="steady", ids=[], rate=2, seconds=1) == {
        "error": "ValueError: steady needs at least one id.",
    }
    assert _ask(client, clock, op="edit", id=str(_B), field="title", value="x") == {
        "at": 0,
    }


def test_a_field_of_the_wrong_type_is_named_in_the_error() -> None:
    """N1-04: a field's error names the field, not the whole command."""
    client = _Client()
    clock = _FakeClock(start=0.0)
    error = _ask(client, clock, op="create", title=7, labels=[])["error"]
    assert str(error).startswith("ReadError: 'title': ")
    error = _ask(client, clock, op="edit", id="not-a-uuid", field="title", value="x")
    assert str(error["error"]).startswith("ReadError: 'id': ")
    assert answer(client, "not json", clock=clock.clock())["error"]
    assert client.calls == []


@pytest.mark.parametrize(
    "pacing",
    [
        '"rate": Infinity, "seconds": 1',
        '"rate": 1, "seconds": Infinity',
        '"rate": NaN, "seconds": 1',
        '"rate": 1e308, "seconds": 1e308',
    ],
)
def test_a_burst_paced_beyond_any_count_answers_an_error_and_the_writer_carries_on(
    pacing: str,
) -> None:
    """N1-05: non-finite pacing is refused, never an escaping OverflowError."""
    client = _Client()
    clock = _FakeClock(start=0.0)
    line = f'{{"op": "burst", {pacing}, "title": "t", "labels": []}}'
    assert str(answer(client, line, clock=clock.clock())["error"]).startswith(
        "ValueError: ",
    )
    assert _ask(client, clock, op="create", title="x", labels=[]) == {
        "id": str(_A),
        "at": 0,
    }


def _ask(client: _Client, clock: _FakeClock, **command: object) -> Mapping[str, object]:
    return answer(client, json.dumps(command), clock=clock.clock())


@dataclass(slots=True, kw_only=True)
class _FakeClock:
    """A clock that moves only when slept on or when a fake write takes time."""

    start: float
    slept: list[float] = field(default_factory=list)
    now: float = 0.0

    def __post_init__(self) -> None:
        self.now = self.start

    def clock(self) -> Clock:
        return Clock(now=lambda: self.now, sleep=self._sleep)

    def _sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 6))
        self.now += seconds


@dataclass(slots=True, kw_only=True)
class _Client:
    """Records each write; the n-th write takes ``write_seconds[n]`` on ``clock``."""

    fail: bool = False
    write_seconds: list[float] = field(default_factory=list)
    clock: _FakeClock | None = None
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def submit(
        self,
        kind: Inquiry.InquiryKind,
        body: Mapping[str, object],
    ) -> uuid.UUID:
        self._record("submit", kind, dict(body))
        return _A

    def edit(
        self,
        target_id: uuid.UUID,
        field: str,
        value: object,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        self._record("edit", target_id, field, value, actor)

    def add_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: Inquiry.Actor,
    ) -> object:
        self._record("add_edge", from_id, to_id, edge_kind, actor)
        return None

    def remove_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        self._record("remove_edge", from_id, to_id, edge_kind, actor)

    def _record(self, *call: object) -> None:
        if self.fail:
            raise ClientError("503")
        if self.clock is not None and len(self.calls) < len(self.write_seconds):
            self.clock.now += self.write_seconds[len(self.calls)]
        self.calls.append(call)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
