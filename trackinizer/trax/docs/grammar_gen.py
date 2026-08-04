#!/usr/bin/env python
"""Generate grammar.lark with CONCRETE terminals from the live trax tables.

grammar.lark is the single, machine-checked, LLM-facing description of the trax
command language. Its STRUCTURE (where tails bind, descent vs ``begin/end``, the
query/bulk/tail forms) is the hand-written :data:`_STRUCTURE` template below. Its
TERMINALS -- the concrete keyword inventory (kinds, edge aliases, field names,
filter operators) -- are GENERATED here from the same tables the parser uses, so
the grammar can never drift from the parser:

  * kinds            <- ``trax.grammar.VALID_KINDS`` (lowercased CLI spelling)
  * edge keywords    <- ``trax.grammar.EDGE_ALIASES`` (every CLI spelling)
  * scalar fields    <- ``trax.grammar.EDITABLE_FIELDS``
  * list fields      <- ``trax.grammar.LIST_FIELDS`` minus the ref-list ones
  * ref-list fields  <- the ``_FIELDS`` entries carrying a ``ref_kind``
  * cost fields      <- ``trax.grammar.COST_FIELDS``
  * filter ops       <- ``wire.filters.FILTER_OPS`` (valued vs valueless)

The edge-meta words (``note``/``valence``/``priority``/``label``/``labels``) are
NOT a separate lexer class: ``note``/``valence`` are inline literals in
``safe_meta`` and the collision words reuse the ``FIELD`` / ``LIST_FIELD``
spellings, resolved to the edge reading by position (and the ``edge`` marker).

Because real CLI tokens overlap across classes (``codechange`` is BOTH a kind and
a ref-list field; ``label`` is both a row list field and an edge-meta field), the
terminals are NOT disjoint. The grammar resolves each by PARSE CONTEXT, so
``grammar_check.py`` reads it with Earley's scannerless ``dynamic`` lexer (which
tries every terminal match and lets the parser choose) rather than a fixed
tokenizer. This is the same context-sensitivity the hand-written parser applies.

Run to regenerate (writes grammar.lark in place):
    uv --quiet run --frozen python trax/docs/grammar_gen.py

``grammar_check.py`` / the drift test assert the committed grammar.lark equals
``render_grammar()``; a table edit that is not regenerated fails the check.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final, get_args

import argparse

from trackinizer.trax.cli import DISPATCHERS
from trackinizer.trax.commands import Command
from trackinizer.trax.grammar import (
    COST_FIELDS,
    EDGE_ALIASES,
    EDITABLE_FIELDS,
    FIELDS_BY_NAME,
    LIST_FIELDS,
    PRIORITY_ALIASES,
    VALID_KINDS,
    WRITE_FIELDS_CLI,
)
from trackinizer.trax.profile import Profiles
from trackinizer.trax.run.session import build_parser as run_parser
from trackinizer.types.inquiries import (
    Belief,
    Inquiry,
    Issue,
    Paper,
)
from trackinizer.wire.filters import (
    FILTER_OPS,
    VALUELESS_FILTER_OPS,
)
from trackinizer.wire.wire_metrics_query import (
    METRIC_AXES,
    METRIC_COMPARE_OPS,
    MetricReduce,
)


# The hand-written structural body: the productions and the binding-rule prose.
# Everything ABOVE the ``// --- terminals ---`` banner. The terminal block below
# the banner is generated and appended by :func:`render_grammar`. Edit binding
# structure here; edit the keyword inventory in the source tables, never here.
_STRUCTURE: Final = r"""// grammar.lark -- the complete trax CLI: read this one file to author any command.
// GENERATED (regen: python trax/docs/grammar_gen.py). lowercase=rule, UPPER=keyword
// set (listed below). ? optional, * zero+, + one+, | choice. Tokens are argv words
// (shell quotes "foo bar" into one VALUE); keywords case-insensitive, values verbatim.
// Examples below omit the leading `trax`. Shape is here; value/validity rules are in
// the SEMANTICS block at the bottom -- together they are the whole tool.
// HOW IT PARSES: one left-to-right pass. The grammar is intentionally ambiguous
// (terminals overlap, constructs nest), resolved by ONE rule: GREEDY, longest-match
// (the parse-level analogue of a lexer's maximal munch) -- when a token could
// extend the current construct or start a new one, it EXTENDS. So an inline
// create's fields run to the first non-field; edge metadata runs to the first
// non-meta; a following edge DESCENDS into a just-made node
// (A produced B produced C == A->B->C); a trailing `del` binds to the edge,
// not the row. A token's ROLE thus depends on what precedes it -- the same `label`
// is a row field, edge metadata, or a filter field by position. The query form is
// LR-Regular: classifying it (list vs bulk-edit) can need lookahead to a selector
// arbitrarily far right, so `owner to Josh status is active` is one bulk edit.
//
// trax [GLOBAL-FLAG...] (VERB ... | KIND ...) [WRITE-FLAG...]      flags peeled first:
//   --makeitso   apply a bulk write matching >1 row (else just preview)
//   --as ACTOR   --reason TEXT     audit attribution (on a write; `run --as` differs)
//   --format table|json|ids   --limit N   --sort priority|seq|recent|oldest|valence
//   --show-ids   --profile/--host/--port
// A field VALUE of `-` reads stdin; a value starting with `@` reads that file path.

