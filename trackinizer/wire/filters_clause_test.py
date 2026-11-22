r"""``validate_clause`` is the one contract every filter path enforces.

The CLI, the route, and the row evaluator all refuse through it, so each rule
here is pinned through it: a clause that reaches either evaluator must mean
one thing to Postgres and to Python. The numeric half (``as_numeric``) reads an
operand exactly as Postgres' ``numeric`` input does.
"""

from __future__ import annotations

from decimal import Decimal

import re

import pytest

from trackinizer.wire.filters import (
    MAX_FILTER_VALUE_CHARS,
    as_numeric,
    is_nan,
    validate_clause,
)


class TestClauseShape:
    def test_an_unknown_op_is_refused(self) -> None:
        message = validate_clause("title", "like", "x")
        assert message is not None
        assert message.startswith("unknown filter op 'like'")

    def test_a_presence_op_takes_no_value(self) -> None:
        assert validate_clause("owner", "isnull", "x") == (
            "filter op 'isnull' takes no value"
        )
        assert validate_clause("owner", "isnull", "") is None

    def test_an_oversized_value_is_refused(self) -> None:
        at_cap = "x" * MAX_FILTER_VALUE_CHARS
        assert validate_clause("title", "is", at_cap) is None
        assert validate_clause("title", "is", f"{at_cap}x") == (
            f"filter value exceeds {MAX_FILTER_VALUE_CHARS} characters"
        )

    def test_a_presence_op_on_a_not_null_alias_is_refused(self) -> None:
        # ``agent-cost`` canonicalizes to a NOT NULL cost axis first.
        message = validate_clause("agent-cost", "notnull", "")
        assert message is not None
        assert "'marginal_cost_agent_usd' is NOT NULL" in message


class TestRegexClauses:
    @pytest.mark.parametrize(
        "pattern",
        [
            pytest.param("^alpha[0-9]+$", id="plain"),
            pytest.param("(?=a)a", id="lookahead"),
            pytest.param("(?<!b)a", id="lookbehind"),
            pytest.param("(?:)a", id="empty-group"),
            pytest.param("(?i)abc", id="ascii-fold"),
            pytest.param("(?im)abc$", id="shared-flags"),
            pytest.param(r"a\*+", id="escaped-star-then-plus"),
            pytest.param("[*+]", id="class-members"),
            pytest.param("a}+", id="bare-close-brace"),
            pytest.param("{key}+", id="non-numeric-brace"),
            pytest.param(r"\{2}+", id="escaped-open-brace"),
            pytest.param("a{", id="lone-open-brace"),
            pytest.param("a{x", id="brace-then-letter"),
            pytest.param("a{2,3}", id="closed-repetition"),
            pytest.param(r"[\d]", id="translatable-bracket-shorthand"),
        ],
    )
    def test_a_pattern_both_engines_agree_on_is_accepted(self, pattern: str) -> None:
        assert validate_clause("title", "re", pattern) is None

    @pytest.mark.parametrize(
        ("pattern", "reason"),
        [
            pytest.param(r"a\z", r"'\\z' is Python-only", id="python-only-escape"),
            pytest.param(r"\N{DIGIT ONE}", r"'\\N' is Python-only", id="named-char"),
            pytest.param("(?>a)", r"'\(\?>a\)' is not a construct", id="atomic"),
            pytest.param("(?a)x", r"'\(\?a\)' is not a construct", id="ascii-flag"),
            pytest.param("(?u:x)", r"'\(\?u:\.\.\.\)' is not a", id="scoped-unknown"),
            pytest.param("(?n)a", "inline flag 'n' is a valid Postgres", id="pg-flag"),
            pytest.param("(?i:a)", r"'\(\?i:\.\.\.\)' scopes its flags", id="scoped"),
            pytest.param("a*+", r"possessive quantifier '\*\+'", id="star-plus"),
            pytest.param("a?+", r"possessive quantifier '\?\+'", id="question-plus"),
            pytest.param("a{2,3}+", r"possessive quantifier '}\+'", id="brace-plus"),
            pytest.param("a{2", r"repetition '\{2' is never closed", id="unclosed"),
            pytest.param(".{1{}", r"repetition '\{1\{}' is never", id="bad-body"),
            pytest.param("(?i)\u00e9", "case-folds non-ASCII", id="fold-non-ascii"),
            pytest.param("(?i)[\u017f]", "case-folds non-ASCII", id="fold-member"),
            pytest.param(r"[\D]", r"shorthand '\\D' inside a bracket", id="bracket-D"),
            pytest.param(r"[x\S]", r"shorthand '\\S' inside a bracket", id="bracket-S"),
            pytest.param("(?P<x>a)", "named groups are Python-only", id="named-group"),
            pytest.param("[[:digit:]]", "POSIX bracket construct", id="posix-class"),
            pytest.param("a(", "invalid regex 'a\\('", id="unbalanced-paren"),
            # The dialect gate passes an empty ``(?)``; the compile check settles it.
            pytest.param("(?)a", "unknown extension", id="empty-flag-group"),
            pytest.param(
                "a{999999999999,}",
                "invalid regex",
                id="overflowing-repetition",
            ),
        ],
    )
    def test_a_pattern_the_engines_disagree_on_is_refused(
        self,
        pattern: str,
        reason: str,
    ) -> None:
        message = validate_clause("title", "nre", pattern)
        assert message is not None
        assert re.search(reason, message), message

    def test_a_comment_body_does_not_fold(self) -> None:
        # ``(?i)(?#\u00e9)a`` runs in both engines: comment prose never matches.
        assert validate_clause("title", "re", "(?i)(?#\u00e9)a") is None


class TestAsNumeric:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.5", Decimal("1.5")),
            (" \t1.5\n", Decimal("1.5")),
            ("1_000", Decimal(1000)),
            ("-.5e2", Decimal("-.5e2")),
            ("1.00000000000000001", Decimal("1.00000000000000001")),
            ("INFINITY", Decimal("Infinity")),
            ("-inf", Decimal("-Infinity")),
        ],
    )
    def test_reads_what_postgres_numeric_reads(
        self,
        raw: str,
        expected: Decimal,
    ) -> None:
        assert as_numeric(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", "abc", "1_", "_1", "1__0", "\uff11", "\u0661", "\u00a01", "1e"],
    )
    def test_refuses_what_postgres_refuses(self, raw: str) -> None:
        assert as_numeric(raw) is None

    def test_nan_is_read_in_any_case(self) -> None:
        parsed = as_numeric(" NaN ")
        assert parsed is not None
        assert parsed.is_nan()


class TestIsNan:
    @pytest.mark.parametrize("raw", ["nan", "NaN", " NAN "])
    def test_every_spelling_of_nan_is_nan(self, raw: str) -> None:
        assert is_nan(raw) is True

    @pytest.mark.parametrize("raw", ["1", "inf", "abc", ""])
    def test_numbers_and_non_numbers_are_not(self, raw: str) -> None:
        assert is_nan(raw) is False


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
