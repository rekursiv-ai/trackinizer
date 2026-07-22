---
name: trax-websearch
description: Use when recording a trackinizer WebSearch -- verbatim query, provider, findings as produced_by edges. Not trax grammar (trax).
---

# WebSearch — authoring expectations

A `WebSearch` records a web/paper search and the references it surfaced, so the
search is reproducible and its findings are linked.

Per-kind expectations doc (SoT). Fields owned by `types/inquiries.py`; grammar by
`../trax-skill.md`.

## Completeness bar

- **query** = the search string exactly as issued (reproducibility).
- **provider** = the engine (`google`, `duckduckgo`, `arxiv`, `scholar`, `s2`,
  `crossref`); empty only when genuinely untracked.

## Findings are edges, not a column

Papers/pages a search surfaced are recorded as `produced_by` edges
(WebResult/Paper → WebSearch: the finding is the younger child of the search),
NOT a field. Found artifacts are many-to-one (two searches surfacing the same
Paper share one node, each with its own produce edge).

## Expectations

- A WebSearch can itself cite a claim: `websearch S proves belief P` with positive
  valence ("a standard search confirms this is widely reported") or negative ("the
  position appears in no major index").
- Record the query verbatim; a paraphrase breaks reproducibility.

## Common mistakes

- Storing findings as prose instead of `produced_by` edges to real artifact rows.
- Paraphrasing the query.
- Omitting the provider when it is known.
