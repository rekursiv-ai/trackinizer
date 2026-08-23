"""grammar.lark currency + acceptance tests.

Two guarantees, run in CI so the LLM-facing grammar can never silently rot:

1. The committed ``grammar.lark`` equals ``grammar_gen.render_grammar()`` -- a
   table edit (new kind / edge alias / field) that was not regenerated fails.
2. ``grammar_check.main()`` succeeds: the grammar builds under Earley's dynamic
   lexer, the canonical-command corpus parses, and only the intended
   maximal-munch ambiguities are present.
"""

from __future__ import annotations

from pathlib import Path

from lark import Lark
from lark.exceptions import LarkError

import pytest

from trackinizer.trax.docs import grammar_check
from trackinizer.trax.docs.grammar_gen import (
    grammar_path,
    render_grammar,
)


def test_committed_grammar_is_current() -> None:
    """The on-disk grammar.lark matches the generator's output.

    Regenerate with ``uv --quiet run --frozen python trax/docs/grammar_gen.py``
    after editing any sourced table (VALID_KINDS, EDGE_ALIASES, _FIELDS,
    FILTER_OPS, _EDGE_METADATA_FIELDS).
    """
    path = grammar_path()
    assert path.exists(), "grammar.lark missing; run grammar_gen.py"
    assert path.read_text() == render_grammar(), (
        "grammar.lark is stale; run `python trax/docs/grammar_gen.py`"
    )


def test_grammar_path_resolves_alongside_generator() -> None:
    """``grammar_path`` points at the file next to the generator."""
    assert grammar_path() == Path(grammar_check.__file__).with_name("grammar.lark")


@pytest.mark.compute_large_fixture
def test_grammar_check_passes() -> None:
    """The Earley build + corpus acceptance + ambiguity drift gate all pass."""
    assert grammar_check.main() == 0


def test_grammar_rejects_bare_open_range() -> None:
    """The generated grammar must reject a bare ``..`` range (K6-001).

    ``wire/seq_ranges.py`` rejects a no-bound interval (``SeqRange`` requires at
    least one bound), but the RANGE terminal allowed both sides optional, so the
    grammar accepted ``issue ..`` -- a grammar-vs-parser drift. Each interval
    must carry at least one bound.
    """
    parser = Lark(
        render_grammar(),
        parser="earley",
        start="start",
        lexer="dynamic",
        ambiguity="explicit",
    )
    with pytest.raises(LarkError):
        # lark ships inline (partially-Any) types; the parse result is unused.
        parser.parse("issue ..")  # pyright: ignore[reportUnknownMemberType]
    # A one-bound open range still parses (the legitimate form must survive).
    parser.parse("issue 222..")  # pyright: ignore[reportUnknownMemberType]
    parser.parse("issue ..10")  # pyright: ignore[reportUnknownMemberType]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
