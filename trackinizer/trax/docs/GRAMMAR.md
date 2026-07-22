# trax grammar

`trax` is a prefix-chain DSL: every command is one **subject** followed by
zero or more **tails**, each a keyword-first operation. Tails apply to the
**current cursor**, which starts at the leading subject and DESCENDS into each
inline-created node (so an edge after a create binds to that new node); a
`begin ... end` group pops the cursor back. See §13 for the cursor model. This
document is the definitive grammar; the parser, runner, and help text are
derived from it. There are **no corner cases** -- every input either matches
exactly one production or is rejected with a named error.

The parser is deterministic left-to-right prefix parsing. Variable-length
productions are safe because each has an explicit shape boundary: an existing ref
ends at `seq` or UUID, an inline create's FIELD list ends at the first non-field
token (then the cursor is that new node), edge metadata ends at the first
non-metadata token, and terminal actions (`del`, `help`) reject trailing tokens.

## 1. Conventions

- Productions are EBNF-ish. Literal keywords are quoted. `name` refers
  to another production. `?` is optional, `*` is zero-or-more, `+` is
  one-or-more, `|` is alternation, `( ... )` groups.
- Tokens are whitespace-separated argv elements; quoting (`"foo bar"`)
  is the shell's job, not the grammar's.
- **Keyword case**: keywords (verbs, kinds, fields, edges, relations,
  ops, priorities, statuses) are **case-insensitive**. They are
  canonicalised before comparison.
- **Value case**: values are preserved as-is. `trax issue 7 title to
  Foo` stores `"Foo"`.
- **Lookup tables** (kinds, fields, edges, …) are defined exactly once,
  in `grammar.py`. Token tables in section 9 are auto-generated from
  that module.

## 2. Top level

```
command         ::= top_flag* (verb_command | row_command)
verb_command    ::= verb_name argv*
row_command     ::= kind row_tail?
top_flag        ::= "--profile" NAME
                  | "--host" HOST
                  | "--port" INT
                  | "--show-ids"
```

A command is either a **verb** (one of the standalone reports, profile
management, help) or a **row command** keyed by an inquiry kind.

Top-level flags precede the verb or row-command:

- `--profile NAME`, `--host HOST`, `--port INT` -- override the active
  profile or its URL components for this invocation.
- `--show-ids` -- include UUIDs in echo lines (``created:``,
  ``deleted:``) and the row-detail ``id:`` block. UUIDs are hidden
  by default; users reference rows by ``Kind#seq``.

**Write flags** are accepted on any row write (create/edit/edge), parsed by
argparse so they may appear anywhere in the command:

- `--as ACTOR` (alias `--actor`) -- attribute the change to ACTOR in the
  audit log. Defaults to the active profile actor / `$USER`.
- `--reason TEXT` -- record why, on the change-log entry only (not row state).
- `--makeitso` -- required to apply a bulk edit (a `field to value` mutation
  on a range/filter matching **more than one** row); without it the matches
  are previewed and nothing is written. A zero- or one-row match applies
  directly.

## 3. Row command

```
row_tail   ::= subject_after_kind tail*
            |  subject_list                   -- multi-subject show
            |  list_query
            |                                 -- bare kind: list all rows
subject_after_kind
           ::= seq tail*                      -- existing row by seq
            |  uuid tail*                     -- existing row by uuid
            |  field_action create_tail*      -- anonymous create
subject_list
           ::= (kind? (seq | uuid))+          -- two or more refs, no tails
create_tail
           ::= field_action
            |  list_action
            |  cost_action
            |  edge_action                    -- target may be a deep inline-create
```

`subject_list` triggers when every token in the row tail parses as a
ref (a seq under the leading kind, a `kind seq` pair, or a UUID).
Each subject is shown in sequence. The grammar disambiguates by
shape: a tail keyword (field, edge, op) after the first ref means
single-subject + tails; another ref means multi-subject show. Mixing
tails with multi-subject is rejected — issue one command per subject
if you need tails.

Disambiguation between "existing row" and "anonymous create": after the
kind, peek one token.

- Digits-only token → seq → existing row.
- UUID-shaped token → uuid → existing row.
- Field name → field_action → create.
- Anything else → error `E001 unknown row position`.

A bare kind (`trax issue`) lists all rows of that kind. The list form
(`list_query`) is described in section 6.

## 4. Tails on an existing row

