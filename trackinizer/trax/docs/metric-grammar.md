# trax `metric` grammar

Status: design, in build. Owner: Josh (Issue#602 metrics UI).

`metric` is a row tail on an Experiment (and a cross-experiment query when no
ref is given). It reads and writes the metric grid a run owns: a value at each
`(key, step)` coordinate, stored in `experiment_metrics`.

The design reuses trax's existing filter/set vocabulary. The only new word is
`at`, which scopes a coordinate on the metric grid (the `key` or `step` axis)
the way a bare field name scopes an `inquiries` column in a filter.

## Model

The metric grid of one experiment is an array `(key, step) -> value` -- and the
grammar is numpy boolean-mask indexing spelled in words. Every operation is:

1. **select** cells with one or more `at <field> <op> <value>` clauses. Each
   `at` clause is one trax filter triple (`field op value`) whose field is a
   grid axis (`key`, `step`, `value`); the clauses AND together into a mask,
   exactly like `arr[key=='loss', step>3]`.
2. **operate** on the mask: `to <value>` assigns to it (`arr[mask] = v`); no
   trailing op reads it (`arr[mask]`).

`at` lifts trax's existing filter grammar onto the grid, but only the comparator
subset that makes sense on a numeric, non-nullable grid: `is`/`ne`/`lt`/`le`/
`gt`/`ge`. The regex (`re`/`nre`) and presence (`isnull`/`notnull`) ops of the
inquiry `FilterOp` set are excluded -- a metric cell is neither text-matchable
nor nullable. `is` is equality, uniform with every other filter -- there is no
separate assert. `to` is the sole non-`at` op (the setter).

Fields (axes): `key` (a metric name like `loss`), `step` (an integer), `value`
(the stored number). All three are filterable; `to` writes.

Shorthand: `at <bareword>` with no op is `at key is <bareword>` -- a bare token
after `at` that is not one of the reserved field words `key`/`step`/`value` is a
key with equality implied. So `at loss` is `at key is loss`; `at step is 4` and
`at value gt 0.9` stay explicit (a value follows the field). This keeps the
everyday write terse (`at step is 4 at loss to 0.5 at acc to 0.9`) without a
second syntax.

## Write

`to` sets every selected cell. `step` must be selected (a metric point has no
default step).

`to <value>` assigns to the mask (`arr[mask] = value`). A single-cell write pins
`key` and `step` to exact values with `is`; `step` must be constrained on a
write (a metric point has no default step).

```
# one cell
trax experiment 42 metric at key is loss at step is 3 to 0.5

# many keys at one step (repeat the key clause; step stays constrained)
trax experiment 42 metric at step is 3 at key is loss to 0.5 at key is acc to 0.9

# many steps for one key (repeat the step clause; key stays constrained)
trax experiment 42 metric at key is loss at step is 3 to 0.5 at step is 5 to 0.6

# bulk write: set every loss cell with step > 3 to 0.5 (the mask matches many;
# requires --makeitso, below)
trax experiment 42 metric at key is loss at step gt 3 to 0.5
```

A cell write is an upsert on `(experiment_id, key, step)`; a duplicate cell in
one command (or a re-run) dedups, the count reported via `skipped`, mirroring
the existing log idempotency.

`value` must be finite (the DB CHECK forbids NaN/Inf; the CLI rejects a
non-finite value before sending).

## Read

No trailing `to` = read the masked cells, printed in `(key, step)` order.

```
trax experiment 42 metric at key is loss at step gt 3   # loss cells, step > 3
trax experiment 42 metric at step is 3                  # every key at step 3
trax experiment 42 metric at key is loss                # loss's whole series
trax experiment 42 metric at value gt 0.9               # cells with value > 0.9
trax experiment 42 metric                               # the whole grid
```

`sort` / `limit` order and window a read (same words as the inquiry list):

```
trax experiment 42 metric at key is loss sort desc limit 5   # loss's 5 largest
```

## Cross-experiment

A `metric` tail on the bare `experiment` list (no ref) runs across every
experiment the list selects. This is the leaderboard/rank surface: there is no
`leaderboard` noun -- it is the experiment list query plus a metric slice plus
`sort`/`limit`.

```
# loss@100 across all experiments
trax experiment metric at loss at step is 100

# top 5 experiments by loss@100
trax experiment metric at loss at step is 100 sort desc limit 5

# constrained to a label, then ranked
trax experiment label is ml metric at loss at step is 100 sort desc limit 5

# "final" = highest step per experiment; "best" = sort by value
trax experiment metric at loss at step max            # final per experiment
trax experiment metric at loss sort desc limit 1      # best per experiment
```

The cross-experiment result lists experiments, each annotated with its selected
metric value(s); `sort`/`limit` rank them. Direction is the `sort` order the
caller states -- never a stored declaration -- so the inversion bug class has no
home.

## Create + log in one command

`metric` is a create tail like any other, so a create (`title to ...`) followed
by a `metric` tail creates the Experiment and logs in one command:

```
trax experiment title to "trm exp031" metric at step is 3 at loss to 0.5 at acc to 0.9
```

## Grammar summary

```
metric_tail   ::= "metric" mask_clause* write? read_opts?
mask_clause   ::= "at" field op value        -- explicit mask on a grid axis
                | "at" key                    -- bareword key: "at key is <key>"
field         ::= "key" | "step" | "value"
op            ::= "is" | "ne" | "lt" | "le" | "gt" | "ge"   -- existing FilterOps
                | "max" | "min"               -- reductions on the step axis
write         ::= "to" value                  -- assign to the masked selection
read_opts     ::= ("sort" ("asc" | "desc"))? ("limit" INT)?
```

A `write` (`to`) is optional; its absence is a read. `sort`/`limit` apply only
to a read (a write has no ordering). `at step max` / `at step min` are step-axis
reductions (highest/lowest step per key), used for "final"/"first".

Bulk `to` (a mask that resolves to more than one cell) requires `--makeitso`,
mirroring the inquiry bulk-edit guard, so a fat-fingered `at step gt 0 to 0`
cannot silently overwrite a run.

## Rules

- `at <bareword>` (not `key`/`step`/`value`) means `at key is <bareword>`.
- `step` must be masked on a write (no default step).
- `value` on a write must be finite.
- A bulk `to` (multi-cell mask) requires `--makeitso`.
- No `to` = read.
- `sort` / `limit` apply to reads and cross-experiment ranks, reusing the list
  words.
- Cross-experiment = a `metric` tail on the bare `experiment` list.
