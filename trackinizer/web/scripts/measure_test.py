"""Tests for ``measure.py``: it only reads, measures every baseline, and compares runs."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from threading import Barrier, Lock, get_ident
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypedDict, cast, override
from unittest.mock import ANY, patch
from urllib.parse import parse_qs

import argparse
import gzip
import json
import re
import sys

from treekle import from_plain, json_dumps, json_loads

import httpx2
import pytest

from trackinizer.lib import zstd_compat
from trackinizer.trax.profile import Profile
from trackinizer.web.scripts import measure as measurement
from trackinizer.web.scripts.measure import (
    Row,
    Sample,
    Stream,
    _add_arguments,
    _get,
    _headers,
    _serial,
    _summary,
    _write,
    measure,
    moved,
    report,
    sample_stream,
)


if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import tzinfo


_HUB = "00000000-0000-4000-8000-00000000000h"
_BUDGET = "query exceeded the time budget; narrow the filters"


def test_serial_reads_preserve_queries_and_measure_total_wall_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((10.0, 11.0, 12.0, 13.0, 14.0, 16.0))
    monkeypatch.setattr(
        measurement,
        "time",
        SimpleNamespace(perf_counter=ticks.__next__),
    )
    requests: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(200, content=iter([b'{"x":1}']))

    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(answer),
    ) as http:
        sample = _serial(http, (("/a", (("limit", 2),)), ("/b", (("kind", "Issue"),))))

    assert requests == ["http://server/a?limit=2", "http://server/b?kind=Issue"]
    assert sample == Sample(seconds=6.0, status=200, json_bytes=14, wire_bytes=14)


def test_summary_preserves_samples_and_summarizes_only_successes() -> None:
    row = Row(
        name="read",
        note="two successes",
        samples=(
            Sample(seconds=1.0, status=201, json_bytes=10, wire_bytes=2),
            Sample(seconds=3.0, status=200, json_bytes=13, wire_bytes=5),
            Sample(
                seconds=0.01,
                status=502,
                json_bytes=90,
                wire_bytes=90,
                message="down",
            ),
        ),
    )
    assert _summary(row) == {
        "name": "read",
        "note": "two successes",
        "median_seconds": 2.0,
        "min_seconds": 1.0,
        "max_seconds": 3.0,
        "median_json_bytes": 12,
        "median_wire_bytes": 4,
        "statuses": [200, 201, 502],
        "messages": ["down"],
        "samples": [
            {
                "seconds": 1.0,
                "status": 201,
                "json_bytes": 10,
                "wire_bytes": 2,
                "message": "",
            },
            {
                "seconds": 3.0,
                "status": 200,
                "json_bytes": 13,
                "wire_bytes": 5,
                "message": "",
            },
            {
                "seconds": 0.01,
                "status": 502,
                "json_bytes": 90,
                "wire_bytes": 90,
                "message": "down",
            },
        ],
    }
    assert _summary(Row(name="empty", samples=())) == {
        "name": "empty",
        "note": "",
        "median_seconds": None,
        "min_seconds": None,
        "max_seconds": None,
        "median_json_bytes": None,
        "median_wire_bytes": None,
        "statuses": [],
        "messages": [],
        "samples": [],
    }


def test_stream_stops_at_the_window_and_counts_only_data_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((10.0, 10.5, 11.0, 12.0, 13.0))
    monkeypatch.setattr(
        measurement,
        "time",
        SimpleNamespace(perf_counter=ticks.__next__),
    )

    def answer(request: httpx2.Request) -> httpx2.Response:
        assert (request.method, request.url.path) == ("GET", "/api/web/subscribe")
        assert request.extensions["timeout"] == {
            "connect": 30.0,
            "read": 2.0,
            "write": 30.0,
            "pool": 30.0,
        }
        return httpx2.Response(
            200,
            content=iter(
                [
                    b': keepalive\ndata: {"id":"a"}\ndata: {"id":"a"}\ndata: {"id":"b"}\n',
                ],
            ),
        )

    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(answer),
    ) as http:
        with patch.object(http, "stream", wraps=http.stream) as requests:
            stream = sample_stream(http, seconds=2.0)
        requests.assert_called_once_with("GET", "/api/web/subscribe", timeout=ANY)
    assert stream == Stream(seconds=3.0, frames=2, distinct_ids=1, status=200)


def test_a_quiet_stream_read_timeout_preserves_frames_already_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((10.0, 11.0, 12.0))
    monkeypatch.setattr(
        measurement,
        "time",
        SimpleNamespace(perf_counter=ticks.__next__),
    )

    def body() -> Iterable[bytes]:
        yield b'data: {"id":"a"}\n'
        raise httpx2.ReadTimeout("quiet")

    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, content=body())),
    ) as http:
        stream = sample_stream(http, seconds=2.0)
    assert stream == Stream(seconds=2.0, frames=1, distinct_ids=1, status=200)


def test_a_stream_timeout_before_headers_reports_no_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((10.0, 12.0))
    monkeypatch.setattr(
        measurement,
        "time",
        SimpleNamespace(perf_counter=ticks.__next__),
    )

    def answer(request: httpx2.Request) -> httpx2.Response:
        del request
        raise httpx2.ReadTimeout("no headers")

    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(answer),
    ) as http:
        stream = sample_stream(http, seconds=2.0)
    assert stream == Stream(seconds=2.0, frames=0, distinct_ids=0, status=0)


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


@pytest.mark.parametrize(
    ("count", "membership"),
    [(0, ""), (5, "membership: 5 ids, three kinds")],
)
def test_reads_of_the_newest_issues_are_named_for_the_issues_there_are(
    count: int,
    membership: str,
) -> None:
    """S4: with fewer than 13 Issues no row claims 13, and with none none is taken."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/inquiries":
            rows = from_plain(_body("/api/inquiries"), list[object])[:count]
            return httpx2.Response(200, content=iter([json.dumps(rows).encode()]))
        return _answer(request, [])

    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(answer),
    ) as http:
        names = [row.name for row in measure(http, repeats=1, queries=(), hub="")]
    assert [n for n in names if n.startswith("membership")] == (
        [membership] if membership else []
    )
    assert ("detail: newest Issues" in names) == bool(count)
    assert ("refetch: 50 Issues by seq_range" in names) == bool(count)


