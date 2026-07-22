---
name: trax-experiment
description: Use when authoring a trackinizer Experiment -- codechange links, outcome with metric/denominator/split, config JSON, proved_by. Not trax grammar (trax).
---

# Experiment — authoring expectations

An `Experiment` is an empirical measurement produced by code at one or more
commits. Write it so the result is reproducible and its provenance is traceable.

Per-kind expectations doc (SoT). Fields owned by `types/inquiries.py`; grammar by
`../trax-skill.md`.

## Completeness bar

- **title** = what was measured, specifically ("TRM 7M on ARC-AGI-2 eval,
  test-time-FT").
- **codechanges** = the `CodeChange` row(s) the run executed at — the exact code
  state. More than one when the experiment compares states. Link real rows; use
  the full 40-char SHA (see `codechange.md`).
- **outcome** = what was observed, as free text with the number AND its
  measurement condition ("24.0% exact on ARC-AGI-2 private, pass@2, $0.20/task").
  Empty until the run concludes.
- **config** = the run's input settings (hyperparameters) as one JSON object — the
  `wandb.init(config=...)` analogue. Opaque to trackinizer; store verbatim.
- Lifecycle on `status`: `active` while running, `complete` when done, `invalid`
  if retracted.

## Provenance and citation

- An Experiment is produced by the Issue/Belief whose inquiry spawned it
  (`produced_by`, auto-inferred by creation order).
- An Experiment is a **citation target** like a Belief: `paper P proves
  experiment E` / `favors` with signed valence. Whether a run is load-bearing
  for/against a claim is the citer's stance, not the Experiment's — there is no
  confirmed/refuted field.

## Expectations

- The outcome names the metric, denominator, split, and cost — never a bare
  percentage. Label report-only / best-of-sweep numbers as such.
- config is complete enough to re-run; do not bury settings in the outcome prose.
- Negative and abandoned runs are recorded at the same resolution as wins
  (`status` = `invalid`/`abandoned` with the reason).

## Common mistakes

- A bare number in outcome with silent denominator/split.
- Omitting the codechange link (unreproducible provenance).
- Putting hyperparameters in prose instead of `config` JSON.
- Treating the Experiment itself as "confirmed/refuted" rather than letting the
  citing artifact carry the signed stance.
- Deleting a failed run instead of marking it `invalid`/`abandoned`.
