---
name: trax-paper
description: Use when creating, uploading, or completing a trackinizer Paper (bibliographic node) -- full field cascade, DOI resolution, companion WebResults, cites vs proves. Not trax grammar (trax) or graph-scale snowball (survey-snowball).
---

# Paper — authoring expectations

Populate a `Paper` node as completely as the public record allows. Every
bibliographic field is **best-effort ("try")**: attempt each via the source
cascade below, fill what resolves, record what does not — never fail the node
for a missing optional field, never invent a value, never write a placeholder.

Per-kind expectations doc (SoT). Fields are owned by `types/inquiries.py`; write
grammar by `../trax-skill.md`. This file owns *what a complete Paper looks like
and how to source it.*

## Completeness bar

A well-formed Paper has attempted (filled OR gap-noted) every field:
`title, authors, abstract, publish_date, venue, publication_type, source`, plus
`doi` resolution, companion WebResults, and ≥1 label. Report which fields stayed
empty and WHY (source had no value vs. not attempted — the latter is a bug).

## Source cascade (attempt each in order; first hit wins)

| Field | Cascade | Notes |
|---|---|---|
| title | S2 batch → arXiv API `<title>` → Crossref | collapse whitespace |
| authors | S2 `authors[].name` → arXiv `<name>` → Crossref `author[]` | ordered byline; ~20 cap |
| abstract | S2 `abstract` → arXiv `<summary>` → Crossref | ~2000 char truncate |
| publish_date | S2 `publicationDate` → arXiv `<published>` → Crossref `issued` | ISO. Never FABRICATE a date: no epoch, no invented `-01-01`. But a source's genuine year-only date (Crossref returns `YYYY-01-01`) is REAL — keep it. Distinguish by provenance: a `-01-01` that came WITH authors from a real Crossref hit is a legit year-only date; a `-01-01` with no source behind it is a fabricated placeholder — leave empty instead. |
| venue | S2 `venue` → Crossref `container-title` | canonical name below |
| publication_type | Crossref `type` / inference | `article`, `inproceedings`, `misc` |
| doi | S2 `externalIds.DOI` → Crossref bibliographic query | resolve EVEN for arXiv papers |
| source | the scheme-tagged id chosen | `arXiv:...` / `doi:...` / `https://...` |
| google_scholar_cluster_id | ONLY when a Scholar result is already in hand | the Scholar `data-cid` cluster handle — the paper's stable Scholar IDENTITY, present for EVERY indexed paper (cited or not). Set it when a `search`/`cited_by`/`related` result already carries it; coexists with `source`. |
| google_scholar_cites_id | ONLY when a Scholar result shows citations | the Scholar `cites_id` cited-by pivot handle — the "cited by N" link, present ONLY once a paper HAS citations. An uncited paper legitimately has none; leaving it empty is normal, not a gap. |

**Set BOTH whenever either is used/needed/known.** A single Scholar result
(`search`/`cited_by`/`related`) carries `cluster_id` AND (if the paper is cited)
`cites_id` together — so if you have a reason to record one, record both from the
same result; never persist one and drop the other you already hold. The only
asymmetry is that an uncited paper has a `cluster_id` but no `cites_id` (normal,
not a gap).

**Do NOT issue a Scholar search just to obtain either handle**: every Scholar
call is rate-limited and risks a CAPTCHA. Free to set from a result already in
hand; never worth a dedicated fetch.

### Canonical venues

Store venue names without years, volume numbers, or `Proceedings of`. Put
workshops, tracks, and `Findings` in `subvenue`.

Canonical preprint/nontraditional names:
`arXiv`, `TechRxiv`, `SSRN`, `ARC Prize`.

Canonical conference names:
`AAAI`, `ACL`, `AISTATS`, `CoLLAs`, `COLT`, `CoNLL`, `CogSci`, `CVPR`,
`CVPRW`, `EMNLP`, `GCWCN`, `ICASSP`, `ICLR`, `ICML`, `ICRA`, `IDC`,
`IJCAI`, `KDD`, `NeurIPS`, `SCA/HPCAsia Workshops`, `UAI`.

Canonical journal/proceedings names:
`ACM TIST`, `ACM TOPML`, `Artificial Intelligence and Law`, `Artificial Life`,
`Automatica`, `Cognition`, `JMLR`, `Nature`, `Neurocomputing`, `PACMPL`,
`Pattern Recognition Letters`, `Physical Review B`, `Physical Review E`,
`PLOS ONE`, `PNAS`, `Science`, `Science Advances`, `TMLR`.

Known aliases normalize to those exact strings. Examples:

- `Advances in Neural Information Processing Systems 38 (NeurIPS 2025)` →
  `NeurIPS`.
- `International Conference on Machine Learning` → `ICML`.
- `The Thirteenth International Conference on Learning Representations
  (ICLR 2025)` → `ICLR`.
- `Findings of the Association for Computational Linguistics: ACL 2026` →
  venue `ACL`, subvenue `Findings`.
