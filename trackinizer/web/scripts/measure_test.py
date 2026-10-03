"""Tests for ``measure.py``: it only reads, measures every baseline, and compares runs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import argparse
import gzip
import json

import httpx2
import pytest

from trackinizer.lib import zstd_compat
from trackinizer.lib.custom_json import DictCodec, ListCodec, loads
from trackinizer.web.scripts.measure import (
    Row,
    Sample,
    Stream,
    _add_arguments,
    _get,
    _write,
    measure,
    moved,
    report,
    sample_stream,
)


if TYPE_CHECKING:
    from pathlib import Path


_HUB = "00000000-0000-4000-8000-00000000000h"
_BUDGET = "query exceeded the time budget; narrow the filters"


def test_every_request_is_a_get_and_every_baseline_is_measured() -> None:
    methods: list[str] = []
    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(lambda request: _answer(request, methods)),
    ) as http:
        rows = measure(http, repeats=1, queries=("hub",), hub="")
        stream = sample_stream(http, seconds=1.0)

    assert set(methods) == {"GET"}
    assert (stream.frames, stream.distinct_ids) == (3, 2)
    names = {row.name for row in rows}
    assert {
        "boot, one after another",
        "boot, in parallel",
        "list: 50 Issues",
        "list: Mine, every kind, 20 per kind",
        "list: Mine, every kind, 50 per kind",
        "detail: newest Issues",
        "detail: hub",
        "activity: status tab, 50 rows",
        "activity: status since the newest",
        "search: one kind, limit 5",
        "search: every kind in parallel, limit 5",
        "search: all kinds, limit 50",
        "refetch: 50 Issues by seq_range",
        "membership: 13 ids, three kinds",
    } <= names
    hub = next(row for row in rows if row.name == "detail: hub")
    assert hub.note == "Issue#7, 3 relations"
    # The list answers gzipped: its size on the wire is the compressed body's.
    (fifty,) = next(row for row in rows if row.name == "list: 50 Issues").samples
    assert fifty.json_bytes == len(json.dumps(_body("/api/inquiries")))
    assert 0 < fifty.wire_bytes < fifty.json_bytes / 4
    across = next(row for row in rows if row.name == "search: all kinds, limit 50")
    assert [(s.status, s.message) for s in across.samples] == [(400, _BUDGET)]


def test_moved_flags_a_median_time_or_size_over_2x_either_way() -> None:
    def row(name: str, seconds: float, json_bytes: int) -> Row:
        sample = Sample(
            seconds=seconds,
            status=200,
            json_bytes=json_bytes,
            wire_bytes=0,
        )
        return Row(name=name, samples=(sample,))

    baseline = DictCodec.coerce(
        loads(
            json.dumps(
                report(
                    [
                        row("steady", 1.0, 1000),
                        row("slower", 1.0, 1000),
                        row("smaller", 1.0, 3000),
                        row("gone", 1.0, 1000),
                    ],
                    stream=Stream(seconds=20.0, frames=0, distinct_ids=0, status=200),
                    url="http://server",
                    sha="abc",
                ),
            ),
        ),
    )
    current = [
        row("steady", 2.0, 500),
        row("slower", 2.5, 1000),
        row("smaller", 1.0, 900),
        row("new", 9.0, 9),
    ]

    assert moved(current, baseline) == [
        ("slower", "median seconds", 1.0, 2.5),
        ("smaller", "median JSON bytes", 3000, 900),
    ]


def test_an_error_answer_is_reported_but_never_timed_as_a_read() -> None:
    """WEB-06: a fast 502 must not pull a row's median time or size down."""
    ok = Sample(seconds=1.0, status=200, json_bytes=1000, wire_bytes=100)
    refused = Sample(
        seconds=0.01,
        status=502,
        json_bytes=20,
        wire_bytes=20,
        message="bad",
    )
    rows = [
        Row(name="mixed", samples=(ok, refused)),
        Row(name="refused", samples=(refused,)),
    ]
    stream = Stream(seconds=1.0, frames=0, distinct_ids=0, status=200)
    summaries = ListCodec.mappings(
        report(rows, stream=stream, url="u", sha="s")["rows"],
    )

    assert [(r["median_seconds"], r["median_json_bytes"]) for r in summaries] == [
        (1.0, 1000),
        (None, None),
    ]
    assert summaries[1]["statuses"] == [502]
    baseline = DictCodec.coerce(
        loads(
            json.dumps(
                report(
                    [Row(name="mixed", samples=(ok,))],
                    stream=stream,
                    url="u",
                    sha="s",
                ),
            ),
        ),
    )
    assert moved(rows, baseline) == []


