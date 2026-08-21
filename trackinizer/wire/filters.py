"""Filter shapes for the list endpoint.

The list endpoint accepts zero or more ``filter`` query parameters, each
a JSON object decoded to :class:`Filter`. A filter is a triple: a
canonical column name (matching the Inquiry dataclass in
:mod:`types.inquiries`), one of the ``FilterOp`` literals, and a string
value.

The CLI accepts ergonomic aliases (``kind`` for ``issue_kind``,
``agent-cost`` for ``marginal_cost_agent_usd``, and so on) and translates
them before sending; the route canonicalizes again through the same
:func:`canonical_filter_field`, so a direct HTTP caller may send either
spelling and both resolve to one column.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Final, Literal

import re
import warnings

from trackinizer.types.columns import (
    column_specs,
    flat_column_specs,
    storage_name,
)
from trackinizer.types.inquiries import (
    INQUIRY_CLASSES,
    KIND_TO_CLASS,
)
from trackinizer.wire.posix_regex import (
    UNTRANSLATABLE_IN_BRACKET,
    escapes,
    has_posix_bracket_construct,
    has_python_named_group,
    is_flag_run,
    live_indices,
    matchable_indices,
    paren_extensions,
    posix_pattern,
)
from trackinizer.wire.routes import MAX_LIST_LIMIT


__all__ = [
    "FILTER_FIELD_ALIASES",
    "FILTER_OPS",
    "IDENTITY_COLUMNS",
    "MAX_FILTER_VALUE_CHARS",
    "NON_NULLABLE_COLUMNS",
    "ORDER_OPS",
    "REGEX_OPS",
    "VALUELESS_FILTER_OPS",
    "Filter",
    "FilterOp",
    "as_numeric",
    "canonical_filter_field",
    "folds_case",
    "is_nan",
    "validate_clause",
    "validate_presence_op",
    "validate_regex_dialect",
]


# Bounds the operand's SIZE, not the cost of running it: compiling the worst
# 512-char pattern measures 0.5ms, while MATCHING ``(a+)+$`` against 24
# characters takes 620ms. Backtracking is a matching cost, bounded by where
# the match runs -- Postgres' statement timeout for a lowered filter, and a
# refusal by ``row_filter.reject_inadmissible`` for one that cannot lower.
# 512 chars fits any real column value or regex (mirrors the message-body caps
# in ``wire_sessions.py``).
MAX_FILTER_VALUE_CHARS: Final = 512

# Escapes Python parses and Postgres refuses outright, mapped to the POSIX
# spelling that means the same thing in both. ``\N{NAME}`` names a codepoint
# in Python and is an "invalid escape \ sequence" to live PG16, which Python
# then MATCHES -- so the literal character is the spelling that works in
# both. Every other multi-character escape form agrees (measured: ``\x41``,
# ``\u0041``, ``\U00000041``, ``\101``, ``\0``).
_PYTHON_ONLY_ESCAPES: Final[Mapping[str, str]] = {
    "z": "use '\\Z' instead",
    "N": "write the character itself instead",
}

# The flag letters Postgres implements, measured on live PG16. Python's
# ``a`` / ``u`` / ``L`` are absent, and ``(?a)`` matters most: it would narrow
# ``\w`` to ASCII, and ``\w`` is the one class the two engines agree on
# Unicode-wide.
_POSTGRES_FLAGS: Final[frozenset[str]] = frozenset("ismnxwbeq")

# The flag letters Python implements. The gap against ``_POSTGRES_FLAGS`` --
# ``n w b e q`` -- is why a valid Postgres pattern can still be refused: this
# evaluator cannot reproduce it (live PG16 runs ``(?n)a``, Python says
# "unknown extension ?n").
_PYTHON_FLAGS: Final[frozenset[str]] = frozenset("ismx")

# The whitespace Postgres' ``numeric`` parser accepts as padding, measured
# against live PG16: the six ASCII characters and nothing wider.
_ASCII_WHITESPACE: Final = " \t\n\r\v\f"

# Repetition bounds are ASCII digits, not anything ``str.isdigit()`` accepts.
_ASCII_DIGITS: Final[frozenset[str]] = frozenset("0123456789")

# What Postgres' ``numeric`` input parser accepts, transcribed from a scan of
# 14,424 candidate operands against live PG16. It is NOT ``float()``, which
# reads ``\uff11`` and ``\u0661`` as 1 (Postgres: SQLSTATE 22P02) and takes
# ``1_``, ``_1`` and ``1__0`` as underscore-separated where Postgres rejects
# all three -- a separator there is single and interior, which the inner
# ``_?`` spells.
_NUMERIC_DIGITS: Final = r"[0-9](?:_?[0-9])*"
_NUMERIC_OPERAND: Final[re.Pattern[str]] = re.compile(
    rf"\A[-+]?(?:(?:{_NUMERIC_DIGITS})(?:\.(?:{_NUMERIC_DIGITS})?)?"
    rf"|\.(?:{_NUMERIC_DIGITS}))(?:[eE][-+]?{_NUMERIC_DIGITS})?\Z"
)

# ``numeric`` also takes these by name, case-insensitively -- live PG16 parses
# ``inf``, ``INFINITY`` and ``NaN``. ``nan`` is refused separately for
# ORDERING, because the two engines sort it differently.
_NUMERIC_NAMES: Final[frozenset[str]] = frozenset(
    {"nan", "inf", "-inf", "+inf", "infinity", "-infinity", "+infinity"}
)

# The two escapes both engines accept while MEANING different things, each
# mapped to what Postgres reads, what Python reads, and the POSIX spelling
# that works in both. Live PG16: ``chr(8) ~ '\b'`` is true (backspace) and
# ``'a\b' ~ '\B'`` is true (a literal backslash). Neither can be translated,
# so a filter carrying one is refused.
#
# The ambiguity is a property of the escape's POSITION: inside a bracket
# expression neither engine has a boundary to mean and both read BACKSPACE
# (live: ``chr(8) ~ '[\b]'`` true, ``'b' ~ '[\b]'`` false -- identical to
# Python), so :func:`validate_regex_dialect` asks the scanner where it sits.
_AMBIGUOUS_ESCAPES: Final[Mapping[str, tuple[str, str, str]]] = {
    "b": ("a backspace", "a word boundary", r"\y"),
    "B": ("a literal backslash", "a non-word-boundary", r"\Y"),
}

REGEX_OPS: Final[frozenset[FilterOp]] = frozenset({"re", "nre"})
"""Ops whose operand is a pattern, matched rather than compared."""

ORDER_OPS: Final[frozenset[FilterOp]] = frozenset({"lt", "le", "gt", "ge"})
"""Ops that compare magnitude."""


@lru_cache(maxsize=2 * MAX_LIST_LIMIT)
def validate_clause(field: str, op: str, value: str) -> str | None:
    r"""Reject a filter clause no row could make meaningful, else ``None``.

    Every rule decidable from the clause ALONE lives here, so the wire type
    and the row evaluator enforce one contract rather than a subset each: a
    structural ``RowFilter`` reaches ``match_filter`` without ever running
    ``Filter.__post_init__``.

    What needs the column's SQL -- whether the clause lowers, and whether the
    operand suits the template it lowers to -- is NOT decidable here; that is
    :func:`row_filter.reject_inadmissible`'s half.

    CACHED, because ``match_filter`` re-asks per ROW while the clause is
    loop-invariant: a regex clause costs 33us to validate and 4us to answer,
    so an uncached call makes validation 90% of a filtered page. Bounded
    (rather than unbounded) because the key is a caller's operand, and sized
    above the route's per-request filter cap so one request cannot evict its
    own entries.

    Args:
      field: The filter field, canonical or a CLI alias.
      op: A ``FilterOp`` spelling, or anything a structural filter carried.
      value: The operand.

    Returns:
      message: Why the clause is refused, or ``None`` when it is admissible.

    """
    if op not in FILTER_OPS:
        return f"unknown filter op {op!r}; expected one of {sorted(FILTER_OPS)}"
    if op in VALUELESS_FILTER_OPS and value:
        return f"filter op {op!r} takes no value"
    # Bounds the operand's SIZE, not its cost: compiling the worst 512-char
    # pattern measures 0.5ms, while MATCHING can be unbounded.
    if len(value) > MAX_FILTER_VALUE_CHARS:
        return f"filter value exceeds {MAX_FILTER_VALUE_CHARS} characters"
    if op in REGEX_OPS:
        return (
            # Dialect BEFORE compatibility: a construct one engine implements
            # and the other does not deserves that diagnosis, not "invalid
            # regex". ``(?n)a`` is a working Postgres pattern.
            validate_regex_dialect(value)
            or _runs_in_postgres(value)
            or _compilable(value)
        )
    return validate_presence_op(canonical_filter_field(field), op)


def is_nan(value: str) -> bool:
    """Whether ``value`` parses as a NaN, whatever its spelling."""
    parsed = as_numeric(value)
    return parsed is not None and parsed.is_nan()


def as_numeric(value: str) -> Decimal | None:
    r"""Parse ``value`` as Postgres' ``numeric`` does, or ``None``.

    ``Decimal``, not ``float``, because the SQL casts the operand to
    ``numeric`` and ``numeric`` keeps every digit: live PG16 says
    ``1 < '1.00000000000000001'::numeric`` is TRUE where ``float`` rounds the
    operand to exactly ``1.0`` and answers false. Both engines answer, so
    nothing downstream catches the disagreement.

    The ACCEPTED set is Postgres', not ``Decimal``'s either. ``float`` and
    ``Decimal`` both read ``\uff11`` and ``\u0661`` as 1 -- live PG16 raises
    SQLSTATE 22P02, which no handler maps, so the caller got a 500 -- and both
    take ``1_``, ``_1`` and ``1__0``, which Postgres rejects. Transcribed from
    a 14,424-operand scan against a live engine, on which this agrees with
    ``::numeric`` exactly.

    Args:
      value: The raw operand.

    Returns:
      parsed: The value as ``numeric`` reads it, or ``None`` when Postgres
        would reject it outright.

    """
    # ``str.strip()`` would also remove U+00A0, U+2003, U+2007 and U+3000,
    # which Postgres does NOT take as padding: live PG16 answers "invalid
    # input syntax for type numeric" for each, where the stripped operand
    # parsed here and compared. Postgres takes exactly the six ASCII
    # whitespace characters, so the set is spelled rather than inherited.
    stripped = value.strip(_ASCII_WHITESPACE)
    if stripped.lower() in _NUMERIC_NAMES:
        return Decimal(stripped)
    if not stripped.isascii() or _NUMERIC_OPERAND.match(stripped) is None:
        return None
    return Decimal(stripped.replace("_", ""))


def validate_regex_dialect(pattern: str) -> str | None:
    r"""Reject regex escapes the two evaluators disagree about, else ``None``.

    Returns a message naming the working spelling. Enforced by
    :meth:`Filter.__post_init__`, so the CLI and the server refuse the same
    patterns: a pattern that reaches an evaluator must mean one thing.

    Scans for escapes rather than substring-searching: ``\\b`` is a literal
    backslash then ``b`` (no escape at all), and ``[\b]`` is an unambiguous
    backspace in both engines. Refusing either is refusing a pattern that
    always agreed.
    """
    for escape in escapes(pattern):
        if escape.in_bracket:
            continue
        if (found := _AMBIGUOUS_ESCAPES.get(escape.char)) is not None:
            postgres_meaning, python_meaning, replacement = found
            return (
                f"regex escape '\\{escape.char}' is {postgres_meaning} to "
                f"Postgres and {python_meaning} to Python; use "
                f"{replacement!r}, which is {python_meaning} in both"
            )
    return None


FilterOp = Literal["is", "ne", "re", "nre", "lt", "le", "gt", "ge", "isnull", "notnull"]
FILTER_OPS: Final[tuple[FilterOp, ...]] = (
    "is",
    "ne",
    "re",
    "nre",
    "lt",
    "le",
    "gt",
    "ge",
    "isnull",
    "notnull",
)

# Ops that test column presence rather than compare a value. They carry no
# operand: the CLI parser consumes no value token and the server accepts a
# filter object with the ``value`` key absent, materializing ``value=""``.
VALUELESS_FILTER_OPS: Final[frozenset[FilterOp]] = frozenset({"isnull", "notnull"})


# Identity/housekeeping columns the schema declares NOT NULL directly; they
# carry no ColumnSpec, so they can't be derived from the spec metadata below.
IDENTITY_COLUMNS: Final[frozenset[str]] = frozenset(
    {"id", "kind", "seq", "created", "modified"}
)


def _derive_non_nullable_columns() -> frozenset[str]:
    """Canonical columns that are NOT NULL in ``inquiries``.

    Derived from the column specs rather than hand-listed, so a future
    ``required=True`` field (or a new flattened axis) is covered automatically
    instead of silently breaking presence-op validation. A column is NOT NULL
    when its spec is ``required`` (``status``, ``title``) or ``flatten``-ed
    (the cost axes, ``NOT NULL DEFAULT 0`` in ``schema.sql``); everything else
    is a ``| None`` field on the Inquiry dataclass.
    """
    columns = set(IDENTITY_COLUMNS)
    for source in INQUIRY_CLASSES:
        for name, flat in flat_column_specs(source).items():
            if flat.spec.required or flat.spec.flatten is not None:
                columns.add(storage_name(name, flat.spec))
    return frozenset(columns)


# Presence ops (``isnull`` / ``notnull``) on a NOT-NULL column are always-empty
# / always-all -- a silent wrong answer. Refused by :func:`validate_clause`,
# so every path gets it; naming the CLI and the route individually is what let
# the store and a direct ``Filter`` run what those two refused.
NON_NULLABLE_COLUMNS: Final[frozenset[str]] = _derive_non_nullable_columns()


def validate_presence_op(field: str, op: FilterOp) -> str | None:
    """Return an error message if ``op`` is a presence test on a NOT-NULL field.

    ``isnull`` / ``notnull`` only make sense on a nullable column; on a
    NOT-NULL one they silently match nothing / everything. ``field`` must
    already be canonical (post :func:`canonical_filter_field`). Returns
    ``None`` when the pairing is valid.
    """
    if op in VALUELESS_FILTER_OPS and field in NON_NULLABLE_COLUMNS:
        return f"filter field {field!r} is NOT NULL; {op} does not apply"
    return None


def _kind_specific_aliases() -> dict[str, str]:
    """Bare field name -> flat storage column, for every kind-specific column.

    A kind-specific column stores under a ``<kind>_`` prefix (``paper_source``),
    but the CLI filters by the bare field (``source``), the kind already in
    scope. DERIVED from the specs (the same ``storage_name`` rule the schema and
    routes use), so a new kind-specific field is filterable with no edit here --
    the GSI-02 bug class (a bare filter the CLI accepted but the server 400'd
    because its alias was missing) cannot recur.

    This may emit an alias for a column with no CLI filter token today
    (``config`` -> ``experiment_config``, ``opened_by_api_key_id`` -> ...): the
    alias is harmless -- its target is already in the server whitelist (which
    derives from the same specs), so it can never 400, and with no grammar token
    it is unreachable. Deriving the whole set is deliberately preferred over a
    hand-tuned exclusion, which would re-introduce the drift the derivation kills.
    """
    out: dict[str, str] = {}
    for cls in KIND_TO_CLASS.values():
        for name, spec in column_specs(cls).items():
            storage = storage_name(name, spec)
            if storage != name:
                out[name] = storage
    return out


# CLI-ergonomic filter field -> canonical SQL column on ``inquiries``. The one
# place this aliasing is declared; the CLI parser, the server route, and the row
# evaluator all canonicalize through :func:`canonical_filter_field`. The
# kind-specific bare->storage aliases (``source`` -> ``paper_source``, ...) are
# DERIVED (:func:`_kind_specific_aliases`); only the non-derivable ergonomic
# spellings (alternate names, cost axes) are hand-declared here.
FILTER_FIELD_ALIASES: Final[Mapping[str, str]] = {
    "kind": "issue_kind",
    "issuekind": "issue_kind",
    "label": "labels",
    "subscriber": "subscribers",
    "agent-cost": "marginal_cost_agent_usd",
    "resource-cost": "marginal_cost_resource_usd",
    **_kind_specific_aliases(),
}


def canonical_filter_field(field: str) -> str:
    """Resolve a CLI filter-field alias to its canonical SQL column name.

    A canonical column or an unknown field passes through unchanged, so
    callers can canonicalize unconditionally and the server still rejects
    genuinely-unknown fields with a precise error.
    """
    return FILTER_FIELD_ALIASES.get(field, field)


@dataclass(frozen=True, kw_only=True, slots=True)
class Filter:
    """One ``field op value`` clause as the wire carries it.

    ``field`` is the canonical column name from :mod:`types.inquiries`,
    matching the SQL column on the ``inquiries`` table. The CLI translates
    its ergonomic aliases before constructing this.
    """

    field: str
    op: FilterOp
    value: str

    def __post_init__(self) -> None:
        if (err := validate_clause(self.field, self.op, self.value)) is not None:
            raise ValueError(err)


def _compilable(pattern: str) -> str | None:
    r"""Reject a pattern Python cannot compile, in its TRANSLATED form.

    The evaluator compiles ``posix_pattern(value)``, so the TRANSLATED form is
    what must compile: checking the raw text would refuse a ``\\y`` both
    engines accept.

    A POSIX bracket construct is refused before that: Postgres implements
    ``[:class:]`` / ``[.x.]`` / ``[=x=]`` and Python does not, so the two
    select different rows (live PG16: ``'x9' ~ '[[:digit:]]'`` is true, Python
    false). It is refused structurally rather than by the ``FutureWarning``
    Python emits, since CPython serves a cached pattern before parsing and one
    earlier compile silences that warning for the process.
    """
    if has_posix_bracket_construct(pattern):
        return (
            f"regex {pattern!r} uses a POSIX bracket construct "
            "([:class:], [.x.], or [=x=]) that Postgres implements and Python "
            "does not, so the two evaluators would select different rows"
        )
    try:
        with warnings.catch_warnings():
            # ``[[]`` is a literal ``[`` class both engines agree on (live
            # PG16 matches ``'a[b'``, as Python does) and Python only warns
            # about. The construct that does NOT agree is refused above, so
            # the warning carries no verdict -- and under this repo's
            # ``filterwarnings = ["error"]`` it would raise on a valid pattern.
            warnings.simplefilter("ignore", FutureWarning)
            re.compile(posix_pattern(pattern))
    except (re.error, OverflowError) as err:
        # A huge repetition bound (``a{999999999999,}``) raises OverflowError,
        # not re.error, so catching only the latter lets a caller typo escape
        # as a 500 where every other malformed pattern earns a 400.
        return f"invalid regex {pattern!r}: {err}"
    return None


def _runs_in_postgres(pattern: str) -> str | None:
    r"""Reject a pattern only one of the two engines can run, else ``None``.

    Postgres runs POSIX ARE and Python does not, and neither dialect contains
    the other. Whichever way the gap falls the result is the same: one
    evaluator answers where the other 400s, and the caller cannot tell which
    ran. Five classes, each measured on live PG16:

    * escapes Python alone has (``\z``);
    * named groups and backreferences (``(?P<x>...)``, ``(?P=x)``);
    * ``(?...)`` groups outside the closed set Postgres implements, which
      covers atomic groups, conditionals, and every scoped ``(?i:...)`` form;
    * possessive quantifiers (``a*+``), added in Python 3.11;
    * flags POSTGRES alone has (``(?n)``) -- the one class that runs on the
      lowered path and cannot be reproduced here, so it is refused for the
      opposite reason to the rest.

    A sixth lives in :func:`_untranslatable_bracket_member`: ``[\D]`` parses
    in both and ANSWERS differently, which needs no dialect gap at all.

    The ``(?...)`` rule is a WHITELIST of what Postgres implements, so a
    construct nobody has met yet is refused rather than admitted. The rest of
    POSIX's grammar is not enumerable, so the route still maps a Postgres-side
    failure through ``regex_failures_as_400``.
    """
    for found in escapes(pattern):
        if not found.in_bracket and found.char in _PYTHON_ONLY_ESCAPES:
            return (
                f"regex escape '\\{found.char}' is Python-only; Postgres "
                "rejects it, so the two evaluators would disagree -- "
                f"{_PYTHON_ONLY_ESCAPES[found.char]}"
            )
    if (paren_err := _unsupported_paren_extension(pattern)) is not None:
        return paren_err
    if (quantifier := _possessive_quantifier(pattern)) is not None:
        return (
            f"regex possessive quantifier {quantifier!r} is Python-only; "
            "Postgres rejects it as an invalid quantifier operand, so the two "
            "evaluators would disagree"
        )
    if (unterminated := _unterminated_repetition(pattern)) is not None:
        return (
            f"regex repetition {unterminated!r} is never closed; Postgres "
            "rejects it as an invalid regular expression while Python reads "
            "the brace as a literal and matches, so the two evaluators would "
            "disagree. Close it with '}' or escape the brace"
        )
    if _folds_non_ascii(pattern):
        return (
            "regex flag 'i' case-folds non-ASCII text differently in the two "
            "engines, which both answer rather than error: Python matches "
            "'i' against U+0130 and Postgres does not (6 of 10 measured pairs "
            "disagree). Drop '(?i)', or keep the pattern ASCII"
        )
    if (member := _untranslatable_bracket_member(pattern)) is not None:
        return (
            f"regex shorthand '\\{member}' inside a bracket expression has no "
            "Python spelling as a class member, and the two engines answer it "
            f"oppositely (Postgres matches non-ASCII, Python does not). Use "
            f"'\\{member}' outside the brackets, or spell the members out"
        )
    if has_python_named_group(pattern):
        return (
            "named groups are Python-only; Postgres rejects '(?P<...>' as an "
            "invalid embedded option, so the two evaluators would disagree"
        )
    return None


def _unsupported_paren_extension(pattern: str) -> str | None:
    r"""Refuse a ``(?...)`` group the two engines do not share, else ``None``.

    Postgres implements a CLOSED set of these, measured on live PG16:
    ``(?=`` ``(?!`` ``(?:`` ``(?<=`` ``(?<!`` ``(?#`` and an UNSCOPED run of
    the flag letters ``i s m n x w b e q``. Everything else is an error --
    ``(?>`` atomic groups, ``(?(`` conditionals, ``(?a)`` / ``(?u)`` / ``(?L)``,
    and every scoped ``(?i:...)`` form, all of which Python parses happily.

    Checked as that closed set rather than as a blacklist, so an extension
    nobody has enumerated is refused rather than admitted.
    """
    for found in paren_extensions(pattern):
        if found.body.startswith(("=", "!", "<=", "<!")):
            continue
        if found.body == "":
            # ``(?)`` and ``(?:)``; both engines settle these themselves.
            continue
        if not set(found.body) <= _POSTGRES_FLAGS:
            return (
                f"regex group '(?{found.body}{':...' if found.scoped else ''})' "
                "is not a construct Postgres implements, so the two "
                "evaluators would disagree. Postgres accepts '(?=' '(?!' "
                "'(?:' '(?<=' '(?<!' '(?#' and the unscoped flags "
                "'i s m n x w b e q'"
            )
        # Checked BEFORE the scoped branch, which would otherwise name a
        # replacement this same function refuses: ``(?n:a)`` cannot be fixed
        # by writing ``(?n)``.
        if unmatched := set(found.body) - _PYTHON_FLAGS:
            return (
                f"regex inline flag '{min(unmatched)}' is a valid Postgres "
                "flag that Python does not implement, so this evaluator "
                "cannot reproduce what the query would return. Use 'i', 's', "
                "'m', or 'x', which both engines share"
            )
        if found.scoped:
            return (
                f"regex group '(?{found.body}:...)' scopes its flags, which "
                "Postgres rejects as an invalid embedded option whatever the "
                f"letters. Set them for the whole pattern with '(?{found.body})'"
            )
    return None


def _untranslatable_bracket_member(pattern: str) -> str | None:
    """Name an in-bracket shorthand with no Python member spelling, else ``None``."""
    for escape in escapes(pattern):
        if escape.in_bracket and escape.char in UNTRANSLATABLE_IN_BRACKET:
            return escape.char
    return None


def _possessive_quantifier(pattern: str) -> str | None:
    r"""Name a ``*+`` / ``++`` / ``?+`` / ``{m,n}+`` quantifier, else ``None``.

    Python 3.11 added them; Postgres has never had them and answers "invalid
    regular expression: quantifier operand invalid" for all four (measured).

    Both characters must be LIVE syntax: an escaped quantifier (``a\*+``),
    two adjacent class members (``[*+]``), and comment prose (``(?#*+)a``) all
    run in PG16 and are not possessive anything.
    """
    live = live_indices(pattern)
    for index in range(len(pattern) - 1):
        if index not in live or index + 1 not in live:
            continue
        if pattern[index + 1] != "+":
            continue
        if pattern[index] in "*+?" or _closes_repetition(pattern, index, live):
            return pattern[index : index + 2]
    return None


def _closes_repetition(pattern: str, index: int, live: frozenset[int]) -> bool:
    """Whether ``pattern[index]`` is the ``}`` ending a ``{m,n}`` repetition.

    A bare ``}`` is an ordinary character in both engines -- live PG16 matches
    ``'a}}'`` with ``a}+`` and ``'{key}'`` with ``{key}+`` -- so only a real
    repetition close counts. The body must START with an ASCII digit:
    Postgres reads ``{,3}`` as literal text and runs ``a{,3}+``, where
    ``a{2,}+`` is the error. ASCII specifically, since ``str.isdigit()``
    accepts ``\u0662`` where a repetition bound does not -- live PG16 runs
    ``a{\u0662,\u0663}+`` as a quantified brace.
    """
    if pattern[index] != "}":
        return False
    opening = pattern.rfind("{", 0, index)
    if opening < 0 or opening not in live:
        return False
    body = pattern[opening + 1 : index]
    return (
        bool(body)
        and body[0] in _ASCII_DIGITS
        and all(char in _ASCII_DIGITS or char == "," for char in body)
    )


def _unterminated_repetition(pattern: str) -> str | None:
    r"""Name a ``{`` that opens a repetition and never closes, else ``None``.

    A ``{`` followed by an ASCII digit is a repetition opener to BOTH engines,
    and live PG16 answers "invalid regular expression" when no ``}`` closes
    it -- while Python reads the brace as a literal and MATCHES, so the
    lowered path 400s where this evaluator returns rows.

    The digit is what makes it an opener, exactly as in
    :func:`_closes_repetition`: live PG16 runs ``a{``, ``a{,`` and ``a{x``,
    where the brace is ordinary text to both.
    """
    live = live_indices(pattern)
    for index, char in enumerate(pattern):
        if char != "{" or index not in live:
            continue
        body = pattern[index + 1 :]
        if not body or body[0] not in _ASCII_DIGITS:
            continue
        closing = body.find("}")
        # A closing brace is not enough: the BODY between them must be a bound
        # Postgres can read. ``.{1{}`` closes and still errors there, while
        # Python reads the whole thing as literal text and matches.
        if closing < 0 or not all(
            char in _ASCII_DIGITS or char == "," for char in body[:closing]
        ):
            return pattern[index:]
    return None


def _folds_non_ascii(pattern: str) -> bool:
    """Whether ``pattern`` asks for a case-insensitive non-ASCII match.

    Python's ``(?i)`` folds by Unicode simple case mapping and Postgres folds
    narrowly, so the two disagree on 6 of 10 measured pairs -- and both ANSWER,
    which is the class nothing downstream catches. Neither ``re.ASCII`` nor the
    default reproduces Postgres's fold, so this cannot be translated.

    Gated on the pattern's MATCHABLE characters, not its live ones: a class
    member is inert to every SYNTAX rule but folds exactly like the bare atom
    -- live PG16 says ``'s' ~ '(?i)[\u017f]'`` is FALSE where Python matches,
    identical to ``(?i)\u017f``. Only a comment body is excluded, being prose
    that never matches: ``(?i)(?#\u00e9)a`` runs in both.

    The other half of the divergence -- an ASCII pattern against a non-ASCII
    VALUE -- needs the row, and is checked by
    :func:`row_filter.reject_inadmissible`, which has one.
    """
    if not folds_case(pattern):
        return False
    return any(not pattern[index].isascii() for index in matchable_indices(pattern))


def folds_case(pattern: str) -> bool:
    """Whether ``pattern`` turns on case-insensitive matching."""
    return any(
        is_flag_run(found.body, scoped=found.scoped, flag="i")
        for found in paren_extensions(pattern)
    )
