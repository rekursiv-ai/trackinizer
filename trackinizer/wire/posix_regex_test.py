"""A POSIX pattern is scanned as a GRAMMAR, not searched as a flat string.

Every bug this file pins has one shape: a rule applied with ``str.replace``,
``in``, or a hand-rolled walk, which cannot see that the same characters mean
something else somewhere else -- after another backslash, inside a bracket
expression, after a leading ``]`` that is a literal member, or inside a
``(?#...)`` comment. Each such rule was rewritten several times and was wrong
somewhere new each time, so all of them now read one scan.

Every expectation below was read off a live PostgreSQL 16 (``en_US.UTF-8``)
and is quoted in the test that asserts it.
"""

from __future__ import annotations

import pytest

from trackinizer.wire.posix_regex import (
    escapes,
    has_posix_bracket_construct,
    has_python_named_group,
    live_indices,
    matchable_indices,
)


class TestEscapedBackslashIsNotAnEscape:
    def test_a_doubled_backslash_consumes_itself(self) -> None:
        # ``\\m`` is a literal backslash followed by ``m``. Postgres agrees:
        # ``'a\mb' ~ '\\m'`` is true. A scanner that sees ``\m`` here would
        # rewrite the ``m`` and corrupt the pattern.
        assert [(e.char, e.in_bracket) for e in escapes(r"\\m")] == [("\\", False)]

    def test_a_tripled_backslash_escapes_the_third(self) -> None:
        assert [e.char for e in escapes(r"\\\m")] == ["\\", "m"]


class TestBracketExpressions:
    def test_an_escape_inside_a_class_is_flagged_as_such(self) -> None:
        assert [(e.char, e.in_bracket) for e in escapes(r"[\b]")] == [("b", True)]

    def test_an_escape_outside_a_class_is_not(self) -> None:
        assert [(e.char, e.in_bracket) for e in escapes(r"\b")] == [("b", False)]

    def test_a_leading_bracket_is_a_literal_member(self) -> None:
        # POSIX: ``]`` first in a class is a literal, not the terminator.
        # Live: ``']' ~ '[]a]'`` is true. So ``\b`` after it is still inside.
        assert [e.in_bracket for e in escapes(r"[]a\b]")] == [True]

    def test_a_negated_class_may_also_lead_with_a_bracket(self) -> None:
        assert [e.in_bracket for e in escapes(r"[^]a\b]")] == [True]

    def test_a_character_class_name_does_not_close_the_bracket(self) -> None:
        # ``[[:alpha:]]``: the ``]`` ending ``[:alpha:]`` is not the class's.
        assert [e.in_bracket for e in escapes(r"[[:alpha:]\b]")] == [True]

    def test_an_escaped_bracket_does_not_open_a_class(self) -> None:
        assert [e.in_bracket for e in escapes(r"\[\b")] == [False, False]

    def test_a_closed_class_returns_to_the_outside(self) -> None:
        assert [e.in_bracket for e in escapes(r"[a]\b")] == [False]


class TestPositions:
    def test_the_start_indexes_the_backslash(self) -> None:
        found = list(escapes(r"ab\yc"))
        assert [(e.char, e.start) for e in found] == [("y", 2)]


@pytest.mark.parametrize("pattern", ["", "plain", "[abc]", "a+b*"])
def test_a_pattern_with_no_escapes_yields_none(pattern: str) -> None:
    assert list(escapes(pattern)) == []


class TestPythonNamedGroups:
    r"""``(?P<name>...)`` is Python-only; Postgres rejects it outright.

    Detection is the scan's third question, and the two hand-rolled walks
    that preceded it each missed a rule the scan already knew: a substring
    search refused ``[(?P<]x``, then a simplified walk refused ``[](?P<]x``.
    Both match ``'Px'`` in BOTH engines (measured on live PG16), so both
    refusals were wrong.
    """

    @pytest.mark.parametrize(
        "pattern", ["(?P<x>a)", "a(?P<n>b)", "(?P=x)", "[a](?P<n>b)"]
    )
    def test_a_real_named_group_is_detected(self, pattern: str) -> None:
        assert has_python_named_group(pattern) is True

    @pytest.mark.parametrize(
        "pattern",
        [
            "[(?P<]x",
            "[](?P<]x",
            "[^](?P<]x",
            "(?#(?P<)a",
            "(?#a(?P<x>b)",
            r"\(?P<x>a)",
            "plain",
            "",
        ],
    )
    def test_inert_named_group_text_is_not(self, pattern: str) -> None:
        # Each of these is a class member, comment prose, or an escaped
        # paren -- never a group opener.
        assert has_python_named_group(pattern) is False

    def test_a_backslash_does_not_extend_a_comment(self) -> None:
        # A comment body ends at the FIRST ``)``, backslash or not -- neither
        # engine escapes inside ``(?#...)``. So the ``(?P<x>`` after it opens
        # a real group: live PG16 errors on this pattern where Python matches
        # ``'a'``, and detecting it is what keeps the two in step.
        assert has_python_named_group(r"(?#\)(?P<x>a)") is True


