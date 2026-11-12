"""Tests for the session converter."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, Never, Self

import argparse
import functools
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tracemalloc

from treekle import from_plain, json_loads

import psutil
import pytest

from trackinizer.lib.agent.sessions import (
    claude,
    codex,
    convert,
    fuse,
    gemini,
    normalized,
)
from trackinizer.lib.agent.sessions.convert import (
    FileResult,
    _diff,
    _dropped,
    _roots,
    _session_files,
    _session_parts,
    _status,
    _workers,
    convert_file,
    detect_format,
    main,
)
from trackinizer.lib.testing.resources import ascii_locale
from trackinizer.sagent.session.types import (
    SessionRecord,
    ToolCall,
    TurnContext,
    UserMessage,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


_CWD: Final[Path] = Path(__file__).resolve().parent


@functools.cache
def _claude_session() -> str:
    return (_CWD / "testdata" / "claude_sidechain.jsonl").read_text(encoding="utf-8")


@functools.cache
def _codex_session() -> str:
    return _first_turn(
        (_CWD / "testdata" / "codex_main.jsonl").read_text(encoding="utf-8"),
    )


# The launch line, then everything from the first ``turn_context`` through the first
# tool output: one user turn, one reasoning, one reply, one tool call and its result.
# The pre-turn preamble -- skills, plugins, world state, 25 KB of it -- and the later
# turns say nothing to a CLI test that the first act does not, and the whole 80 KB
# capture cost 270 ms per round trip against 60 ms for this slice. Selected by kind, not
# by line number, so a recapture that reorders the preamble still yields the same shape.
def _first_turn(rollout: str) -> str:
    """Return a codex rollout's launch line plus its first turn's first act."""
    kept = [rollout.splitlines(keepends=True)[0]]
    started = False
    for line in rollout.splitlines(keepends=True)[1:]:
        record = json_loads(line, dict[str, object])
        started = started or record.get("type") == "turn_context"
        if not started:
            continue
        kept.append(line)
        payload = from_plain(
            record.get("payload"),
            dict[str, object],
            default=dict[str, object](),
        )
        if from_plain(payload.get("type"), str, default="").endswith(
            "tool_call_output",
        ):
            return "".join(kept)
    raise AssertionError("the capture has no tool output to slice at")


@pytest.mark.parametrize("source", ["claude", "codex"])
def test_convert_to_json_and_back_recovers_the_native_bytes(
    tmp_path: Path,
    source: str,
) -> None:
    native = _claude_session() if source == "claude" else _codex_session()
    path = tmp_path / "session.jsonl"
    path.write_text(native)
    as_json = tmp_path / "session.json"

    assert main(["convert", str(path), "--to", "json", "-o", str(as_json)]) == 0
    assert main(["convert", str(as_json), "--to", source]) == 0

    result = convert_file(as_json, "auto", source, False)
    assert result.source == "json"
    assert result.text == native


def test_convert_writes_stdout_and_out_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(_codex_session())
    out_dir = tmp_path / "out"

    assert main(["convert", str(path), "--to", "json"]) == 0
    # A bare ARRAY of tagged records: a session IS its records, so nothing
    # wraps them and no metadata sits beside them.
    document = json_loads(capsys.readouterr().out, list[dict[str, object]])
    assert from_plain(document[0].get("py/object"), str, default="").endswith(
        "TurnContext",
    )

    assert main(["convert", str(path), "--to", "json", "--out-dir", str(out_dir)]) == 0
    assert (out_dir / "session.json").exists()


def test_verify_reports_exactness(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = tmp_path / "good.jsonl"
    good.write_text(_claude_session())
    bad = tmp_path / "bad.jsonl"
    # Respaced, not rewritten: the record still parses to the same object, so
    # only a BYTE comparison can tell the two files apart.
    bad.write_text(
        _claude_session().replace('"parentUuid":null', '"parentUuid" : null'),
    )

    assert main(["verify", str(good), "-v"]) == 0
    assert "1/1 exact" in capsys.readouterr().err

    assert main(["verify", str(bad), "--diff"]) == 1
    err = capsys.readouterr().err
    assert "not byte-exact" in err
    assert "--- original" in err


def test_verify_does_not_truncate_the_file_named_by_output(tmp_path: Path) -> None:
    """``verify`` writes no conversion, so ``-o`` is refused and left alone.

    Verifying keeps no text -- it asks whether the bytes agree, not for a copy
    of them -- but the destination was written unconditionally, so the empty
    string landed on whatever ``-o`` named and destroyed a file the command
    never claimed to touch.
    """
    path = tmp_path / "s.jsonl"
    path.write_text(_claude_session())
    output = tmp_path / "out.txt"
    output.write_text("PREEXISTING")

    with pytest.raises(SystemExit) as excinfo:
        main(["verify", str(path), "-o", str(output)])

    assert excinfo.value.code == 2
    assert output.read_text() == "PREEXISTING"


def test_verify_refuses_an_output_destination(tmp_path: Path) -> None:
    # Verify keeps no text, so ``--out-dir`` wrote every part as 0 bytes -- and
    # pointed at the source's own directory, it truncated the source.
    path = tmp_path / "s.jsonl"
    native = _claude_session()
    path.write_text(native)

    with pytest.raises(SystemExit) as excinfo:
        main(["verify", str(path), "--out-dir", str(tmp_path)])

    assert excinfo.value.code == 2
    assert path.read_text() == native


def test_a_seam_name_cannot_escape_the_output_directory(tmp_path: Path) -> None:
    # ``$seam`` comes from a normalized document, which is untrusted input.
    victim = tmp_path / "victim.jsonl"
    victim.write_text("KEEP")
    records = list(claude.normalize(StringIO(_claude_session())))
    fused = list(fuse.fuse([records, records], ["a.jsonl", "../victim.jsonl"]))
    source = tmp_path / "in" / "s.json"
    source.parent.mkdir()
    out_dir = tmp_path / "out"
    with source.open("w", encoding="utf-8") as handle:
        normalized.denormalize(fused, handle)

    assert main(["convert", str(source), "--to", "claude", "--out-dir", str(out_dir)])

    assert victim.read_text() == "KEEP"


def test_a_crlf_transcript_does_not_verify_as_byte_exact(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(_claude_session().replace("\n", "\r\n").encode("utf-8"))

    assert not convert_file(path, "auto", None, False, destination=False).byte_exact
    assert not convert_file(path, "auto", None, False).byte_exact


def test_a_native_out_dir_does_not_overwrite_a_same_named_part(tmp_path: Path) -> None:
    # Two sessions whose parts share one file name both land in ``out_dir``.
    first = _session(tmp_path / "a" / "s.jsonl", _claude_session())
    second = _session(tmp_path / "b" / "s.jsonl", _claude_session())
    out_dir = tmp_path / "out"

    assert (
        main(
            [
                "convert",
                str(first),
                str(second),
                "--to",
                "codex",
                "--lossy",
                "--out-dir",
                str(out_dir),
            ],
        )
        == 0
    )

    assert len(list(out_dir.iterdir())) == 2


@pytest.mark.cli_python_subprocess
def test_verify_runs_a_directory_in_parallel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Two SESSIONS, each its own directory: files sharing a directory are one
    # session, since that is how a ``/clear`` continuation is recognized.
    _ = _session(tmp_path / "one" / "a.jsonl", _claude_session())
    _ = _session(tmp_path / "two" / "b.jsonl", _codex_session())
    _ = _session(tmp_path / "one" / "._a.jsonl", _claude_session())

    assert main(["verify", str(tmp_path), "--workers", "2"]) == 0
    assert "2/2 exact" in capsys.readouterr().err


@pytest.mark.cli_python_subprocess
def test_workers_run_in_separate_processes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # ``--workers 2`` on two sessions must actually FAN OUT. Asserting only on
    # "2/2 exact" cannot fail on a pool that silently ran serial, which is the
    # regression ``_workers`` exists to prevent -- so the pids are counted.
    for name in ("one", "two", "three", "four"):
        _ = _session(tmp_path / name / "s.jsonl", _claude_session())

    assert main(["verify", str(tmp_path), "--workers", "4", "--format", "json"]) == 0
    report = json_loads(capsys.readouterr().err, dict[str, object])

    assert report["files"] == 4
    assert report["ok"] == 4


def test_verify_reports_the_wire_size_against_the_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Byte-exactness alone cannot catch a rewrite that silently emitted
    # nothing: an empty output compares unequal, but so does a one-byte
    # respacing, and only the SIZE distinguishes them.
    path = tmp_path / "s.jsonl"
    path.write_text(_claude_session())

    assert main(["verify", str(path), "--format", "json"]) == 0
    report = from_plain(
        from_plain(
            json_loads(capsys.readouterr().err, dict[str, object])["results"],
            list[object],
        )[0],
        dict[str, object],
    )

    assert report["source_bytes"] == path.stat().st_size
    assert report["output_bytes"] == report["source_bytes"]


def test_verify_reports_a_size_gap_on_a_shortened_rewrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "s.jsonl"
    # A key no field holds and no residual keeps would vanish on rewrite. The
    # respacing here keeps every byte's MEANING and changes only its width, so
    # the output is smaller by exactly the spaces added.
    path.write_text(
        _claude_session().replace('"parentUuid":null', '"parentUuid" : null'),
    )

    assert main(["verify", str(path), "--format", "json"]) == 1
    report = from_plain(
        from_plain(
            json_loads(capsys.readouterr().err, dict[str, object])["results"],
            list[object],
        )[0],
        dict[str, object],
    )

    assert report["byte_exact"] is False
    assert report["source_bytes"] == path.stat().st_size
    assert (
        0
        < from_plain(report["output_bytes"], int)
        < from_plain(report["source_bytes"], int)
    )


def test_detect_format_reads_each_shape(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(_codex_session())
    converted = convert_file(path, "codex", "json", False)

    assert detect_format(converted.text) == "json"
    assert detect_format(_claude_session()) == "claude"
    assert detect_format(_codex_session()) == "codex"
    assert detect_format("not json\n{}\n") == ""
    assert detect_format("\n\n" + _claude_session()) == "claude"


def test_convert_reports_unreadable_and_unknown_inputs(tmp_path: Path) -> None:
    unknown = tmp_path / "unknown.jsonl"
    unknown.write_text('{"kind":"other"}\n')

    absent = convert_file(tmp_path / "absent.jsonl", "auto", "json", False).error or ""
    assert absent
    assert (
        convert_file(unknown, "auto", "json", False).error
        == "unrecognized session format"
    )


def test_convert_reports_a_malformed_normalized_payload(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    # A tagged record whose field holds the wrong type, inside the array the
    # wire format now is: the document parses as JSON and still cannot decode.
    path.write_text(
        '[{"py/object":"trackinizer.sagent.session.types.TurnContext","context_id":"seven"}]',
    )

    malformed = convert_file(path, "json", "claude", False).error or ""
    assert malformed


def test_json_report_and_usage_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(_claude_session())

    assert main(["verify", str(path), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().err)["ok"] == 1

    _ = _session(tmp_path / "other" / "s2.jsonl", _codex_session())
    for argv in (
        ["convert", str(path)],
        ["verify", str(tmp_path / "missing")],
        # ``-o`` names ONE output, so two sessions is a usage error.
        [
            "convert",
            str(path),
            str(tmp_path / "other"),
            "--to",
            "json",
            "-o",
            str(tmp_path / "x"),
        ],
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        assert excinfo.value.code == 2


def test_fail_fast_stops_at_the_first_failure(tmp_path: Path) -> None:
    bad = tmp_path / "a_bad.jsonl"
    bad.write_text('{"kind":"other"}\n')
    good = tmp_path / "b_good.jsonl"
    good.write_text(_claude_session())

    assert main(["verify", str(bad), str(good), "--fail-fast", "-q"]) == 1


def test_a_lossy_conversion_is_refused_then_reported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Codex telemetry has no Claude representation, so this conversion drops
    # records. It must say so, and must not proceed unquestioned.
    path = tmp_path / "s.jsonl"
    path.write_text(
        _codex_session()
        + '{"type":"event_msg","payload":{"type":"token_count","info":{}}}\n',
    )
    out = tmp_path / "out.jsonl"

    with pytest.raises(SystemExit) as excinfo:
        main(["convert", str(path), "--to", "claude", "-o", str(out)])

    assert excinfo.value.code == 2
    assert "TokenUsage:1" in capsys.readouterr().err
    assert not out.exists()

    assert (
        main(["convert", str(path), "--to", "claude", "--lossy", "-o", str(out)]) == 0
    )
    assert out.exists()


def test_a_lossless_conversion_needs_no_flag(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(_codex_session())
    out = tmp_path / "out.json"

    assert main(["convert", str(path), "--to", "json", "-o", str(out)]) == 0
    assert out.exists()


def test_dropped_detects_semantic_changes_but_ignores_provider_metadata() -> None:
    records = list(claude.normalize(StringIO(_claude_session())))
    output = StringIO()
    normalized.denormalize(records, output)
    text = output.getvalue()

    changed_content = text.replace("Your entire job", "Someone else's job", 1)
    assert _dropped(records, changed_content, "json") == {"UserMessage": 1}

    changed_metadata = text.replace(
        "8fd697d5-65a4-4c94-b75c-db9b3559612f",
        "00000000-0000-0000-0000-000000000000",
        1,
    )
    assert _dropped(records, changed_metadata, "json") == {}


def test_status_and_diff_helpers(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"

    assert _status(FileResult(path=path, error="boom"), verifying=False) == "boom"
    assert (
        _status(FileResult(path=path, source="claude", target="json"), verifying=False)
        == "claude -> json"
    )
    assert (
        _status(FileResult(path=path), verifying=True)
        == "not byte-exact (0 -> 0 bytes, n/a)"
    )
    assert (
        _status(
            FileResult(path=path, source_bytes=100, output_bytes=90),
            verifying=True,
        )
        == "not byte-exact (100 -> 90 bytes, 0.9000x)"
    )
    assert (
        _status(
            FileResult(path=path, source="codex", target="claude", dropped=("A:1",)),
            verifying=False,
        )
        == "codex -> claude (drops A:1)"
    )
    assert _dropped([], "{", "json") == {}
    assert _diff("a\n", "b\n").startswith("--- original")


@pytest.mark.cli_python_subprocess
def test_module_entry_point_runs(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(_codex_session())

    completed = subprocess.run(  # noqa: S603 -- fixed argv, tmp_path input.
        [sys.executable, "-m", "trackinizer.lib.agent.sessions", "verify", str(path)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    size = path.stat().st_size
    assert (
        completed.stderr.strip() == f"1/1 exact; {size} bytes in, {size} out (1.0000x)"
    )


def _session(path: Path, text: str = "") -> Path:
    """Write one transcript at ``path``, parents included."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text or _claude_session(), encoding="utf-8")
    return path


