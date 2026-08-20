r"""Locate the constructs in a POSIX ARE pattern whose reading is positional.

One scan answers every question this package asks about a pattern, and each
rule filters what it yields: which escapes the two engines read differently
(:func:`escapes`), which spans are class members or comment prose
(:func:`live_indices`, :func:`matchable_indices`), which ``(?...)`` groups
appear (:func:`paren_extensions`), and whether a POSIX bracket form
(:func:`has_posix_bracket_construct`) or a Python named group
(:func:`has_python_named_group`) is present. :func:`posix_pattern` rewrites
what Python spells differently.

A rule may not walk the pattern itself, because these characters do not always
mean what they spell, and each context is a separate POSIX rule:

- After another backslash. ``\\m`` is a LITERAL backslash then ``m``; live
  PG16 confirms ``'a\mb' ~ '\\m'`` is true.
- Inside a bracket expression. ``[\y]`` and ``[\m]`` are ERRORS to Postgres
  (``invalid escape \ sequence``), while ``[\b]`` is an unambiguous BACKSPACE
  in BOTH engines.
- After a LEADING ``]``, which POSIX reads as a literal member rather than the
  terminator, so the class is still open: ``[]\[[:digit:]]`` matches ``'5'``.
- Inside a ``(?#...)`` comment, whose body is prose to both engines.

It is not a regex parser -- it tracks only the backslash, bracket, and comment
structure those questions need.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final


__all__ = [
    "FLAG_LETTERS",
    "UNTRANSLATABLE_IN_BRACKET",
    "Comment",
    "Escape",
    "Inert",
    "NamedGroup",
    "ParenExtension",
    "PosixClass",
    "escapes",
    "has_posix_bracket_construct",
    "has_python_named_group",
    "is_flag_run",
    "live_indices",
    "matchable_indices",
    "paren_extensions",
    "posix_pattern",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class Escape:
    """One backslash escape found in a pattern."""

    char: str
    """The escaped character: the ``y`` of ``\\y``."""

    start: int
    """Index of the introducing backslash, not of ``char``."""

    in_bracket: bool
    """Whether it sits inside a ``[...]`` bracket expression.

    POSIX gives a backslash a different meaning there, so every rule about an
    escape has to ask this before it applies.
    """


@dataclass(frozen=True, kw_only=True, slots=True)
class Inert:
    """A span that is text rather than syntax: class members, comment prose."""

    start: int
    """Index of the first inert character."""

    stop: int
    """Index just past the last inert character."""


@dataclass(frozen=True, kw_only=True, slots=True)
class Comment:
    """A ``(?#...)`` span, whose body is prose to both engines.

    Distinct from :class:`Inert` because the two questions differ: a class
    member is not SYNTAX (so ``[*+]`` carries no possessive quantifier) but it
    IS matchable text (so ``(?i)[\u017f]`` folds exactly as ``(?i)\u017f``
    does). Comment prose is neither.
    """

    start: int
    """Index of the opening ``(``."""

    stop: int
    """Index just past the closing ``)``, or the end of the pattern."""


@dataclass(frozen=True, kw_only=True, slots=True)
class ParenExtension:
    """One ``(?...)`` group, carrying what follows the ``(?``.

    Postgres implements a CLOSED set of these -- ``(?=`` ``(?!`` ``(?:``
    ``(?<=`` ``(?<!`` ``(?#`` and an unscoped flag run -- and errors on every
    other spelling (measured across ``> ( + & | ~ {`` and the scoped ``(?i:``
    form). Python implements strictly more, so a caller decides by that closed
    set rather than by blacklisting extensions one at a time.
    """

    body: str
    """Everything between ``(?`` and the terminating ``)`` or ``:``."""

    scoped: bool
    """Whether a colon terminated it, as in ``(?i:...)``."""

    start: int
    """Index of the opening ``(``."""


@dataclass(frozen=True, kw_only=True, slots=True)
class NamedGroup:
    """A Python ``(?P<name>...)`` group or ``(?P=name)`` backreference.

    Postgres rejects both as an invalid embedded option. Only the group form
    reaches this detector in practice: a bare ``(?P=name)`` names no group, so
    Python cannot compile it either and the compile check refuses it first.
    """

    start: int
    """Index of the opening ``(``."""


@dataclass(frozen=True, kw_only=True, slots=True)
class PosixClass:
    """One POSIX nested bracket form: ``[:class:]``, ``[.x.]``, or ``[=x=]``."""

    marker: str
    """The character after the inner ``[``: ``:``, ``.``, or ``=``."""

    start: int
    """Index of that inner ``[``."""


UNTRANSLATABLE_IN_BRACKET: Final[frozenset[str]] = frozenset({"D", "S"})
"""Shorthands with no member spelling inside a class; refused, not rewritten."""

# Every letter that may appear in an inline-flag run. A ``(?...)`` body made
# only of these is a flag group; anything else is a lookaround or comment
# whose text happens to contain the same characters.
FLAG_LETTERS: Final[frozenset[str]] = frozenset("ismnxwbeq")


def has_posix_bracket_construct(pattern: str) -> bool:
    r"""Whether ``pattern`` uses a POSIX ``[:class:]`` / ``[.x.]`` / ``[=x=]``.

    Postgres implements all three; Python implements none, reading them as
    ordinary set members -- live PG16 says ``'x9' ~ '[[:digit:]]'`` is true
    where Python says false, so the two evaluators select different rows.

    Detected STRUCTURALLY, not by the ``FutureWarning`` Python emits: CPython
    serves a CACHED pattern before parsing, so one earlier compile silences
    the warning for the process and the check stops working.
    """
    return any(isinstance(found, PosixClass) for found in _scan(pattern))


def live_indices(pattern: str) -> frozenset[int]:
    r"""Indices that are pattern SYNTAX, not class members or comment prose.

    A position is inert when it is a class member, a ``(?#...)`` comment body,
    or either half of an escape pair -- live PG16 runs ``[*+]``, ``(?#*+)a``
    and ``a\*+``, none of which carries a possessive quantifier. Any rule
    about a bare character needs that distinction, because the character
    itself looks identical either way.
    """
    inert: set[int] = set()
    for found in _scan(pattern):
        if isinstance(found, Inert):
            inert.update(range(found.start, found.stop))
    return frozenset(set(range(len(pattern))) - inert)


def matchable_indices(pattern: str) -> frozenset[int]:
    r"""Indices that take part in MATCHING: everything but comment prose.

    A different question from :func:`live_indices`. A class member is not
    syntax -- ``[*+]`` carries no possessive quantifier -- but it is text the
    engine matches, and under ``(?i)`` it case-folds exactly as the bare atom
    does: live PG16 says ``'s' ~ '(?i)[\u017f]'`` is FALSE where Python
    matches, identical to the unbracketed ``(?i)\u017f``.

    Only a ``(?#...)`` body is excluded, being prose that never matches
    anything -- live PG16 runs ``(?i)(?#\u00e9)a``.
    """
    inert: set[int] = set()
    for found in _scan(pattern):
        if isinstance(found, Comment):
            inert.update(range(found.start, found.stop))
    return frozenset(set(range(len(pattern))) - inert)


def paren_extensions(pattern: str) -> Iterator[ParenExtension]:
    """Yield each ``(?...)`` group, with the text that decides its meaning.

    Scanned, not substring-searched: ``[(?a)]`` is five ordinary class members
    and the ``(?a`` in ``(?#(?a)a`` is comment text -- live PG16 runs both, so
    refusing either would refuse a working pattern. A comment body ends at the
    FIRST ``)``, which is why that example carries no closing paren of its own.
    """
    return (found for found in _scan(pattern) if isinstance(found, ParenExtension))


def has_python_named_group(pattern: str) -> bool:
    r"""Whether ``pattern`` uses Python's ``(?P...)`` syntax, group or backref.

    Postgres rejects the construct outright ("invalid embedded option"), so a
    pattern carrying one means different things to the two evaluators. Inert
    spellings are not one: ``[(?P<]x`` and ``[](?P<]x`` are class members and
    match ``'Px'`` in BOTH engines.
    """
    return any(isinstance(found, NamedGroup) for found in _scan(pattern))


def escapes(pattern: str) -> Iterator[Escape]:
    """Yield only the escapes from the shared scan."""
    return (found for found in _scan(pattern) if isinstance(found, Escape))


def is_flag_run(body: str, *, scoped: bool, flag: str) -> bool:
    """Whether a ``(?...)`` body turns ``flag`` on for the whole pattern.

    Three rules ask this -- ``(?m)`` for the ``$`` anchor, ``(?i)`` for case
    folding, ``(?x)`` for comments -- and each got it wrong differently before:
    one read every ``(?...)`` body, so a ``(?=m)`` lookahead disabled the
    anchor fix; another ignored ``scoped``, so ``(?i:a)`` claimed to fold a
    pattern Postgres refuses outright. The body must be flag letters ONLY, and
    a colon-terminated group scopes its flags to its own body.
    """
    return flag in body and not scoped and set(body) <= FLAG_LETTERS


def posix_pattern(pattern: str) -> str:
    r"""Rewrite POSIX-only constructs into their Python equivalents.

    Rewrites only what is an escape AND outside a bracket expression, a
    distinction ``str.replace`` cannot draw: ``\\m`` is a literal backslash
    then ``m`` (live: ``'a\mb' ~ '\\m'`` is true), and ``[\y]`` / ``[\m]`` are
    errors to Postgres rather than boundaries.

    The rewrites are collected by index and applied in ONE ordered pass, so
    two rules cannot rewrite the same span twice.
    """
    # ``.`` matches a newline in Postgres and not in Python: live PG16 says
    # ``E'a\nb' ~ 'a.b'`` is TRUE. Neither engine errors, so nothing catches
    # the disagreement downstream -- it must be closed by translation rather
    # than refusal. ``(?s)`` is Python's spelling of that default and composes
    # with a caller's own leading ``(?i)``.
    replacements: dict[int, tuple[int, str]] = {}
    # ``$`` anchors at end-of-STRING in Postgres and before a final newline in
    # Python: live PG16 says ``E'a\n' ~ 'a$'`` is FALSE where Python says
    # true, and neither errors. ``\Z`` means what Postgres means -- but ONLY
    # without ``(?m)``, since ``\Z`` ignores multiline while both engines'
    # ``$`` honours it, so rewriting unconditionally widens the divergence.
    # Only a real unscoped FLAG group counts: ``(?=m)m$`` is a lookahead, not
    # multiline, and live PG16 answers false there.
    multiline = any(
        is_flag_run(found.body, scoped=found.scoped, flag="m")
        for found in paren_extensions(pattern)
    )
    for index in live_indices(pattern):
        if pattern[index] == "$" and not multiline:
            replacements[index] = (index + 1, "\\Z")
    for escape in escapes(pattern):
        table = _IN_BRACKET_TRANSLATIONS if escape.in_bracket else _POSIX_TRANSLATIONS
        if (python := table.get(escape.char)) is not None:
            replacements[escape.start] = (escape.start + 2, python)
    out: list[str] = ["(?s)"]
    cursor = 0
    for start in sorted(replacements):
        stop, python = replacements[start]
        out.append(pattern[cursor:start])
        out.append(python)
        cursor = stop
    out.append(pattern[cursor:])
    return "".join(out)


def _scan(
    pattern: str,
) -> Iterator[Escape | PosixClass | NamedGroup | ParenExtension | Inert | Comment]:
    r"""Yield each escape, bracket form, named group, and ``(?...)`` in order.

    A backslash consumes the character after it, so a doubled backslash yields
    ONE escape (of ``\``) and leaves the next character ordinary. Bracket
    expressions are tracked with the POSIX rules that a ``]`` immediately after
    ``[`` or ``[^`` is a literal member rather than the terminator (live:
    ``']' ~ '[]a]'`` is true), and that a ``[:name:]`` character class inside
    does not close the enclosing bracket. A ``(?#...)`` comment is skipped
    whole, since its body is prose to both engines.
    """
    index = 0
    in_bracket = False
    bracket_start = -1
    # ``(?x)`` gives ``#`` a second comment syntax, running to end of line.
    # Turned on by an unscoped flag run only: Postgres rejects every scoped
    # group, so ``(?x:...)`` never enables it for anyone.
    expanded = False
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            yield Escape(char=pattern[index + 1], start=index, in_bracket=in_bracket)
            yield Inert(start=index, stop=index + 2)
            index += 2
            continue
        if not in_bracket:
            if expanded and char == "#":
                newline = pattern.find("\n", index)
                stop = len(pattern) if newline < 0 else newline
                yield Comment(start=index, stop=stop)
                yield Inert(start=index, stop=stop)
                index = stop
                continue
            if pattern.startswith("(?#", index):
                closing = pattern.find(")", index + 3)
                stop = len(pattern) if closing < 0 else closing + 1
                yield Comment(start=index, stop=stop)
                yield Inert(start=index, stop=stop)
                index = stop
                continue
            if pattern.startswith("(?P<", index) or pattern.startswith("(?P=", index):
                yield NamedGroup(start=index)
                index += 4
                continue
            if pattern.startswith("(?", index) and index + 2 < len(pattern):
                stop = index + 2
                while stop < len(pattern) and pattern[stop] not in ":)":
                    stop += 1
                # An UNTERMINATED opener is not a group: ``(?n`` contains no
                # flag, so reporting one describes a construct the pattern
                # does not have. Both engines just fail to parse it, and the
                # compile check says so.
                if stop < len(pattern):
                    body = pattern[index + 2 : stop]
                    scoped = pattern[stop] == ":"
                    yield ParenExtension(body=body, scoped=scoped, start=index)
                    expanded = expanded or is_flag_run(body, scoped=scoped, flag="x")
                index += 2
                continue
            if char == "[":
                in_bracket = True
                bracket_start = index
                yield Inert(start=index, stop=index + 1)
            index += 1
            continue
        # Inside a class. ``[:alpha:]`` / ``[.x.]`` / ``[=x=]`` are nested
        # forms whose closing ``]`` is theirs, not the class's.
        if char == "[" and index + 1 < len(pattern) and pattern[index + 1] in ":.=":
            marker = pattern[index + 1]
            yield PosixClass(marker=marker, start=index)
            closing = pattern.find(f"{marker}]", index + 2)
            stop = len(pattern) if closing < 0 else closing + 2
            yield Inert(start=index, stop=stop)
            index = stop
            continue
        if char == "]":
            # A ``]`` in the first member position is a literal, not the end:
            # ``[]a]`` is the class {']', 'a'}. ``[^]a]`` likewise.
            offset = index - bracket_start
            leading = offset == 1 or (offset == 2 and pattern[bracket_start + 1] == "^")
            if not leading:
                in_bracket = False
                index += 1
                continue
        yield Inert(start=index, stop=index + 1)
        index += 1


# POSIX ARE's word-boundary escapes, rewritten to their Python spellings.
# Python raises ``re.error: bad escape \y`` rather than reading a literal, so
# an untranslated pattern is a 500 rather than a silent mismatch.
#
# ``\m`` / ``\M`` (start-of-word, end-of-word) have no single Python escape:
# ``\b`` is a boundary in EITHER direction, so ``\bfoo`` matches "barfoo"
# where ``\mfoo`` must not. The lookarounds spell the same thing out of ``\w``.
#
# ``\w``, NOT a hardcoded ``[0-9A-Za-z_]``: Postgres decides word membership
# by the database collation, and under ``en_US.UTF-8`` a letter of any script
# qualifies -- live PG16 answers true for both ``'é' ~ '\w'`` and
# ``'д' ~ '\w'``. Python's ``\w`` is Unicode-aware by default and agrees.
#
# ``\w`` and its negation, ``\A`` / ``\Z``, lookahead, and bounded repeats
# mean the same thing to both engines. ``(?i)`` agrees only over ASCII, and
# cannot be translated at all: the two fold Unicode differently and both
# ANSWER (6 of 10 measured pairs disagree), so ``filters._folds_non_ascii``
# refuses the non-ASCII cases instead.
_POSIX_TRANSLATIONS: Final[Mapping[str, str]] = {
    "y": r"\b",
    "Y": r"\B",
    "m": r"(?<!\w)(?=\w)",
    "M": r"(?<=\w)(?!\w)",
    # ``\d`` and ``\s`` name DIFFERENT sets in the two engines, and the sets
    # below are Postgres's answer taken verbatim from a scan of EVERY
    # codepoint. Neither has a describable shape: ``\s`` is not "ASCII
    # whitespace" (Postgres also matches U+1680, most of U+2000-U+200A, and
    # U+3000) and not Python's set (which adds U+0085, U+00A0, U+2007, U+202F
    # and the C1 controls). ``\d`` IS exactly ``[0-9]`` -- Postgres matched no
    # non-ASCII digit anywhere. Narrowing either by hand re-opens the gap.
    #
    # Spelled as explicit classes rather than a global ``(?a)`` flag, which
    # would also narrow ``\w`` -- and ``\w`` AGREES.
    "d": "[0-9]",
    "D": "[^0-9]",
    "s": "[\u0009-\u000d\u0020\u1680\u2000-\u2006\u2008-\u200a\u2028\u2029\u205f\u3000]",
    "S": "[^\u0009-\u000d\u0020\u1680\u2000-\u2006\u2008-\u200a\u2028\u2029\u205f\u3000]",
}


# Inside a bracket expression the replacement must be bare MEMBERS, since a
# nested ``[...]`` would be a POSIX class rather than a group: ``[\d]``
# becomes ``[0-9]`` and ``[\dx]`` becomes ``[0-9x]``. The divergence is the
# same one the outside-bracket table fixes -- live PG16 says
# ``E'\u0666' ~ '[\d]'`` is false where Python matches.
#
# The negated forms have no entry because they have no Python spelling: a
# member meaning "not a digit" cannot be written inside a class, since
# ``[^0-9]`` negates the whole class. Untranslated they answer OPPOSITELY --
# live PG16 says ``E'\u0666' ~ '[\D]'`` is true where Python says false -- so
# ``filters.validate_clause`` refuses them by name instead.
_IN_BRACKET_TRANSLATIONS: Final[Mapping[str, str]] = {
    "d": "0-9",
    "s": "\u0009-\u000d\u0020\u1680\u2000-\u2006\u2008-\u200a\u2028\u2029\u205f\u3000",
}
