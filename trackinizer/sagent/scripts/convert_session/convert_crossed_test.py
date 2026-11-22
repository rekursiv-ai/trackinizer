"""Convert a captured session from each CLI to each CLI, end to end.

The command line is what a person runs, so each conversion goes through it. Into
its own format a session is the bytes it was captured as. Into another it is
frozen here, since a cross conversion is never byte-exact and only a frozen file
shows a moved byte. Each golden was loaded once by its own CLI when it was minted;
regenerate only after loading the new one the same way::

    SESSIONS_REGENERATE_GOLDEN=1 uv --quiet run --frozen pytest \
        trackinizer/sagent/scripts/convert_session/convert_crossed_test.py
"""

from __future__ import annotations

from itertools import product
from pathlib import Path
from typing import Final

import json
import os

from treekle import json_loads

import pytest

from trackinizer.sagent.scripts.convert_session.convert import main


_CWD: Final[Path] = Path(__file__).resolve().parent

_TESTDATA: Final[Path] = _CWD / "testdata"
_GOLDEN: Final[Path] = _TESTDATA / "crossed"
_ENV_REGENERATE: Final = "SESSIONS_REGENERATE_GOLDEN"

_SOURCES: Final = {
    "claude": _TESTDATA / "claude_sidechain.jsonl",
    "codex": _TESTDATA / "codex_main.jsonl",
    "sagent": _GOLDEN / "sagent_main.jsonl",
}


def _convert(source: Path, target: str, out: Path) -> str:
    """Run ``convert`` from ``source`` into ``target``; return what it wrote."""
    assert (
        main(["convert", str(source), "--to", target, "-o", str(out), "--lossy", "-q"])
        == 0
    )
    return out.read_text(encoding="utf-8")


@pytest.mark.parametrize(("source", "target"), list(product(_SOURCES, repeat=2)))
def test_a_session_becomes_each_cli_s_file(
    tmp_path: Path,
    source: str,
    target: str,
) -> None:
    fixture = _SOURCES[source]

    written = _convert(fixture, target, tmp_path / "out.jsonl")

    if source == target:
        assert written == fixture.read_text(encoding="utf-8")
        return
    golden = _GOLDEN / f"{fixture.stem}.{target}.jsonl"
    if os.environ.get(_ENV_REGENERATE) == "1":
        golden.write_text(written, encoding="utf-8")
    assert written == golden.read_text(encoding="utf-8")


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_an_unstamped_gemini_session_becomes_lines_its_cli_resumes(
    tmp_path: Path,
    target: str,
) -> None:
    source = tmp_path / "gemini.json"
    _ = source.write_text(
        json.dumps(
            {
                "sessionId": "s1",
                "messages": [
                    {"type": "user", "content": "hi"},
                    {"type": "gemini", "content": "hello", "toolCalls": []},
                ],
            },
        ),
        encoding="utf-8",
    )

    lines = _convert(source, target, tmp_path / "out.jsonl").splitlines()

    stamps = [json_loads(line, dict[str, object]).get("timestamp") for line in lines]
    assert len(lines) >= 2
    assert all(isinstance(stamp, str) for stamp in stamps)


@pytest.mark.parametrize("source", ["claude", "codex"])
def test_through_sagent_a_session_returns_to_its_own_bytes(
    tmp_path: Path,
    source: str,
) -> None:
    """A sagent journal carries another CLI's session whole, to bounce it back."""
    fixture = _SOURCES[source]
    _ = _convert(fixture, "sagent", tmp_path / "j.jsonl")

    back = _convert(tmp_path / "j.jsonl", source, tmp_path / "back.jsonl")

    assert back == fixture.read_text(encoding="utf-8")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