def test_a_tree_of_sessions_is_not_one_session(tmp_path: Path) -> None:
    # The runaway: a directory whose transcripts sit BELOW it, not in it, is
    # a tree. Calling it one session fused a whole corpus into one object --
    # 1984 files, 21 GB, and one worker, since the count that sizes the pool
    # had collapsed to 1.
    _ = _session(tmp_path / "projects" / "one" / "a.jsonl")
    _ = _session(tmp_path / "projects" / "two" / "b.jsonl")

    assert len(_roots(tmp_path)) == 2


def test_a_named_session_directory_is_one_session(tmp_path: Path) -> None:
    # Pointing AT a directory says it is the session: that is what makes a
    # ``/clear`` recoverable, since the transcript claude opens to answer one
    # names nothing and is only tied to its predecessor by sitting beside it.
    project = tmp_path / "project"
    _ = _session(project / "before-clear.jsonl")
    _ = _session(project / "after-clear.jsonl")

    assert _roots(project) == [project]


def test_a_multi_file_session_is_joined_then_split_back_byte_for_byte(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The join is the risk the single-file tests cannot reach: the parts are
    # fused into ONE session, so a seam that lost or reordered a record shows
    # up only when the fused object is split back into the files it came from.
    project = tmp_path / "project"
    before_text = _claude_session()
    after_text = before_text.replace("Your entire job", "This resumed session", 1)
    assert after_text != before_text
    before = _session(project / "before-clear.jsonl", before_text)
    after = _session(project / "after-clear.jsonl", after_text)
    # Both parts carry the fixture's record stamps, so the files' write order
    # decides here. Linux stamps mtimes from a coarse clock, so two back-to-back
    # writes often share one, and the tie then sorts by name, which puts "after"
    # first. Date `before` ten seconds earlier to state the order.
    after_mtime_ns = time.time_ns()
    before_mtime_ns = after_mtime_ns - 10 * 1_000_000_000
    os.utime(before, ns=(before_mtime_ns, before_mtime_ns))
    os.utime(after, ns=(after_mtime_ns, after_mtime_ns))

    result = convert_file(project, "auto", None, False)

    assert result.byte_exact
    # ONE session, whose parts are the two files -- not two sessions.
    assert result.parts == (
        (before.name, before_text),
        (after.name, after_text),
    )
    assert result.source_bytes == before.stat().st_size + after.stat().st_size
    assert result.output_bytes == result.source_bytes

    assert main(["verify", str(project), "--format", "json"]) == 0
    report = json_loads(capsys.readouterr().err, dict[str, object])
    assert report["files"] == 1
    assert report["ok"] == 1


def test_parts_written_in_one_tick_join_in_the_order_their_records_were_stamped(
    tmp_path: Path,
) -> None:
    # A coarse filesystem clock gives two back-to-back parts one mtime, and
    # file names cannot break that tie: claude names a transcript with a fresh
    # id, so "after" sorting before "before" is as likely as not. The records
    # carry the provider's own clock, which here says "after" came a minute
    # later.
    project = tmp_path / "project"
    before_text = _claude_session()
    after_text = before_text.replace("2026-08-24T20:30:", "2026-08-24T20:31:")
    assert after_text != before_text
    before = _session(project / "before-clear.jsonl", before_text)
    after = _session(project / "after-clear.jsonl", after_text)
    tick_ns = time.time_ns()
    for part in (before, after):
        os.utime(part, ns=(tick_ns, tick_ns))

    result = convert_file(project, "auto", None, False)

    assert result.byte_exact
    assert result.parts == (
        (before.name, before_text),
        (after.name, after_text),
    )


def test_a_session_keeps_the_subagents_nested_under_it(tmp_path: Path) -> None:
    # A claude session spawns subagents into a directory named for it, so its
    # parts are recursive even though the session test is not.
    root = _session(tmp_path / "s.jsonl")
    child = _session(tmp_path / "s" / "subagents" / "agent-a1.jsonl")

    assert _session_parts(root) == [root, child]


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="POSIX signals only")
@pytest.mark.cli_python_subprocess
def test_killing_the_run_takes_its_workers_with_it(tmp_path: Path) -> None:
    # A SIGKILLed parent runs no cleanup, and the pool's workers are spawned by
    # a forkserver whose argv does not name this program -- so ``pkill -f`` on
    # the obvious pattern left 11 orphans holding 14 GB. The kernel has to be
    # the one that reaps them.
    for index in range(6):
        _ = _session(tmp_path / "in" / f"s{index}" / "s.jsonl", _claude_session() * 40)
    # A session no one ever writes: the worker that opens it blocks, so the run
    # is still alive when it is killed. Without it the run could END before the
    # loop below saw it: measured, the pool converted all six in 0.36 s and
    # exited 90 ms after its first output, and a poll whose /proc scan took
    # 62 ms on a host holding 4,000 pids missed that window 10 times in 10.
    held = tmp_path / "held.jsonl"
    os.mkfifo(held)
    started = subprocess.Popen(  # noqa: S603 -- fixed argv, tmp_path input.
        [
            sys.executable,
            "-m",
            "trackinizer.lib.agent.sessions",
            "convert",
            str(tmp_path / "in"),
            str(held),
            "--to",
            "json",
            "--out-dir",
            str(tmp_path / "out"),
            "--workers",
            "3",
            "--parent-poll-sec",
            "0.05",
        ],
    )
    children: list[psutil.Process] = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and started.poll() is None:
        children = psutil.Process(started.pid).children(recursive=True)
        # Output on disk, not just live pids: a worker whose INITIALIZER raised
        # leaves nothing behind either, so a pid check alone passes on a pool
        # that never converted anything -- which is how a missing import in
        # the death-watch went green here.
        if len(children) >= 2 and list((tmp_path / "out").glob("*.json")):
            break
        # The pool is other processes; only the process table and disk say when it works.
        time.sleep(0.1)  # house-ignore[sleep] -- See above.
    assert started.poll() is None, "the run ended before it could be killed"
    assert children, "the pool never started"
    assert list((tmp_path / "out").glob("*.json")), "the pool never did any work"

    started.kill()
    _ = started.wait(timeout=10)

    gone, alive = psutil.wait_procs(children, timeout=15)
    assert not alive, f"{len(alive)} workers outlived the run they belonged to"
    assert gone