def test_measure_keeps_nonempty_issue_reads_when_narrows_is_missing() -> None:
    paths: list[str] = []
    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(partial(_answer_without_narrows, paths=paths)),
    ) as http:
        rows = measure(http, repeats=1, queries=(), hub="")
    by_name = {row.name: row for row in rows}
    assert "detail: hub" not in by_name
    for name, count in (
        ("detail: newest Issues", 2),
        ("refetch: 50 Issues by seq_range", 1),
        ("membership: 2 ids, three kinds", 1),
    ):
        assert [sample.status for sample in by_name[name].samples] == [200] * count
    assert [path for path in paths if path.startswith("/api/web/get/")] == [
        "/api/web/get/orphan-a",
        "/api/web/get/orphan-b",
    ]


def test_moved_flags_a_median_time_or_size_over_2x_either_way() -> None:
    def row(name: str, seconds: float, json_bytes: int) -> Row:
        sample = Sample(
            seconds=seconds,
            status=200,
            json_bytes=json_bytes,
            wire_bytes=0,
        )
        return Row(name=name, samples=(sample,))

    baseline = json_loads(
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
        dict[str, object],
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
    summaries = from_plain(
        report(rows, stream=stream, url="u", sha="s")["rows"],
        list[dict[str, object]],
    )

    assert [(r["median_seconds"], r["median_json_bytes"]) for r in summaries] == [
        (1.0, 1000),
        (None, None),
    ]
    assert summaries[1]["statuses"] == [502]
    baseline = json_loads(
        json.dumps(
            report(
                [Row(name="mixed", samples=(ok,))],
                stream=stream,
                url="u",
                sha="s",
            ),
        ),
        dict[str, object],
    )
    assert moved(rows, baseline) == []


def test_a_baseline_row_with_no_successful_answer_is_not_compared() -> None:
    """N1-01: a baseline's null median is skipped, as the docstring promises."""
    refused = Sample(seconds=0.01, status=502, json_bytes=2, wire_bytes=2)
    ok = Sample(seconds=1.0, status=200, json_bytes=1000, wire_bytes=100)
    stream = Stream(seconds=1.0, frames=0, distinct_ids=0, status=200)
    baseline = json_loads(
        json.dumps(
            report(
                [Row(name="r", samples=(refused,))],
                stream=stream,
                url="u",
                sha="s",
            ),
        ),
        dict[str, object],
    )
    assert moved([Row(name="r", samples=(ok,))], baseline) == []


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (422, b'{"detail": [{"msg": "x"}]}', '[{"msg": "x"}]'),
        (400, b'{"detail": "too broad"}', "too broad"),
        (502, b"[]", "[]"),
        (502, b"null", "null"),
        (502, b'{"error": "down"}', '{"error": "down"}'),
        (502, b"<html>bad gateway</html>", "<html>bad gateway</html>"),
        (502, b"\xff" + b"x" * 300, "\ufffd" + "x" * 199),
    ],
)
def test_an_error_answer_of_any_shape_is_kept_with_its_message(
    status: int,
    body: bytes,
    message: str,
) -> None:
    """N1-02: an error body that is not a dict with a string detail still samples."""
    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(status, content=iter([body])),
        ),
    ) as http:
        sample = _get(http, "/api/web/search")
    assert (sample.status, sample.message) == (status, message)


def test_the_profile_token_goes_to_its_server_however_its_url_is_spelled() -> None:
    """N1-03: one rule decides the profile's server, as mirror_local's does."""
    profile = Profile(url="https://h", api_key="trax_secret")
    assert _headers("https://h:443", profile)["Authorization"] == "Bearer trax_secret"
    assert _headers("https://h/", profile)["Authorization"] == "Bearer trax_secret"
    assert "Authorization" not in _headers("https://h.evil", profile)
    assert "Authorization" not in _headers("http://127.0.0.1:8765", profile)