```
tail            ::= field_action
                 |  list_action
                 |  cost_action
                 |  edge_action
                 |  relation_action         -- list/select related rows
                 |  delete_action
                 |  help_action

field_action    ::= scalar_field "to" value
list_action     ::= list_field ( "to" | "add" | "del" ) value
cost_action     ::= cost_field "add" signed_value
edge_action     ::= edge_keyword pre_meta* edge_target post_meta* edge_terminator?
edge_target     ::= ref | inline_create | "begin" inline_create "end"
pre_meta        ::= safe_meta_op | collide_meta_op   -- pre-target: marker not needed
post_meta       ::= safe_meta_op | "edge" collide_meta_op
edge_terminator ::= "del"
relation_action ::= relation_keyword index?
delete_action   ::= "del"                    -- must be the last token
help_action     ::= "help"                   -- must be the last token
index           ::= [0-9]+
ref             ::= kind seq | kind uuid | uuid
inline_create   ::= kind (field_action | list_set_action | cost_action | safe_meta_op
                          | "edge" collide_meta_op)+ (edge_action cost_action*)*
                    -- fields, costs, and metadata interleave in any order before
                    -- the first outgoing edge; must lead with a field
list_set_action ::= list_field "to" value (list_field "add" value)*
```

An inline `list_set_action` seeds a list field with `to` and extends it with
zero or more `add`, building an ordered multi-value byline at create time
(`paper ... author to "Ada Lovelace" author add "Alan Turing"` lands a
two-entry `authors`). A second `to` on a field that already holds values is
rejected as a self-clobber; use `add` to append. Scalar fields stay single-set.

An `inline_create` must **lead with a field**: the dispatch recognizes a create
by a field token right after the kind, so `produced issue title to X ...` is a
create while `produced issue 7` is a ref. Leading with anything else (a `note`,
`edge`, a cost, an edge keyword) is rejected with "inline create ... must lead
with a field" rather than the opaque ref error. After that first field, the
node's `edge_meta` (the unambiguous `note`/`valence`; `edge`-marked collision
words) and `cost_action`s interleave freely with further fields, before the
node's first outgoing edge -- annotating the edge that PRODUCED this node (the
deepest edge so far), so a verdict `note` sits beside the node it describes, e.g.
`produced websearch <fields> note to "<verdict>"` binds the note to the
`produced` edge. Once an outgoing `edge_action` is seen the inbound-metadata
window closes (further metadata would belong to the child) and a stray field is
an error (the node is closed), though `cost_action`s may still follow the edges.

The canonical, machine-checked grammar is [`grammar.lark`](grammar.lark),
accepted (with its intended ambiguities accounted for) by
[`grammar_check.py`](grammar_check.py); both sit alongside this file in
`trax/docs/`. The checker uses a general (Earley) parser for two reasons:
`grammar.lark` is deliberately ambiguous (so a parser-table generator
would reject it), and the language is LR-Regular -- not LALR(k) -- since a
bulk apply can place its mutation before its deciding selector
arbitrarily far right. Earley is not implied by LR-Regularity (Earley
parses every CFG, a strictly larger class); it is simply the general
parser that covers both facts. This prose mirrors `grammar.lark`.

DEEP cursor by default; `begin ... end` for WIDE. An
`inline_create` may carry its OWN `cost_action`s and `edge_action`s after its
fields: the cursor DESCENDS into each created node, so an edge written after the
create binds to IT, not the leading subject. `A produced B produced C` means
`A -> B -> C`. To attach a SIBLING under an earlier node (fan out), wrap the child
subtree in `begin ... end`; `end` pops the cursor back to the parent, so
`A produced begin B end produced C` means `A -> B` and `A -> C`. The delimiters
are bare words (not punctuation) so they are inert in bash, zsh and fish, and
balanced so a miscounted depth is a loud error, not a silent mis-binding.

`begin ... end` is for WIDTH ALONE -- never to place metadata. Edge metadata
(`note`, `priority`, ...) written right after a node's fields binds to the edge that
produced that node (the deepest edge so far), so a verdict note needs no wrapper:
`A produced B note to "v" produced C` puts `"v"` on `A -> B`. An inline create
may seed and extend its own list fields (`author to X author add Y`), but it may
not contain a bare relation projection -- a relation keyword with no target
inside a create is read as an outgoing edge missing its ref, and rejected.

Create commands accept create fields, list mutations, and edge actions in any
order. Existing edge refs terminate immediately, so a non-edge-metadata field
after a complete ref returns to the leading create row:

```trax
trax issue title to test requires issue 125 description to foo
```

That creates one issue with `description=foo` and a `requires Issue#125` edge.
An inline create has no seq/UUID after the kind, so its fields belong to the
inline target:

```trax
trax issue title to test requires issue description to foo
```

That creates the leading issue plus an inline prerequisite whose description is
`foo`. After an edge target, `note` and `valence` are edge metadata bare (they
are edge-only words). The words `priority`, `label`, `labels` are ALSO row
fields, so they require the `edge` marker to annotate the edge:

```trax
trax issue title to test requires issue 125 edge label add urgent
```

The label lands on the edge. Without `edge`, `label`/`priority`/`labels`
are never edge metadata -- maximal munch rolls them up to the nearest construct
that can take a field, so this:

```trax
trax issue 7 narrows issue 3 priority to high
```

