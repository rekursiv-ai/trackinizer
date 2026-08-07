"""The console is a live tail: one row per event, however long the event is.

``renderRow`` used to append every line of an event body as its own row.
Measured against a real 7,138-record agent transcript: 1,585 tool-result events
whose bodies total **22,249 lines**, median 7 and one of 574. The console
emitted a row for each -- a screenful per event, in the one view whose whole
purpose is to be scanned while work is happening.

It now renders the first line and a ``+N`` control. Same events, 1,585 rows,
92.9% fewer. Nothing is discarded: the remainder expands in place, because
terse must not mean unreachable -- a console that hides output is as useless as
one that drowns in it, and the second failure is the one that hides the first.

Static scan, in the idiom of :mod:`assets_drift_test`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


CONSOLE = Path(__file__).parent / "assets" / "console.html"


@pytest.fixture(scope="module")
def source() -> str:
    return CONSOLE.read_text(encoding="utf-8")


def test_a_multiline_body_does_not_become_multiple_rows(source: str) -> None:
    """The 22,249-row case.

    The old loop appended ``lines.slice(1)`` directly into the row; the fold is
    what keeps a 574-line tool result from owning the viewport.
    """
    assert "const rest = lines.slice(1);" in source
    assert "for (const extra of lines.slice(1))" not in source


def test_the_folded_remainder_is_still_reachable(source: str) -> None:
    """Terse by default, complete on demand.

    Truncating without a way back would trade an unreadable console for a lying
    one -- and a reader cannot tell a truncated body from a short one.
    """
    assert 'class: "more"' in source
    assert "extra.classList.toggle" in source


def test_tool_calls_are_named_the_way_a_cli_names_them(source: str) -> None:
    """``Read(foo.py)``, not the whole argument object.

    Measured on the same transcript: 851 characters of argument JSON per call at
    the median, 11,768 at the worst. A reader scanning a hundred calls needs to
    tell them apart, not to audit their parameters.
    """
    assert "function callTarget" in source
    assert "JSON.stringify(tc.args" not in source
