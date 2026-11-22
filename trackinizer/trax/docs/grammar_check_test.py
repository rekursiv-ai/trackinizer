"""``grammar_check.main`` over a toy grammar: every verdict it can reach.

The real grammar and corpus run in ``grammar_gen_test``; here a three-rule
grammar makes each failure class cheap to provoke: ``x`` parses two ways,
``y`` one way, and ``z`` not at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.trax.docs import grammar_check
from trackinizer.trax.docs.grammar_check import Case


if TYPE_CHECKING:
    from pathlib import Path


_TOY = 'start: p | q | u\np: "x"\nq: "x"\nu: "y"\n'

_PASSING = (
    Case(label="ambiguous x", tokens="x", ambiguous=True),
    Case(label="unique y", tokens="y"),
    Case(label="rejected z", tokens="z", rejects=True),
)


def _use_grammar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    committed: str | None,
    rendered: str = _TOY,
) -> None:
    """Point the checker at ``committed`` on disk and ``rendered`` as expected."""
    path = tmp_path / "grammar.lark"
    if committed is not None:
        path.write_text(committed)
    monkeypatch.setattr(grammar_check, "grammar_path", lambda: path)
    monkeypatch.setattr(grammar_check, "render_grammar", lambda: rendered)


def test_passing_corpus_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_grammar(monkeypatch, tmp_path, committed=_TOY)
    monkeypatch.setattr(grammar_check, "CORPUS", _PASSING)
    assert grammar_check.main() == 0
    assert "current, accepts the corpus" in capsys.readouterr().out


@pytest.mark.parametrize("committed", [None, "start: u\n"])
def test_missing_or_stale_file_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    committed: str | None,
) -> None:
    _use_grammar(monkeypatch, tmp_path, committed=committed)
    assert grammar_check.main() == 1
    assert "grammar.lark: STALE" in capsys.readouterr().err


def test_unbuildable_grammar_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = "start: missing_rule\n"
    _use_grammar(monkeypatch, tmp_path, committed=broken, rendered=broken)
    assert grammar_check.main() == 1
    assert "grammar.lark: INVALID" in capsys.readouterr().err


def test_each_drift_class_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_grammar(monkeypatch, tmp_path, committed=_TOY)
    monkeypatch.setattr(
        grammar_check,
        "CORPUS",
        (
            *_PASSING,
            Case(label="no parse", tokens="z"),
            Case(label="parsed reject", tokens="y", rejects=True),
            Case(label="lost ambiguity", tokens="y", ambiguous=True),
            Case(label="new ambiguity", tokens="x"),
        ),
    )
    assert grammar_check.main() == 1
    captured = capsys.readouterr()
    assert "DRIFT" in captured.out
    failures = [line for line in captured.err.splitlines() if line.startswith("  - ")]
    assert [line.split("'")[1] for line in failures] == [
        "no parse",
        "parsed reject",
        "lost ambiguity",
        "new ambiguity",
    ]
    assert "does not parse: 'z'" in captured.err
    assert "expected to be REJECTED but parsed: 'y'" in captured.err
    assert "expected ambiguous but parsed uniquely" in captured.err
    assert "parsed ambiguously (1 _ambig nodes)" in captured.err


@pytest.mark.compute_large_fixture
def test_grammar_check_passes() -> None:
    """The Earley build + corpus acceptance + ambiguity drift gate all pass."""
    assert grammar_check.main() == 0


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