sets issue **7**'s priority (the leading subject), NOT the edge's and NOT issue
3's. To annotate the narrows edge, write `edge priority to high`. This removes
the old footgun where a bare `priority` after a ref silently became edge
metadata.

Edge metadata may also be written **before** the target, between the edge keyword
and the ref. There it is unambiguous (no row has appeared for it to mean), so the
collision words need no `edge` marker:

```trax
trax issue 7 narrows priority to high issue 3
```

This annotates the narrows edge -- equivalent to `narrows issue 3 edge priority
to high`. Pre- and post-target metadata on one edge merge.

A produced node's fields, `cost_action`, and its UNAMBIGUOUS producer-edge
metadata (`note`/`valence`) interleave freely (their heads are disjoint, so no
order is imposed):

```trax
trax belief title to b produced websearch query to q agent-cost add 1.5 note to verdict
```

The `agent-cost` lands on the inline websearch and the `note` on the `produced`
edge, regardless of which is written first. The collision words
`label`/`labels`/`priority` are the exception: in a create body they are ROW
fields; to put them on the producer edge use the `edge` marker (`edge priority
to high`).

### Tail composition rules

1. Tails compose left-to-right against the **current cursor**. At the top
   level the cursor is the leading subject; it DESCENDS into each
   inline-created node, so an edge after a create binds to that new node,
   not the leading subject (see §13). `begin ... end` pops the cursor back.
2. `delete_action` and `help_action` are **terminal**: any token after
   them is an error.
3. An `edge_action`'s `inline_create` consumes field_actions until it
   hits a non-field-name token. The new node becomes the cursor, so a
   following edge keyword binds to IT (descent); a following non-edge,
   non-field token belongs to the enclosing cursor. A complete existing
   ref target consumes only the ref plus optional edge metadata, and does
   not move the cursor (it has no fields to descend into).
4. Mixing `list_action`/`cost_action`/`edge_action`/`relation_action`
   in any order is permitted on an existing row.
   They execute in source order.
5. A field is writable only on the kinds its column applies to
   (`WRITE_FIELDS_CLI`, derived from the server's
   `applies_to_inquiry_kinds`). A write naming a field invalid for the
   subject's kind is rejected up front, before any request, so a
   multi-field write never applies some fields and then fails on another.
   Base fields (`owner`, `account`, `title`, `description`, `status`,
   labels, subscribers, costs) are writable on every kind. See E008.

## 5. Action operands

```
scalar_field    ::= one of EDITABLE_FIELDS (case-insensitive)
list_field      ::= one of LIST_FIELDS
cost_field      ::= one of COST_FIELDS
edge_keyword    ::= one of EDGE_ALIASES ∪ RELATION_ALIASES
issue_kind      ::= one of ISSUE_KINDS
relation_keyword::= one of RELATION_ALIASES (when not also an edge_keyword
                          with a digit/uuid next token)
safe_meta_op    ::= ("note" | "valence") "to" value
                    -- edge-only words: bare, no marker needed (an `edge` prefix
                    -- is also accepted but never required)
collide_meta_op ::= ("priority" | "label" | "labels") op value
                    -- ALSO row fields, so they MUST be preceded by the `edge`
                    -- marker to annotate the edge. Bare (no `edge`) they are
                    -- never edge metadata -- maximal munch rolls them up to the
                    -- nearest construct that can take a field (an open inline
                    -- create, else the leading subject), so they can never
                    -- silently land on the edge.
op              ::= "to" | "add" | "del"     -- label/labels take add/del too
value           ::= any single token; one field value may be "-" to read stdin;
                    values starting with "@" read the remaining text as a path
signed_value    ::= ( "+" | "-" )? [0-9]+ ( "." [0-9]* )?
seq             ::= [0-9]+                    -- ASCII digits only
uuid            ::= UUID v4 (8-4-4-4-12 hex, dashes required)
```

The ``kind uuid`` ref form is supported as a typed redundancy: the
client validates that the spelled kind matches the kind the server
resolves the UUID to, and raises if they disagree. ``trax belief
<paper-uuid>`` is a typo-catching error, not a silent paper edit.
``uuid`` alone trusts the server's lookup.

**Edge vs relation disambiguation.** Every edge keyword appears in both
`EDGE_ALIASES` and `RELATION_ALIASES` (e.g. `blocked_by`, `blocks`,
`narrows`, `broadens`). The next token decides
(`parser.py::_parse_relation_or_edge`):

- Nothing, or a **bare digit** → `relation_action` (projection: list
  related rows; a bare digit is the 1-based INDEX, e.g. `broadens 2`
  selects the 2nd related row -- it is NOT an edge to seq 2).
- A `kind seq` / `kind uuid` ref, or a bare UUID, or a `kind` + fields
  inline-create → `edge_action` (mutate the edge / create the target).

So a ref target is always kind-qualified (`issue 8`) or a UUID; a bare
seq after the keyword is a relation index, never an edge ref.