def test_converting_to_a_directory_writes_as_it_goes(tmp_path: Path) -> None:
    # Measured: converting a 4.2 GB corpus held 3 GB and climbing, because
    # every session's converted TEXT rode home in its result and nothing
    # reached disk until the last one finished. When the destination is known
    # per session, the text belongs on disk rather than in a list.
    for name in ("one", "two", "three", "four"):
        _ = _session(tmp_path / name / "s.jsonl", _claude_session())
    out_dir = tmp_path / "out"

    assert (
        main(
            [
                "convert",
                str(tmp_path),
                "--to",
                "json",
                "--out-dir",
                str(out_dir),
                "-q",
            ],
        )
        == 0
    )

    assert len(list(out_dir.glob("*.json"))) == 4
    written = [
        convert_file(path, "auto", "json", False, destination=False)
        for path in _session_files([tmp_path / "one"])
    ]
    assert not written[0].text, "the text belongs on disk, not in the result"
    assert written[0].output_bytes > 0


@pytest.mark.compute_large_fixture
def test_converting_a_session_does_not_hold_many_copies_of_it(
    tmp_path: Path,
) -> None:
    # Measured, not assumed: a 273 MB session peaked at 4.3 GB, because the
    # source text, the parsed records, and the rewritten text were all held at
    # once. A whole corpus of them took 21 GB and thrashed the machine.
    # Prime ABC caches with one small session before measuring: their first-use
    # cost depends on the classes imported during collection (313 KiB in the
    # full suite), not on how many copies of this session the converter holds.
    warmup = _session(tmp_path / "warmup" / "s.jsonl", _claude_session())
    assert convert_file(warmup, "auto", None, False).byte_exact

    session = _session(tmp_path / "big" / "s.jsonl", _claude_session() * 50)
    size = session.stat().st_size
    tracemalloc.start()
    try:
        result = convert_file(session, "auto", None, False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.byte_exact
    assert peak < size * 8, f"{peak / size:.0f}x the session's own size"


def test_workers_scale_with_the_session_count() -> None:
    # The miss that let the runaway run serial: the pool is sized by how many
    # sessions there are, so a bug that collapses the count also silently
    # turns off parallelism.
    assert _workers(paths=8, workers=5) == 5
    assert _workers(paths=2, workers=5) == 2
    assert _workers(paths=1, workers=5) == 1


def test_empty_directory_reports_no_session_files(tmp_path: Path) -> None:
    assert (
        convert_file(tmp_path, "auto", "json", False).error == "no session files found"
    )


def test_convert_all_uses_a_process_pool_when_multiple_workers_are_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _session(tmp_path / "s.jsonl", _claude_session())
    calls: list[int] = []

    class Pool:
        def __init__(self, max_workers: int, **_: object) -> None:
            calls.append(max_workers)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def map(
            self,
            function: object,
            paths: object,
            *_args: object,
        ) -> list[FileResult]:
            del paths
            assert function is convert.convert_file
            return [convert.convert_file(path, "auto", "json", False)]

    monkeypatch.setattr(convert, "ProcessPoolExecutor", Pool)

    second = _session(tmp_path / "second.jsonl", _claude_session())
    results = convert._convert_all(
        (path, second),
        workers=2,
        source="auto",
        target="json",
        want_diff=False,
        fail_fast=False,
    )

    assert calls == [2]
    assert results[0].ok


def _no_sleep(seconds: float) -> None:
    del seconds


def _gone_kill(pid: int, signal: int) -> None:
    del pid, signal
    raise OSError("gone")


def _gone_exit(code: int) -> Never:
    del code
    raise RuntimeError("gone")


def test_parent_watcher_exits_when_launcher_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", _no_sleep)
    monkeypatch.setattr(os, "kill", _gone_kill)
    monkeypatch.setattr(os, "_exit", _gone_exit)

    with pytest.raises(RuntimeError, match=r"gone"):
        convert._watch_launcher(123, 0.0)


def test_die_with_parent_starts_a_daemon_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[tuple[object, tuple[object, ...], bool]] = []

    class Thread:
        def __init__(
            self,
            *,
            target: object,
            args: tuple[object, ...],
            daemon: bool,
        ) -> None:
            started.append((target, args, daemon))

        def start(self) -> None:
            return None

    monkeypatch.setattr(threading, "Thread", Thread)
    convert._die_with_parent(7, 0.5)

    assert started == [(convert._watch_launcher, (7, 0.5), True)]


def test_detect_format_recognizes_sagent_records() -> None:
    assert convert.detect_format('{"kind":"meta"}\n') == "sagent"


def test_a_known_but_unconvertible_format_is_refused_by_name(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text('{"kind":"meta"}\n')

    assert convert_file(path, "auto", "json", False).error == (
        "unsupported session format: sagent"
    )


def test_each_format_names_its_own_adapter() -> None:
    assert convert._adapter("claude") is claude
    assert convert._adapter("codex") is codex
    assert convert._adapter("gemini") is gemini
    assert convert._adapter("json") is normalized


def test_an_empty_normalized_document_is_readable(tmp_path: Path) -> None:
    # ``normalized.denormalize([])`` writes ``[]``, and detection raised on it.
    path = tmp_path / "empty.json"
    with path.open("w", encoding="utf-8") as handle:
        normalized.denormalize([], handle)

    assert detect_format(path.read_text()) == "json"
    assert convert_file(path, "auto", None, False).ok


def test_a_line_that_is_not_an_object_names_no_format() -> None:
    assert detect_format("[1]\n") == ""
    assert detect_format("3\n") == ""


def test_a_long_pretty_printed_gemini_document_is_sniffed(tmp_path: Path) -> None:
    path = tmp_path / "g.json"
    messages = [{"type": "user", "content": f"m{i}"} for i in range(80)]
    path.write_text(json.dumps({"sessionId": "s", "messages": messages}, indent=2))

    assert convert._sniff(path) == "gemini"


def test_convert_file_reports_conversion_type_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _session(tmp_path / "s.jsonl", _claude_session())

    def raise_type_error(*_args: object, **_kwargs: object) -> FileResult:
        raise TypeError("bad")

    monkeypatch.setattr(convert, "_compared", raise_type_error)

    result = convert.convert_file(path, "auto", "json", False)

    assert (result.path, result.error) == (path, "TypeError: bad")


def test_destination_uses_a_hash_when_all_parent_names_collide(tmp_path: Path) -> None:
    path = Path("session.jsonl")
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "session.json").write_text("")

    assert convert._destination(destination, path, ".json").name.startswith("session-")


def test_write_out_dir_writes_named_parts(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = FileResult(
        path=tmp_path / "source.json",
        target="claude",
        parts=(("part.jsonl", "native"),),
    )

    convert._write([result], output=None, out_dir=out, stream=StringIO())

    assert (out / "part.jsonl").read_text() == "native"


def test_matches_rejects_same_sized_different_text(tmp_path: Path) -> None:
    path = tmp_path / "x"
    path.write_text("abc")

    assert convert._matches(path, "and") is False


@pytest.mark.parametrize("edit", [False, True], ids=["same", "same-size-edit"])
def test_a_streamed_json_rewrite_is_exact_by_content(
    tmp_path: Path,
    *,
    edit: bool,
) -> None:
    # Equal sizes are not equal bytes: a respelling that kept the length -- one
    # separator moved -- still reported byte-exact.
    source = tmp_path / "in" / "s.json"
    source.parent.mkdir()
    with source.open("w", encoding="utf-8") as handle:
        normalized.denormalize(claude.normalize(StringIO(_claude_session())), handle)
    if edit:
        text = source.read_text()
        source.write_text("[ " + text[1:].replace(',"', ',"', 1).replace("]\n", "]", 1))

    result = convert_file(source, "auto", "json", False, destination=tmp_path / "out")

    assert result.byte_exact is (not edit)
    assert result == FileResult(
        path=source,
        source="json",
        target="json",
        byte_exact=not edit,
        source_bytes=source.stat().st_size,
        output_bytes=(tmp_path / "out" / "s.json").stat().st_size,
    )


def test_a_streamed_rewrite_from_another_format_is_not_exact_and_makes_its_dir(
    tmp_path: Path,
) -> None:
    source = _session(tmp_path / "s.jsonl", _claude_session())
    out_dir = tmp_path / "deep" / "out"

    result = convert_file(source, "auto", "json", False, destination=out_dir)

    assert result.byte_exact is False
    assert (out_dir / "s.json").exists()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["verify", "{tmp}/missing"], "no session files found"),
        (["convert", "{session}"], "convert requires --to"),
        (
            ["convert", "{session}", "{other}", "--to", "json", "-o", "{tmp}/x"],
            "--output takes a single input path",
        ),
        (
            ["verify", "{session}", "--out-dir", "{tmp}/o"],
            "verify writes nothing; --output and --out-dir need convert",
        ),
    ],
)
def test_each_usage_error_names_its_fault(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    session = _session(tmp_path / "s.jsonl")
    other = _session(tmp_path / "other" / "s2.jsonl")
    filled = [
        arg.format(tmp=tmp_path, session=session, other=other.parent) for arg in argv
    ]

    with pytest.raises(SystemExit):
        main(filled)

    assert capsys.readouterr().err.splitlines()[-1].endswith(f"error: {message}")


def test_a_lossy_refusal_names_every_drop_of_every_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _session(tmp_path / "s.jsonl")

    def lossy(*args: object, **flags: object) -> list[FileResult]:
        del args, flags
        return [FileResult(path=path, target="claude", dropped=("A:1", "B:2"))]

    monkeypatch.setattr(convert, "_convert_all", lossy)

    with pytest.raises(SystemExit):
        main(["convert", str(path), "--to", "claude"])

    err = capsys.readouterr().err.splitlines()
    assert err[0] == f"{path}: drops A:1, B:2"
    assert err[-1].endswith(
        "error: conversion drops records; pass --lossy to accept",
    )


def test_main_passes_every_flag_to_the_run_and_the_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _session(tmp_path / "s.jsonl")
    runs: list[dict[str, object]] = []
    reports: list[dict[str, object]] = []

    def run(paths: tuple[Path, ...], **flags: object) -> list[FileResult]:
        runs.append({"paths": paths, **flags})
        return [
            FileResult(path=path, source="claude", target="claude", byte_exact=True),
        ]

    def report(results: object, **flags: object) -> None:
        del results
        reports.append(flags)

    monkeypatch.setattr(convert, "_convert_all", run)
    monkeypatch.setattr(convert, "_report", report)
    out_dir = tmp_path / "out"

    assert (
        main(
            [
                "convert",
                str(path),
                "--to",
                "claude",
                "--source",
                "claude",
                "-w",
                "3",
                "--fail-fast",
                "--diff",
                "--parent-poll-sec",
                "0.25",
                "--out-dir",
                str(out_dir),
                "-q",
                "-v",
            ],
        )
        == 0
    )
    assert main(["verify", str(path)]) == 0

    assert runs == [
        {
            "paths": (path,),
            "workers": 3,
            "source": "claude",
            "target": "claude",
            "want_diff": True,
            "fail_fast": True,
            "destination": out_dir,
            "parent_poll_sec": 0.25,
        },
        {
            "paths": (path,),
            "workers": 1,
            "source": "auto",
            "target": None,
            "want_diff": False,
            "fail_fast": False,
            "destination": False,
            "parent_poll_sec": 1.0,
        },
    ]
    assert reports == [
        {"output_format": "text", "quiet": True, "verbose": True, "verifying": False},
        {"output_format": "text", "quiet": False, "verbose": False, "verifying": True},
    ]


def test_the_help_states_the_module_and_each_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])

    out = capsys.readouterr().out
    assert "Convert agent sessions between native formats" in out
    assert "Write to this file." in out


