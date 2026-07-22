---
name: trax-codechange
description: Use when attaching a git commit as a trackinizer CodeChange -- full 40-char SHA, produced-edge linking, purpose labels. Not trax grammar (trax).
---

# CodeChange — authoring expectations

A `CodeChange` is one git commit, citeable like any other artifact.

Per-kind expectations doc (SoT). Fields owned by `types/inquiries.py`; grammar by
`../trax-skill.md`.

## Completeness bar

- **sha** = the **full 40-character** SHA from `git rev-parse <ref>`, never
  abbreviated.
- **title** = what the commit does (mandatory when created inline via the
  `produced` edge; omitting it returns HTTP 422 `title Field required`).
- Purpose tags (`bugfix`, `feature`, `refactor`) go in `labels`.

## Linking

Attach a commit via the `produced` edge, not a field-set:

```bash
# create + link (title mandatory):
trax issue 316 produced codechange sha to "$(git rev-parse 8c5658d2a)" \
  title to "retry gate + NoticeMessage" --as Agent
# link an existing codechange by seq:
trax issue 316 produced codechange 73 --as Agent
```

`trax issue 7 codechange to 73` is WRONG — that projects a field, it does not
link. Verify with `trax issue 7 produced`.

## Expectations

- Full SHA always; a run's provenance breaks on an abbreviated one.
- As a first-class artifact, a CodeChange can `proves`/`favors` a Belief and be
  produced by an Issue.

## Common mistakes

- Abbreviated SHA.
- Inline create without `title to` (422).
- `codechange to <seq>` field-set instead of the `produced` edge.