Every keyword is writable from both endpoints, so there are no relation-only
keys: any alias accepts a ref (to write the edge) or stands alone (to project).

## 6. List queries

```
list_query      ::= ( kind_or_range | filter )*
kind_or_range   ::= kind | range
range           ::= element ( "," element )*       -- a comma-separated union
element         ::= interval | seq                 -- bare seq is the row n..n
interval        ::= ( seq )? ".." ( seq )?         -- at least one side required
filter          ::= filter_field value_op value
                 |  filter_field null_op
filter_field    ::= one of FILTER_FIELDS[kind]
value_op        ::= "is" | "ne" | "re" | "nre" | "lt" | "le" | "gt" | "ge"
null_op         ::= "isnull" | "notnull"
```

A `null_op` tests column presence and takes **no value**: `kind isnull`
matches rows whose `issue_kind` is unset, `owner notnull` matches rows
with an owner. They mirror SQL `IS NULL` / `IS NOT NULL` -- presence is an
operator, never a sentinel value, so no real value is ever shadowed. A
`null_op` applies only to a nullable column; on a NOT-NULL column (`id`,
`kind`, `seq`, `account`, `created`, `modified`, `status`, `title`, the
cost axes) it is always-empty / always-all, so it is rejected rather than run.

List query tokens follow a bare kind (or a kind in a multi-kind list).
Filters and ranges combine with AND semantics. Multiple kinds widen
the result; per-kind ranges associate with the most recently named
kind. The literal token `to` never appears in a plain list query --
when a `field to value` mutation does appear alongside a range or
filter, the command is a `bulk_apply` (section 6.1), not a list query.

One token, `codechange`, is both a kind keyword and a list field. It
widens the listed set only when no mutation operator follows; a
following `to` / `add` / `del` makes it a field mutation. A bare kind is
never followed by an operator, so the next token disambiguates without
ambiguity (`trax experiment status is active codechange add 7` is a bulk
apply, not a two-kind list).

A range token is a comma-separated union of elements; each element is
an `interval` (`a..b`, `a..`, `..b`) or a bare `seq` (the single row
`n..n`). So `..10,222..225,227,228..` selects rows up to 10, 222
through 225, the single row 227, and everything from 228 on. The union
is one token; a stray space splits it into separate tokens, the later
of which wins per the most-recently-named-kind rule above. Overlapping
elements never list a row twice.

```trax
trax issue ..10,222..225,227,228..
```

## 6.1 Bulk apply

```
bulk_apply      ::= ( kind_or_range | filter | set_mutation )*
set_mutation    ::= scalar_field "to" value
                 |  list_field ( "to" | "add" | "del" ) value
```

A `bulk_apply` is a `list_query` carrying at least one `set_mutation`:
the ranges and filters select rows, and every `set_mutation` is applied
to each selected row. Query and mutation tokens may interleave in any
order; each `field OP value` triple is routed by its operator -- `to`
is a mutation, a `filter_op` is a predicate -- so no separator keyword
is needed.

Because a mutation may sit *before* the deciding selector, telling a
bulk apply from a bare create/tail can need lookahead to a selector
arbitrarily far right: the command language is LR-Regular, not LALR(k).
The parser realises this with one linear scan that routes every triple
by its operator, then commits to a bulk apply once it has seen both a
selector and a mutation. `grammar.lark` states this order-free language
directly and `grammar_check.py` proves it with a general (Earley)
parser; see that file for the full ambiguity accounting.

The selector is mandatory and is a range or a filter -- a bare kind
only widens the listed set, so it does not count. With no range or
filter the tokens are an anonymous create (section 3), not a bulk
apply. Only field and list mutations may ride a bulk apply; edges,
costs, and `del` may not.

**The `--makeitso` guard.** When the selector matches more than one
row, `bulk_apply` requires `--makeitso`; without it the matches are
previewed and nothing is written. A zero- or one-row match applies
directly, exactly as a seq-targeted edit would. The flag is purely an
execution guard: it never changes how tokens parse.

The matched set is selected up to the server row ceiling, not the
display `--limit`; a write covers every match, and a result that fills
the ceiling is flagged. The preview-then-rerun workflow re-queries, so
a concurrent edit between the two runs can shift the matched set.

```trax
trax issue 222.. owner to Josh
```

```trax
trax issue status is active owner to Josh --makeitso
```

```trax
trax issue 1..50 label add triage --makeitso
```

## 7. Verb commands

These are the non-row top-level verbs. Each parses its own argv with
`argparse`. Standalone verbs do not chain tails; they accept only the
flags documented here.

```
verb_name   ::= "help" | "profile" | "next" | "search" | "recent"
             |  "cost" | "blocked" | "board" | "graph" | "id"
             |  "version" | "send" | "run"
```

- `trax help [topic]` -- top-level or per-verb help. `--help`/`-h`
  anywhere is **only** accepted as a leading or trailing token; never
  in a value position.
