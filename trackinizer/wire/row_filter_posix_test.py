r"""``posix_pattern`` translation must preserve everything it does not translate.

The Python predicate mirrors what Postgres would have answered, so a rewrite
that changes a pattern's meaning makes the two evaluators disagree on real
input -- the exact failure the translation exists to prevent.

Every Postgres expectation below was read off a live PostgreSQL 16 server
(``en_US.UTF-8``, UTF8) and is quoted at its assertion.
"""

from __future__ import annotations

import re

import pytest

from trackinizer.wire.posix_regex import posix_pattern


class TestUntranslatableContexts:
    def test_a_literal_backslash_before_m_is_left_alone(self) -> None:
        # ``\\m`` is a literal backslash then ``m``; live: ``'a\mb' ~ '\\m'``
        # is true. Rewriting the ``m`` produced an uncompilable pattern.
        translated = posix_pattern(r"\\m")
        assert re.search(translated, r"a\mb") is not None

    def test_an_escape_inside_a_class_is_left_alone(self) -> None:
        # Live: ``'y' ~ '[\y]'`` is an ERROR (invalid escape \ sequence), so
        # Postgres never matches this pattern at all. Translating it to
        # ``[\b]`` would have silently matched BACKSPACE instead.
        assert posix_pattern(r"[\y]").endswith(r"[\y]")

    def test_a_word_start_inside_a_class_is_left_alone(self) -> None:
        # Same: live ``'m' ~ '[\m]'`` errors. Splicing a lookaround into a
        # bracket expression produced ``re.error: unbalanced parenthesis``.
        assert posix_pattern(r"[\m]").endswith(r"[\m]")


class TestTranslationsStillApply:
    @pytest.mark.parametrize(
        ("pattern", "subject", "expected"),
        [
            (r"\yfoo", "bar foo", True),
            (r"\yfoo", "barfoo", False),
            (r"\mfoo", "bar foo", True),
            (r"\mfoo", "barfoo", False),
            (r"foo\M", "foo bar", True),
            (r"foo\M", "foobar", False),
            (r"\Yoo", "foo", True),
        ],
    )
    def test_boundaries_translate(
        self, pattern: str, subject: str, expected: bool
    ) -> None:
        assert (re.search(posix_pattern(pattern), subject) is not None) is expected


class TestDigitAndSpaceAreAsciiInBothEngines:
    r"""``\d`` and ``\s`` are ASCII-only in Postgres and Unicode in Python.

    Measured on live PG16: ``E'\u0666' ~ '\d'`` (an Arabic-Indic digit) is
    FALSE and ``E'\u00a0' ~ '\s'`` (a non-breaking space) is FALSE, while
    Python's defaults match both. ``\w`` is the exception -- Postgres IS
    Unicode-aware for word characters under ``en_US.UTF-8``, so it must not
    be narrowed with it.
    """

    @pytest.mark.parametrize(
        ("pattern", "subject", "postgres_says"),
        [
            (r"\d", "\u0666", False),
            (r"\d", "7", True),
            (r"\D", "\u0666", True),
            (r"\s", "\u00a0", False),
            (r"\s", "\t", True),
            (r"\S", "\u00a0", True),
            # ``\w`` agrees Unicode-wide and must stay that way.
            (r"\w", "é", True),
            (r"\w", "д", True),
            # INSIDE a bracket expression the same divergence holds: live PG16
            # says ``E'\u0666' ~ '[\d]'`` is false and ``E'\u00a0' ~ '[\s]'``
            # is false, where Python matches. A translation that only fired
            # outside brackets left this half of the gap open.
            (r"[\d]", "\u0666", False),
            (r"[\d]", "7", True),
            (r"[\s]", "\u00a0", False),
            (r"[\s]", "\t", True),
            (r"[\dx]", "\u0666", False),
            (r"[\dx]", "x", True),
        ],
    )
    def test_it_matches_the_engine(
        self, pattern: str, subject: str, postgres_says: bool
    ) -> None:
        found = re.search(posix_pattern(pattern), subject) is not None
        assert found is postgres_says


@pytest.mark.compute_large_fixture
class TestTheWhitespaceClassIsExact:
    r"""``\s`` is neither ASCII-only nor Python's set; it is its own set.

    Guessing its shape got it wrong twice -- first leaving Python's Unicode
    default (which over-matches U+00A0), then narrowing to ASCII (which
    under-matches U+1680 and U+2000-U+200A). Scanning a PREFIX of the
    codepoints got it wrong a third time: a scan of 1..12000 missed U+3000,
    the IDEOGRAPHIC SPACE, which live PG16 matches and the class did not. The
    scan below covers EVERY codepoint, so there is no range left to fall off
    the end of.
    """

    #: Every codepoint live PG16 answers ``~ '\s'`` for, over the whole
    #: character set (surrogates excluded -- ``chr(55296)`` is not text to
    #: Postgres).
    POSTGRES_SPACE = frozenset(
        {9, 10, 11, 12, 13, 32, 5760, *range(8192, 8199), 8200, 8201, 8202}
        | {8232, 8233, 8287, 12_288}
    )

    @staticmethod
    def scanned() -> tuple[int, ...]:
        """Every codepoint the live scan covered, surrogates excluded."""
        return (*range(1, 0xD800), *range(0xE000, 0x110000))

    def matching(self, pattern: str) -> frozenset[int]:
        """Codepoints the translated ``pattern`` matches, over the whole set."""
        compiled = re.compile(posix_pattern(pattern))
        return frozenset(i for i in self.scanned() if compiled.match(chr(i)))

    def test_it_matches_postgres_codepoint_for_codepoint(self) -> None:
        assert self.matching(r"\s") == self.POSTGRES_SPACE

    def test_the_negation_is_the_exact_complement(self) -> None:
        negated = self.matching(r"\S")
        assert negated.isdisjoint(self.POSTGRES_SPACE)
        assert len(negated) == len(self.scanned()) - len(self.POSTGRES_SPACE)

    @pytest.mark.parametrize("pattern", [r"\s", r"[\s]", r"[\sx]"])
    def test_it_is_exact_inside_a_bracket_too(self, pattern: str) -> None:
        # The in-bracket form needs bare MEMBERS rather than a nested class,
        # so it is a second table -- and the first version of it was ASCII-only
        # while the outside-bracket one was already exact. One table being
        # right does not make the other right.
        extra = frozenset({ord("x")} if "x" in pattern else set[int]())
        assert self.matching(pattern) == self.POSTGRES_SPACE | extra

    def test_the_digit_class_matches_no_non_ascii_digit(self) -> None:
        # Postgres matched zero non-ASCII digits anywhere in the same scan, so
        # ``[0-9]`` is exact rather than an approximation.
        assert self.matching(r"\d") == frozenset(range(48, 58))


