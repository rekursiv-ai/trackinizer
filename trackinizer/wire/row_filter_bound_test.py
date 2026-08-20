r"""``match_filter`` must answer what SQL would answer, or refuse.

The Python predicate exists to mirror the store's SQL for rows already in
memory. Where the two evaluators would return DIFFERENT rows, neither may run
the filter -- that is the whole contract, and each op below breaks it in its
own way:

* an order op on a shape with no SQL ordering compared ``str(['a','b'])``;
* ``NaN`` sorts largest in Postgres and compares False in Python.

Regex breaks the contract two ways. In COST: Postgres bounds a pathological
pattern with its hybrid NFA/DFA plus a statement timeout, while Python
backtracks -- ``(a+)+$`` over 30 characters measured **79.89 seconds** through
``re``. The store never routes a regex here
(``store/regex_stays_in_sql_test.py`` pins that), so the reachable cost rule
is the narrow one below: refuse a regex whose column has no SQL at all.

And in GRAMMAR, in more ways than one file should try to list: ``(?P<x>a)``
and ``\z`` parse in Python and error in Postgres; so do ``(?a)`` and every
scoped ``(?i:...)`` group; and ``[\D]`` answers OPPOSITELY in the two. Each
is refused outright -- no column makes them agree -- and the classes are
enumerated where they are enforced, in ``wire/filters.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import re
import warnings

import pytest

from trackinizer.types.errors import ValidationError
from trackinizer.wire.filters import Filter, FilterOp
from trackinizer.wire.row_filter import match_filter


@dataclass(frozen=True, kw_only=True, slots=True)
class _BareFilter:
    """A structural ``RowFilter`` that never runs ``Filter.__post_init__``.

    The wire type's validation is not a chokepoint: anything satisfying the
    protocol reaches ``match_filter``. A guard that only ran in
    ``__post_init__`` would miss this entirely.
    """

    field: str
    op: FilterOp
    value: str


class TestRegexOnANonLoweringColumnIsRefused:
    def test_a_non_lowering_column_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="experiment_config"):
            match_filter(
                {"experiment_config": {"lr": 0.1}},
                Filter(field="experiment_config", op="re", value="lr"),
            )

    def test_the_negated_form_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            match_filter(
                {"experiment_config": {}},
                Filter(field="experiment_config", op="nre", value="lr"),
            )

    def test_a_bare_row_filter_is_refused(self) -> None:
        # The wire type's ``__post_init__`` is not a chokepoint; anything
        # satisfying the protocol arrives here.
        with pytest.raises(ValidationError):
            match_filter(
                {"experiment_config": {}},
                _BareFilter(field="experiment_config", op="re", value="(a+)+$"),
            )

    def test_the_cli_alias_is_refused(self) -> None:
        # ``config`` aliases ``experiment_config``. Comparing the raw field
        # let this through once already.
        with pytest.raises(ValidationError, match="experiment_config"):
            match_filter(
                {"experiment_config": {}},
                _BareFilter(field="config", op="re", value="lr"),
            )


class TestOrderOpsAreRefusedWhereOrderIsUndefined:
    def test_an_array_column_is_refused(self) -> None:
        # Without this the comparison was ``str(['a', 'b']) > 'x'`` -- the
        # Python repr of a list, deterministic nonsense.
        with pytest.raises(ValidationError, match="labels"):
            match_filter(
                {"labels": ["a", "b"]},
                _BareFilter(field="labels", op="gt", value="x"),
            )

    def test_a_text_column_is_refused(self) -> None:
        # Python compares ``"9" > "10"`` numerically, SQL lexically. The two
        # answer differently, so neither may run it.
        with pytest.raises(ValidationError, match="title"):
            match_filter(
                {"title": "9"}, _BareFilter(field="title", op="gt", value="10")
            )

    def test_a_uuid_column_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            match_filter({"id": "0" * 8}, _BareFilter(field="id", op="gt", value="0"))

    @pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
    def test_a_numeric_column_still_orders(self, op: FilterOp) -> None:
        assert isinstance(
            match_filter(
                {"issue_priority": 5},
                Filter(field="issue_priority", op=op, value="5"),
            ),
            bool,
        )


class TestNaNIsRefused:
    r"""NaN orders differently in the two evaluators, so no one may order by it.

    Live PG16: ``5 < 'nan'::numeric`` is TRUE (NaN sorts as the largest
    numeric). Python: every comparison against ``float('nan')`` is False. The
    filter LOWERS -- ``seq lt nan`` becomes ``seq < $1::numeric`` -- so the
    same request returns different rows depending on which evaluator ran it.

    Infinity does NOT diverge (measured: ``inf``, ``-inf``, ``1e400`` agree in
    both), so only NaN is refused.
    """

    @pytest.mark.parametrize("value", ["nan", "NaN", "NAN", " nan "])
    @pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
    def test_nan_is_refused(self, value: str, op: FilterOp) -> None:
        with pytest.raises(ValidationError, match="NaN"):
            match_filter({"seq": 5}, _BareFilter(field="seq", op=op, value=value))

    @pytest.mark.parametrize("value", ["-nan", "+nan"])
    def test_a_signed_nan_is_refused_as_unparseable(self, value: str) -> None:
        # Live PG16 rejects a SIGNED nan outright ("invalid input syntax for
        # type numeric"), so it never becomes a NaN there at all -- the
        # refusal is the parse one, and calling it NaN would describe a value
        # Postgres never builds. ``float('-nan')`` does build one, which is
        # what made the two disagree about which refusal applies.
        with pytest.raises(ValidationError, match="not a number"):
            match_filter({"seq": 5}, _BareFilter(field="seq", op="lt", value=value))

    @pytest.mark.parametrize("value", ["inf", "-inf"])
    def test_infinity_is_allowed(self, value: str) -> None:
        # Both engines agree, so refusing it would remove capability for
        # nothing.
        assert match_filter({"seq": 5}, Filter(field="seq", op="lt", value=value)) is (
            value == "inf"
        )


class TestTheWireTypeRefusesTheSameThings:
    r"""Every rule decidable from the CLAUSE is refused at construction.

    Validation that lives at one call site is validation the other sites
    skip -- the presence-op check sat in the HTTP route while the CLI, a
    direct ``Filter``, and the store all accepted what it refused. So
    everything answerable from ``(field, op, value)`` moved onto the type.

    A rule needing the column's SQL cannot: ``nan`` is refused when ordering
    ``seq``, whose template casts to numeric, and legal when ordering
    ``created``, whose template compares text. Those live in
    ``reject_inadmissible``, which sees the column.
    """

    def test_a_presence_op_refuses_an_operand(self) -> None:
        # Accepting it means ``{"op": "isnull", "value": "Dan"}`` silently
        # answers "owner is null" -- a question the caller did not ask.
        with pytest.raises(ValueError, match="takes no value"):
            Filter(field="owner", op="isnull", value="Dan")

    def test_a_presence_op_accepts_the_empty_operand(self) -> None:
        assert Filter(field="owner", op="isnull", value="").value == ""

    @pytest.mark.parametrize("pattern", ["a{999999999999,}", "a{99999999999999999999}"])
    def test_a_huge_repetition_bound_is_refused(self, pattern: str) -> None:
        # ``re.compile`` raises OverflowError here, NOT ``re.error``, so an
        # ``except re.error`` let a 16-character caller typo escape as a 500.
        with pytest.raises(ValueError, match="invalid regex"):
            Filter(field="title", op="re", value=pattern)

    @pytest.mark.parametrize("flag", ["n", "w", "b", "e", "q", "a"])
    def test_an_unterminated_flag_group_is_a_parse_error(self, flag: str) -> None:
        # ``(?n`` never closes, so it is not a flag group at all -- calling it
        # "a valid Postgres flag" describes a construct the pattern does not
        # contain. Both engines simply fail to parse it.
        with pytest.raises(ValueError, match="invalid regex"):
            Filter(field="title", op="re", value=f"(?{flag}")

    @pytest.mark.parametrize(
        "pattern",
        [
            "(?i)\u0130",
            "(?i)\u017f",
            "(?i)\u03a3x",
            # A bracket MEMBER folds exactly as the bare atom does -- live
            # PG16 says ``'s' ~ '(?i)[\u017f]'`` is false where Python
            # matches. Gating on ``live_indices`` missed these, because a
            # class member is inert to the SYNTAX rules and matchable text
            # to this one.
            "(?i)[\u017f]",
            "(?i)[\u0130]",
            "(?i)[a\u017f]",
            "(?i)[^\u017f]",
        ],
    )
    def test_case_folding_a_non_ascii_pattern_is_refused(self, pattern: str) -> None:
        # The two engines fold Unicode differently and BOTH answer -- measured
        # 6 disagreements in 10 pairs, e.g. ``(?i)i`` matches U+0130 in Python
        # and not in Postgres. Neither ``re.ASCII`` nor the default reproduces
        # Postgres's fold, so it cannot be translated.
        with pytest.raises(ValueError, match="case-fold"):
            Filter(field="title", op="re", value=pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            "(?i)abc",
            "(?i)[a-z]",
            "\u00e9",
            "(?s)\u00e9",
            # Non-ASCII inside a COMMENT is prose: unreachable, unfoldable,
            # and live PG16 runs the pattern.
            "(?i)(?#\u00e9)a",
        ],
    )
    def test_ascii_case_folding_and_bare_non_ascii_still_construct(
        self, pattern: str
    ) -> None:
        # ASCII folds agree exactly, and a non-ASCII pattern WITHOUT ``(?i)``
        # never folds at all.
        assert Filter(field="title", op="re", value=pattern).value == pattern

    def test_a_regex_python_cannot_compile_is_refused(self) -> None:
        # Left unchecked, this surfaced as ``re.PatternError`` from inside
        # ``match_filter`` -- an implementation leak where every other bad
        # operand gets a refusal.
        with pytest.raises(ValueError, match="invalid regex"):
            Filter(field="title", op="re", value="[")

    @pytest.mark.parametrize(
        "pattern",
        [
            "[[=a=]x]",
            "[[.hyphen.]x]",
            "[[:digit:]]",
            "[[:alpha:]]",
            "[[:space:]x]",
            "[^[:digit:]]",
            "[a-z[:digit:]]",
            # A leading ``]`` is a literal MEMBER, so the class is still open
            # when ``[:digit:]`` starts. Live PG16 matches ``'5'``; Python
            # does not. A bracket walk that forgets that rule misses this.
            r"[]\[[:digit:]]",
            r"[][:digit:]]",
            r"[^][:digit:]]",
        ],
    )
    def test_a_posix_bracket_construct_is_refused(self, pattern: str) -> None:
        # Postgres implements ``[:class:]`` / ``[.x.]`` / ``[=x=]`` and Python
        # does not, reading them as ordinary set members -- live PG16 says
        # ``'x9' ~ '[[:digit:]]'`` is true where Python says false. Refusing is
        # the only honest answer: no translation would make the two agree.
        with pytest.raises(ValueError, match="POSIX"):
            Filter(field="title", op="re", value=pattern)

    @pytest.mark.parametrize("pattern", ["[[:digit:]]", "[[=a=]x]"])
    def test_the_refusal_survives_a_warm_regex_cache(self, pattern: str) -> None:
        # Detecting these by their ``FutureWarning`` was bypassable: CPython
        # returns a CACHED pattern before parsing, so once any earlier request
        # compiled it without warnings-as-error, the warning never fired again
        # and the pattern was accepted -- reinstating the divergence.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            _ = re.compile(pattern)
        with pytest.raises(ValueError, match="POSIX"):
            Filter(field="title", op="re", value=pattern)

    @pytest.mark.parametrize(
        "pattern", ["[abc]", "[0-9]", "[^a]", "[]a]", r"[\[]", "[[]"]
    )
    def test_an_ordinary_bracket_expression_still_constructs(
        self, pattern: str
    ) -> None:
        # ``[[]`` is the literal-``[`` class. Python WARNS about it ("Possible
        # nested set") though both engines agree -- live PG16 matches ``'a[b'``
        # and misses ``'x'``, exactly as Python does. Refusing on that warning
        # rejected a valid pattern, so the compile site ignores it and the
        # construct that genuinely diverges is refused structurally instead.
        assert Filter(field="title", op="re", value=pattern).value == pattern

    def test_a_translated_only_escape_still_constructs(self) -> None:
        # ``\y`` is a bad escape to raw Python and valid after translation;
        # checking the untranslated text would refuse a working pattern.
        assert Filter(field="title", op="re", value=r"\yalpha").value == r"\yalpha"

    def test_an_unknown_op_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown filter op"):
            Filter(field="seq", op=cast("FilterOp", "bogus"), value="3")


class TestTheEvaluatorEnforcesTheWholeContract:
    r"""A bare ``RowFilter`` reaches the evaluator without the wire type.

    ``Store.list_kind`` accepts ``Sequence[RowFilter]`` -- a Protocol -- so
    every rule that lived ONLY on ``Filter.__post_init__`` was a question a
    structural filter could still ask. Both now call one
    ``validate_clause``.
    """

    def test_an_ambiguous_escape_is_refused(self) -> None:
        with pytest.raises(ValidationError, match=r"\\b"):
            match_filter(
                {"title": "x"}, _BareFilter(field="title", op="re", value=r"\bbar")
            )

    def test_a_presence_operand_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="takes no value"):
            match_filter(
                {"owner": None},
                _BareFilter(field="owner", op="isnull", value="Dan"),
            )

    def test_an_overlong_operand_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="exceeds"):
            match_filter(
                {"title": "x"},
                _BareFilter(field="title", op="is", value="x" * 513),
            )

    def test_an_uncompilable_regex_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="invalid regex"):
            match_filter({"title": "x"}, _BareFilter(field="title", op="re", value="["))

    def test_an_unknown_op_is_refused(self) -> None:
        # Refused rather than reaching ``_ordered``'s assert: an unknown op
        # is caller input, not a broken invariant, so it earns a refusal the
        # caller can read. Before both, it silently MEANT ``ge``.
        with pytest.raises(ValidationError, match="unknown filter op"):
            match_filter(
                {"seq": 5},
                _BareFilter(field="seq", op=cast("FilterOp", "bogus"), value="3"),
            )


class TestAPresenceOpOnANotNullColumn:
    r"""``isnull`` on a NOT-NULL column is a question with a known answer.

    It matches nothing and ``notnull`` matches everything, whatever the rows
    say -- a silent wrong answer rather than a filter.
    ``validate_presence_op`` has always stated this, but only the HTTP route
    asked it, so the CLI, a direct ``Filter``, and the store each accepted
    what the route refused.
    """

    @pytest.mark.parametrize("column", ["id", "seq", "created", "account"])
    def test_isnull_is_refused_at_construction(self, column: str) -> None:
        with pytest.raises(ValueError, match="NOT NULL"):
            Filter(field=column, op="isnull", value="")

    def test_notnull_is_refused_too(self) -> None:
        with pytest.raises(ValueError, match="NOT NULL"):
            Filter(field="id", op="notnull", value="")

    def test_the_evaluator_refuses_a_bare_filter(self) -> None:
        with pytest.raises(ValidationError, match="NOT NULL"):
            match_filter({"id": "x"}, _BareFilter(field="id", op="isnull", value=""))

    def test_a_nullable_column_still_accepts_it(self) -> None:
        assert Filter(field="owner", op="isnull", value="").op == "isnull"
        assert (
            match_filter({"owner": None}, Filter(field="owner", op="isnull", value=""))
            is True
        )


class TestARegexOnlyOneEngineCanRun:
    r"""A pattern only one engine understands is refused, not half-answered.

    Neither dialect contains the other, so a pattern can be refused for
    either reason: mostly it parses HERE and errors at the engine (the
    lowered path 400s while this evaluator answers), but ``(?n)`` is the
    reverse -- valid Postgres this evaluator cannot reproduce. Both are the
    same defect, a filter whose answer depends on where it ran.

    The cases below span every class the gate refuses: Python-only escapes,
    named groups, ``(?...)`` groups outside Postgres's closed vocabulary
    (flags, scoped flags, atomic groups, conditionals), possessive
    quantifiers, and Postgres-only flags. Each was measured against live
    PG16, and each arrived a round after the last -- which is why the
    ``(?...)`` rule is a whitelist, not a blacklist.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            "(?P<x>a)",
            r"a\z",
            r"\zfoo",
            # A comment body ends at the FIRST ``)``, backslash or not, so
            # the named group after it is real. Live PG16 errors ("quantifier
            # operand invalid") while Python matches ``'a'``.
            r"(?#\)(?P<x>a)",
            # Python's ASCII / Unicode / locale flags. Postgres rejects all
            # three as an invalid embedded option (measured), and ``(?a)``
            # would additionally narrow ``\w``, which the two otherwise agree
            # on. Of the nine Postgres accepts, only ``i s m x`` stay legal:
            # ``n w b e q`` are refused the other way round, below.
            r"(?a)\w",
            r"(?u)\w",
            # A COMBINED group: the Python-only letter is not first, so a
            # check that read one letter missed it. Live PG16 rejects all
            # three as an invalid embedded option.
            r"(?ia)\w",
            r"(?iu)\w",
            r"(?ia:\w)",
            # A SCOPED flag group. Postgres accepts ``(?i)`` and rejects
            # ``(?i:a)`` -- the colon form is an invalid embedded option to it
            # whatever the letter, so a whitelist of letters is not enough.
            r"(?i:a)",
            r"(?s:a)",
            # Flag-unset forms. Live PG16 errors on every one, so refusing
            # them is agreement, not over-caution.
            r"(?i-m)a",
            r"(?-i)a",
            r"(?i-m:a)",
            # Python extensions with no POSIX counterpart at all. Each parses
            # in Python and is an error to live PG16 ("quantifier operand
            # invalid"), so each answered here where the lowered path 400s.
            r"(?>x)",
            r"(x)?(?(1)y|z)",
            r"a*+",
            r"a++",
            r"a{2,}+",
            # Malformed bodies are still repetitions to neither engine, but
            # live PG16 ERRORS on all three ("invalid regular expression"),
            # so refusing them matches it.
            r"a{2,,3}+",
            r"a{2,3,}+",
            r"a{2,3,4}+",
        ],
    )
    def test_it_is_refused(self, pattern: str) -> None:
        with pytest.raises(ValueError, match="Postgres"):
            Filter(field="title", op="re", value=pattern)

    @pytest.mark.parametrize("flag", ["n", "w", "b", "e", "q"])
    def test_a_scoped_postgres_only_flag_says_so_too(self, flag: str) -> None:
        # The scoped branch used to suggest ``(?n)`` as the fix -- which THIS
        # validator refuses on the next line. A message must not name a
        # replacement its own caller will bounce.
        with pytest.raises(ValueError, match="Python does not implement"):
            Filter(field="title", op="re", value=f"(?{flag}:a)")

    @pytest.mark.parametrize("flag", ["n", "w", "b", "e", "q"])
    def test_a_postgres_only_flag_says_so(self, flag: str) -> None:
        # These are VALID Postgres (live PG16 runs ``(?n)a``) and Python
        # cannot compile them. The refusal is right, but calling a working
        # Postgres pattern "invalid regex" sends the caller to fix the wrong
        # thing.
        with pytest.raises(ValueError, match="Python does not implement"):
            Filter(field="title", op="re", value=f"(?{flag})a")

    def test_the_locale_flag_is_refused_as_a_dialect_gap(self) -> None:
        # Both engines reject ``(?L)`` -- Python "cannot use 'L' flag with a
        # str pattern", Postgres "invalid embedded option" -- so either
        # message would be true. The dialect one is checked first because it
        # names the construct rather than the parse failure.
        with pytest.raises(ValueError, match="not a construct Postgres"):
            Filter(field="title", op="re", value=r"(?L)\w")

    def test_a_named_backreference_is_refused_as_a_dialect_gap(self) -> None:
        # Both engines reject ``(?P=x)`` -- Python "unknown group name",
        # Postgres "invalid embedded option". Named groups are the more
        # specific diagnosis, so that gate runs before the compile check.
        with pytest.raises(ValueError, match="named groups are Python-only"):
            Filter(field="title", op="re", value="(?P=x)")

    @pytest.mark.parametrize(
        "pattern",
        # Each verified to parse in BOTH engines against a live PG16 --
        # including ``(?#c)``, which is a comment to each, so accepting it is
        # not a gap.
        [
            r"\yalpha",
            "^a",
            "[0-9]+",
            r"\d{3}",
            "(?i)abc",
            "(?s).",
            "(?m)^a",
            # Lookaround and non-capturing groups open with ``(?`` but set no
            # flags; live PG16 runs all four. Reading their punctuation as
            # flag letters refused them.
            "(?=a)a",
            "(?!b)a",
            "(?<=a)b",
            "(?<!x)a",
            # Quantifiers PG does implement, including the LAZY ``+?`` that
            # differs from the possessive ``*+`` by one character, and the
            # escaped and in-class forms that are not quantifiers at all.
            "a+?",
            r"a\*+",
            "[+*]+",
            "a{2,3}",
            # ``*+`` as inert TEXT rather than a quantifier: adjacent members
            # of a class, or comment prose. Live PG16 runs all four.
            "[*+]",
            "[?+]",
            "[}+x]",
            "(?#*+)a",
            # A bare ``}`` is a literal, not a quantifier close, so ``}+`` is
            # one-or-more braces -- live PG16 matches ``'a}}'`` with ``a}+``
            # and ``'{key}'`` with ``{key}+``.
            "a}+",
            "}+",
            "{key}+",
            # ``{,3}`` is literal text to Postgres, not a repetition, so the
            # trailing ``+`` quantifies the brace -- it runs. ``a{2,}+`` is
            # the one that errors, and is refused below.
            "a{,3}+",
            "a{key}+",
            # Repetition bounds are ASCII digits. ``{٢,٣}`` is literal text to
            # both engines, so the ``+`` quantifies the brace and live PG16
            # runs it -- ``str.isdigit()`` says those ARE digits, which is the
            # trap.
            "a{\u0662,\u0663}+",
            "(?#c)a",
            "(?:a)",
            # ``(?P<`` as literal TEXT, not a group opener: inside a class it
            # is four ordinary members, and inside a comment it is prose.
            # Live PG16 matches ``'Px'`` with the first, exactly as Python
            # does -- a substring search for "(?P<" refused both.
            "[(?P<]x",
            "(?#(?P<)a",
            # ``(?a)`` as literal TEXT: inside a class those are five ordinary
            # members, and live PG16 matches ``'('`` with it. In a comment the
            # body ends at the FIRST ``)``, so the example carries none of its
            # own -- both engines run it.
            "[(?a)]",
            "(?#(?a)a",
            # A leading ``]`` is a literal MEMBER, so the class is still open
            # and the ``(?P<`` inside it is four more members. Live PG16
            # matches ``'Px'``, as Python does. Every hand-rolled bracket walk
            # has missed this rule; the shared scan is what knows it.
            "[](?P<]x",
        ],
    )
    def test_a_pattern_both_engines_run_still_constructs(self, pattern: str) -> None:
        assert Filter(field="title", op="re", value=pattern).value == pattern