def test_a_refused_stream_is_its_answer_not_a_quiet_stream() -> None:
    """WEB-06: a 401 on the stream is not zero frames a second."""
    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(401, content=iter([b'{"detail": "no"}'])),
        ),
    ) as http:
        stream = sample_stream(http, seconds=1.0)

    assert (stream.status, stream.frames) == (401, 0)
    document = report([], stream=stream, url="u", sha="s")
    assert DictCodec.coerce(document["stream"])["frames_per_sec"] is None


@pytest.mark.parametrize("flag", ["--repeats", "--stream-sec"])
def test_a_count_or_a_window_of_zero_is_refused_before_measuring(flag: str) -> None:
    """WEB-12: zero repeats left every row without a sample to take a median of."""
    parser = argparse.ArgumentParser()
    _add_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args([flag, "0"])


def test_two_reports_in_one_second_never_overwrite_each_other(tmp_path: Path) -> None:
    """WEB-22: host and second name a report, and two runs can share both."""
    first = _write({"run": 1}, url="http://server:8765", folder=tmp_path)
    second = _write({"run": 2}, url="http://server:8765", folder=tmp_path)

    assert first != second
    assert [DictCodec.coerce(loads(p.read_text()))["run"] for p in (first, second)] == [
        1,
        2,
    ]


def test_a_content_coding_is_read_in_any_case_and_as_a_list() -> None:
    """WEB-28: codings are case-insensitive, and several apply in order."""
    body = b'{"ok": true}'
    codings = {
        "GZip": gzip.compress(body),
        "gzip, zstd": zstd_compat.compress(gzip.compress(body)),
    }
    for coding, raw in codings.items():
        with httpx2.Client(
            base_url="http://server",
            transport=httpx2.MockTransport(
                lambda _, raw=raw, coding=coding: httpx2.Response(
                    200,
                    content=iter([raw]),
                    headers={"content-encoding": coding},
                ),
            ),
        ) as http:
            sample = _get(http, "/api/version")
        assert (sample.json_bytes, sample.wire_bytes) == (len(body), len(raw)), coding


# Bodies go out as iterators, which stream as a network response does: a body given
# whole is read at once, and the script's raw read would find it spent.
def _answer(request: httpx2.Request, methods: list[str]) -> httpx2.Response:
    """Answer ``request`` as a tiny trackinizer with three kinds would."""
    methods.append(request.method)
    path = request.url.path
    if path == "/api/web/subscribe":
        frames = "".join(f'data: {{"id": "{n}"}}\n\n' for n in (1, 2, 1))
        return httpx2.Response(200, content=iter([frames.encode()]))
    if path == "/api/web/search" and "kind" not in parse_qs(request.url.query.decode()):
        return httpx2.Response(
            400,
            content=iter([json.dumps({"detail": _BUDGET}).encode()]),
        )
    body = json.dumps(_body(path)).encode()
    if path == "/api/inquiries":
        # Answered gzipped, so the size on the wire differs from the JSON's.
        gzipped = gzip.compress(body)
        return httpx2.Response(
            200,
            content=iter([gzipped]),
            headers={"content-encoding": "gzip"},
        )
    return httpx2.Response(200, content=iter([body]))


def _body(path: str) -> object:
    if path == "/api/version":
        return {"sha": "abc"}
    if path == "/api/meta/enums":
        return {"inquiry_kind_all": ["Issue", "Belief", "Paper"]}
    if path == "/api/me/profile":
        return {"email": "me@example.com"}
    if path == "/api/inquiries":
        return [
            {
                "id": f"00000000-0000-4000-8000-{n:012d}",
                "kind": "Issue",
                "seq": 100 - n,
                "narrows": [{"id": _HUB, "kind": "Issue"}],
            }
            for n in range(50)
        ]
    if path == f"/api/web/get/{_HUB}":
        peer = {"id": "p", "kind": "Issue", "seq": 1}
        return {
            "self": {"kind": "Issue", "seq": 7},
            "edges": {"narrows": [peer]},
            "backlinks": {"narrows": [peer, peer]},
        }
    if path.startswith("/api/web/get/"):
        return {"self": {"kind": "Issue", "seq": 1}, "edges": {}, "backlinks": {}}
    if path == "/api/change_log":
        return [{"id": "c", "created": "2026-09-27T00:00:00+00:00"}]
    return [] if path == "/api/web/search" else {}


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