def test_a_refused_stream_is_its_answer_not_a_quiet_stream() -> None:
    """WEB-06: a 401 on the stream is not zero frames a second."""
    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(401, content=iter([b'{"detail": "no"}'])),
        ),
    ) as http:
        stream = sample_stream(http, seconds=1.0)

    assert stream == Stream(seconds=0.0, frames=0, distinct_ids=0, status=401)
    document = report([], stream=stream, url="u", sha="s")
    assert from_plain(document["stream"], dict[str, object])["frames_per_sec"] is None


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
    assert [
        json_loads(p.read_text(), dict[str, object])["run"] for p in (first, second)
    ] == [
        1,
        2,
    ]


def test_a_content_coding_is_read_in_any_case_and_as_a_list() -> None:
    """WEB-28: codings are case-insensitive, and several apply in order."""
    body = b'{"ok": true}'
    codings = {
        "GZip": gzip.compress(body),
        "zstd": zstd_compat.compress(body),
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


@pytest.mark.parametrize("hub", ["", "Paper#81"])
@pytest.mark.parametrize("empty", [False, True])
def test_measure_preserves_request_parameters_and_sample_identities(
    hub: str,
    empty: bool,
) -> None:
    server = _MeasurementServer(empty=empty)
    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(server.answer),
    ) as http:
        rows = measure(http, repeats=2, queries=("alpha", "beta gamma"), hub=hub)
    by_name = {row.name: row for row in rows}
    expected: dict[str, int] = {
        "boot, one after another": 2,
        "boot, in parallel": 2,
        "list: 50 Issues": 2,
        "list: Mine, every kind, 20 per kind": 2,
        "list: Mine, every kind, 50 per kind": 2,
        **{f"activity: {tab} tab, 50 rows": 2 for tab in server.tabs},
        "activity: status since the newest": 2,
        "search: one kind, limit 5": 8,
        "search: every kind in parallel, limit 5": 2,
        "search: all kinds, limit 50": 2,
    }
    if not empty:
        expected |= {
            "detail: newest Issues": 10,
            "refetch: 50 Issues by seq_range": 2,
            "membership: 13 ids, three kinds": 2,
        }
    if hub or not empty:
        expected["detail: hub"] = 2
        selected = "explicit" if hub else "popular"
        assert by_name["detail: hub"].note == (
            "Paper#81, 5 relations" if hub else "Issue#71, 5 relations"
        )
        assert server.calls[(f"/api/web/get/{selected}", ())] == 3
    assert len(rows) == len(expected)
    assert {row.name: len(row.samples) for row in rows} == expected
    document = from_plain(
        report(
            rows,
            stream=Stream(seconds=2.0, frames=3, distinct_ids=2, status=200),
            url="http://server",
            sha="build-123",
        ),
        _MeasurementReport,
    )
    assert (document["url"], document["server_sha"]) == ("http://server", "build-123")
    assert [row["name"] for row in document["rows"]] == [row.name for row in rows]
    assert {row["name"]: len(row["samples"]) for row in document["rows"]} == expected
    assert document["stream"]["frames_per_sec"] == 1.5
    assert server.calls[("/api/inquiries", (("kind", "Issue"), ("limit", "50")))] == 3
    for path, count in (
        ("/api/meta/enums", 5),
        ("/api/meta/fields", 4),
        ("/api/meta/edges", 4),
        ("/api/me/profile", 5),
    ):
        assert server.calls[(path, ())] == count
    boot_bodies = [
        server.body(path, params={})
        for path in (
            "/api/meta/enums",
            "/api/meta/fields",
            "/api/meta/edges",
            "/api/me/profile",
        )
    ]
    for name in ("boot, one after another", "boot, in parallel"):
        assert [(s.json_bytes, s.wire_bytes) for s in by_name[name].samples] == [
            (
                sum(len(body) for body in boot_bodies),
                sum(len(gzip.compress(body, mtime=0)) for body in boot_bodies),
            ),
        ] * 2
    for limit in ("20", "50"):
        assert server.mine[limit] == 2
    for tab in server.tabs:
        assert server.calls[("/api/change_log", (("kind", tab), ("limit", "50")))] == 2
    since = "1970-01-01" if empty else "2026-11-01T12:34:56+00:00"
    assert (
        server.calls[
            (
                "/api/change_log",
                (("kind", "status"), ("limit", "50"), ("since", since)),
            )
        ]
        == 2
    )
    assert server.calls[("/api/change_log", (("kind", "status"), ("limit", "1")))] == 1
    for query in ("alpha", "beta gamma"):
        for kind in server.kinds:
            key = ("/api/web/search", (("kind", kind), ("limit", "5"), ("q", query)))
            assert server.calls[key] == 2
        assert server.calls[("/api/web/search", (("limit", "50"), ("q", query)))] == 1
    assert server.calls[("/api/inquiries/Paper/81", ())] == bool(hub)
    assert server.memberships == (0 if empty else 2)
    assert server.refetches == (0 if empty else 2)
    assert {
        path: count
        for (path, params), count in server.calls.items()
        if path.startswith("/api/web/get/issue-") and not params
    } == ({} if empty else {f"/api/web/get/issue-{n}": 2 for n in range(5)})
    one = by_name["search: one kind, limit 5"]
    assert [(s.json_bytes, s.wire_bytes) for s in one.samples] == [
        server.search_sizes(query, kind=kind)
        for query in ("alpha", "beta gamma")
        for kind in server.kinds
    ]
    together = by_name["search: every kind in parallel, limit 5"]
    assert [(s.json_bytes, s.wire_bytes) for s in together.samples] == [
        tuple(
            sum(sizes)
            for sizes in zip(
                *(server.search_sizes(query, kind=kind) for kind in server.kinds),
                strict=True,
            )
        )
        for query in ("alpha", "beta gamma")
    ]
    assert all(s.status == 200 and s.message == "" for row in rows for s in row.samples)