class TestExpandedModeCommentsAreNotSyntax:
    r"""``(?x)`` comment prose must not trip a syntax rule.

    Live PG16 runs all three below and Python agrees, so each refusal removed
    a working pattern. ``#`` opens a comment only under the flag, which is why
    the scan has to answer it rather than each rule searching the text.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            # Read as a possessive quantifier; it is comment text.
            "(?x)a # *+\nb",
            # Read as a Python-only ``(?a)`` group; comment text.
            "(?x)a#(?a)\nb",
            # Read as a non-ASCII case fold; comment text, which never matches.
            "(?xi)a # \u00e9\nb",
        ],
    )
    def test_a_pattern_both_engines_run_still_constructs(self, pattern: str) -> None:
        assert Filter(field="title", op="re", value=pattern).value == pattern

    @pytest.mark.parametrize(
        "pattern",
        [
            # Outside a comment the same text IS syntax, flag or no flag: live
            # PG16 errors on every one ("quantifier operand invalid").
            "(?x)a*+b",
            "a*+",
            r"(?x)a\#*+",
        ],
    )
    def test_the_same_text_outside_a_comment_is_still_refused(
        self, pattern: str
    ) -> None:
        with pytest.raises(ValueError, match="possessive"):
            Filter(field="title", op="re", value=pattern)


class TestCaseFoldingAgainstANonAsciiRow:
    r"""``(?i)`` over a non-ASCII VALUE is refused where the value is seen.

    The wire type gates on the pattern, which is all it has. The evaluator
    also has the row, and that is where the remaining half of the divergence
    lives: live PG16 says ``'\u0130' ~ '(?i)i'`` is FALSE and Python says
    true, with an ASCII pattern. Documenting that as a residual was wrong --
    this layer can see it.
    """

    def test_a_non_ascii_value_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="case-fold"):
            match_filter(
                {"title": "\u0130"}, Filter(field="title", op="re", value="(?i)i")
            )

    def test_an_ascii_value_still_evaluates(self) -> None:
        row = {"title": "ABC"}
        assert (
            match_filter(row, Filter(field="title", op="re", value="(?i)abc")) is True
        )

    def test_a_non_ascii_value_without_folding_still_evaluates(self) -> None:
        # No ``(?i)``, no fold, no divergence.
        row = {"title": "\u0130"}
        assert match_filter(row, Filter(field="title", op="re", value="\u0130")) is True


class TestAnInBracketNegatedShorthand:
    r"""``[\D]`` and ``[\S]`` cannot be translated, so they are refused.

    Outside a class, ``\D`` becomes ``[^0-9]``. INSIDE one it would have to be
    a member meaning "not a digit", which Python's syntax cannot spell --
    ``[^0-9]`` negates the whole class. Left untranslated the two engines
    answer OPPOSITELY: live PG16 says ``E'\u0666' ~ '[\D]'`` is true where
    Python says false.
    """

    @pytest.mark.parametrize("pattern", [r"[\D]", r"[\S]", r"[\D5]", r"[\Sx]"])
    def test_it_is_refused(self, pattern: str) -> None:
        with pytest.raises(ValueError, match="inside a bracket"):
            Filter(field="title", op="re", value=pattern)

    @pytest.mark.parametrize("pattern", [r"\D", r"\S", r"[\d]", r"[\s]", r"[\w]"])
    def test_the_translatable_forms_still_construct(self, pattern: str) -> None:
        assert Filter(field="title", op="re", value=pattern).value == pattern


class TestAnUnknownFilterFieldIsRefused:
    r"""A field no column answers is a typo, not a filter over NULLs.

    ``canonical_filter_field`` passes an unknown name through, no shape
    classifies it, and the clause lands in Python -- where ``row.get`` returns
    ``None`` for a key the row never had. Absent means "no affirmative
    predicate holds", so ``ne`` KEEPS the row: ``owenr ne nobody`` answered
    true for every row in the table.

    SQL cannot make that mistake; ``WHERE owenr ...`` is an error there. The
    route already whitelists, but the store, the CLI, and a direct ``Filter``
    do not go through the route.
    """

    @pytest.mark.parametrize("op", ["is", "ne", "re", "nre", "lt"])
    def test_an_unknown_field_is_refused(self, op: str) -> None:
        with pytest.raises(ValidationError, match="unknown filter field"):
            match_filter(
                {"title": "x"},
                _BareFilter(field="owenr", op=cast("FilterOp", op), value="nobody"),
            )

    @pytest.mark.parametrize("op", ["isnull", "notnull"])
    def test_a_presence_op_on_an_unknown_field_is_refused(self, op: str) -> None:
        # The presence ops carry no operand, so they reach the field check
        # rather than the operand one.
        with pytest.raises(ValidationError, match="unknown filter field"):
            match_filter(
                {"title": "x"},
                _BareFilter(field="owenr", op=cast("FilterOp", op), value=""),
            )

    def test_a_known_field_absent_from_this_row_still_evaluates(self) -> None:
        # A NULLABLE column the row simply does not carry is not a typo: the
        # column exists, so absent means NULL and ``ne`` keeps the row, exactly
        # as SQL's ``IS DISTINCT FROM`` does.
        assert match_filter({"title": "x"}, Filter(field="owner", op="ne", value="Dan"))

    def test_a_cli_alias_is_not_an_unknown_field(self) -> None:
        # ``priority`` canonicalizes to ``issue_priority``; refusing it would
        # break every CLI filter.
        assert isinstance(
            match_filter(
                {"issue_priority": 5}, Filter(field="priority", op="is", value="5")
            ),
            bool,
        )


class TestAnOperandTheTemplateCannotTake:
    r"""An order op is legal only for an operand its SQL could accept.

    The numeric templates cast the operand (``{col} < $1::numeric``), so a
    non-numeric one is not a slow answer -- it is a live-engine ERROR
    (``invalid input syntax for type numeric: "abc"``, SQLSTATE 22P02) that
    ``regex_failures_as_400`` does not catch, so the caller's typo returns
    500. Python meanwhile answers ``"5" < "abc"`` -> True.

    The timestamp templates compare TEXT, casting nothing, so the same
    operands are perfectly well defined there and stay legal. The rule is
    about the TEMPLATE's operand, never about the op alone.
    """

    @pytest.mark.parametrize("value", ["abc", "", "1,5", "0x10"])
    def test_a_non_numeric_operand_is_refused_on_a_numeric_column(
        self, value: str
    ) -> None:
        with pytest.raises(ValidationError, match="numeric"):
            match_filter({"seq": 5}, _BareFilter(field="seq", op="lt", value=value))

    @pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
    def test_nan_is_refused_on_a_numeric_column(self, op: FilterOp) -> None:
        with pytest.raises(ValidationError, match="NaN"):
            match_filter({"seq": 5}, _BareFilter(field="seq", op=op, value="nan"))

    def test_a_numeric_operand_still_orders(self) -> None:
        assert match_filter({"seq": 5}, Filter(field="seq", op="lt", value="9")) is True

    @pytest.mark.parametrize("value", ["1e-400", "-1e-400"])
    def test_an_operand_too_small_for_a_float_column_is_refused(
        self, value: str
    ) -> None:
        # The mirror of the overflow case, and the one a ceiling check alone
        # misses: live PG16 answers 22003 for ``1e-400`` too, while Python
        # rounds it to ``0.0`` and answers.
        with pytest.raises(ValidationError, match="out of range"):
            match_filter(
                {"belief_confidence": 0.5},
                _BareFilter(field="belief_confidence", op="lt", value=value),
            )

    @pytest.mark.parametrize("value", ["5e-324", "1e-320", "1e308", "-1e308", "0"])
    def test_a_representable_float_operand_is_allowed(self, value: str) -> None:
        # Each round-trips through float8, so live PG16 answers rather than
        # erroring; refusing them would remove capability the engine has.
        assert isinstance(
            match_filter(
                {"belief_confidence": 0.5},
                _BareFilter(field="belief_confidence", op="lt", value=value),
            ),
            bool,
        )

    @pytest.mark.parametrize("value", ["1e400", "-1e400", "1e309"])
    def test_an_operand_too_large_for_a_float_column_is_refused(
        self, value: str
    ) -> None:
        # ``belief_confidence`` is DOUBLE PRECISION, so its template compares
        # ``{col}::float8``: live PG16 answers "out of range for type double
        # precision" (SQLSTATE 22003) rather than a row. Python read the same
        # operand as ``inf`` and answered true, so the two select different
        # rows -- and the operand parses as ``numeric`` perfectly well, which
        # is why the earlier guard let it through.
        with pytest.raises(ValidationError, match="out of range"):
            match_filter(
                {"belief_confidence": 0.5},
                _BareFilter(field="belief_confidence", op="lt", value=value),
            )

    @pytest.mark.parametrize("value", ["1e400", "-1e400"])
    def test_the_same_operand_is_legal_on_an_integer_column(self, value: str) -> None:
        # ``seq`` is INTEGER, whose template compares as ``numeric`` -- which
        # has no such ceiling. Live PG16 answers ``5 < '1e400'::numeric`` true,
        # so refusing it here would remove capability the engine has.
        assert isinstance(
            match_filter({"seq": 5}, _BareFilter(field="seq", op="lt", value=value)),
            bool,
        )

    @pytest.mark.parametrize("value", ["inf", "-inf"])
    def test_infinity_is_still_legal_on_a_float_column(self, value: str) -> None:
        # ``'inf'::float8`` is representable, so both engines answer.
        assert isinstance(
            match_filter(
                {"belief_confidence": 0.5},
                _BareFilter(field="belief_confidence", op="lt", value=value),
            ),
            bool,
        )

    @pytest.mark.parametrize(
        ("row_value", "op", "operand", "postgres_says"),
        [
            # ``numeric`` keeps every digit; ``float`` has 53 bits. Live PG16
            # says ``1 < '1.00000000000000001'::numeric`` is TRUE, and Python
            # rounds the operand to exactly 1.0 and says false. Both ANSWER,
            # so nothing downstream catches it.
            (1, "lt", "1.00000000000000001", True),
            (1, "ge", "0.99999999999999999", True),
            (2, "gt", "1.99999999999999999", True),
            (2, "le", "2.00000000000000001", True),
        ],
    )
    def test_a_high_precision_operand_compares_as_postgres_does(
        self, row_value: int, op: FilterOp, operand: str, postgres_says: bool
    ) -> None:
        got = match_filter(
            {"seq": row_value}, _BareFilter(field="seq", op=op, value=operand)
        )
        assert got is postgres_says

    @pytest.mark.parametrize(
        "value", ["\uff11", "\u0661", "1_", "_1", "1__0", "5e", "1.2.3"]
    )
    def test_an_operand_postgres_cannot_parse_is_refused(self, value: str) -> None:
        # ``float()`` accepts every one; live PG16 rejects every one with
        # SQLSTATE 22P02, which no handler maps -- so each was a 500. The
        # guard must ask what ``numeric`` accepts, not what ``float`` does:
        # the fullwidth and Arabic-Indic digits both read as 1 to Python, and
        # a digit separator is single and interior to Postgres.
        with pytest.raises(ValidationError, match="not a number"):
            match_filter({"seq": 5}, _BareFilter(field="seq", op="lt", value=value))

    @pytest.mark.parametrize(
        "value", ["  5  ", "+5", ".5", "5.", "1e5", "1E+5", "1_000", "12_345.6_7"]
    )
    def test_an_operand_postgres_does_parse_is_allowed(self, value: str) -> None:
        # Each is valid ``numeric`` to live PG16 -- including the interior
        # digit separators -- so refusing them would remove capability for
        # nothing.
        assert isinstance(
            match_filter({"seq": 5}, _BareFilter(field="seq", op="lt", value=value)),
            bool,
        )

    @pytest.mark.parametrize("value", ["nan", "abc"])
    def test_a_timestamp_column_takes_any_text(self, value: str) -> None:
        # Live PG16 compares these as text and answers true for both, exactly
        # as Python does; refusing them would remove capability for nothing.
        row = {"created": "2026-01-01 00:00:00+00:00"}
        assert (
            match_filter(row, _BareFilter(field="created", op="lt", value=value))
            is True
        )


class TestOpsThatStillEvaluate:
    @pytest.mark.parametrize(
        ("field", "value", "pattern", "expected"),
        [
            ("owner", "Dan", "Dan", True),
            ("status", "complete", "^(complete|abandoned)$", True),
            ("title", "alpha", "beta", False),
        ],
    )
    def test_a_bounded_regex_is_not_refused(
        self, field: str, value: str, pattern: str, expected: bool
    ) -> None:
        # The refusal is about the PATTERN's cost, not about regex: a pattern
        # whose match is bounded runs as it always did.
        row = {field: value}
        assert (
            match_filter(row, Filter(field=field, op="re", value=pattern)) is expected
        )

    def test_an_array_regex_still_evaluates(self) -> None:
        # knowop2 filters boards this way; it must keep working.
        row = {"labels": ["board:study059:triage"]}
        assert (
            match_filter(row, Filter(field="labels", op="re", value="^board:study059:"))
            is True
        )

    def test_equality_on_a_non_lowering_column_is_unaffected(self) -> None:
        # Stringifies once; its cost is bounded by the value's size.
        row = {"experiment_config": {"lr": 0.1}}
        assert (
            match_filter(row, Filter(field="experiment_config", op="is", value="x"))
            is False
        )

    @pytest.mark.parametrize("op", ["isnull", "notnull"])
    def test_presence_ops_are_unaffected(self, op: FilterOp) -> None:
        assert isinstance(
            match_filter({"owner": None}, Filter(field="owner", op=op, value="")), bool
        )


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
