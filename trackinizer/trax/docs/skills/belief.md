---
name: trax-belief
description: Use when authoring a trackinizer Belief -- falsifiable proposition, judgement/confidence, signed-valence proves/favors citations (claims are cited, papers are not). Not trax grammar (trax).
---

# Belief — authoring expectations

A `Belief` is a proposition whose support comes from cited artifacts. Write it so
the claim is falsifiable and its evidence is legible.

Per-kind expectations doc (SoT). Fields owned by `types/inquiries.py`; grammar by
`../trax-skill.md`.

## Completeness bar

- **title** = the proposition as a single, checkable sentence ("TRM's puzzle-ID
  embedding is load-bearing memorization"), not a topic.
- **judgement** ∈ {proven, disproven, unproven, undecidable} — the coarse verdict;
  set deliberately, `unproven` is the normal state for an open musing.
- **confidence** ∈ [0,1] — calibrated probability the proposition is true; `0.5`
  is the neutral prior. Orthogonal to judgement and status.
- **citations** — at least the load-bearing evidence, as signed edges (below).
- `description` — the argument/context when the title alone underspecifies.

## Citations: the epistemic edge (NOT paper→paper)

Citations store **Artifact → {Belief, Experiment}**. The evidence is the citer;
the Belief is the target. So:
- `trax paper Q proves belief P` — Q is **load-bearing** evidence for P (votes in
  the proof predicate).
- `trax paper Q favors belief P` — Q is **context** (informs, does not vote).
- For-vs-against is the **sign of valence** ([-1,1], default 0.5), not a separate
  kind. `disproves`/`disfavors` are aliases that negate valence.

Do NOT model a paper citing a paper as a Belief citation — that is `cites_paper`
(see `paper.md`). A Belief is a *claim*; a Paper is *evidence*. "Two papers agree"
is expressed as both `proves`/`favors` the same Belief, not one citing the other.

## Expectations

- One proposition per Belief; split compound claims.
- Every consequential Belief carries ≥1 signed citation; a bare Belief with no
  evidence is a TODO, not a finding.
- judgement/confidence are set by the author from the evidence — nothing sets them
  automatically; update them as citations accrue.
- Status (`invalid` = retracted framing) is orthogonal: a `proven` Belief can be
  superseded by a sharper one; an `invalid` Belief can still read `proven`.

## Common mistakes

- A topic as the title instead of a falsifiable sentence.
- Citing a Paper FROM a Belief in the wrong direction (evidence points UP at the
  claim; the stored edge is Artifact→Belief).
- Using a separate edge kind for "against" instead of negative valence.
- Leaving judgement/confidence at defaults after evidence has accrued.
- A Belief with no citations presented as settled.