- `trax profile [name] (action)` -- profile management; see section 8.
- `trax id <uuid> [--format table|json] [--changes]` -- show one row by its
  global id, with no leading kind (the UUID is unique, so the kind is
  redundant). Unlike `trax <kind> <uuid>` it applies no kind typo-guard.
- `trax next [--format text|json|ids]` -- show the next unblocked
  active Issue.
- `trax search QUERY... [--kind KIND] [--limit INT] [--format
  table|json|ids]` -- cross-kind title/description search.
- `trax recent [--limit INT] [--format text|json]` -- audit-log feed.
- `trax cost KIND SEQ [--deep] [--format text|json]` -- cost rollup.
- `trax blocked` -- active Issues with at least one active blocker.
- `trax board [--width INT]` -- Issues grouped by status.
- `trax graph [--open-only]` -- dependency tree.
- `trax version` -- print the CLI version.
- `trax send @actor[:room] TEXT...` -- inject a message into a live agent
  session addressed by its routing name (its `run --as` owner).
- `trax run claude|gemini|codex [--out FILE] [--verbose] [--dry-run]
  [--no-sync] [--as NAME] [--room ROOM]...` -- wrap an agent CLI, tail its
  session log, and sync turn events. `--as NAME` is the session's owner /
  routing handle (others address it `@NAME`), uniquified on collision.

## 8. Profile command

```
profile_command ::= "profile" profile_tail?
profile_tail    ::= profile_name profile_action?
                 |  profile_action
profile_action  ::= "del"
                 |  "current" profile_name
                 |  profile_field ( "to" value )?
profile_field   ::= "url" | "actor" | "token"
profile_name    ::= [a-zA-Z0-9_-]+
```

Bare `trax profile` **lists all profiles** (the active one `*`-marked),
exactly as a bare kind (`trax issue`) lists all rows. `trax profile
<name>` shows one profile's detail, like `trax issue 7` shows one row.
There is no separate `list` action -- the bare verb is the list, matching
the rest of the grammar.

A profile field-set uses the same `field "to" value` action as a row
scalar edit (§4): `profile [name] url to URL`. A bare `profile_field`
(no `to`) projects the field's current value. One field per command --
the read/set grammar matches rows exactly. Examples in section 10.

## 9. Token tables

`grammar.py` is the source of truth for every table below.
`grammar_test.py::test_grammar_tables_match_source` fails if this
section drifts from it, so edit `grammar.py` and update these lists in
the same commit.

### Kinds

`Issue`, `Artifact`, `Experiment`, `Paper`, `Belief`, `CodeChange`,
`WebResult`, `WebSearch`, `AgentSession`.

### Issue kinds (`issue_kind`)

`feature`, `bug`, `task`, `question`.

### Editable scalar fields (`EDITABLE_FIELDS`)

`owner`, `account`, `title`, `description`, `status`, `validation`,
`priority`, `judgement`, `confidence`, `outcome`, `source`,
`google_scholar_cluster_id`, `google_scholar_cites_id`,
`abstract`, `publication_type`, `venue`, `subvenue`, `publish_date`, `query`,
`provider`, `sha`, `url`,
`cli`, `cli_session_id`, `started`.

### List fields (`LIST_FIELDS`)

`label`, `labels`, `subscriber`, `subscribers`, `kind`, `issuekind`,
`issue_kind`, `author`, `authors`, `codechange`, `codechanges`.

### Cost fields (`COST_FIELDS`)

`agent-cost`, `resource-cost`.

### Edge keywords (`EDGE_ALIASES`)

`narrows`, `narrowed_by`, `broadens`, `broadened_by`, `requires`, `required_by`,
`blocked_by`, `blocks`, `produced`, `produced_by`, `produces`, `proves`,
`proved_by`, `disproves`, `disproved_by`, `favors`, `favored_by`, `disfavors`,
`disfavored_by`, `supersedes`, `superseded_by`, `cites`, `cited_by`.

"proved/disproved" are intentionally hyperbolic. Epistemologically,
"credited/discredited" would be more precise, but Trackinizer uses
stronger names to keep load-bearing evidence (`proves`/`disproves`)
sharply separated from weaker framing context
(`favors`/`disfavors`).

`proves`/`disproves` and `favors`/`disfavors` all store as
`Artifact -> claim` (`{Belief, Experiment}`): a claim is proven or favored
*by* the citing artifact, so the active reading is `paper proves belief` with
the Artifact on the from-side. The polarity (proves vs disproves) is the sign
of the edge's `valence`, not a separate stored kind. The `*_by` aliases let the
user anchor at the other endpoint without changing the stored edge:

    trax belief 3 proved_by paper 5  -> stores Paper#5 -> Belief#3
    trax paper 5 proves belief 3     -> stores Paper#5 -> Belief#3
    trax belief 1 proved_by experiment 5
                                     -> stores Experiment#5 -> Belief#1
    trax paper 5 favors belief 3     -> stores Paper#5 -> Belief#3
    trax belief 3 favored_by paper 5 -> stores Paper#5 -> Belief#3

