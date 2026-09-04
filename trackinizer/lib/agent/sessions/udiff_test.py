"""A unified diff decomposes into splices and rebuilds byte for byte.

:class:`FileEditResult` states its edits as :class:`Splice` records, so the
diff text a provider wrote has to survive that shape exactly -- a rebuild that
loses a context line silently rewrites a patch.

The cases here are captured codex ``unified_diff`` bodies. The trailing-context
one is load-bearing: a first attempt at this decomposition carried only the
context BEFORE each change and lost 10 of 71 real diffs, all of which ended in
a context line.
"""

from __future__ import annotations

import pytest

from trackinizer.lib.agent.sessions.udiff import parse_udiff, render_udiff
from trackinizer.lib.agent.types.sessions import Splice


def diffs() -> tuple[tuple[str, str], ...]:
    """Return captured diff bodies, with an id for each.

    Returns:
      cases: One ``(id, diff)`` pair per shape observed in the corpus.

    """
    return (
        (
            "single-hunk",
            (
                "@@ -1,2 +1,2 @@\n def apply_discount(price, pct):\n"
                "-    return price - pct\n+    return price * (1 - pct / 100)\n"
            ),
        ),
        (
            "added-line",
            (
                "@@ -1,2 +1,3 @@\n-def apply_discount(price, pct):\n"
                "+def apply_discount(price: float) -> float:\n"
                '+    """Return it."""\n'
                "     return price - price * pct / 100\n"
            ),
        ),
        (
            # Two changed runs in ONE hunk, separated by context, and ending
            # in context. The shape that defeated a lead-only decomposition.
            "trailing-context",
            (
                "@@ -1,2 +1,2 @@\n-def fetchUserData():\n+def fetch_user_data():\n"
                "     return {}\n@@ -4,2 +4,2 @@\n \n-x = fetchUserData()\n"
                "+x = fetch_user_data()\n"
            ),
        ),
        (
            "context-both-sides",
            (
                "@@ -97,3 +97,3 @@\n"
                "     def __init__(self, cache: Cache | None) -> None:\n"
                "-        self.cache = cache or Cache()\n"
                "+        self.cache = cache if cache is not None else Cache()\n \n"
            ),
        ),
        ("pure-insert", "@@ -1,0 +1,2 @@\n+one\n+two\n"),
        ("pure-delete", "@@ -1,2 +1,0 @@\n-one\n-two\n"),
        ("no-header", "-one\n+two\n"),
        # A body whose last line carries no newline. Captured codex patches
        # write this, and a parser splitting on ``\n`` discarded that line --
        # the ``+A`` vanished and the edit replayed as a pure deletion.
        ("unterminated", "@@ -1 +1 @@\n-a\n+A"),
        ("unterminated-context", "@@ -1 +1 @@\n-a\n+A\n unchanged"),
        ("empty", ""),
    )


@pytest.mark.parametrize(
    "diff", [pytest.param(diff, id=name) for name, diff in diffs()]
)
def test_a_diff_rebuilds_from_its_splices(diff: str) -> None:
    assert render_udiff(parse_udiff(diff)) == diff


def test_a_two_run_hunk_becomes_two_splices() -> None:
    """Each changed run is its own splice, so an edit is addressable.

    One splice per hunk would make the interior context part of a change it
    does not belong to.
    """
    _, diff = diffs()[2]

    edits = parse_udiff(diff)

    assert len(edits) == 2
    assert edits[0].before == "def fetchUserData():\n"
    assert edits[0].after == "def fetch_user_data():\n"
    # Context keeps its leading marker space, so the diff rebuilds verbatim.
    assert edits[0].trail == "     return {}\n"
    assert edits[1].before == "x = fetchUserData()\n"


def test_a_splice_carries_the_line_its_hunk_named() -> None:
    """The ``@@`` header is a position, which the splice keeps.

    One changed run at the head of the hunk, so the run's own start IS the
    hunk's and its count is the lines it replaced.
    """
    edits = parse_udiff("@@ -97,3 +97,3 @@\n-old\n+new\n")

    assert edits[0].start == 97
    assert edits[0].count == 1


def test_each_run_states_the_lines_it_replaced_not_its_hunk() -> None:
    """``start``/``count`` name the REPLACED text, per the field's contract.

    Copying the ``@@`` header verbatim made them the hunk's: a run preceded by
    context reported the hunk's first line rather than its own, and every run
    after the first reported nothing at all. Counted in old-file lines, ``-a``
    sits at 11 and ``-b`` at 13.
    """
    diff = "@@ -10,6 +10,6 @@\n ctx\n-a\n+A\n ctx2\n-b\n+B\n ctx3\n"

    edits = parse_udiff(diff)

    assert [(edit.start, edit.count) for edit in edits] == [(11, 1), (13, 1)]
    assert render_udiff(edits) == diff


def test_a_no_newline_marker_is_metadata_not_context() -> None:
    r"""``\ No newline at end of file`` describes the line before it.

    Git writes it as a diff annotation, not as content, so treating it as
    context split ONE replacement into two splices and gave both sides a
    trailing newline the file does not have.
    """
    diff = (
        "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n"
        "+new\n\\ No newline at end of file\n"
    )

    edits = parse_udiff(diff)

    assert len(edits) == 1
    assert edits[0].before == "old"
    assert edits[0].after == "new"
    assert render_udiff(edits) == diff


def test_a_no_newline_marker_on_trailing_context_stays_there() -> None:
    r"""The annotation describes the line before it, context included.

    Git writes it after a trailing CONTEXT line whenever the unchanged last
    line of the file has no terminator -- verified against ``git diff -U1`` on
    a file ending ``ctx`` with no newline. The reader attributed it to whatever
    side the run had last touched, so it came back one line early: after the
    ``+``, before the context it actually described.
    """
    diff = "@@ -1,3 +1,3 @@\n a\n-b\n+B\n ctx\n\\ No newline at end of file\n"

    edits = parse_udiff(diff)

    assert edits[0].after == "B\n"
    assert edits[0].trail == " ctx"
    assert render_udiff(edits) == diff


@pytest.mark.parametrize(
    ("splice", "rendered"),
    [
        # An unterminated field renders unterminated, so a patch that ended
        # mid-line rebuilds byte for byte rather than gaining a newline.
        (Splice(before="", after="hi"), "+hi"),
        (Splice(before="hi", after=""), "-hi"),
        (Splice(before="", after="one\ntwo"), "+one\n+two"),
        (Splice(before="", after="hi\n"), "+hi\n"),
    ],
    ids=["insert", "delete", "two-lines", "terminated"],
)
def test_text_renders_with_the_termination_it_carries(
    splice: Splice, rendered: str
) -> None:
    r"""A last line lacking its newline is still a line, and stays that way.

    Splitting on ``\n`` alone yielded an empty final piece that dropped the
    text entirely -- the whole edit rendered as nothing.
    """
    assert render_udiff((splice,)) == rendered


def test_an_empty_diff_has_no_splices() -> None:
    assert parse_udiff("") == ()
    assert render_udiff(()) == ""


def test_a_hand_built_splice_renders_without_a_header() -> None:
    """A splice from another provider has no hunk header to reprint.

    Claude states ``oldString``/``newString`` and no position, so rendering
    one must not invent an ``@@`` line it never had.
    """
    rendered = render_udiff((Splice(before="old\n", after="new\n"),))

    assert rendered == "-old\n+new\n"


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