def test_report_keeps_status_boundary_samples_and_error_messages() -> None:
    samples = (
        Sample(seconds=2.0, status=200, json_bytes=11, wire_bytes=3),
        Sample(seconds=8.0, status=399, json_bytes=20, wire_bytes=10),
        Sample(
            seconds=100.0,
            status=400,
            json_bytes=900,
            wire_bytes=700,
            message="first",
        ),
        Sample(
            seconds=200.0,
            status=502,
            json_bytes=800,
            wire_bytes=600,
            message="second",
        ),
    )
    document = from_plain(
        report(
            [Row(name="boundary", note="retained note", samples=samples)],
            stream=Stream(seconds=2.0, frames=3, distinct_ids=2, status=200),
            url="server",
            sha="sha",
        ),
        _MeasurementReport,
    )
    (summary,) = document["rows"]
    assert summary == {
        "name": "boundary",
        "note": "retained note",
        "median_seconds": 5.0,
        "min_seconds": 2.0,
        "max_seconds": 8.0,
        "median_json_bytes": 16,
        "median_wire_bytes": 6,
        "statuses": [200, 399, 400, 502],
        "messages": ["first", "second"],
        "samples": [
            {
                "seconds": s.seconds,
                "status": s.status,
                "json_bytes": s.json_bytes,
                "wire_bytes": s.wire_bytes,
                "message": s.message,
            }
            for s in samples
        ],
    }


def test_report_json_preserves_complete_stream_measurements() -> None:
    document = report(
        [],
        stream=Stream(seconds=2.0, frames=7, distinct_ids=3, status=200),
        url="server",
        sha="build",
    )
    restored = json_loads(json_dumps(document), _MeasurementReport)
    assert restored["stream"] == {
        "status": 200,
        "seconds": 2.0,
        "frames": 7,
        "distinct_ids": 3,
        "frames_per_sec": 3.5,
    }


@pytest.mark.parametrize("direction", ["increase", "decrease"])
@pytest.mark.parametrize("after", [2.0, 2.25])
def test_moved_continues_after_missing_rows_and_uses_strict_ratios(
    direction: str,
    after: float,
) -> None:
    before_value, after_value = (
        (1.0, after) if direction == "increase" else (after, 1.0)
    )
    baseline = report(
        [
            _comparison_row(
                "matched",
                seconds=before_value,
                size=int(before_value * 100),
            ),
        ],
        stream=Stream(seconds=1.0, frames=0, distinct_ids=0, status=200),
        url="server",
        sha="old",
    )
    current = [
        _comparison_row("absent", seconds=100.0, size=10_000),
        _comparison_row("matched", seconds=after_value, size=int(after_value * 100)),
    ]
    assert moved(current, baseline) == (
        []
        if after == 2.0
        else [
            ("matched", "median seconds", before_value, after_value),
            ("matched", "median JSON bytes", before_value * 100, after_value * 100),
        ]
    )


def test_measure_times_serial_boot_to_the_last_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _MeasurementServer(empty=True)
    monkeypatch.setattr(measurement, "time", SimpleNamespace(perf_counter=server.clock))
    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(server.answer),
    ) as http:
        rows = measure(http, repeats=2, queries=("alpha",), hub="")
    serial = next(row for row in rows if row.name == "boot, one after another")
    assert [sample.seconds for sample in serial.samples] == [24.5, 24.5]


def test_measure_parallel_rows_use_outer_elapsed_not_summed_worker_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _MeasurementServer(empty=True)
    clock = partial(_thread_tick, main=get_ident(), ticks={})
    monkeypatch.setattr(measurement, "time", SimpleNamespace(perf_counter=clock))
    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(server.answer),
    ) as http:
        rows = measure(http, repeats=2, queries=("alpha", "beta gamma"), hub="")
    by_name = {row.name: row for row in rows}
    for name in ("boot, in parallel", "search: every kind in parallel, limit 5"):
        assert [sample.seconds for sample in by_name[name].samples] == [10.0, 10.0]