- `Mechanistic Interpretability Workshop at NeurIPS 2025` → venue `NeurIPS`,
  subvenue `Mechanistic Interpretability Workshop`.
- `arXiv.org` and mixed arXiv/award strings → `arXiv`; preserve the award in
  `subvenue`.

Unlisted journals retain their official title. When an alias is discovered,
add it to the shared venue-alias table rather than storing a new spelling.

### Source-link verification

Real-GET every non-arXiv `source` before completion; a plausible URL or copied
index result is not evidence that the source resolves. Follow redirects and
verify the final response identifies the intended paper. A `403` proves only
that the current transport is blocked: retry with Zendriver before calling the
link dead. A connection failure shared by every URL on one host is a host or
egress failure, not evidence that each path is wrong. Replace genuinely dead
links with an authoritative DOI, proceedings page, or stable project record.

For graph-wide audits, test every active non-arXiv source, retain the per-Paper
result, and stop completion while any link remains untested or unresolved.

### DOI resolution

`https://api.crossref.org/works?query.bibliographic=<title>&rows=1` with a
`User-Agent: ... (mailto:you@example)` for the polite pool. Fuzzy-match the
returned title before accepting — Crossref returns a nearest hit even on a miss.
A preprint DOI (Research-Square/Zenodo) is valid but is NOT a venue.

## Non-published sources are first-class

Benchmark records (ARC especially) are Kaggle-like: much of the field is
**GitHub repos, Kaggle notebooks/writeups, blogs, theses, workshop PDFs** with no
arXiv id. Make these real Paper nodes:
- `source` = URL; `publication_type` = `misc`; label `non-published`.
- title = page/repo title (slug if none); abstract = README/snippet when available.
- **Never drop a citer for lacking an arXiv id** — that discards exactly the
  effort these benchmarks care about.

## Companion WebResults (code / data / project page)

Every paper: **try** to find and attach companions.
1. Parse the paper PDF for `github.com`/`github.io`/`kaggle.com`/`huggingface.co`/
   `gitlab.com`/project-page URLs; also check S2 `externalIds` and the abstract.
2. Filter dependency/library repos (`faiss`, `d3pm`, generic forks) — keep the
   paper's OWN artifact.
3. Create + link: `trax webresult title to "Code: <slug>" url to <URL>` then
   `trax paper <seq> produces webresult <wseq>`. Dedup WebResults on `url`.
See `webresult.md`.

## Citation edges

- Paper→Paper bibliographic citation = `cites_paper` (`trax paper A cites paper
  B`): historical, no valence, provenance-neutral. Add an edge to every in-graph
  reference. This is NOT `proves`/`favors` (those target Belief/Experiment — see
  `belief.md`).
- **Getting a paper's references is usually a PDF-read, not an API call.** S2 and
  arXiv only carry reference lists for older, well-indexed papers; for anything
  recent (the common case) S2 returns nothing, so you **fetch the PDF and parse
  its bibliography** for arXiv ids / DOIs — treat PDF-reading references as the
  norm, not a fallback. Cascade: S2 references+citations → **PDF bibliography**
  (recent papers, and non-arXiv references like repo URLs S2 never has) → Google
  Scholar `cited_by` for the recent / non-published tail. (Bulk graph-scale
  harvest is the `survey-snowball` research skill; one paper's refs still follow
  this cascade.)

## Labels

- Domain/version tags only where the paper's own text pins them (e.g.
  `arcagi1/2/3` when it RAN that split); when unpinned use
  `arc-version-unverified` (domain equivalent) rather than guessing.
- Provenance tag for how the node entered (`scholar`, `closure`, `snowball`,
  `manual`) — makes audits filterable.

## Idempotency

Dedup on `source` before creating: skip if present; fill only MISSING fields on a
partial row. Never create a second row for an existing source — that becomes a
`supersedes` cleanup later (`survey-snowball`).

## Lifecycle

A Paper starts `active` while you are still resolving its fields. Once it is
fully populated (fields attempted, companions linked, citation edges wired) and
has passed its completeness check, **mark it `status complete`**
(`trax paper <seq> status to complete`). `complete` means "done being authored,
verified" — not "the paper was published." A row missing fields that CAN still be
filled stays `active`; a row whose remaining gaps are genuinely unresolvable
(flag `no-bib-record`) may still be completed. Re-open to `active` only if new
fields become fillable.

## Common mistakes

- Dropping a non-arXiv citer instead of making it a `misc` node.
- FABRICATING a `-01-01`/epoch date with no source behind it (leave empty
  instead) — but do NOT strip a genuine year-only `-01-01` a source actually
  returned.
- Fabricating authors/venue on a cascade miss — leave empty, note the gap.
- Skipping DOI resolution for arXiv papers (they often have one).
- Attaching a dependency repo as the companion WebResult.
- Using `proves`/`favors` for a paper→paper citation (wrong target kind; use
  `cites`).
- Creating a duplicate row instead of filling the existing one.
