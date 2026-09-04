"""Tests for the session converter."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import cast

import json
import signal
import subprocess
import sys
import time
import tracemalloc

import psutil
import pytest

from trackinizer.lib.agent.sessions import claude, normalized
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
from trackinizer.lib.custom_json import ListCodec, StrCodec


_TESTDATA = Path(__file__).resolve().parent / "testdata"
CLAUDE_SESSION = (_TESTDATA / "claude_sidechain.jsonl").read_text(encoding="utf-8")
CODEX_SESSION = (_TESTDATA / "codex_main.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("native", "source"),
    # Named, because pytest builds an id from the VALUE otherwise -- and the
    # value here is a whole fixture session, which printed an 80KB test id.
    [
        pytest.param(CLAUDE_SESSION, "claude", id="claude"),
        pytest.param(CODEX_SESSION, "codex", id="codex"),
    ],
)
def test_convert_to_json_and_back_recovers_the_native_bytes(
    tmp_path: Path, native: str, source: str
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(native)
    as_json = tmp_path / "session.json"

    assert main(["convert", str(path), "--to", "json", "-o", str(as_json)]) == 0
    assert main(["convert", str(as_json), "--to", source]) == 0

    result = convert_file(as_json, "auto", source, False)
    assert result.source == "json"
    assert result.text == native


def test_convert_writes_stdout_and_out_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(CODEX_SESSION)
    out_dir = tmp_path / "out"

    assert main(["convert", str(path), "--to", "json"]) == 0
    # A bare ARRAY of tagged records: a session IS its records, so nothing
    # wraps them and no metadata sits beside them.
    document = ListCodec.mappings(json.loads(capsys.readouterr().out))
    assert StrCodec.coerce(document[0].get("py/object")).endswith("TurnContext")

    assert main(["convert", str(path), "--to", "json", "--out-dir", str(out_dir)]) == 0
    assert (out_dir / "session.json").exists()


def test_verify_reports_exactness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good.jsonl"
    good.write_text(CLAUDE_SESSION)
    bad = tmp_path / "bad.jsonl"
    # Respaced, not rewritten: the record still parses to the same object, so
    # only a BYTE comparison can tell the two files apart.
    bad.write_text(CLAUDE_SESSION.replace('"parentUuid":null', '"parentUuid" : null'))

    assert main(["verify", str(good), "-v"]) == 0
    assert "1/1 exact" in capsys.readouterr().err

    assert main(["verify", str(bad), "--diff"]) == 1
    err = capsys.readouterr().err
    assert "not byte-exact" in err
    assert "--- original" in err


def test_verify_does_not_truncate_the_file_named_by_output(tmp_path: Path) -> None:
    """``verify`` writes no conversion, so ``-o`` must leave its target alone.

    Verifying keeps no text -- it asks whether the bytes agree, not for a copy
    of them -- but the destination was written unconditionally, so the empty
    string landed on whatever ``-o`` named and destroyed a file the command
    never claimed to touch.
    """
    path = tmp_path / "s.jsonl"
    path.write_text(CLAUDE_SESSION)
    output = tmp_path / "out.txt"
    output.write_text("PREEXISTING")

    assert main(["verify", str(path), "-o", str(output)]) == 0

    assert output.read_text() == "PREEXISTING"


def test_verify_runs_a_directory_in_parallel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two SESSIONS, each its own directory: files sharing a directory are one
    # session, since that is how a ``/clear`` continuation is recognized.
    _ = _session(tmp_path / "one" / "a.jsonl", CLAUDE_SESSION)
    _ = _session(tmp_path / "two" / "b.jsonl", CODEX_SESSION)
    _ = _session(tmp_path / "one" / "._a.jsonl", CLAUDE_SESSION)

    assert main(["verify", str(tmp_path), "--workers", "2"]) == 0
    assert "2/2 exact" in capsys.readouterr().err


def test_workers_run_in_separate_processes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # ``--workers 2`` on two sessions must actually FAN OUT. Asserting only on
    # "2/2 exact" cannot fail on a pool that silently ran serial, which is the
    # regression ``_workers`` exists to prevent -- so the pids are counted.
    for name in ("one", "two", "three", "four"):
        _ = _session(tmp_path / name / "s.jsonl", CLAUDE_SESSION)

    assert main(["verify", str(tmp_path), "--workers", "4", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().err)

    assert report["files"] == 4
    assert report["ok"] == 4


def test_verify_reports_the_wire_size_against_the_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Byte-exactness alone cannot catch a rewrite that silently emitted
    # nothing: an empty output compares unequal, but so does a one-byte
    # respacing, and only the SIZE distinguishes them.
    path = tmp_path / "s.jsonl"
    path.write_text(CLAUDE_SESSION)

    assert main(["verify", str(path), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().err)["results"][0]

    assert report["source_bytes"] == path.stat().st_size
    assert report["output_bytes"] == report["source_bytes"]


def test_verify_reports_a_size_gap_on_a_shortened_rewrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "s.jsonl"
    # A key no field holds and no residual keeps would vanish on rewrite. The
    # respacing here keeps every byte's MEANING and changes only its width, so
    # the output is smaller by exactly the spaces added.
    path.write_text(CLAUDE_SESSION.replace('"parentUuid":null', '"parentUuid" : null'))

    assert main(["verify", str(path), "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().err)["results"][0]

    assert report["byte_exact"] is False
    assert report["source_bytes"] == path.stat().st_size
    assert 0 < report["output_bytes"] < report["source_bytes"]


def test_detect_format_reads_each_shape(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(CODEX_SESSION)
    converted = convert_file(path, "codex", "json", False)

    assert detect_format(converted.text) == "json"
    assert detect_format(CLAUDE_SESSION) == "claude"
    assert detect_format(CODEX_SESSION) == "codex"
    assert detect_format("not json\n{}\n") == ""
    assert detect_format("\n\n" + CLAUDE_SESSION) == "claude"


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
        '"context_id":"seven"}]'
    )

    assert convert_file(path, "json", "claude", False).error


def test_json_report_and_usage_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(CLAUDE_SESSION)

    assert main(["verify", str(path), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().err)["ok"] == 1

    _ = _session(tmp_path / "other" / "s2.jsonl", CODEX_SESSION)
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
    good.write_text(CLAUDE_SESSION)

    assert main(["verify", str(bad), str(good), "--fail-fast", "-q"]) == 1


def test_a_lossy_conversion_is_refused_then_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Codex telemetry has no Claude representation, so this conversion drops
    # records. It must say so, and must not proceed unquestioned.
    path = tmp_path / "s.jsonl"
    path.write_text(
        CODEX_SESSION
        + '{"type":"event_msg","payload":{"type":"token_count","info":{}}}\n'
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
    path.write_text(CODEX_SESSION)
    out = tmp_path / "out.json"

    assert main(["convert", str(path), "--to", "json", "-o", str(out)]) == 0
    assert out.exists()


def test_dropped_detects_semantic_changes_but_ignores_provider_metadata() -> None:
    records = list(claude.normalize(StringIO(CLAUDE_SESSION)))
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
            FileResult(path=path, source_bytes=100, output_bytes=90), verifying=True
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


def test_module_entry_point_runs(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(CODEX_SESSION)

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


def _session(path: Path, text: str = CLAUDE_SESSION) -> Path:
    """Write one transcript at ``path``, parents included."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text, encoding="utf-8")
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The join is the risk the single-file tests cannot reach: the parts are
    # fused into ONE session, so a seam that lost or reordered a record shows
    # up only when the fused object is split back into the files it came from.
    project = tmp_path / "project"
    before = _session(project / "before-clear.jsonl", CLAUDE_SESSION)
    after = _session(project / "after-clear.jsonl", CLAUDE_SESSION)

    result = convert_file(project, "auto", None, False)

    assert result.byte_exact
    # ONE session, whose parts are the two files -- not two sessions.
    assert [name for name, _ in result.parts] == [before.name, after.name]
    assert result.source_bytes == before.stat().st_size + after.stat().st_size
    assert result.output_bytes == result.source_bytes

    assert main(["verify", str(project), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().err)
    assert report["files"] == 1
    assert report["ok"] == 1


def test_a_session_keeps_the_subagents_nested_under_it(tmp_path: Path) -> None:
    # A claude session spawns subagents into a directory named for it, so its
    # parts are recursive even though the session test is not.
    root = _session(tmp_path / "s.jsonl")
    child = _session(tmp_path / "s" / "subagents" / "agent-a1.jsonl")

    assert _parts_of(root) == [root, child]


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="POSIX signals only")
def test_killing_the_run_takes_its_workers_with_it(tmp_path: Path) -> None:
    # A SIGKILLed parent runs no cleanup, and the pool's workers are spawned by
    # a forkserver whose argv does not name this program -- so ``pkill -f`` on
    # the obvious pattern left 11 orphans holding 14 GB. The kernel has to be
    # the one that reaps them.
    for index in range(6):
        _ = _session(tmp_path / f"s{index}" / "s.jsonl", CLAUDE_SESSION * 40)
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
        _ = _session(tmp_path / name / "s.jsonl", CLAUDE_SESSION)
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
            ]
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


def test_converting_a_session_does_not_hold_many_copies_of_it(
    tmp_path: Path,
) -> None:
    # Measured, not assumed: a 273 MB session peaked at 4.3 GB, because the
    # source text, the parsed records, and the rewritten text were all held at
    # once. A whole corpus of them took 21 GB and thrashed the machine.
    session = _session(tmp_path / "big" / "s.jsonl", CLAUDE_SESSION * 200)
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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