def test_the_help_has_no_description_when_docstrings_are_stripped(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # As ``python -OO`` runs it.
    monkeypatch.setattr(convert, "__doc__", None)

    with pytest.raises(SystemExit):
        main(["--help"])

    # Usage, then straight to the arguments.
    after_usage = capsys.readouterr().out.split("\n\n")[1]
    assert after_usage.startswith(("positional arguments:", "options:"))


@pytest.mark.parametrize(
    "argv",
    [
        ["bogus", "p"],
        ["convert", "p", "--to", "bogus"],
        ["convert", "p", "--source", "bogus"],
        ["convert", "p", "--format", "bogus"],
    ],
)
def test_a_choice_outside_its_list_is_refused(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    convert._add_arguments(parser, formats=("claude", "json"))

    with pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_each_flag_has_its_default_and_its_type() -> None:
    parser = argparse.ArgumentParser()
    convert._add_arguments(parser, formats=("claude", "json"))

    assert vars(parser.parse_args(["convert", "p"])) == {
        "command": "convert",
        "paths": [Path("p")],
        "to": None,
        "source": "auto",
        "output": None,
        "out_dir": None,
        "workers": 1,
        "parent_poll_sec": 1.0,
        "format": "text",
        "lossy": False,
        "fail_fast": False,
        "diff": False,
        "quiet": False,
        "verbose": False,
    }
    flags = vars(
        parser.parse_args(
            [
                "verify",
                "p",
                "-w",
                "3",
                "--parent-poll-sec",
                "0.5",
                "--format",
                "json",
                "--source",
                "claude",
                "--to",
                "json",
            ],
        ),
    )
    assert {key: flags[key] for key in ("workers", "parent_poll_sec")} == {
        "workers": 3,
        "parent_poll_sec": 0.5,
    }
    assert (flags["format"], flags["source"], flags["to"]) == ("json", "claude", "json")
    assert vars(parser.parse_args(["convert", "p", "--format", "text"]))["format"] == (
        "text"
    )
    assert vars(parser.parse_args(["convert", "p", "--source", "auto"]))["source"] == (
        "auto"
    )


class _Pool:
    """A process pool that runs ``map`` here and records what it was given."""

    made: ClassVar[list[dict[str, object]]] = []

    def __init__(self, **options: object) -> None:
        self.made.append(options)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def map(
        self,
        function: Callable[..., FileResult],
        *columns: Iterable[object],
    ) -> list[FileResult]:
        assert function is convert.convert_file
        # ``repeat`` columns never end; the paths decide how many rows there are.
        return [function(*row) for row in zip(*columns, strict=False)]


def test_a_pool_run_hands_each_worker_the_watch_and_every_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = _session(tmp_path / "a" / "s.jsonl")
    bad = tmp_path / "b.jsonl"
    bad.write_text('{"kind":"other"}\n')
    respaced = _session(
        tmp_path / "c" / "s.jsonl",
        _claude_session().replace('"parentUuid":null', '"parentUuid" : null'),
    )
    _Pool.made = []
    monkeypatch.setattr(convert, "ProcessPoolExecutor", _Pool)

    results = convert._convert_all(
        (good, bad, good),
        workers=2,
        source="auto",
        target="json",
        want_diff=False,
        fail_fast=True,
        parent_poll_sec=0.25,
    )
    named = convert._convert_all(
        (respaced, respaced),
        workers=2,
        source="claude",
        target=None,
        want_diff=True,
        fail_fast=False,
        destination=False,
    )

    assert _Pool.made == [
        {
            "max_workers": 2,
            "initializer": convert._die_with_parent,
            "initargs": (os.getpid(), 0.25),
        },
        {
            "max_workers": 2,
            "initializer": convert._die_with_parent,
            "initargs": (os.getpid(), 1.0),
        },
    ]
    # Fail-fast: the first failure is the last result.
    assert [(r.path, r.target) for r in results] == [(good, "json"), (bad, None)]
    # Each worker read as named, kept no text, and made the asked-for diff.
    assert [
        (r.path, r.source, r.target, r.text, r.diff.startswith("--- original"))
        for r in named
    ] == [(respaced, "claude", "claude", "", True)] * 2


def test_one_worker_runs_here_and_stops_at_a_failure_when_asked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = _session(tmp_path / "a" / "s.jsonl")
    bad = tmp_path / "b.jsonl"
    bad.write_text('{"kind":"other"}\n')

    def no_pool(**_: object) -> Never:
        raise AssertionError("one worker needs no pool")

    monkeypatch.setattr(convert, "ProcessPoolExecutor", no_pool)

    def run(*, fail_fast: bool) -> list[Path]:
        return [
            r.path
            for r in convert._convert_all(
                (bad, good),
                workers=1,
                source="auto",
                target=None,
                want_diff=False,
                fail_fast=fail_fast,
            )
        ]

    assert run(fail_fast=True) == [bad]
    assert run(fail_fast=False) == [bad, good]


def test_collect_stops_only_on_a_failure_and_only_when_asked(tmp_path: Path) -> None:
    ok = FileResult(path=tmp_path / "ok")
    failed = FileResult(path=tmp_path / "bad", error="boom")

    assert convert._collect([ok, failed, ok], fail_fast=False) == [ok, failed, ok]
    assert convert._collect([ok, failed, ok], fail_fast=True) == [ok, failed]
    assert convert._collect([ok, ok], fail_fast=True) == [ok, ok]


@pytest.mark.parametrize(("cpus", "running"), [(None, 1), (4, 4)])
def test_zero_workers_uses_every_cpu_there_is(
    monkeypatch: pytest.MonkeyPatch,
    cpus: int | None,
    running: int,
) -> None:
    def cpu_count() -> int | None:
        return cpus

    monkeypatch.setattr(os, "cpu_count", cpu_count)

    assert _workers(paths=8, workers=0) == running


def test_the_watch_sleeps_its_poll_then_probes_the_launcher_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def sleep(seconds: float) -> None:
        calls.append(("sleep", seconds))

    def kill(pid: int, sig: int) -> None:
        calls.append(("kill", (pid, sig)))
        raise OSError("gone")

    def exit_(code: int) -> Never:
        calls.append(("exit", code))
        raise RuntimeError("gone")

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "_exit", exit_)

    with pytest.raises(RuntimeError, match="gone"):
        convert._watch_launcher(123, 0.25)

    assert calls == [("sleep", 0.25), ("kill", (123, 0)), ("exit", 1)]


def test_a_destination_takes_as_many_parent_names_as_it_needs(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    path = Path("/a/b/session.jsonl")

    assert convert._destination(out, path, ".json") == out / "session.json"
    (out / "session.json").write_text("")
    assert convert._destination(out, path, ".json") == out / "b-session.json"
    (out / "b-session.json").write_text("")
    assert convert._destination(out, path, ".json") == out / "a-b-session.json"
    (out / "a-b-session.json").write_text("")
    # The root's own part is ``/``, which a file name cannot hold.
    assert convert._destination(out, path, ".json") == out / "--a-b-session.json"


def test_sessions_named_alike_with_no_parents_left_get_names_of_their_own(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "session.json").write_text("")

    first = convert._destination(out, Path("session.jsonl"), ".json")
    second = convert._destination(out, Path("session.json"), ".json")

    assert first == out / f"session-{abs(hash('session.jsonl'))}.json"
    assert first != second


def test_a_diff_is_the_changed_lines_alone_each_cut_at_200() -> None:
    native = "".join(f"line {i}\n" for i in range(20))
    rebuilt = native.replace("line 3\n", "x" * 300 + "\n")
    rebuilt = "".join(
        "changed\n" if line.startswith("line 1") else line
        for line in rebuilt.splitlines(keepends=True)
    )

    lines = _diff(native, rebuilt).split("\n")

    assert lines[:5] == [
        "--- original",
        "+++ converted",
        "@@ -2 +2 @@",
        "-line 1",
        "+changed",
    ]
    assert lines[5:8] == ["@@ -4 +4 @@", "-line 3", "+" + "x" * 199]
    assert len(lines) == 12


def test_two_records_of_one_kind_lost_count_as_two() -> None:
    records = [UserMessage(content="one"), UserMessage(content="two")]

    assert _dropped(records, "[]", "json") == {"UserMessage": 2}


def test_a_context_index_and_the_extra_are_not_what_a_record_means() -> None:
    same = convert._semantic_record(UserMessage(content="x", context_id=None))

    assert convert._semantic_record(UserMessage(content="x", context_id=3)) == same
    assert convert._semantic_record(UserMessage(content="x", extra={"k": 1})) == same


def test_a_nested_value_is_part_of_what_a_record_means() -> None:
    def meaning(record: SessionRecord) -> tuple[str, str]:
        return convert._semantic_record(record)

    assert meaning(ToolCall(call_id="c", name="n", arguments={"a": 1})) != meaning(
        ToolCall(call_id="c", name="n", arguments={"a": 2}),
    )
    assert meaning(TurnContext(tools=("a",))) != meaning(TurnContext(tools=("b",)))


@pytest.mark.parametrize(
    ("head", "found"),
    [
        # Whitespace before the array is still the normalized form.
        ("  \n[]", "json"),
        # Read only in its head: a tag past the first 200 characters is unseen.
        ("[" + " " * 188 + '{"py/object":"x"}]', ""),
        ("[" + " " * 187 + '{"py/object":"x"}]', "json"),
        # Both keys, or no gemini document.
        ('{\n  "sessionId": "s"\n}', ""),
        ('{"sessionId":"s","messages":[]}', "gemini"),
        ('{"sessionId":"s","messages":[],"kind":"x"}', "gemini"),
        ('{"sessionId":"s"}', "claude"),
        # A line that is no JSON object is passed over, not the end of the walk.
        ('not json\n{"uuid":"u"}\n', "claude"),
        ('[1]\n{"uuid":"u"}\n', "claude"),
        ('{"uuid":"u"}\n', "claude"),
        ('{"parentUuid":null}\n', "claude"),
        ('{"agentId":"a"}\n', "claude"),
    ],
)
def test_each_head_names_the_format_that_wrote_it(head: str, found: str) -> None:
    assert detect_format(head) == found


def test_a_gemini_document_is_an_object_holding_both_keys() -> None:
    assert convert._is_gemini_document('{"sessionId":"s","messages":[]}')
    assert not convert._is_gemini_document('{"sessionId":"s"}')
    assert not convert._is_gemini_document('[{"sessionId":"s","messages":[]}]')
    assert not convert._is_gemini_document("{")


def test_matches_compares_the_raw_bytes(tmp_path: Path) -> None:
    path = tmp_path / "x"
    path.write_bytes(b"a\r\nb\r\n")

    assert convert._matches(path, "a\r\nb\r\n")
    assert not convert._matches(path, "a\nb\n\n\n")


def test_the_sniff_reads_64_lines_and_names_no_format_for_an_unreadable_file(
    tmp_path: Path,
) -> None:
    def head(skipped: int) -> Path:
        path = tmp_path / f"{skipped}.jsonl"
        path.write_text("x\n" * skipped + '{"uuid":"u"}\n')
        return path

    assert convert._sniff(head(63)) == "claude"
    assert convert._sniff(head(64)) == ""
    assert convert._sniff(tmp_path / "absent") == ""


def test_a_walk_finds_sessions_and_nothing_else(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    session = _session(tree / "p" / "s.jsonl")
    subagent = _session(tree / "p" / "s" / "subagents" / "a.jsonl")
    second = _session(tree / "p" / "s" / "subagents" / "b.jsonl")
    unknown = tree / "q" / "u.jsonl"
    unknown.parent.mkdir()
    unknown.write_text('{"kind":"other"}\n')
    gemini_doc = tree / "g" / "g.json"
    gemini_doc.parent.mkdir()
    gemini_doc.write_text('{"sessionId":"s","messages":[]}')
    (tree / "g" / "sessions-index.json").write_text("{}")
    (tree / "g" / "._g.json").write_text('{"sessionId":"s","messages":[]}')
    (tree / "q" / "._u.jsonl").write_text('{"uuid":"u"}\n')
    (tree / "q" / "dir.jsonl").mkdir()
    (tree / "q" / "dir.json").mkdir()

    assert _roots(tree) == sorted([session, unknown, gemini_doc])
    assert subagent not in _roots(tree)
    assert second not in _roots(tree)


def test_a_directory_whose_only_transcripts_are_no_files_is_no_session(
    tmp_path: Path,
) -> None:
    (tmp_path / "d.jsonl").mkdir()
    (tmp_path / "._a.jsonl").write_text('{"uuid":"u"}\n')

    assert _roots(tmp_path) == []
    assert convert._beneath(tmp_path) == []


def test_a_session_named_twice_is_one_session(tmp_path: Path) -> None:
    path = _session(tmp_path / "s.jsonl")

    assert list(_session_files([path, path])) == [path]


def test_a_part_with_no_stamp_opens_at_the_empty_stamp() -> None:
    assert convert._opened([]) == ""
    assert convert._opened([UserMessage(content="x")]) == ""
    assert convert._opened([UserMessage(timestamp="t")]) == "t"


def test_the_json_report_states_every_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exact = FileResult(
        path=tmp_path / "a",
        source="claude",
        target="claude",
        byte_exact=True,
        source_bytes=3,
        output_bytes=3,
    )
    inexact = FileResult(
        path=tmp_path / "b",
        source="codex",
        target="claude",
        dropped=("A:1",),
    )

    convert._report(
        [exact, inexact],
        output_format="json",
        quiet=False,
        verbose=False,
        verifying=True,
    )

    assert json.loads(capsys.readouterr().err) == {
        "files": 2,
        "ok": 1,
        "results": [
            {
                "path": str(tmp_path / "a"),
                "source": "claude",
                "target": "claude",
                "byte_exact": True,
                "source_bytes": 3,
                "output_bytes": 3,
                "dropped": [],
                "error": None,
            },
            {
                "path": str(tmp_path / "b"),
                "source": "codex",
                "target": "claude",
                "byte_exact": False,
                "source_bytes": 0,
                "output_bytes": 0,
                "dropped": ["A:1"],
                "error": None,
            },
        ],
    }


def test_the_text_report_names_each_shortfall_then_sums_the_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exact = FileResult(
        path=tmp_path / "a",
        source="claude",
        target="claude",
        byte_exact=True,
        source_bytes=4,
        output_bytes=4,
    )
    inexact = FileResult(
        path=tmp_path / "b",
        source="claude",
        target="claude",
        source_bytes=4,
        output_bytes=2,
        diff="--- original",
    )

    convert._report(
        [exact, inexact],
        output_format="text",
        quiet=False,
        verbose=False,
        verifying=True,
    )
    convert._report(
        [exact, inexact],
        output_format="text",
        quiet=False,
        verbose=False,
        verifying=False,
    )
    convert._report(
        [exact],
        output_format="text",
        quiet=True,
        verbose=True,
        verifying=True,
    )

    assert capsys.readouterr().err.splitlines() == [
        f"{tmp_path / 'b'}: not byte-exact (4 -> 2 bytes, 0.5000x)",
        "--- original",
        "1/2 exact; 8 bytes in, 6 out (0.7500x)",
        "2/2 converted; 8 bytes in, 6 out (0.7500x)",
        f"{tmp_path / 'a'}: claude -> claude",
    ]


def test_a_status_names_every_drop() -> None:
    result = FileResult(
        path=Path("s"),
        source="codex",
        target="claude",
        dropped=("A:1", "B:2"),
    )

    assert _status(result, verifying=False) == "codex -> claude (drops A:1, B:2)"


def test_write_skips_failures_and_streamed_json_and_never_overwrites(
    tmp_path: Path,
) -> None:
    out = tmp_path / "deep" / "out"
    streamed = FileResult(path=tmp_path / "j.json", target="json")
    failed = FileResult(
        path=tmp_path / "f.jsonl",
        target="claude",
        parts=(("f.jsonl", "F"),),
        error="boom",
    )
    first = FileResult(
        path=tmp_path / "a" / "s.jsonl",
        target="claude",
        parts=(("s.jsonl", "first"),),
    )
    second = FileResult(
        path=tmp_path / "b" / "s.jsonl",
        target="claude",
        parts=(("s.jsonl", "second"),),
    )

    convert._write(
        [streamed, failed, first, second],
        output=None,
        out_dir=out,
        stream=StringIO(),
    )

    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*")) == [
        "s",
        "s.jsonl",
        "s/s.jsonl",
    ]
    assert (out / "s" / "s.jsonl").read_text() == "second"


def test_write_sends_one_conversion_to_stdout_and_none_of_several(
    tmp_path: Path,
) -> None:
    one = FileResult(path=tmp_path / "a", source="claude", target="json", text="A")
    same = FileResult(path=tmp_path / "b", source="json", target="json", text="B")
    failed = FileResult(path=tmp_path / "c", target="json", text="C", error="boom")

    def written(results: list[FileResult]) -> str:
        stream = StringIO()
        convert._write(results, output=None, out_dir=None, stream=stream)
        return stream.getvalue()

    assert written([one]) == "A"
    assert written([one, one]) == ""
    assert written([same]) == ""
    assert written([failed, one]) == "A"


def test_convert_file_names_its_path_and_the_fault_on_every_failure(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "absent.jsonl"
    empty = tmp_path / "empty"
    empty.mkdir()
    binary = tmp_path / "binary.jsonl"
    binary.write_bytes(b"\xff\xfe")

    missing = convert_file(absent, "auto", "json", False)
    undecodable = convert_file(binary, "auto", "json", False)
    nothing = convert_file(empty, "auto", "json", False)

    assert (missing.path, (missing.error or "").split(":")[0]) == (
        absent,
        "FileNotFoundError",
    )
    assert (undecodable.path, (undecodable.error or "").split(":")[0]) == (
        binary,
        "UnicodeDecodeError",
    )
    assert (nothing.path, nothing.error) == (empty, "no session files found")


def test_a_named_source_format_is_read_as_that_format(tmp_path: Path) -> None:
    path = _session(tmp_path / "s.jsonl", _claude_session())

    result = convert_file(path, "codex", None, False)

    assert result.source == "codex"


def test_codex_parts_join_in_the_order_each_forked_from_the_other(
    tmp_path: Path,
) -> None:
    # The fork was started first by the clock, but it continues its parent.
    session = tmp_path / "session"
    session.mkdir()
    parent = session / "parent.jsonl"
    child = session / "child.jsonl"
    parent.write_text(
        '{"timestamp":"2026-01-01T00:00:10Z","type":"session_meta",'
        '"payload":{"id":"p"}}\n',
    )
    child.write_text(
        '{"timestamp":"2026-01-01T00:00:05Z","type":"session_meta",'
        '"payload":{"id":"c","forked_from_id":"p"}}\n',
    )

    result = convert_file(session, "auto", None, False)

    assert result.byte_exact
    assert [name for name, _ in result.parts] == ["parent.jsonl", "child.jsonl"]

    # The same rollouts in the normalized form still name the thread each forked
    # from, so they join in that order too.
    for part in (parent, child):
        records = list(codex.normalize(StringIO(part.read_text())))
        with part.open("w", encoding="utf-8") as handle:
            normalized.denormalize(records, handle)

    from_json = convert_file(session, "auto", "codex", False)

    assert [name for name, _ in from_json.parts] == ["parent.jsonl", "child.jsonl"]


def _two_part_session(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write a claude session of two parts, oldest first; return it and its parts."""
    project = tmp_path / "project"
    before = _session(project / "before.jsonl", _claude_session())
    after = _session(
        project / "after.jsonl",
        _claude_session().replace("Your entire job", "This resumed session", 1),
    )
    now = time.time_ns()
    os.utime(before, ns=(now - 10**10, now - 10**10))
    os.utime(after, ns=(now, now))
    return project, before, after


def test_a_verified_multi_part_session_sums_its_parts(tmp_path: Path) -> None:
    project, before, after = _two_part_session(tmp_path)

    result = convert_file(project, "auto", None, False, destination=False)

    assert result.byte_exact
    assert result.text == ""
    assert result.diff == ""
    assert result.parts == ((before.name, ""), (after.name, ""))
    assert result.output_bytes == result.source_bytes


def test_a_converted_multi_part_session_keeps_each_part_and_the_whole(
    tmp_path: Path,
) -> None:
    project, before, after = _two_part_session(tmp_path)

    native = convert_file(project, "auto", None, False)
    as_json = convert_file(project, "auto", "json", False)

    assert native.text == before.read_text() + after.read_text()
    records = list(normalized.normalize(StringIO(as_json.text)))
    assert len(fuse.part_names(records)) == 2
    assert as_json.parts == ()
    assert as_json.output_bytes == len(as_json.text.encode())


def test_a_single_part_keeps_its_file_name(tmp_path: Path) -> None:
    path = _session(tmp_path / "s.jsonl")

    result = convert_file(path, "auto", None, False)
    verified = convert_file(path, "auto", None, False, destination=False)

    assert [name for name, _ in result.parts] == ["s.jsonl"]
    assert verified.parts == (("s.jsonl", ""),)


def test_no_diff_is_made_unless_asked(tmp_path: Path) -> None:
    path = _session(
        tmp_path / "s.jsonl",
        _claude_session().replace('"parentUuid":null', '"parentUuid" : null'),
    )

    assert convert_file(path, "auto", None, False).diff == ""
    assert convert_file(path, "auto", None, True).diff.startswith("--- original")


def test_a_fused_document_splits_into_parts_beyond_the_one_file_it_came_in(
    tmp_path: Path,
) -> None:
    records = list(claude.normalize(StringIO(_claude_session())))
    source = tmp_path / "s.json"
    with source.open("w", encoding="utf-8") as handle:
        normalized.denormalize(list(fuse.fuse([records, records])), handle)

    converted = convert_file(source, "auto", "claude", False)
    verified = convert_file(source, "auto", None, False, destination=False)

    assert [name for name, _ in converted.parts] == ["s.json", ""]
    assert verified.ok


def test_lost_records_are_summed_across_the_parts(tmp_path: Path) -> None:
    session = tmp_path / "session"
    lossy = (
        _codex_session()
        + '{"type":"event_msg","payload":{"type":"token_count","info":{}}}\n'
    )
    for name in ("a.jsonl", "b.jsonl"):
        _ = _session(session / name, lossy)

    one = convert_file(session / "a.jsonl", "auto", "claude", False).dropped
    both = convert_file(session, "auto", "claude", False).dropped

    assert "TokenUsage:1" in one
    assert "TokenUsage:2" in both


def test_a_session_is_read_and_written_as_utf8_whatever_the_locale(
    tmp_path: Path,
) -> None:
    """A transcript is UTF-8 on every host; the locale's own codec never applies."""
    native = _claude_session().replace("Your entire job", "Ťhe whole job", 1)
    path = _session(tmp_path / "s.jsonl", native)
    out = tmp_path / "out.jsonl"
    native_dir = tmp_path / "native"
    json_dir = tmp_path / "json"
    args = ["convert", str(path), "--to", "claude", "--out-dir", str(native_dir), "-q"]

    with ascii_locale():
        assert convert_file(path, "auto", None, False, destination=False).byte_exact
        assert convert_file(path, "auto", None, False).byte_exact
        assert convert._sniff(path) == "claude"
        assert main(["convert", str(path), "--to", "claude", "-o", str(out), "-q"]) == 0
        assert main(args) == 0
        as_json = convert_file(path, "auto", "json", False, destination=json_dir)
        back = convert_file(
            json_dir / "s.json",
            "auto",
            "json",
            False,
            destination=json_dir,
        )

    assert out.read_bytes() == path.read_bytes()
    assert (native_dir / "s.jsonl").read_bytes() == path.read_bytes()
    assert as_json.ok
    assert back.byte_exact


# A torn line is kept as the bytes it was, so its ``\r\n`` is part of the session; a
# reader or a comparison that translated it would call the rewrite inexact.
def test_a_torn_crlf_line_rewrites_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes((_claude_session() + "{torn\r\n").encode())

    assert convert_file(path, "auto", None, False, destination=False).byte_exact
    assert convert_file(path, "auto", None, False).byte_exact


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
