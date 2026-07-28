---
name: trax-issue
description: ALWAYS invoke this skill when writing a trackinizer Issue or bug report -- context, success criteria, implementation notes, verification; the standard for a well-formed issue body. Do not write the Issue body directly -- invoke this skill first.
---

# Issue — authoring expectations

Write bug and issue reports that preserve enough evidence for another
engineer or agent to act without redoing the investigation. The writeup
must make the work independently implementable and verifiable.

This is the per-kind expectations doc for `Issue` (one of the
`trax/docs/skills/*.md` set, the SoT for how each inquiry kind should be
filled). It covers CONTENT quality; for command mechanics (`trax issue ...`),
see `../trax-skill.md`.

## General issue body standard

Use this structure when details are available:

```markdown
## Context
Why does this issue exist? What problem does it solve?
Link to related issues, prior discussions, or external references.

## Success Criteria
- [ ] Concrete, verifiable outcome 1
- [ ] Concrete, verifiable outcome 2
- [ ] Tests pass / no regressions

## Implementation
Step-by-step plan for how to implement this.
1. First, ...
2. Then, ...
3. Finally, ...

Include relevant code paths, function names, or architectural notes.

## Affected Files
- `path/to/file1.py` -- what changes here
- `path/to/file2.py` -- what changes here

## Verification
How to verify this issue is done:
- Run `command` and expect `result`
- Check that `behavior` works as expected
```

Minimum quality bar: an engineer or agent reading only the issue
should be able to implement it without asking clarifying questions.

Before considering an issue ready, verify:

1. The title is actionable.
2. Context explains why the issue matters.
3. Success criteria are checkable.
4. Implementation notes mention actual files, functions, or patterns
   when known.
5. Affected files are listed when known.
6. Verification names concrete commands or observable behavior.

## Bug report standard

Match the level of specificity of a strong, concrete bug writeup —
observable symptom, minimal repro, and expected-vs-actual — not
necessarily any particular set of headings.

Each bug entry should include:

1. **Short title.** Name the broken behavior, not the fix.
2. **File and line evidence.** Cite `path:line` or a tight range.
3. **Observed behavior.** State what the code does now.
4. **Why it is wrong.** Explain the violated invariant, user-visible
   failure, corrupted state, or misleading metric.
5. **Question.** Name the design or causality question the fix must
   answer.
6. **Discussion.** Record the evidence and alternatives considered
   when the answer is not obvious.
7. **Conclusion.** State the chosen fix or decision precisely enough
   to implement.
8. **Verification guard.** Name the test, assertion, repro, or
   invariant that prevents recurrence.
9. **Status.** Open, fixed, deferred, or rejected, with the reason
   when not fixed.

For one bug:

```markdown
## Short broken-behavior title

**File:** `path/to/file.py:123-130`

Current behavior and evidence.

**Question:** What has to be decided or explained?

**Discussion:** Evidence, tradeoffs, rejected fixes, and why.

**Conclusion:** Specific fix or decision.

**Verification:** Test or check that catches this bug.

**Status:** Open.
```

For an audit with many bugs, use numbered sections like `bugs16` and
keep each entry self-contained.

## Sufficiency check

A bug or issue writeup is sufficient when a reader can answer:

- What exact code or behavior is implicated?
- What failure happens, or what value will the work deliver?
- Why does it matter?
- What uncertainty remains?
- What decision or implementation path was chosen?
- How will we know the work is done and the bug cannot recur?

If any answer is missing, keep investigating or mark the gap
explicitly.

## Common mistakes

| Mistake | Fix |
|---|---|
| Vague title like "serialization issue" | Name the failure: "Bytes serialization round-trip is broken" |
| No file or line citation | Read the code and cite the smallest relevant range |
| Jumping straight to a patch | Write the question and conclusion so the design choice is visible |
| Listing symptoms only | Explain the invariant or contract being violated |
| No recurrence guard | Add the test or check that would have caught it |
| Shell-escaped multi-line issue bodies | Use `trax issue ... description to @path` or `description to -` with a heredoc, not raw shell escaping |
| Hiding uncertainty | Put uncertainty under `Question` or `Discussion` |