start: invocation
invocation: verb_command | row_command

// VERB: a report/management verb + its own argparse flags.  next --format ids
verb_command: VERB VERB_ARG*

// ROW: a KIND, then the token after it decides what the command is (there is no
// `create` verb -- a field with no preceding ref or selector IS the create):
//   nothing            -> list every row of the kind          issue
//   a seq/uuid ref     -> act on that existing row            issue 7 status to complete
//   a range/filter     -> list query, or bulk-edit if a mutation rides it
//   a FIELD only       -> CREATE a new row from the fields    issue title to "Retry bug"
// A field-led command and a bulk-edit share the same clauses; the SELECTOR decides:
// a range/filter present -> bulk-edit existing rows; none -> create one new row.
row_command: KIND command
command: query_command           // list / bulk-edit / create (selector decides)
       | query_command metric_action   // cross-experiment: a filtered/bare list + a metric slice
       | subject_list tail_seq   // ref(s): one ref+tails = edit; many refs = show
       | tail_seq                // tails on a just-named subject
subject_list: subject_atom+
subject_atom: SEQ | UUID | KIND SEQ | KIND UUID

// QUERY / BULK-EDIT: ranges+filters pick rows; a set_mutation makes it a bulk edit
// of every match (--makeitso past one row). Order-free: trax routes each
// `field OP value` by its OP -- a filter op (is/re/..) is a selector, to/add/del is
// a mutation -- so selectors and mutations may interleave with no separator. An
// extra KIND widens the query to that kind too (multi-kind list).
//   issue status is active   |   issue belief 1..20 status is active  (issues+beliefs)
//   issue 1..50 owner to jvd --makeitso  (bulk)   |   issue owner to a status is active
query_command: query_clause+
query_clause:  KIND | range | filter | set_mutation
range:  RANGE                          // intervals use `..` (NOT `-`): 1..50  ..10  227,228..
filter: FILTER_FIELD FILTER_OP VALUE   // owner is Josh ; title re bug
      | FILTER_FIELD VALUELESS_OP      // owner isnull ; kind notnull

// TAILS on the subject, left to right.  DEL and a bare relation projection are
// TERMINAL (must be last).  A scalar FIELD may be set at most once per command.
tail_seq: tail*
tail: set_mutation | cost_action | edge_action | relation_action | read_field | metric_action | DEL
// Setting ANY field needs the operator -- `FIELD to VALUE`, never bare
// `FIELD VALUE` (`title to x`, not `title x`; `priority to high`, not `priority
// high`). The same holds inside an inline create.
set_mutation: FIELD TO VALUE              // title to x
            | LIST_FIELD list_op VALUE    // label add backend
            | REF_LIST_FIELD list_op ref  // codechange add codechange 9
list_op:  TO | ADD | DEL
cost_action: COST_FIELD ADD VALUE         // agent-cost add 1.25 (signed)
read_field:  FIELD                        // bare field = read it
relation_action: EDGE_KEYWORD SEQ?        // no target = list related rows

