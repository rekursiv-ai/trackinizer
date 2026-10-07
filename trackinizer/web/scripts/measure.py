#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Measure the reads the web app makes, with GET requests only, against one server.

Takes the web app's baseline measurements: the boot calls one after another and
in parallel, a 50-Issue list, Mine across every kind at 20 and 50 per kind,
details of the newest Issues and of a hub, each Activity tab and a `since`
refetch, search one kind at a time, every kind in parallel as the palette sends
it, and all kinds in one request, a seq-range refetch of 50 rows, a membership
check of up to 13 ids, and the live stream's frame rate; the reads of the newest
Issues are skipped when there are none. Times run to the last byte.
Sizes are the JSON's and the body's on the wire, compressed as the server's proxy
sends them (zstd, gzip). An answer that is an error (a 400 over the search budget, a
proxy's 502) is kept with its status and message, but never timed as a read:
medians are of successful answers only. The report goes to
/opt/scratch/artifacts/trackinizer-web/measure/<date>/ as JSON. With --baseline,
it lists every row whose median time or JSON size moved more than 2x from an
earlier report.

The server is the active trax profile's by default, read with its token. A --url
that names another server (host, port and path, as mirror_local.py compares
them) gets no token, which suits the local preview (--no-auth), so the profile's
token never leaves for another host.

Examples:
  ./measure.py                                  # the profile's server, read-only
  ./measure.py --url http://127.0.0.1:8765      # the local preview
  ./measure.py --baseline /opt/scratch/artifacts/trackinizer-web/measure/2026-09-27/host-120000.json

'''
# fmt: on

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast
from urllib.parse import urlsplit

import argparse
import gzip
import json
import statistics
import time

import httpx2

from trackinizer.lib import zstd_compat
from trackinizer.lib.codec import PlainTree, ReadError, from_plain, loads
from trackinizer.trax.profile import load_profile
from trackinizer.web.scripts.mirror_local import source_token


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from trackinizer.trax.profile import Profile


# How to undo each `Content-Encoding` the client offers: the body is read raw so
# its size on the wire can be counted, which leaves the decoding to this table.
# No `br`: the deployment's proxy compresses with zstd and gzip only, and Brotli's
# decoder is not a dependency of the published package.
_DECODERS: Final[Mapping[str, Callable[[bytes], bytes]]] = {
    "gzip": gzip.decompress,
    "zstd": zstd_compat.decompress,
}


type Params = tuple[tuple[str, str | int], ...]
"""Query parameters in order; a key may repeat (`kind`, `filter`, `seq_range`)."""


@dataclass(frozen=True, kw_only=True, slots=True)
class Sample:
    """One timed read, or several read together and timed as one."""

    seconds: float
    status: int
    json_bytes: int
    wire_bytes: int
    message: str = ""
    """The server's `detail` on an error answer."""


@dataclass(frozen=True, kw_only=True, slots=True)
class Row:
    """One measurement: what it read, and its samples."""

    name: str
    samples: tuple[Sample, ...]
    note: str = ""


@dataclass(frozen=True, kw_only=True, slots=True)
class Stream:
    """What the live stream sent while it was watched."""

    seconds: float
    frames: int
    distinct_ids: int
    status: int
    """The stream's answer: 200, or the refusal it got instead of frames."""


def main() -> int:
    """Measure one server, write the report, and print it.

    Returns:
      exit_code: 0; a failed read raises instead.

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args())
    profile = load_profile()
    url = (flags.url or profile.url).rstrip("/")
    queries = flags.query or ["trackinizer", "membership check", "title:^web"]
    with httpx2.Client(
        base_url=url,
        headers=_headers(url, profile),
        timeout=30.0,
    ) as http:
        version = from_plain(_read(http, "/api/version"), dict[str, object])
        rows = measure(http, repeats=flags.repeats, queries=queries, hub=flags.hub)
        stream = sample_stream(http, seconds=flags.stream_sec)
    sha = from_plain(version.get("sha"), str)
    now = datetime.now(UTC).astimezone()
    folder = Path("/opt/scratch/artifacts/trackinizer-web/measure") / f"{now:%Y-%m-%d}"
    path = _write(report(rows, stream=stream, url=url, sha=sha), url=url, folder=folder)
    _print_table(rows, stream)
    print(f"\nserver {sha} at {url}\nwrote {path}")
    if flags.baseline:
        changes = moved(
            rows,
            from_plain(loads(flags.baseline.read_text()), dict[str, object]),
        )
        print(f"\nmoved more than 2x from {flags.baseline}: {len(changes) or 'none'}")
        for name, what, before, after in changes:
            print(f"  {name}: {what} {before:g} -> {after:g}")
    return 0


def measure(
    http: httpx2.Client,
    *,
    repeats: int,
    queries: Sequence[str],
    hub: str,
) -> list[Row]:
    """Take every measurement but the stream's, reading only.

    Args:
      http: A client on the server to measure, with any credentials it needs.
      repeats: Samples per read; searches take one per query instead.
      queries: Search queries, each searched every way.
      hub: `Kind#seq` of the hub whose detail to read, or "" for the parent
        the most of the newest 50 Issues narrow.

    Returns:
      rows: One per measurement, in the order of the plan's table.

    """
    kinds = from_plain(
        from_plain(_read(http, "/api/meta/enums"), dict[str, object]).get(
            "inquiry_kind_all",
        ),
        list[str],
    )
    issues = from_plain(
        _read(http, "/api/inquiries", (("kind", "Issue"), ("limit", 50))),
        list[dict[str, object]],
    )
    return [
        *_boot_rows(http, repeats=repeats),
        *_list_rows(http, kinds=kinds, repeats=repeats),
        *_detail_rows(http, issues=issues, hub=hub, repeats=repeats),
        *_activity_rows(http, repeats=repeats),
        *_search_rows(http, kinds=kinds, queries=queries),
        *_live_rows(http, kinds=kinds, issues=issues, repeats=repeats),
    ]


def sample_stream(http: httpx2.Client, *, seconds: float) -> Stream:
    """Watch `GET /api/web/subscribe` and count its frames and distinct ids.

    Args:
      http: A client on the server to watch.
      seconds: How long to watch.

    Returns:
      stream: The frames and distinct ids seen, the time watched, and the
        answer: a refused stream (a 401, a proxy's 502) sends its error body,
        not frames, and is reported as refused rather than as a quiet one.

    """
    ids: list[str] = []
    status = 0
    start = time.perf_counter()
    # A read that waits out the window means a quiet stream, not a failure.
    try:
        with http.stream(
            "GET",
            "/api/web/subscribe",
            timeout=httpx2.Timeout(30.0, read=seconds),
        ) as response:
            status = response.status_code
            if response.is_error:
                return Stream(seconds=0.0, frames=0, distinct_ids=0, status=status)
            for line in response.iter_lines():
                if line.startswith("data:"):
                    frame = from_plain(
                        loads(line.removeprefix("data:")),
                        dict[str, object],
                    )
                    ids.append(from_plain(frame.get("id"), str))
                if time.perf_counter() - start >= seconds:
                    break
    except httpx2.ReadTimeout:
        pass
    elapsed = time.perf_counter() - start
    return Stream(
        seconds=elapsed,
        frames=len(ids),
        distinct_ids=len(set(ids)),
        status=status,
    )


def report(
    rows: Sequence[Row],
    *,
    stream: Stream,
    url: str,
    sha: str,
) -> dict[str, PlainTree]:
    """Build the JSON report of one run.

    Args:
      rows: The run's measurements.
      stream: What the live stream sent.
      url: The server measured.
      sha: The server's build, from `/api/version`.

    Returns:
      document: Each row's medians and samples, and the stream's rate; a
        median, or the rate of a refused stream, is null where nothing
        succeeded to take it from.

    """
    return {
        "url": url,
        "server_sha": sha,
        "measured_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "rows": [_summary(row) for row in rows],
        "stream": {
            "status": stream.status,
            "seconds": stream.seconds,
            "frames": stream.frames,
            "distinct_ids": stream.distinct_ids,
            "frames_per_sec": stream.frames / stream.seconds
            if stream.status == 200
            else None,
        },
    }


def moved(
    rows: Sequence[Row],
    baseline: Mapping[str, object],
) -> list[tuple[str, str, float, float]]:
    """Rows whose median time or JSON size moved more than 2x from `baseline`.

    Args:
      rows: This run's measurements.
      baseline: An earlier report, as `report` wrote it.

    Returns:
      changes: `(row, what, before, after)` for each move, in row order. Rows
        missing from either run, and medians that are zero or were not taken
        (no successful answer), are not compared.

    """
    before = {
        from_plain(old.get("name"), str): old
        for old in from_plain(baseline.get("rows"), list[dict[str, object]], default=[])
    }
    changes: list[tuple[str, str, float, float]] = []
    for row in rows:
        old = before.get(row.name)
        if old is None:
            continue
        now_seconds, now_bytes = _medians(row)
        pairs = (
            (
                "median seconds",
                from_plain(old.get("median_seconds"), float, default=None),
                now_seconds,
            ),
            (
                "median JSON bytes",
                from_plain(old.get("median_json_bytes"), float, default=None),
                now_bytes,
            ),
        )
        changes.extend(
            (row.name, what, then, now)
            for what, then, now in pairs
            if (
                then is not None
                and now is not None
                and then != 0
                and now != 0
                and max(then / now, now / then) > 2
            )
        )
    return changes


class _Flags(Protocol):
    url: str
    repeats: int
    query: list[str]
    hub: str
    stream_sec: float
    baseline: Path | None


def _headers(url: str, profile: Profile) -> dict[str, str]:
    """Return the request headers: every coding decoded, and the token on its server."""
    headers = {"Accept-Encoding": ", ".join(_DECODERS)}
    if token := source_token(url, profile):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument(
        "--url",
        default="",
        help="Server to measure; default the active trax profile's, with its token.",
    )
    parser.add_argument(
        "--repeats",
        type=partial(_above_zero, int),
        default=3,
        help="Samples per read.",
    )
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        help=(
            "Search query to time; repeat for several. Default: trackinizer, "
            "'membership check', title:^web."
        ),
    )
    parser.add_argument(
        "--hub",
        default="",
        help="Kind#seq of a hub to read; default the newest Issues' commonest parent.",
    )
    parser.add_argument("--stream-sec", type=partial(_above_zero, float), default=20.0)
    parser.add_argument("--baseline", type=Path, help="An earlier report to compare.")


# Zero repeats would leave every row without a sample to take a median of, and a zero-
# second window a stream with no rate.
def _above_zero[N: (int, float)](kind: Callable[[str], N], text: str) -> N:
    """Read a flag's `text` as `kind`, refused unless above zero (an argparse type)."""
    value = kind(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be above zero, not {text}")
    return value


def _boot_rows(http: httpx2.Client, *, repeats: int) -> list[Row]:
    boot: tuple[tuple[str, Params], ...] = (
        ("/api/meta/enums", ()),
        ("/api/meta/fields", ()),
        ("/api/meta/edges", ()),
        ("/api/me/profile", ()),
    )
    return [
        _row("boot, one after another", partial(_serial, http, boot), repeats=repeats),
        _row("boot, in parallel", partial(_parallel, http, boot), repeats=repeats),
    ]


def _list_rows(
    http: httpx2.Client,
    *,
    kinds: Sequence[str],
    repeats: int,
) -> list[Row]:
    profile = from_plain(_read(http, "/api/me/profile"), dict[str, object])
    mine = json.dumps(
        {
            "field": "account",
            "op": "is",
            "value": from_plain(profile.get("email"), str),
        },
    )
    every: Params = tuple(("kind", kind) for kind in kinds)
    return [
        _row(
            "list: 50 Issues",
            partial(_get, http, "/api/inquiries", (("kind", "Issue"), ("limit", 50))),
            repeats=repeats,
        ),
        *(
            _row(
                f"list: Mine, every kind, {limit} per kind",
                partial(
                    _get,
                    http,
                    "/api/inquiries",
                    (*every, ("filter", mine), ("limit", limit)),
                ),
                repeats=repeats,
            )
            for limit in (20, 50)
        ),
    ]


def _detail_rows(
    http: httpx2.Client,
    *,
    issues: Sequence[Mapping[str, object]],
    hub: str,
    repeats: int,
) -> list[Row]:
    newest = [from_plain(row.get("id"), str) for row in issues[:5]]
    rows: list[Row] = []
    # With no Issues the row would take no sample, and report an empty median.
    if newest:
        rows.append(
            _row(
                "detail: newest Issues",
                *(partial(_get, http, f"/api/web/get/{id_}") for id_ in newest),
                repeats=repeats,
                note=f"{len(newest)} Issues",
            ),
        )
    hub_id = _hub_id(http, issues=issues, hub=hub)
    if hub_id:
        view = from_plain(_read(http, f"/api/web/get/{hub_id}"), dict[str, object])
        head = from_plain(view.get("self"), dict[str, object])
        relations = sum(
            len(from_plain(peers, list[object]))
            for side in ("edges", "backlinks")
            for peers in from_plain(view.get(side), dict[str, object]).values()
        )
        ref = f"{from_plain(head.get('kind'), str)}#{from_plain(head.get('seq'), int)}"
        rows.append(
            _row(
                "detail: hub",
                partial(_get, http, f"/api/web/get/{hub_id}"),
                repeats=repeats,
                note=f"{ref}, {relations} relations",
            ),
        )
    return rows


def _hub_id(
    http: httpx2.Client,
    *,
    issues: Sequence[Mapping[str, object]],
    hub: str,
) -> str:
    """Return the id of `hub` (`Kind#seq`), or of the parent most `issues` narrow."""
    if hub:
        kind, _, seq = hub.partition("#")
        found = from_plain(
            _read(http, f"/api/inquiries/{kind}/{seq}"),
            dict[str, object],
        )
        return from_plain(found.get("id"), str)
    parents = Counter(
        from_plain(parent.get("id"), str)
        for row in issues
        for parent in from_plain(
            row.get("narrows"),
            list[dict[str, object]],
            default=[],
        )
    )
    return parents.most_common(1)[0][0] if parents else ""


def _activity_rows(http: httpx2.Client, *, repeats: int) -> list[Row]:
    # The five tabs of `src/activity/feed.ts`; All merges them on the client.
    tabs = ("status", "belief_judgement", "created", "edge_added", "description")
    rows = [
        _row(
            f"activity: {tab} tab, 50 rows",
            partial(_get, http, "/api/change_log", (("kind", tab), ("limit", 50))),
            repeats=repeats,
        )
        for tab in tabs
    ]
    newest = from_plain(
        _read(http, "/api/change_log", (("kind", "status"), ("limit", 1))),
        list[dict[str, object]],
    )
    since = from_plain(newest[0].get("created"), str) if newest else "1970-01-01"
    rows.append(
        _row(
            "activity: status since the newest",
            partial(
                _get,
                http,
                "/api/change_log",
                (("kind", "status"), ("since", since), ("limit", 50)),
            ),
            repeats=repeats,
        ),
    )
    return rows


def _search_rows(
    http: httpx2.Client,
    *,
    kinds: Sequence[str],
    queries: Sequence[str],
) -> list[Row]:
    def one(q: str, kind: str) -> tuple[str, Params]:
        return "/api/web/search", (("q", q), ("kind", kind), ("limit", 5))

    return [
        _row(
            "search: one kind, limit 5",
            *(partial(_get, http, *one(q, kind)) for q in queries for kind in kinds),
            note=f"{len(queries)} queries x {len(kinds)} kinds",
        ),
        _row(
            "search: every kind in parallel, limit 5",
            *(
                partial(_parallel, http, tuple(one(q, kind) for kind in kinds))
                for q in queries
            ),
            note="wall time of the palette's requests, one per kind",
        ),
        _row(
            "search: all kinds, limit 50",
            *(
                partial(_get, http, "/api/web/search", (("q", q), ("limit", 50)))
                for q in queries
            ),
        ),
    ]


def _live_rows(
    http: httpx2.Client,
    *,
    kinds: Sequence[str],
    issues: Sequence[Mapping[str, object]],
    repeats: int,
) -> list[Row]:
    # With no Issues, both reads would ask for nothing (no seq_range, `^()$`) and time
    # an empty answer as if it were the live layer's refetch.
    if not issues:
        return []
    spans: Params = tuple(
        ("seq_range", span)
        for span in _spans(sorted(from_plain(row.get("seq"), int) for row in issues))
    )
    ids = [from_plain(row.get("id"), str) for row in issues[:13]]
    check = json.dumps({"field": "id", "op": "re", "value": f"^({'|'.join(ids)})$"})
    three: Params = tuple(("kind", kind) for kind in kinds[:3])
    return [
        _row(
            "refetch: 50 Issues by seq_range",
            partial(
                _get,
                http,
                "/api/inquiries",
                (("kind", "Issue"), *spans, ("limit", 50)),
            ),
            repeats=repeats,
            note=f"{len(spans)} ranges",
        ),
        _row(
            f"membership: {len(ids)} ids, three kinds",
            partial(_get, http, "/api/inquiries", (*three, ("filter", check))),
            repeats=repeats,
            note=", ".join(kinds[:3]),
        ),
    ]


def _spans(seqs: Iterable[int]) -> list[str]:
    """Sorted `seqs` as the fewest inclusive `a..b` ranges, as the live layer sends."""
    spans: list[list[int]] = []
    for seq in seqs:
        if spans and spans[-1][1] == seq - 1:
            spans[-1][1] = seq
        else:
            spans.append([seq, seq])
    return [f"{first}..{last}" for first, last in spans]


def _row(
    name: str,
    *takes: Callable[[], Sample],
    repeats: int = 1,
    note: str = "",
) -> Row:
    """Run each of `takes` `repeats` times, round-robin, as one row."""
    samples = tuple(take() for _ in range(repeats) for take in takes)
    return Row(name=name, samples=samples, note=note)


def _get(http: httpx2.Client, path: str, params: Params = ()) -> Sample:
    """Time one GET to its last byte, and size its body raw and decoded."""
    start = time.perf_counter()
    with http.stream("GET", path, params=params) as response:
        raw = b"".join(response.iter_raw())
    seconds = time.perf_counter() - start
    body = _decoded(raw, response.headers.get("content-encoding", ""))
    return Sample(
        seconds=seconds,
        status=response.status_code,
        json_bytes=len(body),
        wire_bytes=len(raw),
        message=_message(body) if response.is_error else "",
    )


# FastAPI's `detail` is a string for a refusal and a list for a 422; a proxy answers
# JSON of its own shape, or an HTML page.
def _message(body: bytes) -> str:
    """Return an error body's `detail`, else its JSON, else the start of its text."""
    try:
        value = loads(body)
    except ValueError:
        return body[:200].decode(errors="replace")
    try:
        fields = from_plain(value, dict[str, object])
    except ReadError:
        fields = {}
    detail = fields.get("detail", value)
    return detail if isinstance(detail, str) else json.dumps(detail)


def _serial(http: httpx2.Client, reads: Iterable[tuple[str, Params]]) -> Sample:
    start = time.perf_counter()
    samples = [_get(http, path, params) for path, params in reads]
    return _together(samples, seconds=time.perf_counter() - start)


def _parallel(http: httpx2.Client, reads: Sequence[tuple[str, Params]]) -> Sample:
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futures = [pool.submit(_get, http, path, params) for path, params in reads]
        samples = [future.result() for future in futures]
    return _together(samples, seconds=time.perf_counter() - start)


def _together(samples: Sequence[Sample], *, seconds: float) -> Sample:
    return Sample(
        seconds=seconds,
        status=max(sample.status for sample in samples),
        json_bytes=sum(sample.json_bytes for sample in samples),
        wire_bytes=sum(sample.wire_bytes for sample in samples),
        message="; ".join(sorted({sample.message for sample in samples} - {""})),
    )


def _decoded(raw: bytes, content_encoding: str) -> bytes:
    """Undo `content_encoding`: case-insensitive codings, applied in the order listed."""
    codings = [c.strip().lower() for c in content_encoding.split(",") if c.strip()]
    for coding in reversed(codings):
        decoder = _DECODERS.get(coding)
        if decoder is None:
            raise ValueError(
                f"unknown Content-Encoding {coding!r} in {content_encoding!r}",
            )
        raw = decoder(raw)
    return raw


def _read(http: httpx2.Client, path: str, params: Params = ()) -> PlainTree:
    """GET untimed, for what the measurements need to know first."""
    return loads(
        http.get(path, params=params).raise_for_status().content.decode(),
    )


def _summary(row: Row) -> dict[str, PlainTree]:
    ok = _ok(row)
    seconds = [sample.seconds for sample in ok]
    median_seconds, median_bytes = _medians(row)
    return {
        "name": row.name,
        "note": row.note,
        "median_seconds": median_seconds,
        "min_seconds": min(seconds, default=None),
        "max_seconds": max(seconds, default=None),
        "median_json_bytes": None if median_bytes is None else round(median_bytes),
        "median_wire_bytes": round(statistics.median(s.wire_bytes for s in ok))
        if ok
        else None,
        "statuses": sorted({sample.status for sample in row.samples}),
        "messages": sorted({sample.message for sample in row.samples} - {""}),
        "samples": [
            {
                "seconds": sample.seconds,
                "status": sample.status,
                "json_bytes": sample.json_bytes,
                "wire_bytes": sample.wire_bytes,
                "message": sample.message,
            }
            for sample in row.samples
        ],
    }


def _ok(row: Row) -> list[Sample]:
    """Keep the row's successful answers: the only ones timed and sized as reads."""
    return [sample for sample in row.samples if sample.status < 400]


def _medians(row: Row) -> tuple[float | None, float | None]:
    """Take the median seconds and JSON bytes of the successful answers, or None each."""
    ok = _ok(row)
    if not ok:
        return None, None
    return (
        statistics.median(sample.seconds for sample in ok),
        statistics.median(sample.json_bytes for sample in ok),
    )


# Two runs in one second against one server share a name, so the file is made only if it
# is not there (``"x"``), and a later one takes the next number.
def _write(document: Mapping[str, PlainTree], *, url: str, folder: Path) -> Path:
    """Write `document` in `folder`, named for the server and time, never over another."""
    now = datetime.now(UTC).astimezone()
    host = (urlsplit(url).netloc or url).replace(":", "-")
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2) + "\n"
    taken = 0
    while True:
        taken += 1
        path = folder / f"{host}-{now:%H%M%S}{'' if taken == 1 else f'-{taken}'}.json"
        try:
            with path.open("x") as out:
                out.write(text)
        except FileExistsError:
            continue
        return path


def _print_table(rows: Sequence[Row], stream: Stream) -> None:
    print(
        f"{'measurement':42} {'n':>3} {'median s':>8} {'min-max s':>11} "
        f"{'JSON KB':>8} {'wire KB':>8}  status",
    )
    for row in rows:
        ok = _ok(row)
        statuses = ",".join(str(s) for s in sorted({s.status for s in row.samples}))
        if ok:
            seconds = [sample.seconds for sample in ok]
            json_kb = statistics.median(s.json_bytes for s in ok) / 1000
            wire_kb = statistics.median(s.wire_bytes for s in ok) / 1000
            spread = f"{min(seconds):.2f}-{max(seconds):.2f}"
            timed = f"{statistics.median(seconds):8.3f} {spread:>11} {json_kb:8.1f} {wire_kb:8.1f}"
        else:
            timed = f"{'-':>8} {'-':>11} {'-':>8} {'-':>8}"
        print(
            f"{row.name:42} {len(ok):3} {timed}  {statuses}"
            + (f"  ({row.note})" if row.note else ""),
        )
        for message in sorted({sample.message for sample in row.samples} - {""}):
            print(f"{'':46}{message}")
    if stream.status != 200:
        print(f"stream: refused with {stream.status}")
        return
    print(
        f"stream: {stream.frames} frames, {stream.distinct_ids} distinct ids in "
        f"{stream.seconds:.1f} s ({stream.frames / stream.seconds:.2f} a second)",
    )


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
