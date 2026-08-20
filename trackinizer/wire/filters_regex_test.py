r"""``Filter`` refuses regex escapes the two evaluators read differently.

Whether a regex can be BOUNDED is not decided here -- that depends on whether
the column lowers into SQL, which only the store knows, so
``read._reject_unboundable_regex`` owns it. This file covers the other half:
a pattern that means one thing to Postgres and another to Python selects
different rows depending on which evaluator ran it, and no bound makes that
correct.
"""

from __future__ import annotations

import pytest

from trackinizer.wire.filters import Filter, validate_regex_dialect


class TestAmbiguousEscapes:
    def test_backslash_b_is_refused(self) -> None:
        # POSIX reads ``\b`` as BACKSPACE, Python as a word boundary --
        # verified on a live engine: ``'foo bar' ~ '\bbar'`` is false in SQL
        # and true in Python. It cannot be translated, so it is refused with
        # the spelling that works in both.
        message = validate_regex_dialect(r"\balpha")
        assert message is not None
        assert r"\y" in message

    def test_negated_form_is_refused(self) -> None:
        assert validate_regex_dialect(r"\Balpha") is not None

    def test_the_posix_spelling_is_allowed(self) -> None:
        assert validate_regex_dialect(r"\yalpha") is None

    def test_an_ordinary_pattern_is_allowed(self) -> None:
        assert validate_regex_dialect("^alpha[0-9]+$") is None


class TestFilterEnforcesTheDialect:
    def test_construction_raises_on_an_ambiguous_escape(self) -> None:
        # On the wire type, so the CLI and the server refuse identically.
        with pytest.raises(ValueError, match=r"\\b"):
            Filter(field="title", op="re", value=r"\balpha")

    def test_a_valid_regex_filter_still_constructs(self) -> None:
        assert Filter(field="title", op="re", value=r"\yalpha").value == r"\yalpha"

    def test_a_non_regex_op_is_not_dialect_checked(self) -> None:
        # ``\b`` is an ordinary two-character string to an equality filter.
        assert Filter(field="title", op="is", value=r"\balpha").value == r"\balpha"


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