// METRIC: read/write one Experiment's metric grid ((key,step)->value), or rank
// across the bare `experiment` list (cross-experiment). The grid is numpy
// boolean-mask indexing in words: `at` clauses AND into a mask, `to` writes it,
// no `to` reads it, `sort`/`limit` window a read. Clauses are ORDER-FREE (the
// parser routes each by its lead word), so masks/write/sort/limit may interleave;
// the shape below lists the four clause kinds, not a fixed order. See
// metric-grammar.md for the authoritative spec.
//   experiment 42 metric at key is loss at step is 3 to 0.5   (one-cell write)
//   experiment 42 metric at loss sort desc limit 5            (loss's 5 largest)
//   experiment metric at loss at step is 100 sort desc limit 5  (rank, cross-exp)
// FIELD is a grid axis key/step/value; OP is a METRIC_OP comparator (is/ne/lt/le/
// gt/ge -- the grid has no regex/presence ops) or the step-axis reduction max/min
// (NO value: `at step max` = highest step). A bare
// `at <word>` (word not key/step/value) is the shorthand `at key is <word>`.
// SEMANTIC (parser-enforced, grammar-accepted): max/min apply to step only; a
// write forbids sort/limit; a bulk write (multi-cell mask) needs --makeitso.
metric_action: METRIC metric_clause*
metric_clause: metric_mask | metric_write | metric_sort | metric_limit
metric_mask: AT METRIC_FIELD METRIC_OP VALUE   // at step is 4
           | AT METRIC_FIELD METRIC_REDUCE                       // at step max
           | AT VALUE                                            // at loss (== at key is loss)
metric_write: TO VALUE                     // to 0.5  (assign the mask; absent = read)
metric_sort:  SORT (ASC | DESC)            // sort desc  (a read/rank ordering)
metric_limit: LIMIT VALUE                  // limit 5    (window a read/rank)

// EDGE: link the subject to a row (existing ref or one created inline). Same
// EDGE_KEYWORD with no target is the relation projection above.  `del` unlinks.
//   issue 7 requires issue 8   |   issue 7 narrows issue 3 priority to high (edge meta)
// Edge metadata may appear BEFORE the target (always unambiguous -- nothing else
// to attach to yet, so collision words need no marker) and/or AFTER it. After the
// target, the COLLISION words (priority/label/labels, also row fields) require the
// `edge` marker; the edge-only words (note/valence) are bare. A bare collision
// word after the target is NOT edge metadata (maximal munch rolls it up to the
// nearest field-taking construct), closing the old silent footgun.
edge_action: EDGE_KEYWORD pre_meta* edge_target post_meta* edge_term?
edge_term: DEL
// The `edge` marker is OPTIONAL on every meta word and forces the edge reading.
// Bare: safe words (note/valence) always read as edge; collision words
// (priority/label/labels, also row fields) read as edge only PRE-target (nothing
// else to attach to yet). Post-target a bare collision word rolls up to the
// nearest field-taking construct, so it needs the marker.
pre_meta:  EDGE? edge_meta                    // pre-target: collision words bare OK
post_meta: safe_meta | EDGE edge_meta          // post-target: collision words need EDGE
edge_meta:    safe_meta | collide_meta
safe_meta:    ("note" | "valence") TO VALUE
collide_meta: "priority" TO VALUE | ("label" | "labels") list_op VALUE
edge_target: ref | deep_create | BEGIN group_create END
ref: KIND SEQ | KIND UUID | UUID