`cites`/`cited_by` are a separate, non-epistemic edge: a HISTORICAL citation
storing `Paper -> Paper` (`cites_paper`), the citing paper's bibliography. Unlike
`proves`/`favors` it carries NO valence (it records that one external source
cites another, not our judgement) and is `Paper -> Paper` only. `A cites B`
stores `A -> B`; `B cited_by A` is the same stored edge anchored at the cited
paper.

    trax paper 5 cites paper 8   -> stores Paper#5 -> Paper#8
    trax paper 8 cited_by paper 5 -> stores Paper#5 -> Paper#8

### Relation-only keywords (`RELATION_ALIASES` minus edges)

None: every keyword is writable from both endpoints, so every relation alias is
also an edge alias.

### Statuses (`Inquiry.Status`)

`active`, `complete`, `abandoned`, `invalid`.

### Priority aliases (`PRIORITY_ALIASES`)

`critical=0`, `high=10`, `medium=20`, `low=30`, `backlog=40`. Integer
literals are also accepted.

### Sort choices (`SORT_CHOICES`)

`priority`, `seq`, `recent`, `oldest`, `valence`.

### Filter ops (`FILTER_OPS`)

`is`, `ne`, `re`, `nre`, `lt`, `le`, `gt`, `ge`, `isnull`, `notnull`.

`re` matches a regex; `nre` is its negation (no match). `ne` is not-equal
(exact); there is no separate negated-ordering op, since `not gt` is `le`,
`not lt` is `ge`, and so on. `isnull` / `notnull` take no value and test
column presence (SQL `IS NULL` / `IS NOT NULL`); a NULL column also passes
`ne` / `nre` (absent is trivially unequal) but fails the order ops.

## 10. Examples

Every fenced `trax` block in this section must parse. The
`grammar_test.py` extracts and parses each.

### Listing

```trax
trax
```

```trax
trax issue
```

```trax
trax issue 1..50
```

```trax
trax issue belief 1..20 status is active
```

```trax
trax issue status is active kind notnull owner isnull
```

### Show / detail

```trax
trax issue 7
```

```trax
trax issue 7 title
```

```trax
trax belief 3 confidence
```

### Multi-subject show

```trax
trax issue 5 6 12
```

```trax
trax issue 1 belief 3
```

### Create

```trax
trax issue title to "Retry bug" priority to high
```

```trax
trax belief title to "PNT holds" confidence to 0.9 judgement to proven
```

One field value may be `-`, which reads stdin at runtime. Any field value that
starts with `@` reads the rest of the token as a file path, including shell
process-substitution paths such as `@<(printf ...)`:

```bash
trax issue title to "Retry bug" description to - <<'EOF'
Long body.
EOF
```

```bash
trax issue title to "Retry bug" description to @body.md
```

### Edit / mutate

```trax
trax issue 7 priority to high
```

```trax
trax issue 7 status to complete
```

```trax
trax issue 7 label add backend
```

```trax
trax issue 7 label del backend
```

```trax
trax issue 7 agent-cost add 1.25
```

```trax
trax issue 7 resource-cost add -0.20
```

### Delete

```trax
trax issue 7 del
```

### Edge link / annotate / unlink

The Issue prerequisite relation is spelled `requires` / `required_by`:

- `A requires B` -- B is a prerequisite of A; B must be done before A.
- `A required_by B` -- the inverse: A is a prerequisite of B; A must be
  done before B.

The two spellings address the SAME stored edge from opposite endpoints
(`A requires B` == `B required_by A`); pick whichever reads naturally for
the subject you are editing. These examples use `requires` / `required_by`
because they are unambiguous about direction. The `blocks` / `blocked_by`
aliases name the same edge but invert the reading -- `A blocks B` is the
same edge as `A required_by B`, and `A blocked_by B` is the same as
`A requires B` -- so they are easy to misread and are avoided here.

```trax
trax issue 7 required_by issue 8
```

```trax
trax issue 7 narrows issue 3 priority to high
```

```trax
trax issue 7 required_by issue 8 del
```

### Composite: create + edges

```trax
trax issue title to "foo" requires issue 7
```

```trax
trax issue title to "foo" requires issue 7 required_by issue 8
```

```trax
trax issue 7 requires issue title to "foo"
```

```trax
trax issue 7 requires issue title to "foo" priority to high kind to bug required_by issue 8
```

The last example parses as: subject = issue 7, then `requires <inline create
of an Issue with title=foo, priority=high, kind=bug>`. The `required_by` token
cannot be a field name, so the inline create's FIELD list terminates at it --
but the cursor has DESCENDED into the new `foo`, so `required_by issue 8` is
`foo`'s edge, not issue 7's: `7 requires foo` and `foo required_by issue 8`. To
put the second edge on issue 7 instead, close `foo` with `begin ... end`:
`issue 7 requires begin issue title to "foo" ... end required_by issue 8`.

