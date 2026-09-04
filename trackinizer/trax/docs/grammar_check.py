#!/usr/bin/env python
"""Verify grammar.lark is current and accepts the true trax command language.

grammar.lark represents the language the manual parser at trax/parser.py
ACCEPTS -- it is truth, not a narrowed definition. Its terminals are CONCRETE
(real trax keywords), with the live parser tables as their source through
grammar_gen.py. The file alone documents the command vocabulary. This checker enforces three
things:

0. Currency: the committed grammar.lark equals ``grammar_gen.render_grammar()``.
   A parser-table edit that was not regenerated (a new kind, edge alias, or
   field) fails here, so the grammar can never silently drift from the parser.

1. Acceptance: the grammar builds and every canonical command in CORPUS parses.
   CORPUS is written in CONCRETE tokens, so it doubles as a worked-example
   catalogue of valid trax commands.

2. Drift detection on ambiguity. The language is LR-Regular: a bulk apply may
   place its mutations before its deciding selector
   (``owner to Josh status is active``), so classifying the first token uses
   whole-suffix (regular) lookahead to find that selector -- hence not LALR(k).
   This checker uses a general (Earley) parser, chosen because ``grammar.lark``
   is also intentionally AMBIGUOUS (a parser-table generator would reject it);
   Earley parses every CFG, a strictly larger class than LR-Regular, so it
   covers both. It runs with the scannerless ``dynamic`` lexer, which tries
   every terminal match and lets the parser pick -- so the deliberately-overlapping
   concrete terminals (``codechange`` is a KIND and a REF_LIST_FIELD; ``label``
   is a LIST_FIELD and -- via the inline ``collide_meta`` literal -- an edge
   annotation) resolve by context, exactly as the parser does. With ``ambiguity="explicit"`` every genuinely ambiguous parse
   surfaces as an ``_ambig`` node. The grammar has THREE intended ambiguity
   classes, each resolved by the parser greedily, longest-match (the parse-level
   analogue of a lexer's maximal munch -- take the longer construct):

     a. Bulk-vs-tail. A run of leading mutations with no query clause is both a
        bulk apply (``query_command``) and a single-row edit (``tail_seq``); the
        parser resolves it from leading-subject context (a ref-led command is a
        tail edit; a selector-led one is a bulk apply). A trailing ``metric``
        tail rides this same fork (``experiment title to X metric ...`` is a
        create-then-log OR a two-tail edit) -- no new class, just class (a)
        extended; the parser takes the create-then-log reading greedily.

     b. Descent-vs-sibling. A ``deep_create`` target followed by another tail is
        both "the new node DESCENDS and absorbs that tail" (``issue produced
        belief ... produced paper ...`` => belief->paper) and "the tail is a
        SIBLING on the leading subject". The parser takes descent, exactly as
        ``parser_test.test_inline_create_carries_nested_edge_deep`` asserts;
        ``begin ... end`` is the explicit pop that forces the sibling reading, so
        a ``begin/end`` group parses UNAMBIGUOUSLY.

     c. Trailing ``del``. A ``del`` right after an edge is both that edge's
        ``edge_term`` (delete the edge) and a row ``tail`` (delete the subject);
        the parser binds it to the edge (``_parse_edge_action``, maximal munch).

   This is a DRIFT gate over a representative CORPUS, not an enumeration of every
   ambiguous string: each command must keep parsing, an expected-unambiguous one
   must stay unique, and an expected-ambiguous one must stay ambiguous.

Each command in CORPUS is a full invocation (the ``trax`` program name stripped,
top-level ``--flags`` peeled), exactly the span grammar.lark covers.

Run:  uv --quiet run --frozen python trax/docs/grammar_check.py

Exit 0 + "grammar.lark: current, accepts the corpus, no unexpected ambiguity."
on success; non-zero with the offending command (or a stale-file notice) on
failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import sys

from lark import Lark
from lark.exceptions import LarkError

from trackinizer.trax.docs.grammar_gen import (
    grammar_path,
    render_grammar,
)


if TYPE_CHECKING:
    from lark import Token
    from lark.tree import Tree


@dataclass(frozen=True, slots=True, kw_only=True)
class Case:
    """One corpus command: a label, its CONCRETE token stream, and either the
    expected ambiguity or, for a negative case, that it must be REJECTED.

    ``rejects=True`` marks a SYNTACTIC rejection: the grammar itself refuses the
    token stream. Many trax errors are instead SEMANTIC (a field invalid on a
    kind, ``del`` not terminal, ``isnull`` on a NOT-NULL column) -- the grammar
    accepts those and the parser rejects them, so they are NOT in this corpus;
    they are covered by ``trax/grammar_test.py``'s parser-execution tests.
    """

    label: str
    tokens: str
    ambiguous: bool = False
    rejects: bool = False


# Canonical command forms in CONCRETE trax tokens, each a FULL invocation (the
# `trax` program name already stripped; top-level `--flags` peeled). Positive
# cases assert a parse; ``ambiguous=True`` marks the intended maximal-munch
# classes (a) leading-mutation bulk-or-tail, (b) deep_create + trailing tail
# descent-or-sibling, (c) trailing del edge-or-row; ``rejects=True`` marks a
# syntactic rejection.
CORPUS: tuple[Case, ...] = (
    # == verb commands (a leading non-kind VERB + opaque argparse args) =======
    # A verb's args are an opaque VERB_ARG run (argparse owns their structure).
    # VERB_ARG is its own terminal used only here, so within a verb command the
    # args lex unambiguously, even though they share spellings with keywords.
    Case(label="bare verb (next)", tokens="next"),
    Case(label="bare verb (blocked)", tokens="blocked"),
    Case(label="verb with flags (recent)", tokens="recent --limit 10"),
    Case(label="verb id + uuid", tokens="id 0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"),
    Case(label="verb cost + ref", tokens="cost issue 7 --deep"),
    Case(label="verb profile bare", tokens="profile"),
    Case(label="verb profile field set", tokens="profile work url to https://x"),
    # == row commands: a leading KIND, then the row grammar ===================
    # -- bare kind + list queries (no mutation) -----------------------------
    Case(label="bare kind (list all)", tokens="issue"),
    Case(label="filter query", tokens="issue status is active"),
    Case(label="range query", tokens="issue 3..10"),
    Case(label="range union with bare seqs", tokens="issue ..10,222..225,227,228.."),
    # -- multi-subject show (a subject list, no tails) ----------------------
    Case(label="multi bare-seq show", tokens="issue 5 6 12"),
    Case(label="multi kind-switched show", tokens="issue 1 belief 3"),
    # ``isnull`` is a single VALUELESS_OP token, but a scannerless parser can
    # also tile the same characters as ``is`` (FILTER_OP) + ``null`` (VALUE).
    # That second lexing is UNREACHABLE from real argv (the shell pre-splits on
    # whitespace, so one atom ``isnull`` is never two tokens), but the
    # char-stream grammar admits it -- an expected, argv-impossible ambiguity.
    Case(label="valueless filter", tokens="issue owner isnull", ambiguous=True),
    Case(label="multi-kind-widened query", tokens="issue belief status is active"),
    # -- bulk apply: a query carrying >=1 mutation, EITHER order -------------
    Case(label="bulk, selector first", tokens="issue status is active owner to Josh"),
    Case(
        label="bulk, mutation first (LR-Regular case)",
        tokens="issue owner to Josh status is active",
    ),
    Case(
        label="bulk, range selector + list mutation", tokens="issue 3..10 label add hot"
    ),
    # -- class (a): a leading mutation run = create/bulk-or-tail -------------
    Case(label="bare scalar set", tokens="issue owner to Josh", ambiguous=True),
    Case(label="bare list add", tokens="issue label add hot", ambiguous=True),
    Case(
        label="two scalar sets (still bulk-or-tail)",
        tokens="issue owner to Josh status to complete",
        ambiguous=True,
    ),
    # -- subject-ref-led tails (unambiguous: distinct heads) ----------------
    Case(label="read field", tokens="issue 7 owner"),
    Case(label="cost add", tokens="issue 7 agent-cost add 5"),
    Case(label="del row", tokens="issue 7 del"),
    # A trailing ``del`` after chained scalar sets is the terminal ROW delete,
    # not a per-field delete: the sets apply, then the row is purged. Pins that
    # the parser no longer mis-rejects a set-then-del chain (BUG-001).
    Case(label="chained set then row del", tokens="issue 7 title to x owner to y del"),
    Case(label="relation projection", tokens="belief 7 produces"),
    Case(label="relation by index", tokens="belief 7 produces 2"),
    Case(label="edge to existing ref", tokens="issue 7 requires issue 8"),
    Case(
        label="edge to uuid ref",
        tokens="belief 7 proves belief 0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d",
    ),
    # -- inline create: single create + ref target are unambiguous ----------
    Case(
        label="edge inline deep create", tokens="issue 7 produced belief title to claim"
    ),
    Case(
        label="edge to ref then sibling edge",
        tokens="issue 7 requires issue 8 required_by issue 9",
    ),
    Case(
        label="begin/end wide (sibling) -- pop disambiguates",
        tokens=(
            "issue 7 produced begin belief title to c proves experiment 3 end "
            "produced paper 4"
        ),
    ),
    # -- class (b): a deep_create followed by a tail (descent vs sibling) ----
    Case(
        label="deep chain (descent vs sibling)",
        tokens="issue 7 produced belief title to c produced paper 4",
        ambiguous=True,
    ),
    Case(
        label="inline create + edge meta + cost (descent)",
        tokens="issue 7 produced belief title to c note to verdict agent-cost add 2",
        ambiguous=True,
    ),
    Case(
        label="inline byline then trailing list mutation (descent)",
        tokens="paper 7 produced paper author to Smith author add Jones",
        ambiguous=True,
    ),
    # The ``edge`` marker is OPTIONAL on the SAFE words (note/valence) and forces
    # the producer-edge reading of a COLLISION word inside a create body. The
    # grammar formerly paired ``EDGE`` only with ``collide_meta``, so ``edge note
    # to v`` in a body (which the hand-parser accepts) failed to parse -- a
    # grammar<->parser drift this corpus now pins. Descent-ambiguous like any
    # deep_create + tail.
    Case(
        label="edge-marked safe meta in create body",
        tokens="issue 7 produced issue title to X edge note to v",
        ambiguous=True,
    ),
    Case(
        label="edge-marked collision meta in create body",
        tokens="issue 7 produced issue title to X edge priority to high",
        ambiguous=True,
    ),
    # And ``edge``-marked safe meta AFTER an existing ref (post-target): writing
    # ``edge`` is never wrong, even on the edge-only words.
    Case(
        label="edge-marked safe meta after ref",
        tokens="issue 7 favors belief 3 edge note to v",
        ambiguous=False,
    ),
    # -- class (c): a trailing del binds to the edge, not the row -----------
    # ``label`` after a ref now requires the ``edge`` marker (A: the collision
    # words label/labels/priority are row fields unless marked). With the marker
    # the del-binding overlap is back.
    Case(
        label="edge + label meta + del (edge_term vs row del)",
        tokens="issue 7 requires issue 8 edge label add urgent del",
        ambiguous=True,
    ),
    Case(
        label="edge to ref + bare del (edge_term vs row del)",
        tokens="issue 7 requires issue 8 del",
        ambiguous=True,
    ),
    # Bare ``priority`` after a ref is NO LONGER ambiguous (A: maximal munch).
    # ``requires issue 8 priority to 0`` deterministically sets the LEADING
    # SUBJECT's priority -- the edge reading is gone (needs ``edge priority``), so
    # this parses uniquely. The marked form ``edge priority to 0`` is the
    # meta-vs-... case, but with the marker it is unambiguous too.
    Case(
        label="bare priority after ref is the subject's field (deterministic)",
        tokens="issue 7 requires issue 8 priority to 0",
        ambiguous=False,
    ),
    # == metric tail: read/write an Experiment's metric grid, or rank across ==
    # the bare `experiment` list (cross-experiment). Mirrors parser.py's
    # ``parse_metric_action``; clauses are ORDER-FREE, so masks/write/sort/limit
    # interleave. Every metric-grammar.md Write/Read/Cross-experiment/Create
    # example appears here, in shell-split tokens (a quoted title is one VALUE).
    # -- Read: mask a slice, no `to` = read ---------------------------------
    Case(label="metric whole grid (bare)", tokens="experiment 42 metric"),
    Case(label="metric bareword key read", tokens="experiment 42 metric at loss"),
    Case(
        label="metric explicit key is",
        tokens="experiment 42 metric at key is loss",
    ),
    Case(label="metric step is", tokens="experiment 42 metric at step is 3"),
    Case(label="metric value gt", tokens="experiment 42 metric at value gt 0.9"),
    Case(
        label="metric two masks (key + step)",
        tokens="experiment 42 metric at key is loss at step gt 3",
    ),
    Case(
        label="metric read sort/limit",
        tokens="experiment 42 metric at key is loss sort desc limit 5",
    ),
    # -- Write: a trailing `to` assigns the mask ----------------------------
    Case(
        label="metric one-cell write",
        tokens="experiment 42 metric at key is loss at step is 3 to 0.5",
    ),
    Case(
        label="metric many-keys-one-step write",
        tokens=(
            "experiment 42 metric at step is 3 at key is loss to 0.5 "
            "at key is acc to 0.9"
        ),
    ),
    Case(
        label="metric many-steps-one-key write",
        tokens=(
            "experiment 42 metric at key is loss at step is 3 to 0.5 "
            "at step is 5 to 0.6"
        ),
    ),
    Case(
        label="metric bulk write (step gt)",
        tokens="experiment 42 metric at key is loss at step gt 3 to 0.5",
    ),
    Case(
        label="metric bareword-key + step write",
        tokens="experiment 42 metric at step is 4 at loss to 0.5",
    ),
    # -- Cross-experiment: a metric tail on the bare/filtered `experiment` list.
    Case(
        label="metric cross-exp loss@100",
        tokens="experiment metric at loss at step is 100",
    ),
    Case(
        label="metric cross-exp ranked",
        tokens="experiment metric at loss at step is 100 sort desc limit 5",
    ),
    # A filter (`label is ml`) constrains the list, then the metric tail slices
    # it: a `query_command metric_action`, the only command form that carries a
    # metric tail after query clauses. Parses uniquely (the `metric` keyword is a
    # distinct head, so no bulk-vs-tail fork here -- unlike a leading FIELD set).
    Case(
        label="metric cross-exp filtered + ranked",
        tokens="experiment label is ml metric at loss at step is 100 sort desc limit 5",
    ),
    Case(
        label="metric cross-exp step max (final)",
        tokens="experiment metric at loss at step max",
    ),
    Case(
        label="metric cross-exp best (sort/limit 1)",
        tokens="experiment metric at loss sort desc limit 1",
    ),
    Case(label="metric step max reduction", tokens="experiment 42 metric at step max"),
    # -- Create + log in one command: a `title to ...` create then a metric tail.
    # A leading FIELD set with no selector is BOTH a create (`query_command`) +
    # metric tail AND a single-row `tail_seq` (`title` set, then `metric` tail):
    # the SAME class-(a) leading-mutation bulk-or-tail overlap the corpus already
    # documents, here extended to carry the metric tail. The parser resolves it
    # greedily to the create-then-log reading (metric-grammar.md "Create + log").
    Case(
        label="metric create + log (class a)",
        tokens="experiment title to trm-exp031 metric at step is 3 at loss to 0.5 at acc to 0.9",
        ambiguous=True,
    ),
    Case(
        label="metric create + one-cell log (class a)",
        tokens="experiment title to x metric at step is 3 at loss to 0.5",
        ambiguous=True,
    ),
    # == syntactic rejections (the concrete grammar refuses these) ============
    # A bare unknown word after a kind is neither a ref, a field, nor a selector
    # -- with concrete terminals it is only a VALUE, which cannot head a command
    # (GRAMMAR.md E001). The abstract-terminal grammar could not catch this.
    Case(label="unknown word after kind", tokens="issue foo", rejects=True),
    # An edge action may not ride a bulk apply: after a range selector, an edge
    # keyword is not a further selector or a mutation (GRAMMAR.md E006).
    Case(
        label="edge after range selector",
        tokens="issue 222.. required_by issue 8",
        rejects=True,
    ),
    # A lone unknown verb is neither a VERB nor a KIND.
    Case(label="unknown leading verb", tokens="frobnicate the thing", rejects=True),
    # A bare ``..`` range has no bound on either side; ``wire/seq_ranges.py``
    # rejects a no-bound interval, so the grammar must too (K6-001). Each
    # interval carries at least one bound.
    Case(label="bare open range", tokens="issue ..", rejects=True),
    # A metric mask op is a METRIC_OP comparator (is/ne/lt/le/gt/ge) -- the grid
    # has no regex/presence ops. ``re`` / ``notnull`` are not METRIC_OP, so the
    # concrete grammar refuses them (the drift-bug fix: the op set is the wire's
    # METRIC_COMPARE_OPS, not the broad FILTER_OPS the parser once keyed off).
    Case(
        label="metric regex op rejected",
        tokens="experiment 42 metric at key re loss",
        rejects=True,
    ),
    Case(
        label="metric presence op rejected",
        tokens="experiment 42 metric at value notnull",
        rejects=True,
    ),
)


def _ambiguity_count(tree: Tree[Token]) -> int:
    """Number of ``_ambig`` nodes Earley emitted for a parse (0 = unambiguous)."""
    # ``iter_subtrees`` yields only ``Tree`` nodes (terminals are not walked), so
    # every node carries ``.data``; Earley tags each ambiguous fork ``_ambig``.
    return sum(1 for node in tree.iter_subtrees() if node.data == "_ambig")


def main() -> int:
    path = grammar_path()
    expected = render_grammar()
    actual = path.read_text() if path.exists() else ""
    if actual != expected:
        print(
            "grammar.lark: STALE -- does not match grammar_gen.render_grammar(). "
            "Run `uv --quiet run --frozen python trax/docs/grammar_gen.py`.",
            file=sys.stderr,
        )
        return 1

    try:
        parser = Lark(
            expected,
            parser="earley",
            start="start",
            lexer="dynamic",
            ambiguity="explicit",
        )
    except LarkError as exc:
        print(f"grammar.lark: INVALID -- will not build:\n{exc}", file=sys.stderr)
        return 1

    failures: list[str] = []
    for case in CORPUS:
        try:
            # lark ships inline (partially-Any) types and no separate stub, so
            # ``Lark.parse``'s own signature reads as partially unknown; the
            # return is a concrete ``Tree[Token]``. Stubbing the whole class for
            # this one doc utility is disproportionate.
            tree = parser.parse(case.tokens)  # pyright: ignore[reportUnknownMemberType]
            count = _ambiguity_count(tree)
        except LarkError as exc:
            if not case.rejects:
                failures.append(
                    f"{case.label!r} does not parse: {case.tokens!r}\n    {exc}"
                )
            continue
        if case.rejects:
            failures.append(
                f"{case.label!r} was expected to be REJECTED but parsed: "
                f"{case.tokens!r}"
            )
        elif case.ambiguous and count == 0:
            failures.append(
                f"{case.label!r} was expected ambiguous but parsed uniquely "
                f"-- an intended maximal-munch overlap changed: {case.tokens!r}"
            )
        elif not case.ambiguous and count > 0:
            failures.append(
                f"{case.label!r} parsed ambiguously ({count} _ambig nodes) "
                f"-- an UNINTENDED ambiguity was introduced: {case.tokens!r}"
            )

    if failures:
        print("grammar.lark: DRIFT -- grammar no longer matches the corpus:")
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("grammar.lark: current, accepts the corpus, no unexpected ambiguity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