class TestLiveIndices:
    r"""Which positions are pattern SYNTAX, and which are inert text.

    Rule after rule re-derived this by walking the pattern itself, and each
    got a different case wrong -- a substring search, then a simplified
    bracket walk, then an escaped-position set. It is one question, so it has
    one answer here, and the scan is the only thing that walks.
    """

    def test_bare_syntax_is_live(self) -> None:
        assert live_indices("a*+") == frozenset({0, 1, 2})

    def test_class_members_are_inert(self) -> None:
        # Only the closing ``]`` is syntax; ``*`` and ``+`` are members.
        assert live_indices("[*+]") == frozenset({3})

    def test_comment_bodies_are_inert(self) -> None:
        assert live_indices("(?#*+)a") == frozenset({6})

    def test_an_escaped_character_is_inert(self) -> None:
        # ``\*`` is a literal asterisk; the trailing ``+`` quantifies it.
        assert live_indices(r"a\*+") == frozenset({0, 3})

    def test_a_leading_bracket_member_does_not_close_the_class(self) -> None:
        # ``[]*]`` is the class {']', '*'}; only the final ``]`` is syntax.
        assert live_indices("[]*]") == frozenset({3})

    def test_an_empty_pattern_has_no_live_positions(self) -> None:
        assert live_indices("") == frozenset()

    def test_a_posix_class_close_is_not_the_outer_close(self) -> None:
        # ``[[:alpha:]]`` -- the ``]`` at index 9 belongs to ``[:alpha:]``, so
        # the outer class is still open and only the final ``]`` is syntax.
        # A second bracket walk missed the nested form the scan already knew.
        assert live_indices("[[:alpha:]]") == frozenset({10})


class TestMatchableIndices:
    r"""Which positions the engine MATCHES, which is a different question.

    A class member is inert to every syntax rule and matchable to the
    case-fold one: live PG16 says ``'s' ~ '(?i)[\u017f]'`` is false where
    Python matches, exactly as for the bare ``(?i)\u017f``. Answering the
    fold question with :func:`live_indices` let every bracket member through.
    """

    def test_class_members_are_matchable(self) -> None:
        assert matchable_indices("[*+]") == frozenset(range(4))

    def test_comment_bodies_are_not(self) -> None:
        assert matchable_indices("(?#*+)a") == frozenset({6})

    def test_an_unterminated_comment_swallows_the_rest(self) -> None:
        # A ``(?#`` with no ``)`` runs to the end in both engines, so nothing
        # after it matches anything.
        assert matchable_indices("(?#abc") == frozenset()

    def test_escaped_characters_are_matchable(self) -> None:
        # ``a\*+`` matches a literal asterisk; the escape pair is inert to the
        # quantifier rule and matchable text here.
        assert matchable_indices(r"a\*+") == frozenset(range(4))

    def test_an_empty_pattern_matches_nothing(self) -> None:
        assert matchable_indices("") == frozenset()


class TestPosixBracketConstructs:
    r"""``[:class:]`` / ``[.x.]`` / ``[=x=]`` exist in Postgres, not in Python.

    Live PG16 reads ``'x9' ~ '[[:digit:]]'`` as true; Python reads the same
    pattern as an ordinary set and answers false. The two evaluators would
    select different rows, so the construct is refused -- which first requires
    seeing it.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            "[[:digit:]]",
            "[[=a=]x]",
            "[[.hyphen.]x]",
            "[^[:digit:]]",
            "[a-z[:digit:]]",
        ],
    )
    def test_it_is_detected(self, pattern: str) -> None:
        assert has_posix_bracket_construct(pattern) is True

    @pytest.mark.parametrize("pattern", [r"[]\[[:digit:]]", "[][:digit:]]"])
    def test_a_leading_bracket_member_does_not_hide_it(self, pattern: str) -> None:
        # The leading ``]`` is a literal member, so the class is still OPEN
        # when ``[:digit:]`` begins. A walk that closed on any ``]`` read the
        # construct as outside the class and missed it.
        assert has_posix_bracket_construct(pattern) is True

    @pytest.mark.parametrize(
        "pattern", ["[abc]", "[0-9]", "[^a]", "[]a]", r"[\[]", "[[]", "a[b]c", ""]
    )
    def test_an_ordinary_bracket_is_not_one(self, pattern: str) -> None:
        assert has_posix_bracket_construct(pattern) is False

    def test_an_escaped_open_bracket_does_not_start_a_class(self) -> None:
        # ``\[`` is a literal, so the ``[:digit:]`` that follows is outside any
        # bracket expression -- and live PG16 agrees, matching neither digit.
        assert has_posix_bracket_construct(r"\[[:digit:]]") is False


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