### Multiple new vertices in one command

The cursor DESCENDS into each inline-created node, so an edge written
after a create binds to that **new node**, not the leading subject. A
chain of creates therefore nests:

```trax
trax issue 7 requires issue title to "prereq-a" requires issue 5 required_by issue title to "leaf-b"
```

Only `prereq-a` attaches to issue 7. The cursor then descends into
`prereq-a`, so `requires issue 5` and `required_by "leaf-b"` both attach
to `prereq-a` (the current cursor), giving
`7 -> prereq-a`, `prereq-a -> issue 5`, `prereq-a -> leaf-b`.

```trax
trax issue title to "root" requires issue title to "leaf-a" required_by issue title to "leaf-b"
```

This is a CHAIN, not two siblings: `root -> leaf-a -> leaf-b`. The
`required_by "leaf-b"` descends into `leaf-a` (the cursor after creating
it). To attach two NEW children to `root` as SIBLINGS, wrap each in
`begin ... end` so `end` pops the cursor back to `root`:

```trax
trax issue title to "root" requires begin issue title to "leaf-a" end requires begin issue title to "leaf-b" end
```

```trax
trax issue 7 requires issue 5 requires issue 8
```

Adds two existing prerequisites in one command. Repeating an edge keyword
under the same anchor is **legal** -- each occurrence introduces a new
edge to a distinct target. The parser disambiguates by reading each
`edge_action` greedily up to the next tail keyword or end-of-tokens.

#### Worked example -- two inline creates as siblings under one anchor

Two inline creates chained directly would NEST (the second `requires`
descends into `foo`). To attach both as siblings of issue 7, wrap each in
`begin ... end`:

```trax
trax issue 7 requires begin issue title to foo end requires begin issue title to bar end
```

Token walk:

```
issue                 → row_command, kind = Issue
  7                   → seq → subject = ref(Issue, 7)
  requires            → tail1: edge_action
    begin             → push: edge_target is a begin..end group
      issue           → group_create starts with a kind
        title to foo  → field_action
    end               → pop: cursor back to issue 7, group closes (title=foo)
  requires            → tail2: edge_action (cursor is issue 7 again)
    begin             → push
      issue           → group_create
        title to bar  → field_action
    end               → pop: cursor back to issue 7 (title=bar)
```

Parse tree:

```
Command(
  subject = Ref(Issue, 7),
  tails = [
    EdgeAction(requires, InlineCreate(Issue, title="foo")),
    EdgeAction(requires, InlineCreate(Issue, title="bar")),
  ],
)
```

Plan:

```
1. create anon_1 = Issue(title="foo")
2. create anon_2 = Issue(title="bar")
3. edge: Ref(7)  requires  anon_1
4. edge: Ref(7)  requires  anon_2
```

Net effect: issue 7 ends with two new prerequisites, both freshly created.
Each `requires` is a distinct edge to a distinct target; the
inline_create boundary disambiguates without any "duplicate keyword"
rule.

### Deep chains and `begin ... end` for width

The cursor descends into each inline create, so an edge after a create
binds to that NEW node (a chain), not the leading subject:

```trax
# `produced artifact 5` binds to the new "foo", NOT issue 7
# (foo produced artifact 5):
trax issue 7 requires issue title to "foo" produced artifact 5
```

That is `7 -> foo` and `foo -> artifact 5`. To instead attach a sibling
under issue 7 (fan out), wrap the child in `begin ... end`; `end` pops
the cursor back to issue 7:

```trax
trax issue 7 requires begin issue title to "foo" end produced artifact 5
```

Now `produced artifact 5` rebinds to issue 7 (`7 -> foo`, `7 -> artifact
5`). See section 13 for the descent model.

### Relation projection

```trax
trax issue 7 requires
```

```trax
trax issue 7 broadens
```

### Verb commands

```trax
trax help
```

```trax
trax help issue
```

```trax
trax next --format ids
```

```trax
trax search retry timeout --kind issue --limit 10
```

```trax
trax recent --limit 20
```

```trax
trax cost issue 7 --deep
```

```trax
trax blocked
```

```trax
trax graph --open-only
```

### Profile

```trax
trax profile
```

Profile commands are stateful: the named profile must exist before
``trax profile <name> token ...`` / ``trax profile current <name>`` /
``trax profile <name> del`` will work. The lifecycle below runs as a
single sequence so each step builds on the previous one:

```trax-seq
trax profile prod url to https://trackinizer.example
trax profile prod
trax profile prod token to trax__abcdef
trax profile current prod
```

Deleting a profile must happen while it is *not* the active one:

```trax-seq
trax profile staging url to https://staging.trackinizer.example
trax profile staging del
```

## 11. Counterexamples

Every fenced `trax!` block must be rejected with the named error code.