// INLINE CREATE: an edge target that is a new row. The cursor DESCENDS into it, so
// a following edge binds to IT (chain):
//   issue title to A produced belief title to B produced paper 3  == A->B->paper3
// For SIBLINGS under one anchor, wrap each child in begin..end (end pops the cursor
// back to the anchor) so the next edge re-attaches to the anchor, not the child:
//   issue title to root requires begin issue title to leaf-a end requires begin issue title to leaf-b end
//   == root requires leaf-a AND root requires leaf-b (two new siblings, not a chain).
// A create body must LEAD with a field. After that first field, three classes
// INTERLEAVE in any order: fields, cost deltas, and the UNAMBIGUOUS producer-edge
// metadata `note`/`valence` (edge-only words, so their position is free):
//   `title to T note to N status to complete` == `title to T status to complete note to N`.
// The COLLISION words `label`/`labels`/`priority` name BOTH a row field and an
// edge annotation; in a create body they are ROW fields (via inline_field), and
// their edge-annotation meaning is reachable only AFTER an explicit edge ref
// (edge_meta below). The first edge closes the node: after it a field is an error
// and metadata binds to the child. >=1 field required (parser rejects a body with
// none). `safe_meta` is the note/valence subset of edge_meta.
deep_create: KIND (inline_field | cost_action | safe_meta | EDGE edge_meta)+ (edge_action cost_action*)*
inline_field: FIELD TO VALUE | list_set | REF_LIST_FIELD (TO | ADD) ref
list_set: LIST_FIELD TO VALUE (LIST_FIELD ADD VALUE)*   // seed then append
group_create: KIND (inline_field | cost_action | safe_meta | EDGE edge_meta)+ (edge_action cost_action*)*
"""


# The pattern terminals (regex, not keyword sets). RANGE is a comma-union that
# must contain AT LEAST ONE `a..b` interval (so a lone bare seq stays a SEQ, not
# a RANGE): an optional run of atoms, then a mandatory interval, then more
# atoms. An atom is a bare seq (`227`) or an interval; every interval carries AT
# LEAST ONE bound (`222..`, `..10`, `222..260`). A bare `..` (no bound either
# side) is rejected -- it lowers to a fully-open SeqRange that wire/seq_ranges.py
# refuses, so the grammar must refuse it too (K6-001).
_PATTERN_TERMINALS: Final = r"""SEQ:   /\d+/                 // a row number: 7  (a kind-qualified ref may also be written Kind#seq, e.g. issue#7)
UUID:  /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/
RANGE: /((\d+\.\.\d*|\.\.\d+|\d+),)*(\d+\.\.\d*|\.\.\d+)(,(\d+\.\.\d*|\.\.\d+|\d+))*/   // 1..50  ..10  227,228..  (each interval needs >=1 bound)
VALUE:    /\S+/
VERB_ARG: /\S+/"""


def _literal_terminal(name: str, spellings: Sequence[str], comment: str = "") -> str:
    """Render one terminal as an ordered alternation of quoted literals.

    Spellings are sorted longest-first so a scannerless match prefers the longer
    keyword (``codechanges`` before ``codechange``, ``isnull`` before ``is``),
    then alphabetically for a stable, regenerable order.
    """
    ordered = sorted(spellings, key=lambda s: (-len(s), s))
    body = " | ".join(f'"{s}"' for s in ordered)
    tail = f"   // {comment}" if comment else ""
    return f"{name}: {body}{tail}"


def _verb_spellings() -> list[str]:
    """The non-kind top-level verb spellings (`next`, `search`, `profile`, ...).

    Sourced from the live ``DISPATCHERS`` table minus the kind verbs (which lead
    row commands, not verb commands), plus ``run`` -- special-cased in
    ``parse_and_run`` rather than registered as a dispatcher.
    """
    kinds = {k.lower() for k in VALID_KINDS}
    names = {name for dispatcher in DISPATCHERS for name in dispatcher.names}
    return sorted((names - kinds) | {"run"})


def _terminal_block() -> str:
    """Build the concrete terminal block from the live parser tables."""
    kinds = [k.lower() for k in VALID_KINDS]
    edge_keywords = list(EDGE_ALIASES)
    ref_list_fields = sorted(
        {f.cli_name for f in FIELDS_BY_NAME.values() if f.ref_kind is not None}
    )
    # A ref-list spelling (codechange[s]) is ALSO in LIST_FIELDS; split it out so
    # REF_LIST_FIELD and LIST_FIELD name disjoint *concepts* (the productions
    # differ: a ref-list takes a typed ref, a plain list takes a VALUE). The
    # tokens still overlap with KIND (codechange) -- resolved by parse context.
    plain_list_fields = [f for f in LIST_FIELDS if f not in ref_list_fields]
    valued_ops = [op for op in FILTER_OPS if op not in VALUELESS_FILTER_OPS]
    valueless_ops = [op for op in FILTER_OPS if op in VALUELESS_FILTER_OPS]
    # FILTER_FIELD is every public CLI field spelling with filtering semantics.
    # Per-kind validity remains a semantic check in FILTER_FIELDS_CLI.
    filter_fields = sorted(
        {
            *(field for field in EDITABLE_FIELDS if FIELDS_BY_NAME[field].filterable),
            *LIST_FIELDS,
            *COST_FIELDS,
            "seq",
            "id",
            "created",
            "modified",
        }
    )
    lines = [
        "// ---- terminals (keyword sets; spellings overlap across classes, e.g.",
        "// codechange is KIND+REF_LIST_FIELD, resolved by position) ----",
        _literal_terminal("VERB", _verb_spellings()),
        _literal_terminal("KIND", kinds),
        _literal_terminal("EDGE_KEYWORD", edge_keywords),
        _literal_terminal("FIELD", list(EDITABLE_FIELDS)),
        _literal_terminal("LIST_FIELD", plain_list_fields),
        _literal_terminal("REF_LIST_FIELD", ref_list_fields),
        _literal_terminal("COST_FIELD", list(COST_FIELDS)),
        _literal_terminal("FILTER_FIELD", filter_fields),
        _literal_terminal("FILTER_OP", valued_ops),
        _literal_terminal("VALUELESS_OP", valueless_ops),
        _literal_terminal("TO", ["to"]),
        _literal_terminal("ADD", ["add"]),
        _literal_terminal("DEL", ["del"]),
        _literal_terminal("EDGE", ["edge"]),
        _literal_terminal("BEGIN", ["begin"]),
        _literal_terminal("END", ["end"]),
        # Metric-tail keywords. Every spelling also matches VALUE (/\\S+/); the
        # dynamic lexer resolves each by parse position, exactly as the parser's
        # ``parse_metric_action`` dispatches on the lead word.
        _literal_terminal("METRIC", ["metric"]),
        _literal_terminal("AT", ["at"]),
        _literal_terminal("SORT", ["sort"]),
        _literal_terminal("LIMIT", ["limit"]),
        _literal_terminal("ASC", ["asc"]),
        _literal_terminal("DESC", ["desc"]),
        _literal_terminal(
            "METRIC_FIELD", list(METRIC_AXES), "grid axes (key/step/value)"
        ),
        # The metric comparator set is the wire's ``METRIC_COMPARE_OPS`` -- the
        # SAME single definition the parser gates on and the store maps to SQL.
        # It excludes the regex/presence ops in FILTER_OP by construction, so the
        # grammar cannot admit an op the parser rejects (the drift-bug fix).
        _literal_terminal(
            "METRIC_OP", list(METRIC_COMPARE_OPS), "grid comparators (is/ne/lt/...)"
        ),
        _literal_terminal(
            "METRIC_REDUCE",
            list(get_args(MetricReduce.__value__)),
            "step-axis reductions: final/first",
        ),
        _PATTERN_TERMINALS,
        "",
        "%import common.WS",
        "%ignore WS",
    ]
    return "\n".join(lines)


def _kind_field_lines() -> list[str]:
    """One line per kind listing its KIND-SPECIFIC writable fields (base fields,
    valid everywhere, are listed once separately).
    """
    kinds = list(WRITE_FIELDS_CLI)
    base: set[str] = set(WRITE_FIELDS_CLI[kinds[0]])
    for kind in kinds[1:]:
        base &= WRITE_FIELDS_CLI[kind]
    lines = [f"//   any kind: {', '.join(sorted(base))}"]
    for kind in kinds:
        extra = sorted(set(WRITE_FIELDS_CLI[kind]) - base)
        if extra:
            lines.append(f"//   {kind}: + {', '.join(extra)}")
    return lines


def _edge_direction_lines() -> list[str]:
    """One line per stored edge kind: its forward spellings and reverse-voice
    aliases (which address the SAME stored edge from the opposite endpoint).
    """
    forward: dict[str, list[str]] = {}
    reverse: dict[str, list[str]] = {}
    for spelling, edge in EDGE_ALIASES.items():
        (reverse if edge.reverse else forward).setdefault(edge.name, []).append(
            spelling
        )
    lines: list[str] = []
    for name, fwd_spellings in forward.items():
        fwd = " ".join(sorted(fwd_spellings))
        rev = " ".join(sorted(reverse.get(name, [])))
        lines.append(f"//   {name}: {fwd}   (reverse voice: {rev})")
    return lines


def _arg_token(action: argparse.Action) -> str:
    """Render one argparse action as a usage token (``--flag V`` / ``NAME...``)."""
    if action.option_strings:
        flag = "/".join(action.option_strings)
        if action.nargs == 0:
            return f"[{flag}]"
        if action.choices:
            return f"[{flag} {'|'.join(map(str, action.choices))}]"
        return f"[{flag} V]"
    # Positional.
    name = action.dest.upper()
    if action.choices:
        name = "|".join(map(str, action.choices))
    if action.nargs == "+":
        return f"{name}..."
    if action.nargs in ("?", "*"):
        return f"[{name}]"
    return name


def _verb_usage(verb: str, parser: argparse.ArgumentParser) -> str:
    """One ``verb POSITIONALS [--flags]`` usage line from an argparse parser."""
    positionals = [
        _arg_token(a)
        for a in parser._actions  # noqa: SLF001 -- argparse exposes args only here
        if not a.option_strings and not isinstance(a, argparse._HelpAction)  # noqa: SLF001
    ]
    options = [
        _arg_token(a)
        for a in parser._actions  # noqa: SLF001
        if a.option_strings and not isinstance(a, argparse._HelpAction)  # noqa: SLF001
    ]
    parts = [verb, *positionals, *options]
    return f"//   {' '.join(parts)}"


def _verb_lines() -> list[str]:
    """A usage line per top-level verb, introspected from its live argparse parser.

    Covers every non-kind dispatcher plus ``run`` (special-cased in the CLI) and
    ``profile`` (whose ``rest`` hides a hand-parsed sub-grammar stated inline).
    """
    kinds = {k.lower() for k in VALID_KINDS}
    lines: list[str] = []
    for dispatcher in DISPATCHERS:
        verb = next((n for n in dispatcher.names if n not in kinds), None)
        if verb is None or dispatcher is Profiles:
            continue  # kind dispatcher, or profile (handled below)
        lines.append(_verb_usage(verb, _verb_parser(dispatcher)))
    lines.append(_verb_usage("run", run_parser()))
    # profile parses ``rest`` by hand; its sub-grammar is fixed, stated directly.
    lines.append("//   profile [NAME] [ url|actor|token [to V] | current NAME | del ]")
    lines = sorted(lines)
    # argparse positionals/--as carry meaning the bare usage can't show; gloss the
    # non-obvious ones (sourced from the verbs' own help text).
    lines.append(
        "//   ^ run --as NAME = the session's owner; doubles as its routing handle"
    )
    lines.append(
        "//     (others address it @NAME), uniquified on collision (scientist#2)."
    )
    lines.append(
        "//     send TARGET = @actor[:room] of a live session; QUERY/TEXT are free words."
    )
    return lines


def _verb_parser(dispatcher: type[Command]) -> argparse.ArgumentParser:
    """The argparse parser a verb dispatcher builds (its ``make_parser``)."""
    return dispatcher.make_parser()


def _semantics_block() -> str:
    """The generated ``// SEMANTICS`` section: the value/validity rules the
    context-free shape cannot carry, so grammar.lark alone suffices to author
    commands. Every list is sourced from the live tables.
    """
    status = " ".join(get_args(Inquiry.Status.__value__))
    judgement = " ".join(get_args(Belief.Judgement.__value__))
    issue_kind = " ".join(get_args(Issue.Kind.__value__))
    pub_type = " ".join(get_args(Paper.PublicationType.__value__))
    priority = " ".join(f"{a}={v}" for a, v in PRIORITY_ALIASES.items())
    lines = [
        "// ==== SEMANTICS: rules the shape accepts but the tool enforces (generated) ====",
        "// ENUM VALUES (a set to one of these fields must use a listed value;",
        "// all other fields take free text -- title, description, validation, etc.):",
        f"//   status: {status}   judgement: {judgement}",
        f"//   issue_kind/kind: {issue_kind}   publication_type: {pub_type}",
        f"//   priority: an int, or {priority}",
        "//   confidence/valence: a float (valence in [-1,1], sign = for/against)",
        "//   config: one standard JSON object; readable/editable, not filterable",
        "// FIELD VALIDITY (a field is writable only on these kinds):",
        *_kind_field_lines(),
        "// EDGE DIRECTION (edges store child->parent; a reverse-voice alias writes the",
        "// SAME edge, e.g. `belief 3 proved_by paper 5` == `paper 5 proves belief 3`;",
        "// proves/favors carry a signed valence, dis* negates it = against):",
        *_edge_direction_lines(),
        "// VERBS (full usage, generated from each verb's argparse). What they do:",
        "//   next=next unblocked Issue; blocked/board/graph=Issue views; search=text",
        "//   search; recent=audit feed; cost=cost rollup; id=show row by uuid;",
        "//   profile=manage server profiles; send=message a live agent session;",
        "//   run=wrap an agent CLI (claude/gemini/codex) and sync its session.",
        *_verb_lines(),
    ]
    return "\n".join(lines)


def render_grammar() -> str:
    """The full grammar.lark text: structural template, terminals, and the
    generated semantics block that makes the file self-sufficient.
    """
    return f"{_STRUCTURE}\n{_terminal_block()}\n\n{_semantics_block()}\n"


def grammar_path() -> Path:
    """The committed grammar.lark, alongside this generator in trax/docs/."""
    return Path(__file__).with_name("grammar.lark")


def main() -> int:
    """Regenerate grammar.lark in place; report whether it changed."""
    path = grammar_path()
    new = render_grammar()
    old = path.read_text() if path.exists() else ""
    if new == old:
        print(f"grammar.lark: already current ({len(new)} bytes).")
        return 0
    path.write_text(new)
    print(f"grammar.lark: regenerated ({len(new)} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
