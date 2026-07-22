# trax authoring skills — per-inquiry expectations (SoT)

This directory is the **source of truth** for how each Trackinizer inquiry kind
should be filled: not the field list (that is `types/inquiries.py`), not the
command grammar (that is `../trax-skill.md`, `../grammar.lark`, `../GRAMMAR.md`),
but the **content expectations** — what makes a well-formed, complete instance of
each kind.

One file per instantiable kind:

| Kind | File | Owns |
|---|---|---|
| Issue | `issue.md` | issue-body + bug-report standard; success criteria; verification |
| Paper | `paper.md` | full bib completeness; source cascade; DOI resolution; companion WebResults |
| Belief | `belief.md` | judgement/confidence; signed-valence citations; claims-not-papers |
| Experiment | `experiment.md` | codechanges; outcome; config; proved_by |
| CodeChange | `codechange.md` | full 40-char SHA; produced-link |
| WebResult | `webresult.md` | url; companion-of-paper pattern |
| WebSearch | `websearch.md` | query/provider; findings as produced_by |
| AgentSession | `agentsession.md` | envelope vs events asymmetry |

Rules for these docs:
- Describe EXPECTATIONS, not field enumerations. `types/inquiries.py` owns fields;
  do not hand-maintain them here.
- Reference `../trax-skill.md` for the write grammar; do not duplicate it.
- Every value is best-effort ("try each source, fill what resolves, flag gaps");
  never fabricate, never leave a placeholder (e.g. a `-01-01` date).

`../trax-skill.md` is the router: grammar, workflow, and a pointer into this dir.