class TestDotMatchesNewlineLikePostgres:
    r"""``.`` matches a newline in Postgres and not in Python by default.

    Live PG16: ``E'a\nb' ~ 'a.b'`` is TRUE. Python's default says false. This
    is not an error in either engine -- both run the pattern happily and
    return DIFFERENT rows, which is the one failure mode with no 400 to catch
    it, so the translation must close it rather than the gate refuse it.
    """

    @pytest.mark.parametrize(
        ("pattern", "subject", "postgres_says"),
        [
            ("a.b", "a\nb", True),
            ("a.b", "axb", True),
            ("a.b", "ab", False),
            # An explicit ``(?i)`` must survive the added flag.
            ("(?i)a.b", "A\nB", True),
            # A literal dot is unaffected either way.
            (r"a\.b", "a\nb", False),
            (r"a\.b", "a.b", True),
        ],
    )
    def test_it_matches_the_engine(
        self, pattern: str, subject: str, postgres_says: bool
    ) -> None:
        found = re.search(posix_pattern(pattern), subject) is not None
        assert found is postgres_says


class TestDollarIsEndOfStringLikePostgres:
    r"""``$`` anchors at end-of-STRING in Postgres, before a final newline in
    Python.

    Live PG16: ``E'a\n' ~ 'a$'`` is FALSE. Python's ``$`` says true. Like the
    dot/newline gap, neither engine errors -- they simply return different
    rows -- so it has to be translated rather than refused. Python's
    ``\Z`` is the anchor that means what Postgres means.
    """

    @pytest.mark.parametrize(
        ("pattern", "subject", "postgres_says"),
        [
            ("a$", "a\n", False),
            ("a$", "a", True),
            ("a$", "ab", False),
            # ``^`` does NOT diverge: neither engine is multiline by default.
            ("^a", "\na", False),
            ("^a", "ab", True),
            # A literal dollar is unaffected.
            (r"a\$b", "a$b", True),
            # Under ``(?m)`` the two AGREE that ``$`` matches before each
            # newline, so rewriting it to ``\Z`` -- which ignores multiline --
            # broke the very case the anchor fix was protecting.
            ("(?m)line1$", "line1\nline2", True),
            ("(?m)abc$", "abc", True),
            ("(?m)abc$", "abc\n", True),
            ("(?m)^abc", "def\nabc", True),
            # A LOOKAHEAD whose body merely contains the letter ``m`` is not a
            # multiline flag. Reading every ``(?...)`` body for an ``m``
            # silently disabled the anchor fix for these.
            ("(?=m)m$", "m\n", False),
            ("(?<=m)x$", "mx\n", False),
            ("(?:m)$", "m\n", False),
            ("(?#m)m$", "m\n", False),
        ],
    )
    def test_it_matches_the_engine(
        self, pattern: str, subject: str, postgres_says: bool
    ) -> None:
        found = re.search(posix_pattern(pattern), subject) is not None
        assert found is postgres_says


class TestNonAsciiAgreesWithPostgres:
    r"""``\m`` / ``\M`` must use the same word-character set Postgres does.

    Live PG16 in ``en_US.UTF-8`` counts a letter as a word character whatever
    its script: ``'é' ~ '\w'`` and ``'д' ~ '\w'`` are both true. Python's
    ``\w`` is Unicode-aware by default and agrees on both. A hardcoded
    ``[0-9A-Za-z_]`` class does not, so ``'éfoo' ~ '\mfoo'`` -- false in
    Postgres -- matched in Python.
    """

    @pytest.mark.parametrize(
        ("pattern", "subject", "postgres_says"),
        [
            (r"\mfoo", "éfoo", False),
            (r"\mfoo", "дfoo", False),
            (r"\mfoo", "-foo", True),
            (r"\mfoo", " foo", True),
            (r"foo\M", "fooé", False),
            (r"foo\M", "foo-", True),
        ],
    )
    def test_word_class_matches_the_engine(
        self, pattern: str, subject: str, postgres_says: bool
    ) -> None:
        found = re.search(posix_pattern(pattern), subject) is not None
        assert found is postgres_says


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