@pytest.mark.parametrize("status", [399, 400])
def test_measure_aggregates_compressed_search_status_sizes_and_messages(
    status: int,
) -> None:
    server = _MeasurementServer(empty=True, search_status=status)
    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(server.answer),
    ) as http:
        rows = measure(http, repeats=2, queries=("alpha", "beta gamma"), hub="")
    by_name = {row.name: row for row in rows}
    one = by_name["search: one kind, limit 5"]
    statuses = (200, 201, 399, 399) if status == 399 else (200, 399, 400, 502)
    expected = [
        Sample(
            seconds=sample.seconds,
            status=dict(zip(server.kinds, statuses, strict=True))[kind],
            json_bytes=len(json_dumps({"detail": f"{query}/{kind}"}).encode()),
            wire_bytes=len(
                gzip.compress(
                    json_dumps({"detail": f"{query}/{kind}"}).encode(),
                    mtime=0,
                ),
            ),
            message=f"{query}/{kind}"
            if status == 400 and kind in ("Paper", "Experiment")
            else "",
        )
        for sample, (query, kind) in zip(
            one.samples,
            (
                (query, kind)
                for query in ("alpha", "beta gamma")
                for kind in server.kinds
            ),
            strict=True,
        )
    ]
    assert list(one.samples) == expected
    parallel = by_name["search: every kind in parallel, limit 5"]
    for index, sample in enumerate(parallel.samples):
        group = expected[index * 4 : (index + 1) * 4]
        assert (
            sample.status,
            sample.json_bytes,
            sample.wire_bytes,
            sample.message,
        ) == (
            max(statuses),
            sum(item.json_bytes for item in group),
            sum(item.wire_bytes for item in group),
            "; ".join(sorted({item.message for item in group} - {""})),
        )
    summaries = from_plain(
        report(
            [one, parallel],
            stream=Stream(seconds=1.0, frames=0, distinct_ids=0, status=200),
            url="server",
            sha="sha",
        ),
        _MeasurementReport,
    )["rows"]
    assert [summary["statuses"] for summary in summaries] == [
        sorted(set(statuses)),
        [max(statuses)],
    ]
    assert summaries[0]["median_seconds"] is not None
    assert (summaries[1]["median_seconds"] is None) == (status == 400)
    assert summaries[0]["messages"] == sorted({s.message for s in expected} - {""})


def test_measure_starts_every_parallel_search_before_releasing_any() -> None:
    server = _LatchedSearchServer()
    with httpx2.Client(
        base_url="http://server",
        transport=httpx2.MockTransport(server.answer),
    ) as http:
        rows = measure(http, repeats=1, queries=("alpha",), hub="")
    parallel = next(
        row for row in rows if row.name == "search: every kind in parallel, limit 5"
    )
    assert server.arrived == set(server.kinds)
    assert len(parallel.samples) == 1
    assert parallel.samples[0].status == 200
    assert parallel.samples[0].json_bytes == sum(
        server.search_sizes("alpha", kind=kind)[0] for kind in server.kinds
    )


