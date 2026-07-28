---
name: trax-webresult
description: ALWAYS invoke this skill when adding a trackinizer WebResult -- a web page or a paper's code/data companion, url + produces-link, dedup on url. Do not write the WebResult directly -- invoke this skill first.
---

# WebResult — authoring expectations

A `WebResult` is one web page, citeable like any other artifact. In a paper
graph it is most often a **companion** of a Paper (its code repo, dataset,
notebook, or project page).

Per-kind expectations doc (SoT). Fields owned by `types/inquiries.py`; grammar by
`../trax-skill.md`.

## Completeness bar

- **url** = the page URL.
- **title** = the page/repo title ("Code: TinyRecursiveModels"); slug if none.
- `description` = excerpt/notes when useful.

## Companion-of-paper pattern

A paper's code/data companion is a WebResult the paper `produces`:

```bash
trax webresult title to "Code: NVARC" url to https://github.com/1ytic/NVARC
trax paper 95 produces webresult <wseq>
```

Stored as `WebResult produced_by Paper`. Dedup WebResults on `url` (two papers
citing the same repo share one node, each with its own produce edge). Keep the
paper's OWN artifact; filter dependency/library repos.

## Expectations

- Every paper with a real code/data companion should have ≥1 linked WebResult
  (see `paper.md`).
- A WebResult can stand alone as evidence that `proves`/`favors` a Belief, or be
  produced by a WebSearch that surfaced it.

## Common mistakes

- Duplicating a WebResult per citing paper instead of sharing one node on `url`.
- Attaching a dependency repo rather than the paper's own artifact.
- Burying a URL in a Paper/Issue description instead of a first-class WebResult.
