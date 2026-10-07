"""Tests for the session converter."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Final, Never, Self, cast

import functools
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tracemalloc

import psutil
import pytest

from trackinizer.lib.agent.sessions import claude, convert, fuse, normalized
from trackinizer.lib.agent.sessions.convert import (
    FileResult,
    _diff,
    _dropped,
    _parts_of,
    _roots,
    _session_files,
    _status,
    _workers,
    convert_file,
    detect_format,
    main,
)
from trackinizer.lib.codec import (
    from_plain,
    from_plain as convert_json,
    loads,
)


_CWD: Final = Path(__file__).resolve().parent


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
        record = from_plain(loads(line), dict[str, object])
        started = started or record.get("type") == "turn_context"
        if not started:
            continue
        kept.append(line)
        payload = convert_json(record.get("payload"), dict[str, object], default={})
        if convert_json(payload.get("type"), str, default="").endswith(
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
    document = from_plain(loads(capsys.readouterr().out), list[dict[str, object]])
    assert convert_json(document[0].get("py/object"), str, default="").endswith(
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
    report = from_plain(loads(capsys.readouterr().err), dict[str, object])

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
    report = convert_json(
        convert_json(
            from_plain(loads(capsys.readouterr().err), dict[str, object])["results"],
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
    report = convert_json(
        convert_json(
            from_plain(loads(capsys.readouterr().err), dict[str, object])["results"],
            list[object],
        )[0],
        dict[str, object],
    )

    assert report["byte_exact"] is False
    assert report["source_bytes"] == path.stat().st_size
    assert (
        0
        < convert_json(report["output_bytes"], int)
        < convert_json(report["source_bytes"], int)
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

    assert convert_file(tmp_path / "absent.jsonl", "auto", "json", False).error
    assert (
        convert_file(unknown, "auto", "json", False).error
        == "unrecognized session format"
    )


def test_convert_reports_a_malformed_normalized_payload(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    # A tagged record whose field holds the wrong type, inside the array the
    # wire format now is: the document parses as JSON and still cannot decode.
    path.write_text(
        '[{"py/object":"trackinizer.lib.agent.types.sessions.TurnContext",'
        '"context_id":"seven"}]',
    )

    assert convert_file(path, "json", "claude", False).error


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
    assert _dropped(records, changed_content, "json") == ("UserMessage:1",)

    changed_metadata = text.replace(
        "8fd697d5-65a4-4c94-b75c-db9b3559612f",
        "00000000-0000-0000-0000-000000000000",
        1,
    )
    assert _dropped(records, changed_metadata, "json") == ()


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
    assert _dropped([], "{", "json") == ()
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
    report = from_plain(loads(capsys.readouterr().err), dict[str, object])
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

    assert _parts_of(root) == [root, child]


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="POSIX signals only")
@pytest.mark.cli_python_subprocess
def test_killing_the_run_takes_its_workers_with_it(tmp_path: Path) -> None:
    # A SIGKILLed parent runs no cleanup, and the pool's workers are spawned by
    # a forkserver whose argv does not name this program -- so ``pkill -f`` on
    # the obvious pattern left 11 orphans holding 14 GB. The kernel has to be
    # the one that reaps them.
    for index in range(6):
        _ = _session(tmp_path / f"s{index}" / "s.jsonl", _claude_session() * 40)
    started = subprocess.Popen(  # noqa: S603 -- fixed argv, tmp_path input.
        [
            sys.executable,
            "-m",
            "trackinizer.lib.agent.sessions",
            "convert",
            str(tmp_path),
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
    while time.monotonic() < deadline:
        children = cast(  # pyright: ignore[reportUnnecessaryCast] -- ty needs it; pyright resolves the stub.
            "list[psutil.Process]",
            psutil.Process(started.pid).children(recursive=True),
        )
        # Output on disk, not just live pids: a worker whose INITIALIZER raised
        # leaves nothing behind either, so a pid check alone passes on a pool
        # that never converted anything -- which is how a missing import in
        # the death-watch went green here.
        if len(children) >= 2 and list((tmp_path / "out").glob("*.json")):
            break
        time.sleep(0.1)
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

    assert result.error == "TypeError: bad"


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

    assert result.byte_exact is not edit


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