```trax! E001
trax issue foo
```

Reason: after `kind` the next token must be `seq`, `uuid`, or a field
name. `foo` is none of those.

```trax! E003
trax issue 7 del title to foo
```

Reason: `del` is a terminal tail; nothing may follow.

```trax! E004
trax issue 7 title to foo title to bar
```

Reason: a scalar field can be set at most once per command. Use two
commands or one command with the final value.

(Note: this rule is a deliberate restriction in v1; we may relax it
if a use case appears.)

```trax! E005
trax issue title to foo del
```

Reason: `del` is not valid on a create subject. The row has no
existing identity to delete.

```trax! E006
trax issue 222.. required_by issue 8
```

Reason: a `bulk_apply` accepts only field and list mutations; an edge
action may not ride one. Issue the edge per row instead.

```trax! E006
trax issue kind isnull bug
```

Reason: `isnull` is a `null_op` and takes no value. The trailing `bug`
is neither a further selector nor a field mutation, so once `isnull`
commits the query it is rejected rather than silently ignored.

```trax! E009
trax issue status isnull
```

Reason: `status` is a NOT-NULL column, so `isnull` / `notnull` is
always-empty / always-all -- a silent wrong answer. A `null_op` is
rejected on any NOT-NULL column.

```trax! E007
trax profile prod url https://trackinizer.example
```

Reason: a profile field-set uses the `field "to" value` action, like a
row edit. Bare adjacency (`url VALUE` without `to`) is no longer a
production -- write `profile prod url to https://...`.

```trax! E008
trax issue 1 status to complete judgement to proven
```

Reason: `judgement` is valid only on `Belief`, not `Issue`. The whole
write is rejected up front so `status` does not commit while
`judgement` fails -- a multi-field write applies all fields or none.



## 12. Error codes

| Code | Name                          | Where raised               |
|------|-------------------------------|----------------------------|
| E001 | unknown row position          | row_command disambiguation |
| E002 | relation does not accept a ref| relation_action            |
| E003 | tokens after terminal action  | tail composition           |
| E004 | scalar field set twice        | field_action               |
| E005 | del on create subject         | tail composition           |
| E006 | non-field action in bulk apply| bulk_apply                 |
| E007 | profile set without 'to'      | profile_action             |
| E008 | field not valid on kind       | run_actions / run_create   |
| E009 | null_op on a NOT-NULL column  | list_query / bulk_apply    |
| ...  | (extend as needed)            |                            |

These codes are documentation labels for the rejection, not distinct
exception classes: the parser raises a single `ClientError` with a
descriptive message (see `client/errors.py`). The message text may evolve;
the codes here are stable identifiers for each rejection rule.

## 13. Deep cursor: descent by default, `begin ... end` for width

The cursor model is **deep by default, `begin ... end` for width**. Each
inline-created node becomes the new cursor: an edge written after a
create binds to that node, so juxtaposition descends. `A produced B
produced C` is `A -> B -> C` (the second `produced` binds to B). To go
WIDE -- a sibling under the same anchor -- wrap the child's subtree in
`begin ... end`; `end` pops the cursor back to the parent.

Why:

- **One uniform rule.** Every edge descends into its target; `begin ...
  end` is the only way to go wide. No per-verb special cases.
- **Deterministic binding.** Descent is the common case (research trees
  are mostly spine: belief -> search -> paper) and is free; width is rare
  and costs one balanced `begin ... end` pair.
- **Shell-safe, self-checking delimiters.** `begin`/`end` are bare words
  (inert in bash, zsh, fish, unlike `()`/`[]`/`{}`/`<>`), and a miscounted
  depth is a loud unbalanced-keyword error, not a silent mis-binding.

The plan is therefore a TREE rooted at the leading subject, not a flat
anchor + edges. An inline-created node carries its own outgoing edges,
its own cost, and the producer-edge metadata written right after its
fields (a verdict `note` sits beside the node it describes -- see §4).
This is implemented by `_consume_inline_create` in `parser.py` and the
`deep_create` / `group_create` productions in `grammar.lark`.

## 14. No-corner-cases policy

A parser change without a grammar change is forbidden. The grammar
changes only by editing this file plus `grammar.py` together. CI
enforces:

1. Every example block parses successfully.
2. Every counterexample block raises the named error code.
3. The token tables in section 9 match the live `grammar.py` exports.
4. Every grammar production has at least one example and one
   counterexample (where a counterexample is meaningful).
5. The help renderer's output references only productions from this
   file (no hand-written taxonomies elsewhere).

When a real-world input doesn't match any production, the fix is
either: (a) add a production (with example, counterexample, and code
change in one PR), or (b) reject with a named error. There is no
third option.

## 15. Versioning

This document defines the v1 grammar. Breaking changes increment the
version; the binary refuses to run if a profile records an older
grammar version it cannot interpret. (Until v2 exists this is a
no-op.)