@pytest.mark.parametrize("external", [False, True])
def test_main_preserves_effective_config_provenance_and_endpoint_credentials(
    external: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def output_root(path: str) -> Path:
        assert path == "/opt/scratch/artifacts/trackinizer-web/measure"
        return tmp_path / "reports"

    server = _MainMeasurementServer()
    profile = Profile(url="https://profile.test", api_key="profile-secret")
    monkeypatch.setattr(measurement, "load_profile", lambda: profile)
    monkeypatch.setattr(measurement, "datetime", SimpleNamespace(now=_fixed_now))
    monkeypatch.setattr(measurement, "Path", output_root)
    argv = ["measure"]
    if external:
        argv += [
            "--url",
            "http://preview.test:8765/",
            "--repeats",
            "2",
            "--query",
            "alpha",
            "--query",
            "beta gamma",
            "--hub",
            "Paper#81",
            "--stream-sec",
            "0.25",
        ]
    monkeypatch.setattr(sys, "argv", argv)
    client = partial(httpx2.Client, transport=httpx2.MockTransport(server.answer))
    with patch.object(httpx2, "Client", client):
        assert measurement.main() == 0
    paths = list((tmp_path / "reports").rglob("*.json"))
    assert len(paths) == 1
    assert paths[0].parent == (
        tmp_path / "reports" / f"{_fixed_now(UTC).astimezone():%Y-%m-%d}"
    )
    document = json_loads(paths[0].read_text(), _MeasurementReport)
    url = "http://preview.test:8765" if external else profile.url
    assert (document["url"], document["server_sha"]) == (url, "build-main")
    assert document["measured_at"] == _fixed_now(UTC).astimezone().isoformat(
        timespec="seconds",
    )
    rows = {row["name"]: row for row in document["rows"]}
    repeats = 2 if external else 3
    queries = (
        ("alpha", "beta gamma")
        if external
        else ("trackinizer", "membership check", "title:^web")
    )
    assert len(rows["list: 50 Issues"]["samples"]) == repeats
    assert len(rows["search: one kind, limit 5"]["samples"]) == len(queries) * 4
    assert {query for query, _ in server.searches} == set(queries)
    assert server.calls[("/api/inquiries/Paper/81", ())] == int(external)
    assert ("detail: hub" in rows) == external
    assert server.stream_windows == [0.25 if external else 20.0]
    assert document["stream"]["status"] == 401
    assert document["stream"]["frames_per_sec"] is None
    assert set(server.origins) == {url}
    assert set(server.credentials) == ({""} if external else {"Bearer profile-secret"})
    assert all("gzip" in coding and "zstd" in coding for coding in server.codings)
    output = capsys.readouterr().out
    assert str(paths[0]) in output
    assert "build-main" in output
    assert url in output


def test_main_compares_current_measurements_with_a_baseline_report_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def output_path(path: str) -> Path:
        if path == "/opt/scratch/artifacts/trackinizer-web/measure":
            return tmp_path / "reports"
        return Path(path)

    baseline = report(
        [
            _comparison_row("list: 50 Issues", seconds=0.0, size=8),
            _comparison_row("activity: created tab, 50 rows", seconds=0.0, size=2),
            _comparison_row("absent", seconds=0.0, size=100),
        ],
        stream=Stream(seconds=2.0, frames=7, distinct_ids=3, status=200),
        url="https://profile.test",
        sha="baseline-build",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_text = json_dumps(baseline)
    baseline_path.write_text(baseline_text, encoding="utf-8")
    server = _MainMeasurementServer()
    monkeypatch.setattr(
        measurement,
        "load_profile",
        lambda: Profile(url="https://profile.test"),
    )
    monkeypatch.setattr(measurement, "datetime", SimpleNamespace(now=_fixed_now))
    monkeypatch.setattr(measurement, "Path", output_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "measure",
            "--repeats",
            "1",
            "--query",
            "alpha",
            "--stream-sec",
            "0.25",
            "--baseline",
            str(baseline_path),
        ],
    )
    client = partial(httpx2.Client, transport=httpx2.MockTransport(server.answer))
    with patch.object(httpx2, "Client", client):
        assert measurement.main() == 0
    (written,) = (tmp_path / "reports").rglob("*.json")
    current = json_loads(written.read_text(), _MeasurementReport)
    row = next(row for row in current["rows"] if row["name"] == "list: 50 Issues")
    assert row["median_json_bytes"] == 2
    assert current["server_sha"] == "build-main"
    output = capsys.readouterr().out.splitlines()
    (summary,) = (line for line in output if str(baseline_path) in line)
    assert summary.split()[-1] == "1"
    comparisons = [line for line in output if line.startswith("  ")]
    assert len(comparisons) == 1
    assert comparisons[0].lstrip().startswith("list: 50 Issues:")
    assert [float(token) for token in comparisons[0].split()[-3::2]] == [8.0, 2.0]
    assert baseline_path.read_text(encoding="utf-8") == baseline_text


def test_three_fixed_second_reports_skip_existing_suffix_one_without_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(measurement, "datetime", SimpleNamespace(now=_fixed_now))
    clock = _fixed_now(UTC).astimezone().strftime("%H%M%S")
    folder = tmp_path / "nested" / "reports"
    folder.mkdir(parents=True)
    sentinel = folder / f"server-8765-{clock}-1.json"
    sentinel.write_text("existing suffix one\n", encoding="utf-8")
    paths = [
        _write({"run": run}, url="http://server:8765", folder=folder)
        for run in (1, 2, 3)
    ]
    assert [path.name for path in paths] == [
        f"server-8765-{clock}.json",
        f"server-8765-{clock}-2.json",
        f"server-8765-{clock}-3.json",
    ]
    assert [json_loads(path.read_text(), dict[str, int]) for path in paths] == [
        {"run": 1},
        {"run": 2},
        {"run": 3},
    ]
    assert sentinel.read_text(encoding="utf-8") == "existing suffix one\n"
    assert set(folder.iterdir()) == {sentinel, *paths}


@pytest.mark.parametrize("successful", [False, True])
def test_table_counts_only_successes_and_preserves_statuses_for_refused_stream(
    successful: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    errors = (
        Sample(seconds=100.0, status=502, json_bytes=90_000, wire_bytes=80_000),
        Sample(seconds=200.0, status=400, json_bytes=70_000, wire_bytes=60_000),
    )
    successes = (
        Sample(seconds=2.0, status=200, json_bytes=2000, wire_bytes=1000),
        Sample(seconds=8.0, status=399, json_bytes=6000, wire_bytes=3000),
    )
    measurement._print_table(
        [Row(name="read", samples=(*errors, *successes) if successful else errors)],
        Stream(seconds=0.0, frames=0, distinct_ids=0, status=401),
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].split() == (
        ["read", "2", "5.000", "2.00-8.00", "4.0", "2.0", "200,399,400,502"]
        if successful
        else ["read", "0", "-", "-", "-", "-", "400,502"]
    )
    assert lines[2].split()[-1] == "401"
    assert len(lines) == 3


def test_table_reports_successful_stream_frames_distinct_ids_elapsed_and_rate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    measurement._print_table(
        [],
        Stream(seconds=2.0, frames=7, distinct_ids=3, status=200),
    )
    stream_line = capsys.readouterr().out.splitlines()[-1]
    assert re.findall(r"\d+(?:\.\d+)?", stream_line) == ["7", "3", "2.0", "3.50"]


class _Filter(TypedDict):
    field: str
    op: str
    value: str


class _SampleDocument(TypedDict):
    seconds: float
    status: int
    json_bytes: int
    wire_bytes: int
    message: str


class _RowDocument(TypedDict):
    name: str
    note: str
    median_seconds: float | None
    min_seconds: float | None
    max_seconds: float | None
    median_json_bytes: int | None
    median_wire_bytes: int | None
    statuses: list[int]
    messages: list[str]
    samples: list[_SampleDocument]


class _StreamDocument(TypedDict):
    status: int
    seconds: float
    frames: int
    distinct_ids: int
    frames_per_sec: float | None


class _MeasurementReport(TypedDict):
    url: str
    server_sha: str
    measured_at: str
    rows: list[_RowDocument]
    stream: _StreamDocument


def _thread_tick(*, main: int, ticks: dict[int, float]) -> float:
    thread = get_ident()
    ticks[thread] = ticks.get(thread, 0.0) + (10.0 if thread == main else 1.0)
    return ticks[thread]


def _comparison_row(name: str, *, seconds: float, size: int) -> Row:
    return Row(
        name=name,
        samples=(Sample(seconds=seconds, status=200, json_bytes=size, wire_bytes=7),),
    )


class _MeasurementServer:
    def __init__(self, *, empty: bool, search_status: int = 200) -> None:
        self.empty = empty
        self.search_status = search_status
        self.kinds: tuple[str, ...] = ("Issue", "Belief", "Paper", "Experiment")
        self.tabs = (
            "status",
            "belief_judgement",
            "created",
            "edge_added",
            "description",
        )
        self.calls: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        self.mine: Counter[str] = Counter()
        self.memberships = 0
        self.refetches = 0
        self.elapsed = 0.0

    def clock(self) -> float:
        self.elapsed += 0.5
        return self.elapsed

    def answer(self, request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        params = parse_qs(request.url.query.decode())
        key = (request.url.path, tuple(sorted(request.url.params.multi_items())))
        self.calls[key] += 1
        self.elapsed += 2.0
        body = self.body(request.url.path, params=params)
        raw = gzip.compress(body, mtime=0)
        status = 200
        if request.url.path == "/api/web/search" and self.search_status != 200:
            kind = params.get("kind", [""])[0]
            statuses = (
                (200, 201, 399, 399)
                if self.search_status == 399
                else (200, 399, 400, 502)
            )
            status = dict(zip(self.kinds, statuses, strict=True)).get(
                kind,
                self.search_status,
            )
        return httpx2.Response(
            status,
            content=self.chunks(raw),
            headers={"content-encoding": "gzip"},
        )

    def chunks(self, raw: bytes) -> Iterable[bytes]:
        self.elapsed += 3.0
        yield raw

    def body(self, path: str, *, params: dict[str, list[str]]) -> bytes:
        if path == "/api/meta/enums":
            assert params == {}
            return json_dumps({"inquiry_kind_all": list(self.kinds)}).encode()
        if path == "/api/me/profile":
            assert params == {}
            return json_dumps({"email": "reader@example.net"}).encode()
        if path in ("/api/meta/fields", "/api/meta/edges"):
            assert params == {}
            return json_dumps({"path": path}).encode()
        if path == "/api/inquiries":
            return self.inquiries(params)
        if path == "/api/inquiries/Paper/81":
            assert params == {}
            return json_dumps({"id": "explicit"}).encode()
        if path.startswith("/api/web/get/"):
            assert params == {}
            explicit = path.endswith("/explicit")
            return json_dumps(
                {
                    "self": {
                        "kind": "Paper" if explicit else "Issue",
                        "seq": 81 if explicit else 71,
                    },
                    "edges": {"narrows": [{"id": "a"}, {"id": "b"}]},
                    "backlinks": {
                        "narrows": [{"id": "c"}],
                        "related": [{"id": "d"}, {"id": "e"}],
                    },
                },
            ).encode()
        if path == "/api/change_log":
            assert params["kind"][0] in self.tabs
            assert params["limit"] in (["1"], ["50"])
            return json_dumps(
                [] if self.empty else [{"created": "2026-11-01T12:34:56+00:00"}],
            ).encode()
        assert path == "/api/web/search"
        assert params["q"][0] in ("alpha", "beta gamma")
        kind = params.get("kind", [""])[0]
        assert params["limit"] == (["5"] if kind else ["50"])
        assert kind in (*self.kinds, "")
        return self.search_body(params["q"][0], kind=kind)

    def inquiries(self, params: dict[str, list[str]]) -> bytes:
        if "filter" in params:
            (text,) = params["filter"]
            selected = json_loads(text, _Filter)
            if selected["field"] == "account":
                assert selected == {
                    "field": "account",
                    "op": "is",
                    "value": "reader@example.net",
                }
                assert params["kind"] == list(self.kinds)
                (limit,) = params["limit"]
                assert limit in ("20", "50")
                self.mine[limit] += 1
            else:
                assert params["kind"] == list(self.kinds[:3])
                assert "limit" not in params
                assert selected == {
                    "field": "id",
                    "op": "re",
                    "value": "^(" + "|".join(f"issue-{n}" for n in range(13)) + ")$",
                }
                self.memberships += 1
            return json_dumps([selected]).encode()
        assert params["kind"] == ["Issue"]
        assert params["limit"] == ["50"]
        if "seq_range" in params:
            assert params["seq_range"] == ["2..4", "9..9", "20..29"]
            self.refetches += 1
        seqs = (29, 2, 4, 9, 3, 20, 28, 21, 27, 22, 26, 23, 25, 24)
        return json_dumps(
            []
            if self.empty
            else [
                {
                    "id": f"issue-{n}",
                    "seq": seq,
                    "narrows": [
                        {
                            "id": "rare" if n == 0 else "popular",
                        },
                    ],
                }
                for n, seq in enumerate(seqs)
            ],
        ).encode()

    def search_body(self, query: str, *, kind: str) -> bytes:
        if self.search_status != 200:
            return json_dumps({"detail": f"{query}/{kind}"}).encode()
        return json_dumps(
            {
                "query": query,
                "kind": kind,
                "padding": "x" * (17 * len(kind) + len(query)),
            },
        ).encode()

    def search_sizes(self, query: str, *, kind: str) -> tuple[int, int]:
        body = self.search_body(query, kind=kind)
        return len(body), len(gzip.compress(body, mtime=0))


class _LatchedSearchServer(_MeasurementServer):
    def __init__(self) -> None:
        super().__init__(empty=True)
        self.kinds = tuple(f"Kind{index}" for index in range(33))
        self.latch = Barrier(len(self.kinds))
        self.lock = Lock()
        self.seen: Counter[str] = Counter()
        self.arrived: set[str] = set()

    @override
    def answer(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/web/search" and "kind" in request.url.params:
            kind = request.url.params["kind"]
            with self.lock:
                self.seen[kind] += 1
                parallel = self.seen[kind] == 2
                if parallel:
                    self.arrived.add(kind)
            if parallel:
                self.latch.wait(timeout=5.0)
                assert self.arrived == set(self.kinds)
        return super().answer(request)


class _MainMeasurementServer(_MeasurementServer):
    def __init__(self) -> None:
        super().__init__(empty=True)
        self.origins: list[str] = []
        self.credentials: list[str] = []
        self.codings: list[str] = []
        self.stream_windows: list[float] = []
        self.searches: list[tuple[str, str]] = []

    @override
    def answer(self, request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        self.origins.append(str(request.url).split("/api/", 1)[0])
        self.credentials.append(request.headers.get("authorization", ""))
        self.codings.append(request.headers.get("accept-encoding", ""))
        if request.url.path == "/api/web/subscribe":
            self.stream_windows.append(
                cast(dict[str, float], request.extensions["timeout"])["read"],
            )
            return httpx2.Response(401, content=iter([b"{}"]))
        if request.url.path == "/api/version":
            return httpx2.Response(200, content=iter([b'{"sha":"build-main"}']))
        return super().answer(request)

    @override
    def body(self, path: str, *, params: dict[str, list[str]]) -> bytes:
        if path == "/api/web/search":
            query = params["q"][0]
            kind = params.get("kind", [""])[0]
            assert params["limit"] == (["5"] if kind else ["50"])
            self.searches.append((query, kind))
            return self.search_body(query, kind=kind)
        return super().body(path, params=params)


def _fixed_now(tz: tzinfo | None = None) -> datetime:
    return datetime(2026, 11, 6, 12, 34, 56, tzinfo=UTC).astimezone(tz)


def _answer_without_narrows(
    request: httpx2.Request,
    *,
    paths: list[str],
) -> httpx2.Response:
    assert request.method == "GET"
    paths.append(request.url.path)
    if request.url.path == "/api/inquiries":
        body = json_dumps(
            [{"id": "orphan-a", "seq": 2}, {"id": "orphan-b", "seq": 4}],
        ).encode()
        return httpx2.Response(200, content=iter([body]))
    return _answer(request, [])


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
        return {
            "self": {"kind": "Issue", "seq": 1},
            "edges": dict[str, list[dict[str, object]]](),
            "backlinks": dict[str, list[dict[str, object]]](),
        }
    if path == "/api/change_log":
        return [{"id": "c", "created": "2026-09-27T00:00:00+00:00"}]
    return (
        list[dict[str, object]]() if path == "/api/web/search" else dict[str, object]()
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
